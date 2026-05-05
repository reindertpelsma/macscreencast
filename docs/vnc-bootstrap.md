# VNC bootstrap path

VNC is **how the server gets started**, not how it runs.

When you first run `setup.sh`, the server hasn't been granted Screen Recording yet — so it can't use the fast capture path (ScreenCaptureKit). It falls back to macOS's built-in `screensharingd` (VNC) just long enough for you to see the screen and click "Allow" on the permission dialogs.

Once Screen Recording and Accessibility are granted (usually within 30 seconds of first launch), the server **automatically upgrades** to:

- **ScreenCaptureKit** for video — 60fps from the GPU compositor, hardware-encoded
- **CGEvent** for input — native HID-level events, no modifier glitches

That's the path this project ships. VNC keeps running as a warm spare so a dead `screensharingd` process doesn't lock you out, but it isn't the experience.

## Why VNC is slow on macOS

`screensharingd` caps at around 5–20fps even on a local network. The bottleneck is ZRLE encoding inside Apple's own implementation — we can transcode the wire format to H.264/H.265 for the browser, but we can't speed up the source. Several Mac cloud hosts publicly position `screensharingd` as too slow for daily use, which matches what we see.

VNC also has rougher edges:

- 3-second first-input spikes after idle (HID-idles after ~30s)
- Drops modifier keys under load
- Clipboard (`ClientCutText`) silently ignored on macOS 15+

These all go away on the SCK + CGEvent path.

## Cloud Macs vs fresh physical Macs

**Cloud Macs (Scaleway, AWS EC2 Mac, MacStadium, Hetzner):** the provider pre-grants Screen Recording to `screensharingd` in their base images, so VNC works immediately after `setup.sh` runs over SSH.

**Fresh physical Mac that has never had Screen Recording granted:** `screensharingd` can't capture the display, so the VNC bootstrap path is unavailable. You need one-time physical (or KVM) access to **System Settings → Privacy & Security → Screen Recording** to grant it before running `setup.sh` remotely.

## What "auto-upgrade" actually does

The server polls the TCC database every 5 seconds. When it sees Screen Recording flip to granted, it switches the capture path live — no restart, no reconnect from the browser. Same for Accessibility flipping the input path from VNC to CGEvent. Worst case is a 30-second pause; in practice it's faster.
