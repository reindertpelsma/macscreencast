# mac-vnc-stream — agent notes

This file is for future agents (and humans) working on the codec / rate-control
stack. The Apple VideoToolbox behavior here is non-obvious and we burned several
iterations on it; this is the cliff notes so nobody repeats them.

## Current architecture (as of f816736)

- **Encoder**: `hevc_videotoolbox` (default) or `h264_videotoolbox`,
  `constant_bit_rate=0` (VBR). The encoder treats `bit_rate` as a target
  average and OVERSHOOTS 2-5× on complex content — this is documented Apple
  behavior, but it's a real architectural gap for a real-time encoder.
- **Rate enforcement** lives at the network layer, not the encoder layer:
  the controller's wb-aware drain pause (`congestion.py:on_lag` →
  `_drain_until`) clears the queue when lag accumulates, then resumes.
- **GOP**: 99999 (essentially never). The decoder is fully state-locked
  with the encoder over TCP — no broadcast scenario, no need for I-frame
  refresh. Only forced I-frame is at encoder rebuild (codec change /
  resolution change), which is unavoidable.
- **Disconnect prevention**: ping_monitor (handler.py) MUST NOT close
  the connection on pong timeout. On a constrained link, the WS-layer
  ping queues behind buffered video and arrives many seconds late;
  closing on this signal produces a 7-second disconnect-loop in real
  Chrome with DevTools 2Mbps throttle. Use the timeout as a congestion
  signal (`ctrl.on_lag(30000, 0)`) but keep the connection alive.
  Liveness is the browser-side 15s stall detector + the websockets
  library's own 20s/20s keepalive.

## Apple VideoToolbox: things that DON'T work, validated

### 1. `constant_bit_rate=1` (CBR for H.264)

Apple added this in macOS 13 for `h264_videotoolbox` only (not HEVC, not AV1).
Documented as strict CBR via `kVTCompressionPropertyKey_DataRateLimits`.

**Reality**: on simple/medium-complexity content, the encoder collapses
output to ~10% of target rather than padding to the target rate. Verified
on:
- GitHub macos-14 runner (CI, animation content): 0.12 Mbps when target
  was 1.4 Mbps. Reproduced multiple runs, multiple commits.
- macOS Tahoe 26.x (live, real-Chrome content): caused immediate 2-second
  disconnect-loop after auto-switching to it from HEVC.

So Apple's "strict CBR" mode is not actually CBR-padding — it's "lowest QP
that fits, capped at target." With a 99999-GOP and predictable P-frame
content, this means tiny outputs.

**Do not enable `constant_bit_rate=1`** until you've verified it produces
target-rate output on the GH runner with our specific animation. Last
known state: it doesn't.

### 2. HEVC strict-rate option

Does not exist. `hevc_videotoolbox` has no `constant_bit_rate` support
in FFmpeg's videotoolboxenc.c. There is no Apple-supplied way to make
HEVC respect a strict rate target. The only options for strict HEVC
rate control are:

- libx265 software encode (CPU heavy, but proper VBV via
  `vbv-maxrate`/`vbv-bufsize`) — not currently used; planned as
  constrained-link fallback.
- Application-layer rate enforcement — explicitly avoided per user
  feedback: dropping P-frames forces I-frame recovery; I-frame on a
  constrained link makes the lag worse than the overshoot did.

### 3. AV1 strict-rate option

Same as HEVC: `av1_videotoolbox` has no `constant_bit_rate`. Same
mitigations apply (libsvtav1 / libaom-av1 software).

## Why the disconnect-loop (now fixed) was so confusing

The bug had three stacked causes; only the third one was the actual
disconnect trigger:

1. **VBR overshoot fills the pipe** (Apple bug above)
2. **`drain_until` was capped at 2s** — too short for queues that took
   >2s to clear; drain pauses chained without making progress
3. **`ping_monitor` closed on 5-second pong timeout** — but pongs queue
   behind video frames in TCP; on a constrained link a pong legitimately
   takes 5-15 seconds. Closing on this is the actual disconnect-loop
   trigger.

Fixed in:
- 7a529e8: ping_monitor doesn't close on timeout, just feeds it as
  congestion signal
- e0c9885: drain duration is now wb+age aware, capped at 5s, with
  `end_drain_if_clear` short-circuit when the queue actually clears
- (the wb-aware drain logic survived all subsequent reverts)

## Things that DON'T work, do not retry without strong validation

- **`constant_bit_rate=1`** — see above. Try only with hard live test
  evidence that output matches target on multiple content types.
- **App-layer P-frame drop with forced I-frame** — see encoder.py
  comments. Forced I-frame on a constrained link defeats the purpose:
  one I-frame can be 100KB-1MB which is exactly the queue size we're
  trying to avoid. Per user: GOP=99999 is intentional, decoder is
  state-locked, never force I-frames as recovery from synthetic drops.
- **Auto codec switch HEVC → H.264 CBR** — depends on H.264 CBR working
  (it doesn't). Tried in dba435f, reverted. The recommend_codec /
  lag-tracking machinery is reusable for a future libx265 fallback,
  but reverted as currently dead code.

## Buffer mode (the "responsive vs cinema" knob) — load-bearing design

The quality menu has a "Buffer" setting (UI label "Buffer", values:
responsive / 100ms / 200ms / 500ms / 1s / 3s). It's not a tuning hint —
it switches the product between two genuinely different modes. **Don't
collapse them into one path.**

### Responsive mode (`_qLagMs == 0`)

- Server: `lag_budget_ms() = 50ms`, gradient detector (`on_ping_rtt`
  Signal 1+2) ON, `/metric` ws active, full backoff aggressiveness
- Client: video frames decoded immediately on receipt (no queue); audio
  jitter buffer floor = 250ms
- Optimized for remote-desktop interactive use. Every keystroke
  visible "instantly" (≤ link RTT). Frames may drop / quality may
  blur under stress to preserve responsiveness — that's correct.

### Buffer mode (`_qLagMs >= 100`)

- Server: `lag_budget_ms() = user_value`, gradient detector OFF
  (`on_ping_rtt` early-returns when `lag_budget_override > 0`),
  `/metric` ws disabled client-side. Lag is allowed to grow freely
  up to the user's buffer; only lag *exceeding* the buffer triggers
  backoff. The server stops fighting the buffer.
- Client: incoming video frames are held in `_vidQueue` for `_qLagMs`
  ms (performance.now()-relative `decodeAt`) before being submitted
  to the decoder. Audio target latency floor = `_qLagMs` so A/V stay
  in lockstep.
- Optimized for actually watching video content (e.g. YouTube on the
  remote Mac). No frame drops — the buffer absorbs jitter rather than
  the encoder dropping quality. Mouse interaction is intentionally
  laggy by `_qLagMs` ms; that's the trade.

### Mode-switch handling (don't break this)

- buffered → responsive: `_flushVideoBufferImmediately()` drains the
  queue so the user sees real-time within one frame, not N seconds of
  stale buffer playing out. Audio: `_nextAudioTime = 0` forces the
  next packet through the underrun-reset path → lands at `now + new_target`.
- responsive → buffered: queue starts filling on the next frame. Audio
  ramps up via the same reset.
- The reset hook is in the `lSel.addEventListener('change', …)` and the
  `q-reset` button handler. If you change those handlers, preserve the
  flush + audio reset.

### Why /metric is disabled in buffer mode

We can't be certain whether `/metric` is on the same SSH-tunnel TCP
pipe as `/` (separate WS channel ≠ guaranteed separate buffer behavior
across all transports). In buffer mode the user has explicitly accepted
queue formation, so an "unloaded RTT" signal isn't actually unloaded
and would conflict with the buffer the user wants. Better to ignore
than misuse. Restored automatically on switch back to responsive.

### What does NOT change between modes

- Encoder pipeline (VBR, GOP=99999, bitrate ramp, etc.) — same in both modes
- ping_monitor / drain pause / wb-aware backoff — same in both modes
- Codec selection / WebCodecs decoder configuration — same in both modes
- Video frame format on the wire — same in both modes (the buffer is
  purely a client-side delay; the server doesn't know or care)

The encoder runs at full quality regardless of mode. In buffer mode the
encoder ramps up to high bitrate naturally because lag stays low (link
RTT only — buffer absorbs everything else). On a fast link the buffer
fills with high-quality frames, which is exactly what cinema mode wants.

## Things that probably WILL work, not yet attempted

- **libx265 software encode for constrained links**. Has proper VBV
  rate control via `vbv-maxrate`/`vbv-bufsize`. CPU cost is real
  (~5-15ms/frame at 1080p on M1) but on a constrained link we're
  already at low fps so the budget exists. Same H.265 codec on the
  wire → no client-side reconfig needed. This is the next planned
  experiment.
- **Heartbeat over the metric WebSocket** instead of the main video WS.
  Browser stall detector currently watches main-WS messages; if main
  WS queue is deep, heartbeat is delayed too. Sending heartbeats over
  the separate metric channel (different TCP stream, no queue behind
  video) would prevent the 15s stall detector firing on slow links.
  Independent and safe; deploy when needed.

## How to test changes to the encoder/rate-control stack

Validate in this order, do not skip steps:

1. **Local Mac mini live**: SSH `m1@62.210.195.81`, deploy, restart
   service, watch `tail -f /tmp/macvncstream.log`. Test in real Chrome
   with DevTools 2Mbps throttle. CI does NOT catch real-TCP-backpressure
   bugs.
2. **GH CI**: covers the virtual-clock test (no real backpressure) and
   the tcp_throttle.py harness (real TCP-level backpressure). Useful
   for regression checking, not for proving real-Chrome behavior.
3. **`tcp_throttle.py` locally** with `--latency 40 --jitter 30` is
   closer to a real congested link than the virtual-clock test. The
   CI step "Run real-TCP 2Mbps throttle test" exercises this.

The disconnect-loop bug only manifests in real Chrome; both the virtual
clock test AND `tcp_throttle.py` missed it because Chrome's DevTools
throttle has a different buffer geometry than a TCP rate-limiting proxy.
Real-browser smoke is irreplaceable.

## Test host

`m1@62.210.195.81` (sudo password in private notes / ask owner). macOS
Tahoe 26.x on M1. Server lives at `~/mac-vnc-stream/`, autorestarts via
LaunchAgent `com.macvncstream.server`. Log at `/tmp/macvncstream.log`.
