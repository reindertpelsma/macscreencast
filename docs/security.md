# Security

## macOS password storage — what `setup.sh` does, and when

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

## Access token

The token travels in the URL query string (`?token=…`). This is safe when accessed over an SSH tunnel to `localhost` — SSH encrypts the connection end-to-end. Do not use `--listen 0.0.0.0` without adding HTTPS in front, as the token will appear in server logs and browser history in plaintext.
