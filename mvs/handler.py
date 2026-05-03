import asyncio
import json
import logging
import time
from pathlib import Path

from mvs.codec import (CODEC_JPEG, CODEC_H264, CODEC_H265, CODEC_AV1,
                        _select_codec, _hdr)
import mvs.cgevent as _cge
from mvs.vnc import KEYSYM
from mvs.encoder import EncoderPipeline
from mvs.congestion import AdaptiveController
from mvs.audio import audio_session

log = logging.getLogger("macvnc")

# Per-session queues for server→browser JS eval (debug channel)
_dbg_eval_sessions: set = set()

# live WebSocket sessions; capture loops idle when 0
_active_clients: int = 0

# Read the frontend HTML at module load time (from the split-out file).
_FRONTEND_HTML = (Path(__file__).parent.parent / "frontend" / "index.html").read_bytes()

_LOGIN_HTML = b"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>mac-vnc-stream</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#111;color:#ddd;font-family:system-ui,sans-serif;
     display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#1e1e1e;border:1px solid #333;border-radius:10px;
      padding:2rem 2.4rem;display:flex;flex-direction:column;gap:1rem;width:300px}
h1{font-size:1rem;font-weight:600;color:#fff;letter-spacing:.01em}
input{width:100%;padding:.55rem .75rem;background:#141414;border:1px solid #444;
      border-radius:6px;color:#eee;font-size:.95rem;outline:none}
input:focus{border-color:#4a9eff}
button{width:100%;padding:.55rem;background:#4a9eff;border:none;border-radius:6px;
       color:#fff;font-size:.95rem;cursor:pointer;font-weight:500}
button:hover{background:#3b8fe0}
.err{color:#f77;font-size:.85rem;display:none}
.err.show{display:block}
</style>
</head>
<body>
<div class="card">
  <h1>mac-vnc-stream</h1>
  <form method="get" action="/">
    <div style="display:flex;flex-direction:column;gap:.75rem">
      <input type="password" name="token" placeholder="Access token"
             autofocus autocomplete="current-password">
      <p class="err" id="e">Invalid token &mdash; try again.</p>
      <button type="submit">Connect</button>
    </div>
  </form>
</div>
<script>
if(new URLSearchParams(location.search).has('token'))
  document.getElementById('e').classList.add('show');
</script>
</body>
</html>
"""


def _get_wbuf(ws):
    for attr in ("transport", ):
        try:
            t = getattr(ws, attr, None)
            if t: return t.get_write_buffer_size()
        except Exception: pass
    try:
        return ws.connection.transport.get_write_buffer_size()
    except Exception:
        return 0


async def client_session(ws, cfg, bridge):
    log.info("client connect: %s", ws.remote_address)

    # Wait for VNC to be ready
    for _ in range(30):
        W, H = bridge.dimensions
        if W and H: break
        await asyncio.sleep(0.2)
    else:
        log.warning("VNC not ready"); return

    # Release any modifier keys / mouse buttons left over from a previous session.
    bridge.send_key_reset()

    ctrl = AdaptiveController(cfg)
    # Start with JPEG until client reports WebCodecs capability and supported codecs.
    # target_codec is updated when the client sends its caps (codec negotiation).
    target_codec = CODEC_H264 if cfg.codec == "h264" else CODEC_H265
    encoder = EncoderPipeline(CODEC_JPEG, W, H, ctrl.bitrate)  # JPEG until caps received
    has_webcodecs = False
    _enc_target_w, _enc_target_h = W, H  # current encoder dimensions
    _reinit_deadline = 0.0                # monotonic: reinit encoder at this time; 0=none
    _codec_error_msg = None               # set by _upgrade_encoder on explicit codec failure

    seq_num = 0
    last_send_time = time.monotonic()
    # Shared between frame_sender (read/write) and input_reader (write).
    # Must be in outer scope so both closures reference the same variable.
    _need_keyframe = False
    _last_lag_received = 0.0   # set on first lag report; 0 = never received (skip proactive backoff)

    def _upgrade_encoder(tw: int = 0, th: int = 0, explicit: bool = False):
        nonlocal encoder, has_webcodecs, _enc_target_w, _enc_target_h, _codec_error_msg
        if not has_webcodecs:
            # Client doesn't support WebCodecs (or revoked it after an error).
            # Downgrade to JPEG so the client's JPEG path actually gets JPEG frames.
            if encoder.actual_codec != CODEC_JPEG:
                W2, H2 = bridge.dimensions
                old = encoder
                encoder = EncoderPipeline(CODEC_JPEG, W2 or 1920, H2 or 1080, ctrl.bitrate)
                _enc_target_w, _enc_target_h = W2, H2
                old.close()
            return
        W2, H2 = bridge.dimensions
        if not tw or not th:
            tw, th = ctrl.effective_target(W2, H2)
        tw = tw or W2; th = th or H2
        if encoder.actual_codec != CODEC_JPEG and (tw, th) == (_enc_target_w, _enc_target_h):
            return  # already upgraded at this resolution
        old = encoder
        # Cascade: try target_codec first, then H.265, then H.264.
        # When explicit=True the user chose a specific codec — don't cascade to others;
        # if the chosen codec fails, report an error and fall back to JPEG only.
        seen = set()
        if explicit and target_codec != CODEC_JPEG:
            fallbacks = [target_codec]
        else:
            fallbacks = [target_codec, CODEC_H265, CODEC_H264]
        new_enc = None
        for codec in fallbacks:
            if codec == CODEC_AV1 and codec != target_codec:
                continue  # AV1 is CPU-only; skip in auto cascade but allow when explicitly chosen
            if codec in seen:
                continue
            seen.add(codec)
            e = EncoderPipeline(codec, tw, th, ctrl.bitrate)
            if e.actual_codec != CODEC_JPEG:
                new_enc = e
                break
            e.close()
        if new_enc is None:
            _codec_labels = {CODEC_H264: "H.264", CODEC_H265: "H.265", CODEC_AV1: "AV1"}
            if explicit:
                _codec_error_msg = (f"{_codec_labels.get(target_codec, 'Codec')} encoder not"
                                    f" available on this server — using JPEG fallback")
            else:
                log.warning("Video codec unavailable for %s — staying on JPEG", ws.remote_address)
            new_enc = EncoderPipeline(CODEC_JPEG, tw, th, ctrl.bitrate)
        else:
            log.info("Encoder %s %dx%d for %s",
                     {CODEC_H264:"h264",CODEC_H265:"h265",CODEC_AV1:"av1"}.get(new_enc.actual_codec,"?"),
                     tw, th, ws.remote_address)
        encoder = new_enc
        _enc_target_w, _enc_target_h = tw, th
        old.close()

    loop = asyncio.get_event_loop()

    async def input_reader():
        nonlocal has_webcodecs, target_codec, _reinit_deadline, _codec_error_msg, _need_keyframe, _last_lag_received
        cur_buttons = 0
        try:
            async for raw in ws:
                if not isinstance(raw, str):
                    continue
                try:
                    ev = json.loads(raw)
                    t = ev.get("t")
                    if t == "reset":
                        cur_buttons = 0
                        bridge.send_key_reset()
                    elif t == "caps":
                        has_webcodecs = bool(ev.get("webcodecs", False))
                        client_codecs = ev.get("codecs", [])
                        explicit = bool(ev.get("explicit", False))
                        w, h = int(ev.get("w", 1920)), int(ev.get("h", 1080))
                        ctrl.on_resolution(w, h)
                        # Negotiate codec: pick best that client supports.
                        # If the client sent an explicit codec list, use that to override
                        # the server's configured default. If the client only said
                        # webcodecs=true without a list, keep the configured default.
                        if client_codecs and has_webcodecs:
                            target_codec = _select_codec(client_codecs)
                        _upgrade_encoder(explicit=explicit)
                        if _codec_error_msg:
                            _msg = _codec_error_msg; _codec_error_msg = None
                            await ws.send(json.dumps({"t": "codec_error", "msg": _msg}))
                    elif t == "resolution":
                        ctrl.on_resolution(int(ev.get("w",1920)), int(ev.get("h",1080)))
                        # Schedule encoder reinit if effective target changed (debounced 500ms)
                        nw, nh = bridge.dimensions
                        tw, th = ctrl.effective_target(nw or W, nh or H)
                        if has_webcodecs and (tw != _enc_target_w or th != _enc_target_h):
                            _reinit_deadline = time.monotonic() + 0.5
                    elif t == "quality":
                        _old_bw_cap = ctrl.user_bw_cap
                        ctrl.on_quality(int(ev.get("cap_h", 0)), int(ev.get("fps", 0)),
                                        int(ev.get("maxkbps", 0)), int(ev.get("lag_ms", 0)))
                        if ctrl.user_bw_cap != _old_bw_cap:
                            _need_keyframe = True
                        nw, nh = bridge.dimensions
                        tw, th = ctrl.effective_target(nw or W, nh or H)
                        if has_webcodecs and (tw != _enc_target_w or th != _enc_target_h):
                            _reinit_deadline = time.monotonic() + 0.5
                    elif t == "keyframe":
                        _need_keyframe = True
                    elif t == "lag":
                        age = float(ev.get("age_ms", 0))
                        wb = _get_wbuf(ws)
                        ctrl.on_lag(age, wb)
                        _last_lag_received = time.monotonic()
                        # Positive path: low-lag report = client confirming path is clear.
                        # Unlocks next ramp step in on_fresh without waiting the full 2s heuristic.
                        if age > 0 and age < ctrl.lag_budget_ms() and wb < ctrl.lag_wb_budget():
                            ctrl.on_client_clear()
                    elif t == "metric_rtt":
                        ctrl.on_metric_rtt(float(ev.get("rtt_ms", 0)))
                    elif t == "mm":
                        x2, y2 = int(ev["x"]), int(ev["y"])
                        if not (_cge._cg_kb_ok and _cge._cg_send_pointer(cur_buttons, x2, y2)):
                            bridge.send_pointer(cur_buttons, x2, y2)
                    elif t == "md":
                        b = ev.get("b", 0)
                        cur_buttons |= (1 << b)
                        x2, y2 = int(ev["x"]), int(ev["y"])
                        if not (_cge._cg_kb_ok and _cge._cg_send_pointer(cur_buttons, x2, y2)):
                            bridge.send_pointer(cur_buttons, x2, y2)
                    elif t == "mu":
                        b = ev.get("b", 0)
                        cur_buttons &= ~(1 << b)
                        x2, y2 = int(ev.get("x", 0)), int(ev.get("y", 0))
                        if not (_cge._cg_kb_ok and _cge._cg_send_pointer(cur_buttons, x2, y2)):
                            bridge.send_pointer(cur_buttons, x2, y2)
                    elif t == "sc":
                        x, y = int(ev.get("x",0)), int(ev.get("y",0))
                        dx, dy = int(ev.get("dx",0)), int(ev.get("dy",0))
                        if _cge._cg_kb_ok:
                            # CGEvent scroll wheel — smoother than VNC button-click simulation
                            try:
                                import Quartz as _Q
                                if dy:
                                    e = _Q.CGEventCreateScrollWheelEvent(
                                        None, _Q.kCGScrollEventUnitLine, 1, -dy)
                                    _Q.CGEventPost(_Q.kCGHIDEventTap, e)
                                if dx:
                                    e = _Q.CGEventCreateScrollWheelEvent(
                                        None, _Q.kCGScrollEventUnitLine, 2, 0, -dx)
                                    _Q.CGEventPost(_Q.kCGHIDEventTap, e)
                            except Exception:
                                pass
                        else:
                            evts = []
                            if dy: evts.append((8 if dy < 0 else 16, abs(dy)))
                            if dx: evts.append((32 if dx < 0 else 64, abs(dx)))
                            async def _scroll(evts=evts, sx=x, sy=y):
                                for btn, n in evts:
                                    for _ in range(n):
                                        bridge.send_pointer(btn, sx, sy)
                                        bridge.send_pointer(0, sx, sy)
                                        if n > 1:
                                            await asyncio.sleep(0.012)
                            asyncio.create_task(_scroll())
                    elif t in ("kd","ku"):
                        k = ev.get("k",""); code = ev.get("code","")
                        down = t == "kd"
                        vk = _cge.VK.get(code) if code else None
                        if vk is None and k:
                            vk = _cge.VK.get(k)
                        if vk is not None and _cge._cg_kb_ok:
                            # CGEvent primary path — native VK codes, bypasses screensharingd
                            # keysym→VK translation which changes after VNC reconnects.
                            if vk in _cge._VK_MODS:
                                if down: _cge._cg_mod_held.add(vk)
                                else:    _cge._cg_mod_held.discard(vk)
                            flags = 0
                            for mv in _cge._cg_mod_held:
                                flags |= _cge._VK_FLAGS.get(mv, 0)
                            try:
                                import Quartz as _Q
                                evt = _Q.CGEventCreateKeyboardEvent(None, vk, down)
                                _Q.CGEventSetFlags(evt, flags)
                                _Q.CGEventPost(_Q.kCGHIDEventTap, evt)
                            except Exception as _e:
                                log.debug("CGEvent key vk=%d: %s — VNC fallback", vk, _e)
                                ks = KEYSYM.get(code) or KEYSYM.get(k) or (ord(k) if len(k)==1 else None)
                                if ks: bridge.send_key(down, ks)
                        else:
                            ks = KEYSYM.get(code) or KEYSYM.get(k) or (ord(k) if len(k)==1 else None)
                            if ks: bridge.send_key(down, ks)
                    elif t in ("paste", "setclip"):
                        text = ev.get("text","")
                        if t == "paste":
                            mac_rev = ev.get("mac_rev")
                            if (mac_rev is not None
                                    and mac_rev != bridge.server_clipboard_seq):
                                continue  # client's view of Mac clipboard is stale — ignore
                        if text:
                            # pbcopy is more reliable than VNC ClientCutText on macOS 15+
                            # (ClientCutText may be silently ignored by screensharingd)
                            try:
                                proc = await asyncio.create_subprocess_exec(
                                    'pbcopy', stdin=asyncio.subprocess.PIPE)
                                proc.stdin.write(text.encode('utf-8', errors='replace'))
                                proc.stdin.close()
                                await asyncio.wait_for(proc.wait(), timeout=2.0)
                            except Exception:
                                bridge.send_clipboard(text)  # fallback
                        if t == "paste" and text:
                            # Release any held modifiers, then send Cmd+V
                            if _cge._cg_kb_ok:
                                try:
                                    import Quartz as _Q
                                    _cge._cg_mod_held.clear()
                                    for _vk, _dn, _fl in [
                                        (55, True,  0x100000),
                                        (9,  True,  0x100000),
                                        (9,  False, 0x100000),
                                        (55, False, 0),
                                    ]:
                                        _e2 = _Q.CGEventCreateKeyboardEvent(None, _vk, _dn)
                                        _Q.CGEventSetFlags(_e2, _fl)
                                        _Q.CGEventPost(_Q.kCGHIDEventTap, _e2)
                                except Exception:
                                    pass
                            else:
                                for ks in [KEYSYM["ShiftLeft"], KEYSYM["ShiftRight"],
                                           KEYSYM["Control"], KEYSYM["ControlRight"],
                                           KEYSYM["Alt"], KEYSYM["AltRight"],
                                           KEYSYM["MetaLeft"], KEYSYM["MetaRight"]]:
                                    bridge.send_key(False, ks)
                                bridge.send_key(True,  KEYSYM["MetaLeft"])
                                bridge.send_key(True,  0x76)
                                bridge.send_key(False, 0x76)
                                bridge.send_key(False, KEYSYM["MetaLeft"])
                    elif t == "dbg_result":
                        log.info("DBG[%s]: %s", ev.get("id","?"), ev.get("result",""))
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            cur_buttons = 0
            bridge.send_key_reset()  # VNC path: release all modifier keys + mouse buttons
            _cge._cg_release_all()   # CGEvent path: release modifiers + mouse buttons

    async def frame_sender():
        nonlocal seq_num, last_send_time
        known_clip = bridge.server_clipboard_seq
        # Tell client the Mac's native capture resolution so it can build the quality menu correctly.
        try:
            nw, nh = bridge.dimensions
            await ws.send(json.dumps({"t": "native", "w": nw, "h": nh}))
        except Exception:
            pass
        # Send current Mac clipboard immediately on connect so side menu is populated.
        if bridge.server_clipboard:
            try:
                await ws.send(json.dumps({"t": "clipboard", "text": bridge.server_clipboard,
                                          "seq": bridge.server_clipboard_seq}))
            except Exception:
                pass
        nonlocal _enc_target_w, _enc_target_h, _reinit_deadline, _need_keyframe, _last_lag_received
        last_encoder_codec = encoder.actual_codec
        _bw_sent = []       # list of (monotonic_time, bytes) for rolling 1s bandwidth measurement
        _t_diag = time.monotonic(); _n_diag = 0; _n_drop = 0; _n_nosend = 0
        _last_vnc_fbu = bridge._fbu_count
        # Pipelined encode: start encoding during the rate-limit sleep so that
        # encode time doesn't add to the frame interval.
        _pipe_task = None       # concurrent encode future
        _pipe_cap_ms = 0        # cap_ms captured when pipe_task was started
        _last_encoded_seq = -1  # _fb_seq of last successfully sent frame
        _pipe_enc_seq = -1      # _fb_seq captured when current pipe was started
        _was_static = False     # True when screen has been unchanged this static period
        _static_since = 0.0     # monotonic time when current static period started
        _refresh_br = 0         # bitrate at which last I-frame quality refresh was sent
        _refresh_t = 0.0        # monotonic time of last I-frame quality refresh
        try:
            while True:
                now = time.monotonic()
                if now - _t_diag >= 5.0:
                    dt = now - _t_diag; _t_diag = now
                    _vnc_n = bridge._fbu_count
                    _vnc_fps = (_vnc_n - _last_vnc_fbu) / dt; _last_vnc_fbu = _vnc_n
                    _fb_age = int(time.time() * 1000) - bridge._fb_ms
                    await ws.send(json.dumps({"t": "stale", "ms": _fb_age}))
                    log.info("DIAG: sent=%d/%.1fs=%.1ffps drop_wb=%d nosend=%d ctrl_fps=%.1f ctrl_br=%dk vnc=%.1ffps fb_age=%dms drain=%s",
                             _n_diag, dt, _n_diag/dt, _n_drop, _n_nosend, ctrl.fps, ctrl.bitrate // 1000, _vnc_fps, _fb_age, ctrl.draining)
                    _n_diag = _n_drop = _n_nosend = 0
                fps, bitrate, jq = ctrl.snapshot()
                interval = 1.0 / max(1.0, fps)

                # Proactive backoff: lag reports travel browser→server (upload direction) and
                # usually arrive within RTT (~20ms). If they go silent for >500ms while we're
                # actively sending, the download buffer is backed up to the point where the
                # browser can't decode frames fast enough to produce lag reports. Treat this
                # as a 500ms lag signal so the server backs off before the queue grows further.
                # Triggers only when we're actually sending (n_diag > 3 = at least 3 frames
                # sent this DIAG cycle) and not already in a drain pause.
                if not ctrl.draining and _n_diag > 3 and _last_lag_received > 0 and now - _last_lag_received > 0.5:
                    ctrl.on_lag(500.0, 0)
                    _last_lag_received = now  # reset so backoff debounce has time to fire

                # Detect encoder codec switch — drain in-flight encode and force I-frame.
                # After encoder rebuild the warmup consumed the I-frame; without an explicit
                # keyframe the client's fresh VideoDecoder has no reference frame and freezes.
                current_codec = encoder.actual_codec
                if current_codec != last_encoder_codec:
                    last_encoder_codec = current_codec
                    if _pipe_task is not None:
                        try: await _pipe_task
                        except Exception: pass
                        _pipe_task = None
                    _need_keyframe = True

                # Write buffer check — immediate local backpressure.
                # Threshold is fps+bitrate-aware (lag_wb_budget) so a single
                # large frame draining doesn't trigger false congestion at low fps.
                wb = _get_wbuf(ws)
                if wb > 4 * 1024 * 1024:
                    log.warning("write buf %.1fMB — hard kill %s", wb / 1048576, ws.remote_address)
                    try: await ws.close()
                    except Exception: pass
                    break

                # Drain pause: downstream buffer (SSH/sshd/bufferbloat path) is backed up.
                # Stop sending entirely until ctrl clears the drain window so the queue
                # can empty before we resume. Any in-flight encode is discarded safely
                # (encode() was already called, so force a keyframe on resume to re-sync
                # the decoder reference chain).
                if ctrl.draining:
                    if _pipe_task is not None:
                        try: await _pipe_task
                        except Exception: pass
                        _pipe_task = None
                        if has_webcodecs:
                            _need_keyframe = True
                    # Short-circuit drain once the queue has actually cleared
                    # — the controller sized the pause for worst-case but the
                    # link may have drained faster.
                    ctrl.end_drain_if_clear(_get_wbuf(ws))
                    if ctrl.draining:
                        await asyncio.sleep(0.05)
                        continue

                if wb > ctrl.lag_wb_budget():
                    ctrl.on_lag(0, wb)
                    _n_drop += 1
                    if has_webcodecs:
                        _need_keyframe = True
                    if _pipe_task is not None:
                        try: await _pipe_task
                        except Exception: pass
                        _pipe_task = None
                    await asyncio.sleep(0.01)
                    continue

                # Static-screen skip: no new content — poll at 1ms so the next
                # frame is detected and encoded within 1-2ms of the subprocess writing
                # it.  Shorter poll than the capture interval keeps encode latency low
                # and `last_send_time` drift from piling up between captures.
                cur_fb_seq = bridge._fb_seq
                if cur_fb_seq == _last_encoded_seq and _pipe_task is None:
                    if not _was_static:
                        _was_static = True
                        _static_since = now
                        _refresh_br = 0   # allow first refresh at any bitrate
                        _refresh_t = 0.0
                    else:
                        fps_s, br_s, jq_s = ctrl.snapshot()
                        # Heartbeat: send a frame every 2s when static so the client
                        # sees cursor movement and confirms the stream is alive (0fps
                        # on a static screen feels broken even when latency is fine).
                        # Also send a higher-quality refresh when bitrate improved >25%.
                        last_refresh_age = now - _refresh_t
                        quality = 95 if br_s > _refresh_br * 1.25 else 75
                        if now - _static_since > 1.0 and last_refresh_age > 2.0:
                            _refresh_t = now
                            if br_s > _refresh_br * 1.25:
                                _refresh_br = br_s
                            fb_s, cms_s = bridge.get_current_frame()
                            if fb_s is not None:
                                encoder.set_bitrate(br_s)
                                try:
                                    payload_s, is_kf_s, codec_s = await loop.run_in_executor(
                                        None, encoder.encode_keyframe, fb_s, cms_s, quality)
                                    if payload_s:
                                        seq_num += 1
                                        hdr_s = _hdr(seq_num, int(time.time() * 1000),
                                                     codec_s, True, len(payload_s))
                                        await ws.send(hdr_s + payload_s)
                                        _n_diag += 1
                                        log.debug("static heartbeat: %dkbps q=%d", br_s // 1000, quality)
                                except Exception as e:
                                    log.warning("heartbeat frame err: %s", e)
                    await asyncio.sleep(0.001)
                    continue

                # Screen just changed — jump to peak bitrate immediately
                if _was_static:
                    _was_static = False
                    _refresh_br = 0
                    ctrl.on_screen_active()
                    last_send_time = time.monotonic() - interval  # skip rate-limit delay

                # Debounced encoder reinit when quality cap or canvas size changed.
                if _reinit_deadline > 0 and now >= _reinit_deadline:
                    _reinit_deadline = 0.0
                    if _pipe_task is not None:
                        try: await _pipe_task
                        except Exception: pass
                        _pipe_task = None
                    nw2, nh2 = bridge.dimensions
                    tw2, th2 = ctrl.effective_target(nw2 or W, nh2 or H)
                    if tw2 != _enc_target_w or th2 != _enc_target_h:
                        _upgrade_encoder(tw2, th2)

                # Probe quality up — gated internally on _last_slow (no recent backoff)
                ctrl.on_fresh()

                # Pipeline: start encode NOW so it runs concurrently with the rate-limit sleep.
                # Encode takes ~4.5ms; sleep is ~16.7ms — encode finishes well before we wake.
                if _pipe_task is None:
                    fb, cap_ms = bridge.get_current_frame()
                    if fb is not None:
                        encoder.set_bitrate(bitrate)
                        _pipe_cap_ms = cap_ms
                        _pipe_enc_seq = cur_fb_seq
                        if _need_keyframe:
                            _need_keyframe = False
                            _pipe_task = loop.run_in_executor(None, encoder.encode_keyframe, fb, cap_ms, 85)
                        else:
                            _pipe_task = loop.run_in_executor(None, encoder.encode, fb, cap_ms, jq)

                # Rate limit using deadline: last_send_time advances by interval each frame
                # so encode + send time is absorbed and doesn't compound into the next sleep.
                target = last_send_time + interval
                to_sleep = target - time.monotonic()
                if to_sleep > 0.001:
                    await asyncio.sleep(to_sleep)
                    wb = _get_wbuf(ws)
                    if wb > ctrl.lag_wb_budget():
                        ctrl.on_lag(0, wb)
                        _n_drop += 1
                        if has_webcodecs:
                            _need_keyframe = True
                        if _pipe_task is not None:
                            try: await _pipe_task
                            except Exception: pass
                            _pipe_task = None
                        continue

                # Collect encode result — encode ran during sleep, so this is near-instant
                if _pipe_task is None:
                    _pipe_enc_seq = bridge._fb_seq
                    fb, cap_ms = bridge.get_current_frame()
                    if fb is None:
                        await asyncio.sleep(0.01)
                        continue
                    encoder.set_bitrate(bitrate)
                    try:
                        if _need_keyframe:
                            _need_keyframe = False
                            payload, is_kf, codec_byte = await loop.run_in_executor(
                                None, encoder.encode_keyframe, fb, cap_ms, 85)
                        else:
                            payload, is_kf, codec_byte = await loop.run_in_executor(
                                None, encoder.encode, fb, cap_ms, jq)
                    except Exception as e:
                        log.debug("encode err: %s", e); continue
                else:
                    cap_ms = _pipe_cap_ms
                    try:
                        payload, is_kf, codec_byte = await _pipe_task
                    except Exception as e:
                        log.debug("encode err: %s", e)
                        _pipe_task = None; continue
                    _pipe_task = None

                if payload is None:
                    _n_nosend += 1
                    last_send_time = target
                    continue

                if _get_wbuf(ws) > ctrl.lag_wb_budget():
                    ctrl.on_lag(0, _get_wbuf(ws))
                    _n_drop += 1
                    if has_webcodecs:
                        _need_keyframe = True
                    continue

                last_send_time = target
                _last_encoded_seq = _pipe_enc_seq
                # Optional user_bw_cap enforcement (rolling 1s window).
                # JPEG only — each JPEG is independent so dropping is safe.
                # Video codecs are NOT dropped here. Once a P-frame is encoded
                # the decoder needs it: TCP guarantees delivery, GOP=99999
                # means the state-locked decoder has no I-frame fallback for
                # ~28 minutes. Application-layer drops would force an I-frame
                # to recover, and a forced I-frame on a constrained link
                # actively makes the lag worse (single I-frame can be 100KB-
                # 1MB). The correct primitive is the encoder's own rate
                # control — we just have to use a codec/option pair that
                # actually respects the bitrate target (see encoder.py for
                # the VideoToolbox VBR limitation).
                _bw_cap_bps = ctrl.user_bw_cap
                if _bw_cap_bps:
                    _bw_now = time.monotonic()
                    _bw_sent = [(t, b) for t, b in _bw_sent if t > _bw_now - 1.0]
                    _frame_bytes = 18 + len(payload)  # 18 = struct.calcsize(">IQBBI")
                    if (sum(b for _, b in _bw_sent) + _frame_bytes) * 8 > _bw_cap_bps:
                        if not has_webcodecs:
                            _n_drop += 1
                            continue   # JPEG: safe to drop, no reference frames
                    _bw_sent.append((_bw_now, _frame_bytes))
                _n_diag += 1
                seq_num += 1
                # Use current wall-clock time for cap_ms in the header — the browser
                # uses this to measure transport age. The encoder's PTS (cap_ms passed
                # to encode()) can be older (encode-start time) without affecting the
                # lag reporter. This keeps age_ms ≈ SSH-tunnel RTT / 2 ≈ 18ms,
                # not encode_interval + SSH_latency, preventing false congestion signals.
                hdr = _hdr(seq_num, int(time.time() * 1000), codec_byte, is_kf, len(payload))
                try:
                    await ws.send(hdr + payload)
                except Exception as e:
                    log.debug("send err: %s", e); break

                sc = bridge.server_clipboard_seq
                if sc != known_clip and bridge.server_clipboard:
                    known_clip = sc
                    try:
                        await ws.send(json.dumps({"t":"clipboard","text":bridge.server_clipboard,"seq":sc}))
                    except Exception: pass

        except Exception as e:
            log.debug("sender exit: %s", e)
        finally:
            if _pipe_task is not None:
                try: await _pipe_task
                except Exception: pass
            encoder.close()

    dbg_q: asyncio.Queue = asyncio.Queue()
    _dbg_eval_sessions.add(dbg_q)
    _dbg_seq = [0]

    async def dbg_sender():
        # Race queue.get() with ws closing, so this task exits when the
        # connection dies instead of leaking forever (was the cause of
        # _dbg_eval_sessions ending up with N stale entries after N
        # disconnect cycles).
        while True:
            try:
                js = await dbg_q.get()
            except Exception:
                break
            try:
                if ws.state.name != "OPEN":
                    break
            except Exception:
                pass
            _dbg_seq[0] += 1
            try:
                await ws.send(json.dumps({"t": "eval", "js": js, "id": _dbg_seq[0]}))
            except Exception:
                break

    async def ping_monitor():
        """RFC 6455 WebSocket pings as congestion signal.
        Ping frames queue behind video data frames, so rising RTT means the
        TCP send buffer is building — earlier warning than JS age_ms reports.

        IMPORTANT: a slow pong is NOT a dead connection. On a 2Mbps Chrome
        DevTools throttle the pong can legitimately arrive 5–15s late while
        video is still flowing. Closing on timeout produced a 7-second
        disconnect-loop in real Chrome. The connection-liveness signals
        are the browser-side 15s stall detector and the websockets library's
        own 20s/20s keepalive; this monitor is purely informational."""
        while True:
            await asyncio.sleep(2.0)
            t0 = time.monotonic()
            try:
                pong_waiter = await ws.ping()
                # Generous timeout: this is a congestion signal, not liveness.
                # On a constrained link the pong can be many seconds behind.
                await asyncio.wait_for(pong_waiter, timeout=30.0)
                rtt_ms = (time.monotonic() - t0) * 1000
                ctrl.on_ping_rtt(rtt_ms)
                log.debug("ping rtt=%.1fms metric=%.1fms", rtt_ms, ctrl._metric_rtt)
            except asyncio.TimeoutError:
                # Don't close — feed it as a strong lag signal instead so
                # the controller backs off, but keep the connection alive.
                log.debug("ping rtt >30s for %s — feeding as congestion", ws.remote_address)
                ctrl.on_lag(30000.0, 0)
            except Exception as e:
                log.debug("ping err: %s", e)
                break

    global _active_clients
    _active_clients += 1
    try:
        await asyncio.gather(frame_sender(), input_reader(), dbg_sender(), ping_monitor())
    finally:
        _active_clients -= 1
        _dbg_eval_sessions.discard(dbg_q)
    log.info("client disconnect: %s", ws.remote_address)


def _check_token(path, password):
    if not password:
        return True
    if "?" in path:
        for part in path.split("?",1)[1].split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k == "token" and v == password:
                    return True
    return False


def make_http_handler(cfg, bridge):
    async def handler(connection, request):
        from websockets.http11 import Response
        from websockets.datastructures import Headers
        path = request.path

        # Debug eval endpoint: GET /dbg?js=<url-encoded-JS>  (localhost only)
        if path.startswith("/dbg"):
            from urllib.parse import parse_qs
            qs = parse_qs(path.split("?", 1)[1] if "?" in path else "")
            js = qs.get("js", [""])[0]  # parse_qs already URL-decodes values
            n = 0
            if js:
                for q in list(_dbg_eval_sessions):
                    q.put_nowait(js)
                    n += 1
            body = ("sent to %d session(s)\n" % n).encode()
            return Response(200, "OK", Headers([("Content-Type","text/plain")]), body)

        if not _check_token(path, cfg.password):
            # WebSocket upgrades: reject with 403 (no page)
            if request.headers.get("Upgrade","").lower() == "websocket":
                return Response(403, "Forbidden",
                                Headers([("Content-Type","text/plain")]), b"Forbidden\n")
            # HTTP GET: serve login page
            return Response(200, "OK", Headers([
                ("Content-Type", "text/html; charset=utf-8"),
                ("Content-Length", str(len(_LOGIN_HTML))),
                ("Cache-Control", "no-store"),
            ]), _LOGIN_HTML)
        if request.headers.get("Upgrade","").lower() != "websocket":
            hdrs = Headers([
                ("Content-Type","text/html; charset=utf-8"),
                ("Content-Length", str(len(_FRONTEND_HTML))),
                ("Cache-Control","no-cache"),
            ])
            return Response(200, "OK", hdrs, _FRONTEND_HTML)
    return handler


async def metric_session(ws):
    """Unloaded ping channel — echoes JSON messages immediately, no video data.
    Client sends {t:'ping',ts:N}, measures round-trip, reports delta to video WS."""
    try:
        async for msg in ws:
            if isinstance(msg, str):
                try:
                    await ws.send(msg)
                except Exception:
                    break
    except Exception:
        pass


def make_ws_handler(cfg, bridge):
    async def handler(ws):
        path = (ws.request.path if hasattr(ws, 'request') else "/").split("?")[0]
        if not _check_token(ws.request.path if hasattr(ws, 'request') else "/", cfg.password):
            await ws.close(1008, "Forbidden")
            return
        if path == "/audio":
            await audio_session(ws)
        elif path == "/metric":
            await metric_session(ws)
        else:
            await client_session(ws, cfg, bridge)
    return handler
