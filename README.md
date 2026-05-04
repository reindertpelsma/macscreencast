# mac-vnc-stream

**Browser-based macOS remote desktop at up to 60fps, over SSH.**

No third-party accounts. No cloud relay. Just a Python script and a browser.

> Solo project, ~140 commits, first public release. Read [`STATUS.md`](STATUS.md) for what's tested, what isn't, and what to expect.

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/reindertpelsma/mac-vnc-stream/main/install.sh)
```

Then from your laptop:
```bash
ssh -L 6081:localhost:6081 user@your-mac
open "http://localhost:6081/?token=YOUR_TOKEN"
```

---

## Who is this for

The core offer: browser-based access to a macOS screen through an SSH tunnel, with nothing in the path except your own server. NoMachine, RustDesk, Apple Remote Desktop and Parsec all do remote-desktop-to-macOS; what's distinctive here is the specific combination of **browser-only client, SSH-only transport, no relay infrastructure, hardware-decoded video via WebCodecs, and a congestion controller designed for SSH-tunnelled TCP** — that last point is what keeps it usable on a constrained link, where most browser-VNC stacks fill TCP buffers and produce multi-second lag spikes.

**Cloud and on-premise Mac infrastructure.** AWS EC2 Mac instances, MacStadium, Hetzner Mac minis, and rack-mounted Mac minis in CI rooms all need occasional GUI access — to click through a permission dialog, configure Xcode, or debug something that only reproduces on the physical display. SSH gets you a shell; this gets you a screen.

**On the same network.** If you SSH into a Mac at home or in the office, you can add `-L 6081:localhost:6081` to that command and open a browser tab. No client to install, no account to create, no relay.

**Compliance-conscious teams.** TeamViewer, AnyDesk, and Chrome Remote Desktop route through third-party servers. If that's off-limits — SOC2, ISO 27001, air-gapped environments, or just principle — this runs entirely inside your existing SSH infrastructure.

**Linux, Windows, and ChromeOS users.** Apple's built-in Screen Sharing only works Mac-to-Mac. From any machine with SSH and a modern browser, you get 60fps access in a tab.

---

## Why this exists

Every browser-based macOS remote desktop tool has the same problems:

- **noVNC over websockify runs at 2fps on macOS.** ZRLE decoding in JavaScript takes 400–500ms per frame regardless of network speed. The VNC protocol itself is fine; the bottleneck is running the decoder in the browser's JS engine without hardware acceleration.
- **screensharingd is unreliable.** It freezes, enters HID-idle and stops updating its framebuffer, and silently breaks clipboard and key mapping after reconnects.
- **Clipboard doesn't work.** VNC's `ClientCutText` is silently ignored on macOS 15+. Browser clipboard APIs are blocked on most browsers without user interaction.
- **Key mapping is broken.** Cmd vs Ctrl, Option, and modifier state all behave differently across VNC clients. Modifier keys get stuck after reconnects.

This tool solves all of it by going below screensharingd:

| Problem | Solution |
|---------|----------|
| 2fps ZRLE (JS) | Server decodes ZRLE in Python, re-encodes as H.264/H.265, browser uses WebCodecs hardware decode; primary path uses SCK directly at 60fps |
| screensharingd freezes | Watchdog auto-restarts it within ~30s; CGEvent input doesn't need it at all |
| Broken clipboard | pbpaste polling + native browser paste event; no permission required |
| Broken key mapping | CGEvent keyboard injection with Mac virtual key codes; bypasses VNC keysym translation entirely |
| Inverted/stuck modifiers | CGEvent tracks modifier state explicitly; no screensharingd state involved |
| No audio | SCK captures system audio; streamed as Opus 128kbps over a separate WebSocket |

---

## How it works

```
Browser                    Python server              macOS APIs
  │                             │
  │ ←── H.264/H.265 frames ──── │ ←── ScreenCaptureKit (SCK) ── GPU compositor
  │     (WebCodecs decode)      │         60fps, all windows
  │                             │
  │ ─── mouse/key events ─────→ │ ──── CGEventPost(kCGHIDEventTap) → input system
  │                             │         native HID-level, no screensharingd
  │                             │
  │ ←── clipboard JSON ──────── │ ←── pbpaste poll (1s) ─────── Mac clipboard
  │ ─── Ctrl+V paste ─────────→ │ ──── pbcopy + CGEvent Cmd+V → Mac clipboard
  │                             │
  │         ── VNC (screensharingd, port 5900) ──────────────────────────── │
  │         Used only as bootstrap and fallback while SCK/CGEvent            │
  │         permissions are being granted. Auto-managed and self-healing.    │
```

**VNC is a bootstrap transport, not the primary path.** The server starts over VNC so you can see the screen and click the macOS permission dialogs. Once Screen Recording and Accessibility are granted (typically within 30 seconds of first launch), the server upgrades automatically to SCK capture and CGEvent input. VNC keeps running as a warm spare.

> **VNC bootstrap requires screensharingd to already have Screen Recording permission.**
> Cloud Mac providers (Scaleway, AWS EC2 Mac, MacStadium) pre-seed this permission in their base images, so VNC works immediately after `setup.sh`. On a **fresh physical Mac** that has never had Screen Recording granted, screensharingd cannot capture the display — the VNC bootstrap path is unavailable. You need one-time physical (or KVM) access to open System Settings → Privacy & Security → Screen Recording and grant it before running `setup.sh` remotely.

---

## Quick start

```bash
# One command — installs everything, starts the server, triggers permission dialogs
bash <(curl -fsSL https://raw.githubusercontent.com/reindertpelsma/mac-vnc-stream/main/install.sh)
```

If you prefer to review the script before running it:
```bash
git clone https://github.com/reindertpelsma/mac-vnc-stream.git
cd mac-vnc-stream
bash setup.sh
```

**Installing on a remote Mac over SSH** — the `-t` flag allocates a TTY so the password prompt works:
```bash
ssh -t user@your-mac 'bash <(curl -fsSL https://raw.githubusercontent.com/reindertpelsma/mac-vnc-stream/main/install.sh)'
```

Then connect from your laptop (works for both local and remote installs):
```bash
ssh -NL 6081:localhost:6081 user@your-mac
open "http://localhost:6081/?token=YOUR_TOKEN"   # token shown at end of install
```

When you open the web UI:
1. Two macOS permission dialogs appear: **Screen Recording** and **Accessibility**
2. Click **Allow** on both
3. The server automatically upgrades to 60fps SCK capture and CGEvent input within 30 seconds

If you close the dialogs by accident, open **System Settings → Privacy & Security** and grant them manually. The server detects the change and upgrades without restart.

---

## Requirements

- macOS 13+ (Ventura or later; tested on Sonoma, Sequoia, Tahoe)
- Python 3.9+ with PyObjC (Xcode or Homebrew)
- `pip install av` — PyAV with VideoToolbox (strongly recommended for H.264/H.265)

`setup.sh` installs all Python dependencies automatically.

> **About VNC fallback (the bootstrap path).** On a fresh Mac that hasn't
> granted Python the Screen Recording permission yet, the server cannot use
> ScreenCaptureKit and falls back to Apple's `screensharingd` (VNC). VNC is
> the **bootstrap** path, not the destination — Apple's `screensharingd`
> implementation is laggy (3 s first-input spikes after idle, since it
> HID-idles after ~30 s), drops modifiers under load, and caps at ~21 fps.
> The whole point of the fallback is to let you click "Allow Python" on the
> Screen Recording prompt that appears when the server starts. Once you do
> that and re-run `setup.sh`, the server upgrades to SCK + CGEvent: 60 fps,
> ≤200 ms input latency, no modifier issues, no stored password. **That is
> the real product**; VNC is the on-ramp.

---

## Configuration

All flags can be set via CLI or environment variable:

| Flag | Env var | Default | Description |
|------|---------|---------|-------------|
| `--macos-user` | `MACOS_USER` | current user | macOS username for VNC auth |
| `--macos-pass` | `MACOS_PASS` | *(none)* | macOS login password for VNC auth |
| `--vnc-pass` | `VNC_PASS` | *(none)* | VNC password (type-2 auth; alternative to macos-user/pass) |
| `--listen` | `LISTEN` | `127.0.0.1` | Bind address (use SSH tunnel for remote access) |
| `--port` | `PORT` | `6081` | HTTP/WebSocket port |
| `--max-fps` | `MAX_FPS` | `60` | Maximum fps |
| `--codec` | `CODEC` | `h264` | `h264`, `h265`, or `jpeg` |
| `--password` | `MVS_PASSWORD` | *(none)* | Web UI access token (`?token=...`) |
| `--capture` | `CAPTURE_MODE` | `auto` | `auto` (SCK→VNC fallback), `sck`, `vnc` |
| `--input` | `INPUT_MODE` | `auto` | `auto` (CGEvent→VNC fallback), `cgevent`, `vnc` |
| `--vnc-only` | — | — | Force VNC for both capture and input |
| `--api-only` | — | — | Force SCK + CGEvent only; never contacts screensharingd |
| `--manage-screensharingd` | — | auto | Auto-restart screensharingd when VNC stalls |
| `--no-manage-screensharingd` | — | — | Disable screensharingd management |

---

## Browser controls

| Action | How |
|--------|-----|
| Mouse | Move, click, right-click, middle-click over canvas |
| Scroll | Mouse wheel (smooth via CGEvent, not VNC button simulation) |
| Keyboard | Click canvas to focus, then type normally |
| **Paste to Mac** | **Ctrl+V** — works on all browsers, no clipboard permission needed |
| **Copy from Mac** | Mac clipboard syncs to browser automatically (Chrome: live sync; Firefox/Safari: Ctrl+V fallback) |
| Fullscreen | F11 or the fullscreen button |
| **Audio** | **Click the Audio button — streams system audio via Opus 128kbps** |

### Clipboard in detail

**Browser → Mac (paste):** Ctrl+V captures from the browser's native `paste` event via a hidden `<textarea>`. No `navigator.clipboard` permission required. Works on Chrome, Firefox, and Safari.

**Mac → browser (copy):** The server polls `pbpaste` every second and pushes changes over WebSocket. On Chrome with clipboard permission granted, the browser clipboard is kept in sync automatically — this means paste works inside remote Mac apps via the Edit menu or right-click, not just Ctrl+V.

**Chrome full sync:** On connect, the browser requests `navigator.clipboard.readText()` permission once. If granted, clipboard is polled every second (only while the tab is focused — by design, for privacy). The Mac's clipboard always wins on tab focus: switching back to the remote tab pushes your current browser clipboard to the Mac immediately.

---

## Performance

Measured on a Mac mini M1/M2 over localhost SSH tunnel:

| Capture | Codec | Frame rate | Encode time | Bandwidth |
|---------|-------|-----------|-------------|-----------|
| VNC (screensharingd) | JPEG | ~20fps | ~17ms/frame | ~55 Mbps |
| VNC (screensharingd) | H.264 | ~20fps | ~5ms/frame | ~5 Mbps |
| SCK (GPU compositor) | H.264 | **~60fps** | ~5ms/frame | ~5 Mbps |

The frame rate jump comes from switching capture backends (screensharingd is capped by its own polling rate; SCK delivers directly from the GPU compositor). The codec switch from JPEG to H.264 mainly affects bandwidth — H.264 only encodes changed pixels, JPEG re-encodes the entire frame every time. H.264/H.265 encoding uses Apple VideoToolbox (hardware media engine) — near-zero CPU.

### Browser compatibility

| Browser | Video codec | Audio | Clipboard sync | Notes |
|---------|------------|-------|---------------|-------|
| Chrome 110+ | H.264, H.265, AV1 | ✅ | Full (live sync) | AV1 hardware requires M3+/A17 Pro |
| Firefox 130+ | H.264 | ✅ | Read-only (Ctrl+V) | No H.265 WebCodecs |
| Safari 26+ | H.265, H.264 | ✅ | Read-only (Ctrl+V) | H.265 selected automatically |
The server negotiates the best codec the browser reports it supports. JPEG fallback is used only when WebCodecs is unavailable (rare).

### Tip: keep the screen non-static for best responsiveness

macOS's WindowServer throttles the display compositor to ~3Hz when nothing is animating on screen. This causes 500ms–3s of first-keystroke latency — you type a character, the compositor is asleep, SCK has nothing to capture.

The server runs a compositor keepalive subprocess (a near-invisible window driven by CVDisplayLink) that prevents this throttling. But if you notice sluggishness after a long idle period, simply **moving the mouse** or having any animation running (a terminal with a clock, a browser tab with activity) keeps the compositor warm and eliminates the latency entirely.

This is a macOS WindowServer behavior, not a server bug. The keepalive handles it automatically in most cases.

---

## Auto-healing

The server is designed to run unattended without manual restarts:

**screensharingd watchdog (two-tier):**
- PID watcher checks every 5s — if the process dies, restarts it immediately via `sudo launchctl kickstart -k`
- FBU stall detector — if screensharingd is alive but frozen (no frame updates for 30s), restarts it
- Both use the macOS password already stored in the LaunchAgent environment
- Note: screensharingd can still stall intermittently between watchdog cycles. The SCK capture path (`--api-only`) avoids this entirely and is preferred when both permissions are granted.

**TCC permission watcher:**
- Monitors `TCC.db` mtime every 5s
- When Screen Recording is granted: upgrades from VNC capture to SCK within ~30s, no restart
- When Accessibility is granted: upgrades from VNC input to CGEvent within ~30s, no restart
- When either is revoked: logs a warning and falls back gracefully

**VNC reconnection:**
- Reconnects automatically after screensharingd restarts
- Periodic reconnect every 8 minutes prevents the screensharingd input-stall bug (silent socket kept open but events ignored)

---

## Capture and input modes

```bash
# Default: SCK with VNC fallback, CGEvent with VNC fallback
python3 server.py --macos-user alice --macos-pass password

# API-only: never contacts screensharingd (requires both permissions already granted)
python3 server.py --api-only --macos-user alice --macos-pass password

# VNC-only: force legacy path (useful for debugging)
python3 server.py --vnc-only --macos-user alice --macos-pass password

# Mixed: SCK capture + VNC input
python3 server.py --capture sck --input vnc --macos-user alice --macos-pass password
```

In `--api-only` mode, screensharingd is never contacted. If both Screen Recording and Accessibility are already granted, this is the cleanest mode.

---

## macOS compatibility

| macOS | SCK capture | CGEvent input | VNC input (fallback) |
|-------|------------|--------------|---------------------|
| 13 Ventura | ✅ | ✅ | ✅ |
| 14 Sonoma | ✅ | ✅ | ✅ |
| 15 Sequoia | ✅ | ✅ | ✅ |
| 26 Tahoe | ✅ | ✅ | ✅ |

macOS 15+ restricts unauthenticated VNC (type-2, no-auth) to view-only. Authenticated VNC with a macOS username and password retains full input control. `setup.sh` prompts for your login password during install and passes it automatically — VNC input works out of the box for most users. CGEvent input is unaffected either way; it uses the Accessibility API directly and is the preferred input path.

---

## Security

### macOS password storage — what `setup.sh` does, and when

The honest version: there are two install scenarios with different security shapes, and `setup.sh` adapts to which one you're in.

**Personal Macs (the common case): no password stored.** If `screensharingd` is already running (you've been using Screen Sharing locally) or if Screen Recording has already been granted to Python, `setup.sh` never asks for your login password. SCK is the capture path, the LaunchAgent runs without credentials, and there is nothing sensitive in the plist. This is what most users will see.

**Cloud Macs (Scaleway, AWS EC2 Mac, MacStadium, Hetzner, headless rack-mounted CI Macs): password is in the plist, by design.** A Mac that you only reach over SSH cannot grant Screen Recording TCC interactively. The only working capture path is VNC via `screensharingd`, and `screensharingd`'s AppleDH authentication (the secure auth type macOS 15+ requires for full input control) needs your login password every time the server starts — not just once. Storing it in `~/Library/LaunchAgents/com.macvncstream.server.plist` is what makes the service survive a reboot.

The plist is `0600`, owned by your user, and macOS already trusts your local user with that file (it lives next to many similar plists). The trade-off is: you accept that anyone with file-level access to your home directory could read it. On a throwaway cloud Mac that you control end-to-end and that's only reachable via SSH, this is the correct trade-off; on a shared multi-user box, it isn't.

**Once permissions are granted, you can drop the password:** edit the plist, remove the `MACOS_PASS` environment variable, add `--api-only` to `ProgramArguments`. The server then runs with no stored credentials. This is the recommended end state for any Mac you control long-term.

**Or skip the LaunchAgent entirely:**

```bash
MACOS_PASS=xxx python3 server.py --macos-user alice --api-only
# or, if VNC fallback is still needed:
MACOS_PASS=xxx python3 server.py --macos-user alice --macos-pass "$MACOS_PASS"
```

Environment variables passed at the command line are not written to disk.

### Access token

The token travels in the URL query string (`?token=…`). This is safe when accessed over an SSH tunnel to `localhost` — SSH encrypts the connection end-to-end. Do not use `--listen 0.0.0.0` without adding HTTPS in front, as the token will appear in server logs and browser history in plaintext.

### Uninstalling

```bash
launchctl bootout "gui/$(id -u)/com.macvncstream.server" 2>/dev/null || \
  sudo launchctl bootout system/com.macvncstream.server
sudo rm -f /Library/LaunchDaemons/com.macvncstream.server.plist
rm ~/Library/LaunchAgents/com.macvncstream.server.plist
rm -rf ~/mac-vnc-stream
```

---

## Using mac-vnc-stream in CI (GitHub Actions / similar)

For ephemeral CI workloads — running tests against a live mac-vnc-stream
server inside a workflow — **don't use `setup.sh` or `install.sh`**. Those
target persistent installs (LaunchAgent + .app bundle + TCC grant
ceremony). For CI you want short-lived, no-bundle, runs-once-per-job.

The right pattern: `pip install` Python deps, run `python3 server.py`
directly as a child of the runner shell. On most hosted macOS CI
runners (GitHub Actions's `macos-latest` definitely; others vary), the
runner image pre-grants Screen Recording + Accessibility to `/bin/bash`
or the runner agent. A `python3 server.py` child of the shell inherits
those grants via TCC's responsible-app chain, so SCK + CGEvent work
without any user grant ceremony or LaunchAgent dance.

Working example: [`.github/workflows/tmate-test.yml`](.github/workflows/tmate-test.yml).
Mirror its shape for your own CI:

```yaml
- name: Install Python dependencies
  run: python3 -m pip install --user --break-system-packages \
    'websockets>=13.0' 'numpy>=1.24' 'Pillow>=10.0' \
    'cryptography>=41.0' 'av>=12.0' \
    pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-Quartz \
    pyobjc-framework-AVFoundation pyobjc-framework-ScreenCaptureKit

- name: Enable screensharingd (gives SCK a display backend)
  run: sudo launchctl load -w /System/Library/LaunchDaemons/com.apple.screensharing.plist

- name: Start mac-vnc-stream server
  run: |
    nohup python3 server.py --listen 127.0.0.1 --port 6081 \
      --password citest --no-manage-screensharingd \
      > /tmp/macvncstream.log 2>&1 &
    until nc -z 127.0.0.1 6081; do sleep 1; done

- name: Run your tests against http://127.0.0.1:6081/?token=citest
  run: ...
```

This works because of macOS CI runner image quirks (Microsoft's
`actions/runner-images` pre-grants bash for their orchestration). It is
**not** how end-users on Scaleway / AWS Mac / personal Macs install —
those need the bundle path that `setup.sh` provides, because their bash
is unprivileged.

## Known limitations

- **Screen must be unlocked.** Input events go to whatever is on screen, including the lock screen.
- **Retina/HiDPI.** SCK captures at logical resolution (e.g. 1920×1080 on a 27" 5K display). Physical pixel counts above 4K will strain the encoder; use `--max-fps 30` on very high-res displays.
- **HTTPS required for clipboard on LAN.** If you expose the server directly on a LAN (not via SSH tunnel), `navigator.clipboard.writeText` requires HTTPS. The SSH tunnel works around this by keeping everything on `localhost`.
- **`--api-only` requires permissions already granted.** If Screen Recording or Accessibility haven't been granted yet, the server falls back to VNC automatically in `auto` mode.

---

## License

MIT
