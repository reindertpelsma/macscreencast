# Configuration

All flags can be set via CLI or environment variable.

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

## macOS compatibility

| macOS | SCK capture | CGEvent input | VNC input (fallback) |
|-------|------------|--------------|---------------------|
| 13 Ventura | ✅ | ✅ | ✅ |
| 14 Sonoma | ✅ | ✅ | ✅ |
| 15 Sequoia | ✅ | ✅ | ✅ |
| 26 Tahoe | ✅ | ✅ | ✅ |

macOS 15+ restricts unauthenticated VNC (type-2, no-auth) to view-only. Authenticated VNC with a macOS username and password retains full input control. `setup.sh` prompts for your login password during install and passes it automatically — VNC input works out of the box for most users. CGEvent input is unaffected either way; it uses the Accessibility API directly and is the preferred input path.

## Auto-healing

The server is designed to run unattended without manual restarts.

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

## Uninstalling

```bash
launchctl bootout "gui/$(id -u)/com.macvncstream.server" 2>/dev/null || \
  sudo launchctl bootout system/com.macvncstream.server
sudo rm -f /Library/LaunchDaemons/com.macvncstream.server.plist
rm ~/Library/LaunchAgents/com.macvncstream.server.plist
rm -rf ~/mac-vnc-stream
```
