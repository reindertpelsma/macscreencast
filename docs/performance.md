# Performance

Measured on a Mac mini M1/M2 over localhost SSH tunnel.

| Capture | Codec | Frame rate | Encode time | Bandwidth |
|---------|-------|-----------|-------------|-----------|
| VNC (screensharingd) | JPEG | ~20fps | ~17ms/frame | ~55 Mbps |
| VNC (screensharingd) | H.264 | ~20fps | ~5ms/frame | ~5 Mbps |
| SCK (GPU compositor) | H.264 | **~60fps** | ~5ms/frame | ~5 Mbps |

The frame rate jump comes from switching capture backends: screensharingd is capped by its own polling rate, SCK delivers directly from the GPU compositor. The codec switch from JPEG to H.264 mainly affects bandwidth — H.264 only encodes changed pixels, JPEG re-encodes the entire frame every time. H.264/H.265 encoding uses Apple VideoToolbox (hardware media engine) — near-zero CPU.

## Browser compatibility

| Browser | Video codec | Audio | Clipboard sync | Notes |
|---------|------------|-------|---------------|-------|
| Chrome 110+ | H.264, H.265, AV1 | ✅ | Full (live sync) | AV1 hardware requires M3+/A17 Pro |
| Firefox 130+ | H.264 | ✅ | Read-only (Ctrl+V) | No H.265 WebCodecs |
| Safari 26+ | H.265, H.264 | ✅ | Read-only (Ctrl+V) | H.265 selected automatically |

The server negotiates the best codec the browser reports it supports. JPEG fallback is used only when WebCodecs is unavailable (rare).

## Tip: keep the screen non-static for best responsiveness

macOS's WindowServer throttles the display compositor to ~3Hz when nothing is animating on screen. This causes 500ms–3s of first-keystroke latency — you type a character, the compositor is asleep, SCK has nothing to capture.

The server runs a compositor keepalive subprocess (a near-invisible window driven by CVDisplayLink) that prevents this throttling. But if you notice sluggishness after a long idle period, simply **moving the mouse** or having any animation running (a terminal with a clock, a browser tab with activity) keeps the compositor warm and eliminates the latency entirely.

This is a macOS WindowServer behavior, not a server bug. The keepalive handles it automatically in most cases.

## Known limitations

- **Screen must be unlocked.** Input events go to whatever is on screen, including the lock screen.
- **Retina/HiDPI.** SCK captures at logical resolution (e.g. 1920×1080 on a 27" 5K display). Physical pixel counts above 4K will strain the encoder; use `--max-fps 30` on very high-res displays.
- **HTTPS required for clipboard on LAN.** If you expose the server directly on a LAN (not via SSH tunnel), `navigator.clipboard.writeText` requires HTTPS. The SSH tunnel works around this by keeping everything on `localhost`.
- **`--api-only` requires permissions already granted.** If Screen Recording or Accessibility haven't been granted yet, the server falls back to VNC automatically in `auto` mode.
