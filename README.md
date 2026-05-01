# mac-vnc-stream

**A low-bandwidth macOS remote desktop that streams H.264/H.265 to your browser over SSH.**

No third-party accounts. No cloud relay. No extra macOS permissions. Just a Python script, an SSH tunnel, and a browser.

```
ssh -L 6081:localhost:6081 user@your-mac
open "http://localhost:6081/?token=YOUR_WEB_TOKEN"
```

---

## Why this exists

The standard solution — noVNC over websockify — runs at **2fps** on macOS. The bottleneck is ZRLE decoding in JavaScript: each frame takes 400–500ms in Chrome regardless of network speed.

`mac-vnc-stream` fixes this by:

1. **Decoding ZRLE server-side** — Python + numpy decodes the VNC framebuffer
2. **Re-encoding as H.264/H.265** — Apple VideoToolbox hardware encoder on macOS (~5ms/frame)
3. **WebCodecs decode in browser** — GPU-accelerated `VideoDecoder` API (Chrome 94+, Firefox 130+, Safari 17.4+)
4. **Adaptive bitrate** — per-client controller scales fps and bitrate based on network feedback

Result: **~20–60fps at 2–5 Mbps** on a 1920×1080 display. Around **10× lower bandwidth** than JPEG streaming.

JPEG fallback activates automatically for browsers without WebCodecs.

---

## Quick start

**On the Mac:**

```bash
# Enable Screen Sharing in System Settings → Sharing → Screen Sharing
# Set a VNC password in Screen Sharing → (i) → Allow VNC viewers to control screen

git clone https://github.com/yourname/mac-vnc-stream
cd mac-vnc-stream
bash install.sh
python3 server.py --vnc-pass YOUR_VNC_PASSWORD
```

**From your laptop:**

```bash
ssh -L 6081:localhost:6081 user@your-mac   # keep this open
open http://localhost:6081                  # or paste in browser
```

---

## Requirements

- macOS with **Screen Sharing enabled** (System Settings → Sharing → Screen Sharing)
- Python 3.9+
- `pip install av` — PyAV (bundles VideoToolbox on macOS; **strongly recommended**)
- `brew install jpeg-turbo` — optional, faster JPEG fallback

Python packages (installed by `install.sh`):
```
websockets numpy Pillow cryptography av PyTurboJPEG
```

---

## Authentication modes

### Mode 1 — VNC password (screen capture + **view only** on macOS 15+)

```bash
python3 server.py --vnc-pass YOUR_VNC_PASSWORD
```

Set the VNC password in: System Settings → Sharing → Screen Sharing → click **(i)** → "Allow VNC viewers to control screen with password".

> **macOS 15+ note:** Apple restricts VNC type-2 (password-only) connections to
> **view-only** in macOS Sequoia (15) and later. You'll see the screen but
> mouse/keyboard events won't be injected. For full control use Mode 2.
>
> On macOS 14 (Sonoma) and earlier, type-2 gives full control.

### Mode 2 — Apple DH auth (full control on all macOS versions)

```bash
python3 server.py --macos-user yourname --macos-pass YOUR_MACOS_LOGIN_PASSWORD
```

This uses the Apple Remote Desktop DH key exchange (VNC security type 30),
which authenticates with your **macOS account password** (not the VNC-only
password). It gives `screensharingd` full Accessibility access to inject
mouse and keyboard events.

**Security:** your login password travels in memory only and is never written
to disk by the server. Use `MACOS_PASS` environment variable instead of the
CLI flag to avoid it appearing in `ps` output.

```bash
export MACOS_PASS=your_password
python3 server.py --macos-user yourname
```

---

## Web UI access token

To prevent unauthorized access when the server is reachable over a network:

```bash
python3 server.py --vnc-pass VNC_PASS --password YOUR_WEB_TOKEN
# or
MVS_PASSWORD=YOUR_WEB_TOKEN python3 server.py ...
```

Clients must include `?token=YOUR_WEB_TOKEN` in the URL:
```
http://localhost:6081/?token=YOUR_WEB_TOKEN
```

Without `--password`, access is unrestricted (safe when bound to `127.0.0.1` over SSH).

---

## Configuration

All options can be set via CLI flags or environment variables:

| Flag | Env var | Default | Description |
|------|---------|---------|-------------|
| `--vnc-host` | `VNC_HOST` | `127.0.0.1` | VNC server address |
| `--vnc-port` | `VNC_PORT` | `5900` | VNC server port |
| `--vnc-pass` | `VNC_PASS` | *(none)* | VNC password (type-2 auth) |
| `--macos-user` | `MACOS_USER` | *(none)* | macOS username (type-30 auth) |
| `--macos-pass` | `MACOS_PASS` | *(none)* | macOS password (type-30 auth) |
| `--listen` | `LISTEN` | `127.0.0.1` | WebSocket/HTTP bind address |
| `--port` | `PORT` | `6081` | WebSocket/HTTP port |
| `--fps` | `FPS` | `20` | Initial/minimum fps (responsive floor) |
| `--max-fps` | `MAX_FPS` | `60` | Maximum fps when bandwidth allows |
| `--codec` | `CODEC` | `h264` | Video codec: `h264`, `h265`, `jpeg` |
| `--password` | `MVS_PASSWORD` | *(none)* | Web UI access token (`?token=...`) |

---

## Auto-start at login (LaunchAgent)

```bash
cp launchagent.plist.template ~/Library/LaunchAgents/com.macvncstream.server.plist
# Edit the plist: set PYTHON_PATH, SERVER_PATH, and credentials
launchctl load ~/Library/LaunchAgents/com.macvncstream.server.plist
# Check logs:
tail -f /tmp/macvncstream.log
```

---

## Browser controls

| Action | Gesture |
|--------|---------|
| Move mouse | Move cursor over canvas |
| Left click | Click |
| Right click | Right-click |
| Middle click | Middle-click |
| Scroll | Mouse wheel |
| Keyboard | Click canvas to focus, then type |
| **Paste text** | **Ctrl+V** — works on all browsers, no clipboard permission popup |
| **Copy from Mac** | Mac clipboard automatically syncs to browser |
| Mobile | Touch events supported (tap, drag) |

**Paste works on Firefox and Safari** too — uses a hidden `<textarea>` that captures the browser's native `paste` event, requiring no `navigator.clipboard` permission.

---

## How it works

```
Browser                  Python server           macOS screensharingd
  │                           │                         │
  │ ←─ H.264 binary frames ── │ ←── ZRLE frames ─────── │
  │    (WebCodecs decode)      │   (decode + VideoToolbox │
  │                           │    H.264/H.265 encode)   │
  │ ── mouse/key events ────→ │ ──── VNC PointerEvent ─→ │
  │                           │       KeyEvent           │
  │ ←─ clipboard JSON ─────── │ ←── ServerCutText ─────── │
  │ ── paste text ──────────→ │ ──── ClientCutText ─────→ │
  │ ── lag reports ─────────→ │ (adaptive bitrate/fps)    │
```

**ZRLE → H.264 pipeline:**
1. ZRLE data decompressed with `zlib`; tiles decoded into a numpy RGB framebuffer
2. Per-client adaptive controller picks current fps and bitrate target
3. `av.CodecContext` (`h264_videotoolbox`) encodes RGB→YUV420→H.264 Annex B
4. 18-byte binary header prepended: `seq(4) capture_ms(8) codec(1) flags(1) payload_len(4)`
5. Browser parses header, feeds `EncodedVideoChunk` to `VideoDecoder`, draws to `<canvas>`

**Adaptive controller:**
- Starts at `--fps` (default 20) and 5 Mbps
- Client reports `{t:'lag', age_ms:N}` every 500ms
- Server also reads `ws.transport.get_write_buffer_size()` for immediate TCP backpressure
- On pressure: cut fps first (maintain ≥20fps for responsiveness), then cut bitrate
- On 2s of clean delivery: gradually restore bitrate (15% per step), then fps

**JPEG fallback:** if the client browser has no `VideoDecoder` API, or if VideoToolbox isn't available, all frames are sent as JPEG (same wire format, codec byte = 0). No separate negotiation needed.

---

## Performance

Measured on a Mac mini M2 (Apple Silicon) over localhost SSH tunnel:

| Metric | JPEG mode | H.264 mode |
|--------|-----------|-----------|
| Frame rate | ~20fps | 20–60fps adaptive |
| Encode time | ~17ms/frame | ~5ms/frame (VideoToolbox) |
| Frame size | ~180KB @ 1080p | ~30KB/frame average |
| Bandwidth | ~55 Mbps | **~2–5 Mbps** |
| WebCodecs required | No | Yes (fallback: JPEG) |

H.264 via VideoToolbox uses Apple Silicon's dedicated media engine — essentially zero CPU cost.

---

## macOS compatibility

| macOS version | Screen capture | Mouse/keyboard (type-2) | Mouse/keyboard (type-30) |
|---------------|---------------|------------------------|--------------------------|
| 14 Sonoma     | ✅ | ✅ | ✅ |
| 15 Sequoia    | ✅ | ⚠️ view-only | ✅ |
| 26 Tahoe      | ✅ | ⚠️ view-only | ✅ |

Apple tightened VNC type-2 input injection starting in macOS 15. Use `--macos-user/--macos-pass` for full control.

---

## Known limitations

- **Type-30 requires your macOS login password.** Keep it in `MACOS_PASS` env var, not a CLI flag.
- **Screen must be unlocked for input injection.** On a locked screen, events go to the lock screen.
- **Clipboard sync from Mac to browser requires HTTPS or localhost.** The SSH tunnel keeps you on `localhost`, so it works. Exposing on LAN without TLS blocks `navigator.clipboard.writeText`.
- **No audio.** VNC doesn't carry audio.
- **Retina/HiDPI:** screensharingd presents the display at native resolution. On a 5K display you'll get a 5120×2880 stream — use `--codec jpeg --fps 15` for high-res displays.
- **No runtime codec switching.** Codec is fixed at startup. Restart the server to change.

---

## License

MIT
