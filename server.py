#!/usr/bin/env python3
"""
mac-vnc-stream: High-performance macOS remote desktop in your browser over SSH.

Connects to macOS screensharingd via VNC, decodes ZRLE frames server-side,
re-encodes as JPEG, and streams to the browser via WebSocket at up to 20fps.
No extra macOS permissions needed beyond what Screen Sharing already has.

Usage:
    python server.py --vnc-pass <your_vnc_password>

For full mouse/keyboard control (type-30 Apple DH auth):
    python server.py --macos-user <username> --macos-pass <password>

Then SSH-tunnel port 6081 and open http://localhost:6081 in your browser:
    ssh -L 6081:localhost:6081 user@your-mac
"""
import argparse
import asyncio
import hashlib
import json
import logging
import os
import socket
import struct
import threading
import time
import zlib
from io import BytesIO

import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("macvnc")


# ---------------------------------------------------------------------------
# JPEG encoder (turbojpeg preferred, Pillow fallback)
# ---------------------------------------------------------------------------

def _find_turbojpeg():
    import turbojpeg
    for path in [
        "/opt/homebrew/lib/libturbojpeg.dylib",   # Apple Silicon
        "/usr/local/lib/libturbojpeg.dylib",        # Intel Mac
        None,
    ]:
        try:
            tj = turbojpeg.TurboJPEG(path)
            log.info("Using libturbojpeg for JPEG encoding (~17ms/frame)")
            return tj
        except Exception:
            continue
    return None


try:
    import turbojpeg as _tj_mod
    _TJ = _find_turbojpeg()
except ImportError:
    _TJ = None

if _TJ is None:
    log.warning("turbojpeg not available; falling back to Pillow (slower)")


def encode_jpeg(rgb: np.ndarray, quality: int) -> bytes:
    if _TJ is not None:
        import turbojpeg
        bgr = rgb[:, :, ::-1].copy()
        return _TJ.encode(
            bgr,
            quality=quality,
            pixel_format=turbojpeg.TJPF_BGR,
            jpeg_subsample=turbojpeg.TJSAMP_422,
        )
    from PIL import Image
    img = Image.fromarray(rgb)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=quality, subsampling=1)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# VNC helpers
# ---------------------------------------------------------------------------

def recv_n(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("VNC connection closed")
        buf += chunk
    return bytes(buf)


def vnc_des(password: str, challenge: bytes) -> bytes:
    """VNC type-2 DES challenge response."""
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.backends import default_backend
        key = password.encode()[:8].ljust(8, b"\x00")
        key = bytes(int("{:08b}".format(b)[::-1], 2) for b in key)
        cipher = Cipher(algorithms.TripleDES(key * 3), modes.ECB(),
                        backend=default_backend())
        return cipher.encryptor().update(challenge)


def vnc_apple_dh(sock: socket.socket, username: str, password: str) -> bool:
    """Apple DH (VNC type-30) authentication.

    Used by macOS screensharingd. Requires the macOS user account password,
    not the VNC-only password. Returns True on success.
    """
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.backends import default_backend

        g = struct.unpack("!H", recv_n(sock, 2))[0]
        key_len = struct.unpack("!H", recv_n(sock, 2))[0]
        prime = int.from_bytes(recv_n(sock, key_len), "big")
        server_pub = int.from_bytes(recv_n(sock, key_len), "big")

        # DH key exchange
        client_priv = int.from_bytes(os.urandom(key_len), "big") % (prime - 2) + 1
        client_pub = pow(g, client_priv, prime)
        shared = pow(server_pub, client_priv, prime)

        # AES-128 key = MD5(shared_secret)
        aes_key = hashlib.md5(shared.to_bytes(key_len, "big")).digest()

        # Encrypt: password[0:64] + username[0:64], AES-128-ECB
        payload = (
            password.encode("utf-8")[:64].ljust(64, b"\x00")
            + username.encode("utf-8")[:64].ljust(64, b"\x00")
        )
        encryptor = Cipher(
            algorithms.AES(aes_key), modes.ECB(), backend=default_backend()
        ).encryptor()
        encrypted = encryptor.update(payload) + encryptor.finalize()

        sock.send(client_pub.to_bytes(key_len, "big") + encrypted)

        try:
            result = struct.unpack("!I", recv_n(sock, 4))[0]
            return result == 0
        except ConnectionError:
            return False


# ---------------------------------------------------------------------------
# VNC ZRLE decoder
# ---------------------------------------------------------------------------

def _decode_zrle_rect(
    fb: np.ndarray,
    zd: zlib.Decompress,
    x: int, y: int, w: int, h: int,
    zdata: bytes,
    rs: int, gs: int, bs: int,
) -> None:
    """Decode a ZRLE-encoded rectangle into the framebuffer (in-place).

    screensharingd uses little-endian 32-bit pixels: rs=16, gs=8, bs=0.
    cpixel layout for little-endian: [B, G, R] at byte indices [0, 1, 2].
    rs//8=2 → cpixel[2]=R, gs//8=1 → cpixel[1]=G, bs//8=0 → cpixel[0]=B.
    """
    tiles = zd.decompress(zdata)
    pos = 0
    ri, gi, bi = rs // 8, gs // 8, bs // 8
    cy = 0
    while cy < h:
        th = min(64, h - cy)
        cx = 0
        while cx < w:
            tw = min(64, w - cx)
            if pos >= len(tiles):
                break
            subtype = tiles[pos]; pos += 1
            dy, dx = y + cy, x + cx

            if subtype == 0:  # Raw
                n = tw * th * 3
                tile = np.frombuffer(tiles[pos:pos + n], dtype=np.uint8).reshape(th, tw, 3)
                pos += n
                fb[dy:dy + th, dx:dx + tw, 0] = tile[:, :, ri]
                fb[dy:dy + th, dx:dx + tw, 1] = tile[:, :, gi]
                fb[dy:dy + th, dx:dx + tw, 2] = tile[:, :, bi]

            elif subtype == 1:  # Solid
                cp = tiles[pos:pos + 3]; pos += 3
                fb[dy:dy + th, dx:dx + tw, 0] = cp[ri]
                fb[dy:dy + th, dx:dx + tw, 1] = cp[gi]
                fb[dy:dy + th, dx:dx + tw, 2] = cp[bi]

            elif 2 <= subtype <= 16:  # Packed palette
                pal_n = subtype
                pal = np.frombuffer(tiles[pos:pos + pal_n * 3], dtype=np.uint8).reshape(pal_n, 3)
                pos += pal_n * 3
                bpi = 1 if pal_n <= 2 else 2 if pal_n <= 4 else 4
                nbytes = (tw * th * bpi + 7) // 8
                idx_data = tiles[pos:pos + nbytes]; pos += nbytes
                mask = (1 << bpi) - 1
                indices = []
                for byte in idx_data:
                    for shift in range(8 - bpi, -1, -bpi):
                        indices.append((byte >> shift) & mask)
                indices = np.array(indices[:tw * th], dtype=np.uint8).reshape(th, tw)
                fb[dy:dy + th, dx:dx + tw, 0] = pal[indices, ri]
                fb[dy:dy + th, dx:dx + tw, 1] = pal[indices, gi]
                fb[dy:dy + th, dx:dx + tw, 2] = pal[indices, bi]

            elif subtype == 128:  # Plain RLE
                row = np.zeros((th, tw, 3), dtype=np.uint8)
                fi = 0
                while fi < tw * th:
                    cp = tiles[pos:pos + 3]; pos += 3
                    r, g, b = cp[ri], cp[gi], cp[bi]
                    run = 1
                    while tiles[pos] == 255:
                        run += 255; pos += 1
                    run += tiles[pos]; pos += 1
                    for _ in range(run):
                        if fi >= tw * th:
                            break
                        row[fi // tw, fi % tw] = [r, g, b]
                        fi += 1
                fb[dy:dy + th, dx:dx + tw] = row

            elif subtype >= 130:  # Palette RLE
                pal_n = subtype - 128
                pal = np.frombuffer(tiles[pos:pos + pal_n * 3], dtype=np.uint8).reshape(pal_n, 3)
                pos += pal_n * 3
                row = np.zeros((th, tw, 3), dtype=np.uint8)
                fi = 0
                while fi < tw * th:
                    ib = tiles[pos]; pos += 1
                    idx = ib & 0x7F
                    if ib & 0x80:
                        run = 1
                        while tiles[pos] == 255:
                            run += 255; pos += 1
                        run += tiles[pos] + 1; pos += 1
                    else:
                        run = 1
                    r, g, b = pal[idx, ri], pal[idx, gi], pal[idx, bi]
                    for _ in range(run):
                        if fi >= tw * th:
                            break
                        row[fi // tw, fi % tw] = [r, g, b]
                        fi += 1
                fb[dy:dy + th, dx:dx + tw] = row

            cx += tw
        cy += th


# ---------------------------------------------------------------------------
# VNCBridge
# ---------------------------------------------------------------------------

KEYSYM = {
    "BackSpace": 0xFF08, "Tab": 0xFF09, "Return": 0xFF0D, "Escape": 0xFF1B,
    "Delete": 0xFFFF, "Insert": 0xFF63, "Home": 0xFF50, "End": 0xFF57,
    "PageUp": 0xFF55, "PageDown": 0xFF56,
    "ArrowLeft": 0xFF51, "ArrowUp": 0xFF52, "ArrowRight": 0xFF53, "ArrowDown": 0xFF54,
    "F1": 0xFFBE, "F2": 0xFFBF, "F3": 0xFFC0, "F4": 0xFFC1,
    "F5": 0xFFC2, "F6": 0xFFC3, "F7": 0xFFC4, "F8": 0xFFC5,
    "F9": 0xFFC6, "F10": 0xFFC7, "F11": 0xFFC8, "F12": 0xFFC9,
    "Shift": 0xFFE1, "ShiftLeft": 0xFFE1, "ShiftRight": 0xFFE2,
    "Control": 0xFFE3, "ControlLeft": 0xFFE3, "ControlRight": 0xFFE4,
    "Alt": 0xFFE9, "AltLeft": 0xFFE9, "AltRight": 0xFFEA,
    "Meta": 0xFFE7, "MetaLeft": 0xFFE7, "MetaRight": 0xFFE8,
    "CapsLock": 0xFFE5, " ": 0x0020,
}


class VNCBridge:
    """Persistent VNC connection providing screen capture and input injection.

    Runs a dedicated background thread that maintains the VNC session,
    decodes ZRLE frames into a framebuffer, and encodes JPEG for streaming.
    Thread-safe via a single lock protecting the framebuffer, JPEG bytes,
    and input queue.
    """

    def __init__(self, cfg: argparse.Namespace):
        self._cfg = cfg
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._fb: np.ndarray | None = None
        self._zd: zlib.Decompress | None = None
        self._jpeg: bytes | None = None
        self._input_q: list[bytes] = []
        self._clipboard_q: list[str] = []  # clipboard text to send to VNC
        self._W = self._H = 0
        self._rs = self._gs = self._bs = 0

        # Clipboard text received from server (sent to connected browsers)
        self.server_clipboard: str | None = None
        self.server_clipboard_seq = 0

    # ------------------------------------------------------------------
    # Public API (thread-safe)
    # ------------------------------------------------------------------

    def jpeg_frame(self) -> bytes | None:
        with self._lock:
            return self._jpeg

    def send_pointer(self, buttons: int, x: int, y: int) -> None:
        with self._lock:
            self._input_q.append(struct.pack("!BBHH", 5, buttons, x, y))

    def send_key(self, down: bool, keysym: int) -> None:
        with self._lock:
            self._input_q.append(struct.pack("!BBxxI", 4, int(down), keysym))

    def send_clipboard(self, text: str) -> None:
        """Send text to the Mac clipboard (ClientCutText)."""
        with self._lock:
            self._clipboard_q.append(text)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _flush_input(self) -> None:
        with self._lock:
            msgs = self._input_q[:]
            self._input_q.clear()
            clips = self._clipboard_q[:]
            self._clipboard_q.clear()
        if not self._sock:
            return
        data = b"".join(msgs)
        for text in clips:
            enc = text.encode("latin-1", errors="replace")
            data += struct.pack("!BBxxI", 6, 0, len(enc)) + enc
        if data:
            try:
                self._sock.sendall(data)
            except Exception as e:
                log.debug("input flush error: %s", e)

    def _connect(self) -> socket.socket:
        cfg = self._cfg
        s = socket.socket()
        s.connect((cfg.vnc_host, cfg.vnc_port))
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        # Handshake
        recv_n(s, 12)
        s.send(b"RFB 003.008\n")
        n = recv_n(s, 1)[0]
        types = list(recv_n(s, n))

        if cfg.macos_user and cfg.macos_pass and 30 in types:
            s.send(bytes([30]))
            ok = vnc_apple_dh(s, cfg.macos_user, cfg.macos_pass)
            if not ok:
                s.close()
                raise ConnectionError("Apple DH (type-30) auth failed — check --macos-user/--macos-pass")
            log.info("Authenticated via Apple DH (type-30) — full control enabled")
        elif 2 in types and cfg.vnc_pass:
            s.send(bytes([2]))
            s.send(vnc_des(cfg.vnc_pass, recv_n(s, 16)))
            result = struct.unpack("!I", recv_n(s, 4))[0]
            if result != 0:
                s.close()
                raise ConnectionError("VNC password auth failed — check --vnc-pass")
            log.info("Authenticated via VNC password (type-2) — screen capture only on macOS 15+")
        elif 1 in types:
            s.send(bytes([1]))
            log.info("No authentication (type-1)")
        else:
            s.close()
            raise ConnectionError(
                "No supported auth type. Server offers: %s. "
                "Provide --vnc-pass or --macos-user/--macos-pass." % types
            )

        s.send(b"\x01")  # shared session
        return s

    def _run_vnc(self) -> None:
        while True:
            try:
                s = self._connect()
                si = recv_n(s, 24)
                W, H = struct.unpack("!HH", si[:4])
                bpp = si[4]
                rs, gs, bs = struct.unpack("!BBB", si[14:17])
                nl = struct.unpack("!I", si[20:24])[0]
                recv_n(s, nl)
                log.info("VNC: %dx%d bpp=%d r/g/b shifts=%d/%d/%d", W, H, bpp, rs, gs, bs)

                with self._lock:
                    self._W, self._H = W, H
                    self._rs, self._gs, self._bs = rs, gs, bs
                    self._fb = np.zeros((H, W, 3), dtype=np.uint8)
                    self._zd = zlib.decompressobj()
                self._sock = s

                # Negotiate ZRLE encoding
                s.send(struct.pack("!BBHi", 2, 0, 1, 16))
                # Request initial full frame
                s.send(struct.pack("!BBHHHH", 3, 0, 0, 0, W, H))

                while True:
                    self._flush_input()
                    msg_type = recv_n(s, 1)[0]

                    if msg_type == 0:  # FramebufferUpdate
                        recv_n(s, 1)  # padding
                        nr = struct.unpack("!H", recv_n(s, 2))[0]
                        with self._lock:
                            fb = self._fb
                            zd = self._zd
                        for _ in range(nr):
                            rect = recv_n(s, 12)
                            rx, ry, rw, rh, enc = struct.unpack("!HHHHi", rect)
                            if enc == 16:  # ZRLE
                                dlen = struct.unpack("!I", recv_n(s, 4))[0]
                                zdata = recv_n(s, dlen)
                                try:
                                    _decode_zrle_rect(fb, zd, rx, ry, rw, rh, zdata, rs, gs, bs)
                                except Exception as e:
                                    log.debug("ZRLE decode error %s (rect %dx%d+%d+%d)", e, rw, rh, rx, ry)
                            elif enc == 0:  # Raw fallback
                                raw = recv_n(s, rw * rh * (bpp // 8))
                                arr = np.frombuffer(raw, dtype=np.uint8).reshape(rh, rw, bpp // 8)
                                with self._lock:
                                    if self._fb is not None:
                                        self._fb[ry:ry + rh, rx:rx + rw, 0] = arr[:, :, rs // 8]
                                        self._fb[ry:ry + rh, rx:rx + rw, 1] = arr[:, :, gs // 8]
                                        self._fb[ry:ry + rh, rx:rx + rw, 2] = arr[:, :, bs // 8]
                        # Encode JPEG
                        with self._lock:
                            if self._fb is not None:
                                jpeg = encode_jpeg(self._fb, self._cfg.quality)
                                self._jpeg = jpeg
                        # Request next incremental update
                        s.send(struct.pack("!BBHHHH", 3, 1, 0, 0, W, H))

                    elif msg_type == 2:  # Bell — ignore
                        pass

                    elif msg_type == 3:  # ServerCutText (clipboard from Mac)
                        recv_n(s, 3)  # padding
                        length = struct.unpack("!I", recv_n(s, 4))[0]
                        if length > 0:
                            text = recv_n(s, length).decode("latin-1", errors="replace")
                            self.server_clipboard = text
                            self.server_clipboard_seq += 1
                            log.debug("ServerCutText: %d chars", length)

                    else:
                        log.warning("Unknown VNC message type %d — dropping connection", msg_type)
                        break

            except Exception as e:
                log.warning("VNC error: %s — reconnecting in 3s", e)
                self._sock = None
            time.sleep(3)

    def start(self) -> None:
        t = threading.Thread(target=self._run_vnc, daemon=True)
        t.start()


# ---------------------------------------------------------------------------
# Browser HTML/JS
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mac-vnc-stream</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
html, body { width:100%; height:100%; background:#000; overflow:hidden; cursor:none; }
canvas { display:block; position:absolute; image-rendering:pixelated; }
#hud { position:fixed; top:6px; right:10px; color:#0f0; font:11px monospace;
  background:rgba(0,0,0,.65); padding:2px 8px; border-radius:3px;
  pointer-events:none; z-index:9; }
#cursor { position:fixed; width:12px; height:12px; border:2px solid #fff;
  border-radius:50%; pointer-events:none; transform:translate(-50%,-50%);
  box-shadow:0 0 4px #000; z-index:9; transition:border-color .1s; }
#cursor.click { border-color:#ff0; }
#status { position:fixed; bottom:10px; left:50%; transform:translateX(-50%);
  color:#aaa; font:11px monospace; background:rgba(0,0,0,.65);
  padding:2px 10px; border-radius:3px; pointer-events:none; z-index:9; }
</style></head><body>
<canvas id="c" tabindex="0"></canvas>
<div id="hud">connecting…</div>
<div id="cursor"></div>
<div id="status" id="status"></div>
<script>
const canvas = document.getElementById('c');
const ctx    = canvas.getContext('2d', { alpha: false, desynchronized: true });
const hud    = document.getElementById('hud');
const cursor = document.getElementById('cursor');
const status = document.getElementById('status');

let imgW = 1920, imgH = 1080, scaleX = 1, scaleY = 1, ox = 0, oy = 0;
let ws, wsOpen = false, mouseButtons = 0;
let fc = 0, lastFps = performance.now();
let clipSeq = 0;

function resize() {
  const vw = window.innerWidth, vh = window.innerHeight;
  const ar = imgW / imgH, vr = vw / vh;
  let cw, ch;
  if (ar > vr) { cw = vw; ch = vw / ar; } else { ch = vh; cw = vh * ar; }
  ox = (vw - cw) / 2; oy = (vh - ch) / 2;
  canvas.style.cssText = 'left:'+ox+'px;top:'+oy+'px;width:'+cw+'px;height:'+ch+'px;';
  scaleX = imgW / cw; scaleY = imgH / ch;
}
window.addEventListener('resize', resize);

function toVNC(cx, cy) {
  return [Math.round((cx - ox) * scaleX), Math.round((cy - oy) * scaleY)];
}
function inBounds(vx, vy) { return vx >= 0 && vy >= 0 && vx < imgW && vy < imgH; }
function send(obj) { if (ws && wsOpen) ws.send(JSON.stringify(obj)); }

// Mouse
document.body.addEventListener('mousemove', e => {
  cursor.style.left = e.clientX+'px'; cursor.style.top = e.clientY+'px';
  const [vx,vy] = toVNC(e.clientX, e.clientY);
  if (inBounds(vx,vy)) send({t:'mm', x:vx, y:vy, b:mouseButtons});
});
canvas.addEventListener('mousedown', e => {
  mouseButtons |= 1<<e.button;
  cursor.classList.add('click');
  const [vx,vy] = toVNC(e.clientX, e.clientY);
  send({t:'md', b:e.button, x:vx, y:vy});
  e.preventDefault(); canvas.focus();
});
canvas.addEventListener('mouseup', e => {
  mouseButtons &= ~(1<<e.button);
  cursor.classList.remove('click');
  const [vx,vy] = toVNC(e.clientX, e.clientY);
  send({t:'mu', b:e.button, x:vx, y:vy});
  e.preventDefault();
});
canvas.addEventListener('contextmenu', e => e.preventDefault());
canvas.addEventListener('wheel', e => {
  const [vx,vy] = toVNC(e.clientX, e.clientY);
  send({t:'sc', x:vx, y:vy, dx:e.deltaX, dy:e.deltaY});
  e.preventDefault();
}, {passive: false});

// Touch (mobile)
let lastTouch = null;
canvas.addEventListener('touchstart', e => {
  const t = e.touches[0];
  lastTouch = t;
  const [vx,vy] = toVNC(t.clientX, t.clientY);
  send({t:'md', b:0, x:vx, y:vy});
  e.preventDefault();
}, {passive:false});
canvas.addEventListener('touchmove', e => {
  const t = e.touches[0];
  lastTouch = t;
  const [vx,vy] = toVNC(t.clientX, t.clientY);
  send({t:'mm', x:vx, y:vy, b:1});
  e.preventDefault();
}, {passive:false});
canvas.addEventListener('touchend', e => {
  if (lastTouch) {
    const [vx,vy] = toVNC(lastTouch.clientX, lastTouch.clientY);
    send({t:'mu', b:0, x:vx, y:vy});
  }
  e.preventDefault();
}, {passive:false});

// Keyboard
canvas.addEventListener('keydown', e => {
  if (e.ctrlKey && e.key === 'v') {
    navigator.clipboard.readText().then(text => {
      if (text) send({t:'paste', text});
    }).catch(()=>{});
  }
  send({t:'kd', k:e.key, code:e.code});
  e.preventDefault();
});
canvas.addEventListener('keyup', e => {
  send({t:'ku', k:e.key, code:e.code});
  e.preventDefault();
});

// WebSocket
function connect() {
  ws = new WebSocket('ws://' + location.host + '/stream');
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    wsOpen = true;
    status.textContent = 'connected';
    canvas.focus();
  };
  ws.onclose = () => {
    wsOpen = false;
    hud.textContent = 'disconnected';
    status.textContent = 'reconnecting…';
    setTimeout(connect, 2000);
  };
  ws.onerror = () => {};
  ws.onmessage = e => {
    if (e.data instanceof ArrayBuffer) {
      createImageBitmap(new Blob([e.data], {type:'image/jpeg'})).then(bmp => {
        if (bmp.width !== imgW || bmp.height !== imgH) {
          imgW = bmp.width; imgH = bmp.height;
          canvas.width = imgW; canvas.height = imgH; resize();
        }
        ctx.drawImage(bmp, 0, 0); bmp.close();
        fc++;
        const now = performance.now();
        if (now - lastFps >= 1000) {
          hud.textContent = fc + ' fps  ' + imgW + 'x' + imgH;
          fc = 0; lastFps = now;
        }
      });
    } else {
      const msg = JSON.parse(e.data);
      if (msg.t === 'clipboard') {
        navigator.clipboard.writeText(msg.text).catch(()=>{});
        status.textContent = 'clipboard: ' + msg.text.substring(0,40);
        setTimeout(() => { status.textContent = ''; }, 2000);
      }
    }
  };
}

canvas.width = imgW; canvas.height = imgH; resize(); connect();
</script></body></html>
"""
HTML_BYTES = HTML.encode("utf-8")


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------

async def stream_handler(websocket, cfg: argparse.Namespace, bridge: VNCBridge):
    log.info("client connected: %s", websocket.remote_address)
    interval = 1.0 / cfg.fps
    cur_buttons = 0
    known_clip_seq = bridge.server_clipboard_seq

    async def input_reader():
        nonlocal cur_buttons
        try:
            async for raw in websocket:
                if not isinstance(raw, str):
                    continue
                try:
                    ev = json.loads(raw)
                    t = ev.get("t")
                    if t == "mm":
                        bridge.send_pointer(cur_buttons, int(ev["x"]), int(ev["y"]))
                    elif t == "md":
                        b = ev.get("b", 0)
                        cur_buttons |= 1 << b
                        bridge.send_pointer(cur_buttons, int(ev["x"]), int(ev["y"]))
                    elif t == "mu":
                        b = ev.get("b", 0)
                        cur_buttons &= ~(1 << b)
                        bridge.send_pointer(cur_buttons, int(ev.get("x", 0)), int(ev.get("y", 0)))
                    elif t == "sc":
                        x, y = int(ev.get("x", 0)), int(ev.get("y", 0))
                        dx, dy = float(ev.get("dx", 0)), float(ev.get("dy", 0))
                        if abs(dy) > abs(dx):
                            btn = 8 if dy < 0 else 16
                        else:
                            btn = 32 if dx < 0 else 64
                        bridge.send_pointer(btn, x, y)
                        await asyncio.sleep(0.05)
                        bridge.send_pointer(0, x, y)
                    elif t in ("kd", "ku"):
                        k = ev.get("k", "")
                        code = ev.get("code", "")
                        # Prefer code lookup for modifier disambiguation
                        ks = KEYSYM.get(code) or KEYSYM.get(k) or (ord(k) if len(k) == 1 else None)
                        if ks:
                            bridge.send_key(t == "kd", ks)
                    elif t == "paste":
                        text = ev.get("text", "")
                        if text:
                            bridge.send_clipboard(text)
                except Exception:
                    pass
        except Exception:
            pass

    async def frame_sender():
        nonlocal known_clip_seq
        last_jpeg = None
        try:
            while True:
                t0 = time.monotonic()
                jpeg = bridge.jpeg_frame()
                if jpeg and jpeg is not last_jpeg:
                    last_jpeg = jpeg
                    await websocket.send(jpeg)

                # Push clipboard from Mac to browser
                seq = bridge.server_clipboard_seq
                if seq != known_clip_seq and bridge.server_clipboard:
                    known_clip_seq = seq
                    await websocket.send(json.dumps({
                        "t": "clipboard",
                        "text": bridge.server_clipboard,
                    }))

                elapsed = time.monotonic() - t0
                await asyncio.sleep(max(0, interval - elapsed))
        except Exception as e:
            log.debug("frame sender: %s", e)

    await asyncio.gather(frame_sender(), input_reader())
    log.info("client disconnected")


# ---------------------------------------------------------------------------
# HTTP/WebSocket entry point
# ---------------------------------------------------------------------------

async def http_handler(connection, request):
    from websockets.http11 import Response
    from websockets.datastructures import Headers
    if request.headers.get("Upgrade", "").lower() != "websocket":
        hdrs = Headers([
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(HTML_BYTES))),
            ("Cache-Control", "no-cache"),
        ])
        return Response(200, "OK", hdrs, HTML_BYTES)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Stream a macOS screen to your browser at up to 20fps via VNC+JPEG."
    )
    p.add_argument("--vnc-host", default=os.environ.get("VNC_HOST", "127.0.0.1"))
    p.add_argument("--vnc-port", type=int, default=int(os.environ.get("VNC_PORT", "5900")))
    p.add_argument("--vnc-pass", default=os.environ.get("VNC_PASS", ""),
                   help="VNC password (Screen Sharing password in macOS System Settings)")
    p.add_argument("--macos-user", default=os.environ.get("MACOS_USER", ""),
                   help="macOS username for Apple DH auth (type-30) — enables full control")
    p.add_argument("--macos-pass", default=os.environ.get("MACOS_PASS", ""),
                   help="macOS login password for Apple DH auth (type-30)")
    p.add_argument("--listen", default=os.environ.get("LISTEN", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "6081")))
    p.add_argument("--fps", type=int, default=int(os.environ.get("FPS", "20")))
    p.add_argument("--quality", type=int, default=int(os.environ.get("JPEG_QUALITY", "65")),
                   help="JPEG quality 1-95 (default 65, ~180KB/frame at 1080p)")
    return p.parse_args()


async def main_async(cfg: argparse.Namespace) -> None:
    from websockets import serve

    bridge = VNCBridge(cfg)
    bridge.start()

    handler = lambda ws: stream_handler(ws, cfg, bridge)
    log.info("Listening on %s:%d — open http://localhost:%d in your browser", cfg.listen, cfg.port, cfg.port)
    log.info("(SSH tunnel: ssh -L %d:localhost:%d user@your-mac)", cfg.port, cfg.port)

    async with serve(
        handler,
        cfg.listen,
        cfg.port,
        process_request=http_handler,
        max_size=None,
        compression=None,
    ):
        await asyncio.Future()


def main():
    cfg = parse_args()
    if not cfg.vnc_pass and not (cfg.macos_user and cfg.macos_pass):
        print("Error: provide --vnc-pass or both --macos-user and --macos-pass")
        print("Example: python server.py --vnc-pass mypassword")
        raise SystemExit(1)
    asyncio.run(main_async(cfg))


if __name__ == "__main__":
    main()
