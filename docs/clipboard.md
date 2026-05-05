# Browser controls and clipboard

| Action | How |
|--------|-----|
| Mouse | Move, click, right-click, middle-click over canvas |
| Scroll | Mouse wheel (smooth via CGEvent, not VNC button simulation) |
| Keyboard | Click canvas to focus, then type normally |
| **Paste to Mac** | **Ctrl+V** — works on all browsers, no clipboard permission needed |
| **Copy from Mac** | Mac clipboard syncs to browser automatically (Chrome: live sync; Firefox/Safari: Ctrl+V fallback) |
| Fullscreen | F11 or the fullscreen button |
| **Audio** | **Click the Audio button — streams system audio via Opus 128kbps** |

## Clipboard in detail

**Browser → Mac (paste):** Ctrl+V captures from the browser's native `paste` event via a hidden `<textarea>`. No `navigator.clipboard` permission required. Works on Chrome, Firefox, and Safari.

**Mac → browser (copy):** The server polls `pbpaste` every second and pushes changes over WebSocket. On Chrome with clipboard permission granted, the browser clipboard is kept in sync automatically — this means paste works inside remote Mac apps via the Edit menu or right-click, not just Ctrl+V.

**Chrome full sync:** On connect, the browser requests `navigator.clipboard.readText()` permission once. If granted, clipboard is polled every second (only while the tab is focused — by design, for privacy). The Mac's clipboard always wins on tab focus: switching back to the remote tab pushes your current browser clipboard to the Mac immediately.
