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

## Who this is for

You SSH into a Mac and want to see its screen in a browser tab — no extra client to install, no account, no third-party relay. That's the whole pitch.

A few cases where this matters:

- **Cloud and rack-mounted Macs.** AWS EC2 Mac, MacStadium, Hetzner, Scaleway, Mac minis in a CI room. SSH gets you a shell; this gets you the screen, in a tab.
- **Your own Mac at home or in the office.** Add `-L 6081:localhost:6081` to the SSH command you already use. Done.
- **Linux, Windows, ChromeOS.** Apple's Screen Sharing is Mac-to-Mac only. This works from anything with a browser and an SSH client.
- **Compliance-sensitive environments.** TeamViewer / AnyDesk / Chrome Remote Desktop all route through third-party servers. This doesn't — it runs inside your existing SSH.

What's actually distinctive vs NoMachine / RustDesk / Apple Remote Desktop / Parsec: browser-only client, SSH-only transport, no relay, hardware-decoded video via WebCodecs, and a congestion controller tuned for SSH-tunnelled TCP. That last bit is what keeps it usable on a slow link, where most browser-VNC stacks fill the TCP buffer and stutter for several seconds.

---

## Why this exists

Every browser-based macOS remote desktop tool has the same problems:

- **noVNC over websockify runs at 2fps on macOS.** ZRLE decoding in JavaScript takes 400–500ms per frame regardless of network speed.
- **screensharingd is unreliable.** Freezes, enters HID-idle, silently breaks clipboard and key mapping after reconnects.
- **Clipboard doesn't work.** VNC's `ClientCutText` is silently ignored on macOS 15+. Browser clipboard APIs are blocked without user interaction.
- **Key mapping is broken.** Cmd vs Ctrl, Option, modifier state — all behave differently across VNC clients. Modifiers get stuck after reconnects.

This tool solves all of it by going below screensharingd:

| Problem | Solution |
|---------|----------|
| 2fps ZRLE (JS) | Server transcodes to H.264/H.265 for WebCodecs hardware decode; primary path uses SCK directly at 60fps |
| screensharingd freezes | Watchdog auto-restarts it within ~30s; CGEvent input doesn't need it at all |
| Broken clipboard | `pbpaste` polling + native browser paste event; no permission required |
| Broken key mapping | CGEvent keyboard injection with Mac virtual key codes; bypasses VNC keysym translation entirely |
| No audio | SCK captures system audio; streamed as Opus 128kbps over a separate WebSocket |

---

## How it works

```
Browser                    Python server              macOS APIs
  │                             │
  │ ←── H.264/H.265 frames ──── │ ←── ScreenCaptureKit (SCK) ── GPU compositor
  │     (WebCodecs decode)      │         60fps, all windows
  │                             │
  │ ─── mouse/key events ─────→ │ ──── CGEventPost ─────────── input system
  │                             │         native HID-level
  │                             │
  │ ←── clipboard JSON ──────── │ ←── pbpaste poll (1s) ─────── Mac clipboard
  │ ─── Ctrl+V paste ─────────→ │ ──── pbcopy + CGEvent Cmd+V → Mac clipboard
```

VNC (`screensharingd`, port 5900) is used **only as a bootstrap path** — long enough to see the screen and click "Allow" on the macOS permission dialogs. Once Screen Recording and Accessibility are granted, the server upgrades to SCK + CGEvent automatically. See [`docs/vnc-bootstrap.md`](docs/vnc-bootstrap.md) for the details and gotchas.

---

## Quick start

```bash
# One command — installs everything, starts the server, triggers permission dialogs
bash <(curl -fsSL https://raw.githubusercontent.com/reindertpelsma/mac-vnc-stream/main/install.sh)
```

Review the script before running it:
```bash
git clone https://github.com/reindertpelsma/mac-vnc-stream.git
cd mac-vnc-stream
bash setup.sh
```

Installing on a remote Mac over SSH — `-t` allocates a TTY so the password prompt works:
```bash
ssh -t user@your-mac 'bash <(curl -fsSL https://raw.githubusercontent.com/reindertpelsma/mac-vnc-stream/main/install.sh)'
```

Connect from your laptop:
```bash
ssh -NL 6081:localhost:6081 user@your-mac
open "http://localhost:6081/?token=YOUR_TOKEN"   # token shown at end of install
```

When you open the web UI:
1. Two macOS permission dialogs appear: **Screen Recording** and **Accessibility**
2. Click **Allow** on both
3. Within 30 seconds, the server upgrades to 60fps SCK + CGEvent — no restart

If you close the dialogs by accident, open **System Settings → Privacy & Security** and grant them manually.

---

## Requirements

- macOS 13+ (Ventura or later; tested on Sonoma, Sequoia, Tahoe)
- Python 3.9+ with PyObjC (Xcode or Homebrew)
- `pip install av` — PyAV with VideoToolbox (strongly recommended for H.264/H.265)

`setup.sh` installs all Python dependencies automatically.

---

## More

- **[Configuration](docs/configuration.md)** — flags, env vars, capture/input modes, auto-healing, uninstall
- **[Performance](docs/performance.md)** — measured fps/bandwidth, browser compatibility, tuning tips
- **[Security](docs/security.md)** — when a password is stored, when it isn't, how to drop it
- **[Browser controls and clipboard](docs/clipboard.md)** — keyboard, mouse, paste/copy, audio
- **[VNC bootstrap path](docs/vnc-bootstrap.md)** — why VNC is used, why it's slow, cloud vs fresh-Mac differences
- **[CI usage](docs/ci.md)** — running the server inside GitHub Actions / similar

---

## License

MIT
