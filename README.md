# mac-vnc-stream

**A high-performance macOS remote desktop that runs in your browser, accessible over SSH.**

No third-party accounts. No cloud relay. No extra macOS permissions. Just a Python script, an SSH tunnel, and a browser.

```
ssh -L 6081:localhost:6081 user@your-mac
open http://localhost:6081
```

![20fps demo placeholder](docs/demo.gif)

---

## Why this exists

The standard solution — noVNC over websockify — runs at **2fps** on macOS. The bottleneck is the browser decoding ZRLE frames in JavaScript: each frame takes 400–500ms in Chrome, regardless of your network speed.

`mac-vnc-stream` fixes this by:

1. **Decoding ZRLE server-side** — Python + numpy decodes the VNC framebuffer
2. **Re-encoding as JPEG** — libturbojpeg at 17ms/frame (vs 32ms with Pillow)  
3. **Pushing to the browser** — WebSocket binary push at up to 20fps
4. **GPU-accelerated display** — browser uses `createImageBitmap` (hardware JPEG decode) + `desynchronized` canvas

Result: **~20fps at ~180KB/frame** on a 1920×1080 display at JPEG quality 65. Works fine over a 10Mbit connection.

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
- `brew install jpeg-turbo` (optional, 2× faster than Pillow fallback)

Python packages (installed by `install.sh`):
```
websockets numpy Pillow cryptography PyTurboJPEG
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
| `--fps` | `FPS` | `20` | Target frame rate |
| `--quality` | `JPEG_QUALITY` | `65` | JPEG quality (1–95) |

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
| **Paste text** | **Ctrl+V** (uses browser clipboard API) |
| **Copy from Mac** | Mac clipboard automatically syncs to browser |
| Mobile | Touch events supported (tap, drag) |

---

## How it works

```
Browser                  Python server           macOS screensharingd
  │                           │                         │
  │ ← WebSocket JPEG push ─── │ ←── ZRLE frames ─────── │
  │                           │  (decode + re-encode)    │
  │ ── mouse/key events ────→ │ ──── VNC PointerEvent ─→ │
  │                           │       KeyEvent           │
  │ ←─ clipboard JSON ─────── │ ←── ServerCutText ─────── │
  │ ── paste text ──────────→ │ ──── ClientCutText ─────→ │
```

`screensharingd` already holds Screen Recording and Accessibility entitlements
from Apple's signed bundle. The server connects to it via the VNC protocol
on `localhost:5900` — no extra TCC permissions required for screen capture.

**ZRLE → JPEG pipeline:**
- ZRLE compressed data decompressed with `zlib`
- Tiles (raw, solid, packed palette, RLE, palette RLE) decoded into a numpy framebuffer
- numpy RGB array → libturbojpeg → JPEG bytes (~17ms at 1080p, quality 65)
- JPEG pushed via WebSocket binary frame
- Browser: `createImageBitmap(blob)` → GPU-decoded → drawn to `<canvas>` with `desynchronized:true`

---

## Performance

Measured on a Mac mini M2 over localhost SSH tunnel:

| Metric | Value |
|--------|-------|
| Frame rate | ~20fps (target) |
| JPEG encode time | ~17ms/frame (libturbojpeg) |
| Frame size | ~180KB @ 1080p quality 65 |
| Bandwidth | ~3.6MB/s |
| noVNC comparison | 2fps vs **20fps** (10× faster) |

Reduce `--quality` (e.g. 45) or `--fps` for lower bandwidth. Quality 45 gives ~120KB/frame (~2.4MB/s) with acceptable clarity.

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

- **Type-30 requires your macOS login password.** This is the same credential you use to log in at the keyboard. Keep it in an env var, not a CLI flag.
- **Screen must be unlocked for type-2 auth.** On a locked screen, even type-30 events go to the lock screen — you'll need to type your password to unlock.
- **Clipboard API requires HTTPS or localhost.** The SSH tunnel keeps you on `localhost`, so it works. If you expose the server on a LAN without TLS, clipboard paste from browser will be blocked by the browser's security model.
- **No audio.** VNC doesn't carry audio; this doesn't either.
- **Retina/HiDPI:** screensharingd presents the display at its native resolution to VNC. On a 5K display you'll get a 5120×2880 stream. Use `--quality 40 --fps 15` for high-res displays.

---

## License

MIT
