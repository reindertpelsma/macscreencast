#!/usr/bin/env python3
"""
mac-vnc-stream: Adaptive H.264/H.265/JPEG macOS remote desktop in your browser over SSH.

python server.py --vnc-pass PASSWORD
python server.py --macos-user u --macos-pass p  # full control (macOS 15+)
ssh -L 6081:localhost:6081 user@mac && open http://localhost:6081
"""
import argparse, asyncio, hashlib, json, logging, os, select, socket, struct
import threading, time, zlib
from io import BytesIO
import numpy as np

log = logging.getLogger("macvnc")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------------------------------------------------------------------------
# PyAV (optional — JPEG fallback if unavailable)
# ---------------------------------------------------------------------------
try:
    import av as _av
    _AV_OK = True
except ImportError:
    _av = None
    _AV_OK = False
    log.warning("PyAV not installed (pip install av) — JPEG-only mode")

# JPEG fallback encoder
try:
    import turbojpeg as _tj_mod
    _TJ = None
    for _p in ["/opt/homebrew/lib/libturbojpeg.dylib", "/usr/local/lib/libturbojpeg.dylib", None]:
        try: _TJ = _tj_mod.TurboJPEG(_p); break
        except: pass
except ImportError:
    _TJ = None

def _encode_jpeg(rgb, quality):
    if _TJ:
        import turbojpeg
        return _TJ.encode(rgb[:,:,::-1].copy(), quality=quality,
                          pixel_format=turbojpeg.TJPF_BGR, jpeg_subsample=turbojpeg.TJSAMP_422)
    from PIL import Image
    buf = BytesIO()
    Image.fromarray(rgb).save(buf, format="JPEG", quality=quality, subsampling=1)
    return buf.getvalue()

# ---------------------------------------------------------------------------
# Frame wire format
# Header = 18 bytes:
#   seq(4)  capture_ms(8)  codec(1)  flags(1)  payload_len(4)
# codec: 0=jpeg  1=h264  2=h265
# flags: bit0=keyframe
# ---------------------------------------------------------------------------
CODEC_JPEG, CODEC_H264, CODEC_H265 = 0, 1, 2

def _hdr(seq, capture_ms, codec, keyframe, plen):
    return struct.pack(">IQBBI", seq, capture_ms, codec, 1 if keyframe else 0, plen)

# ---------------------------------------------------------------------------
# VNC helpers
# ---------------------------------------------------------------------------
KEYSYM = {
    "BackSpace":0xFF08,"Tab":0xFF09,"Return":0xFF0D,"Escape":0xFF1B,
    "Delete":0xFFFF,"Insert":0xFF63,"Home":0xFF50,"End":0xFF57,
    "PageUp":0xFF55,"PageDown":0xFF56,
    "ArrowLeft":0xFF51,"ArrowUp":0xFF52,"ArrowRight":0xFF53,"ArrowDown":0xFF54,
    "F1":0xFFBE,"F2":0xFFBF,"F3":0xFFC0,"F4":0xFFC1,"F5":0xFFC2,"F6":0xFFC3,
    "F7":0xFFC4,"F8":0xFFC5,"F9":0xFFC6,"F10":0xFFC7,"F11":0xFFC8,"F12":0xFFC9,
    "Shift":0xFFE1,"ShiftLeft":0xFFE1,"ShiftRight":0xFFE2,
    "Control":0xFFE3,"ControlLeft":0xFFE3,"ControlRight":0xFFE4,
    "Alt":0xFFE9,"AltLeft":0xFFE9,"AltRight":0xFFEA,
    "Meta":0xFFE7,"MetaLeft":0xFFE7,"MetaRight":0xFFE8,
    "CapsLock":0xFFE5," ":0x0020,
}

def _recv(sock, n):
    buf = bytearray()
    while len(buf) < n:
        c = sock.recv(n - len(buf))
        if not c: raise ConnectionError("VNC closed")
        buf += c
    return bytes(buf)

def _vnc_des(password, challenge):
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.backends import default_backend
        key = password.encode()[:8].ljust(8, b"\x00")
        key = bytes(int("{:08b}".format(b)[::-1],2) for b in key)
        return Cipher(algorithms.TripleDES(key*3), modes.ECB(),
                      backend=default_backend()).encryptor().update(challenge)

def _vnc_apple_dh(sock, username, password):
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.backends import default_backend
        g = struct.unpack("!H", _recv(sock, 2))[0]
        kl = struct.unpack("!H", _recv(sock, 2))[0]
        prime = int.from_bytes(_recv(sock, kl), "big")
        spub  = int.from_bytes(_recv(sock, kl), "big")
        cpriv = int.from_bytes(os.urandom(kl), "big") % (prime-2) + 1
        cpub  = pow(g, cpriv, prime)
        shared = pow(spub, cpriv, prime)
        aes_key = hashlib.md5(shared.to_bytes(kl, "big")).digest()
        payload = (username.encode("utf-8")[:64].ljust(64, b"\x00")
                   + password.encode("utf-8")[:64].ljust(64, b"\x00"))
        enc = Cipher(algorithms.AES(aes_key), modes.ECB(),
                     backend=default_backend()).encryptor()
        ciphertext = enc.update(payload) + enc.finalize()
        sock.send(ciphertext + cpub.to_bytes(kl, "big"))
        try:
            return struct.unpack("!I", _recv(sock, 4))[0] == 0
        except ConnectionError:
            return False

# ---------------------------------------------------------------------------
# ZRLE decoder
# ---------------------------------------------------------------------------
def _decode_zrle(fb, zd, x, y, w, h, zdata, rs, gs, bs):
    tiles = zd.decompress(zdata)
    pos = 0
    ri, gi, bi = rs//8, gs//8, bs//8
    cy = 0
    while cy < h:
        th = min(64, h-cy)
        cx = 0
        while cx < w:
            tw = min(64, w-cx)
            if pos >= len(tiles): break
            st = tiles[pos]; pos += 1
            dy, dx = y+cy, x+cx
            if st == 0:  # Raw
                n = tw*th*3
                t = np.frombuffer(tiles[pos:pos+n], dtype=np.uint8).reshape(th,tw,3); pos += n
                fb[dy:dy+th,dx:dx+tw,0]=t[:,:,ri]; fb[dy:dy+th,dx:dx+tw,1]=t[:,:,gi]; fb[dy:dy+th,dx:dx+tw,2]=t[:,:,bi]
            elif st == 1:  # Solid
                cp=tiles[pos:pos+3]; pos+=3
                fb[dy:dy+th,dx:dx+tw,0]=cp[ri]; fb[dy:dy+th,dx:dx+tw,1]=cp[gi]; fb[dy:dy+th,dx:dx+tw,2]=cp[bi]
            elif 2<=st<=16:  # Packed palette
                pn=st; pal=np.frombuffer(tiles[pos:pos+pn*3],dtype=np.uint8).reshape(pn,3); pos+=pn*3
                bpi=1 if pn<=2 else 2 if pn<=4 else 4
                nb=(tw*th*bpi+7)//8; idat=tiles[pos:pos+nb]; pos+=nb
                mask=(1<<bpi)-1; idx=[]
                for byte in idat:
                    for sh in range(8-bpi,-1,-bpi): idx.append((byte>>sh)&mask)
                idx=np.array(idx[:tw*th],dtype=np.uint8).reshape(th,tw)
                fb[dy:dy+th,dx:dx+tw,0]=pal[idx,ri]; fb[dy:dy+th,dx:dx+tw,1]=pal[idx,gi]; fb[dy:dy+th,dx:dx+tw,2]=pal[idx,bi]
            elif st==128:  # Plain RLE
                row=np.zeros((th,tw,3),dtype=np.uint8); fi=0
                while fi<tw*th:
                    cp=tiles[pos:pos+3]; pos+=3; r,g,b=cp[ri],cp[gi],cp[bi]
                    run=1
                    while tiles[pos]==255: run+=255; pos+=1
                    run+=tiles[pos]; pos+=1
                    for _ in range(run):
                        if fi>=tw*th: break
                        row[fi//tw,fi%tw]=[r,g,b]; fi+=1
                fb[dy:dy+th,dx:dx+tw]=row
            elif st>=130:  # Palette RLE
                pn=st-128; pal=np.frombuffer(tiles[pos:pos+pn*3],dtype=np.uint8).reshape(pn,3); pos+=pn*3
                row=np.zeros((th,tw,3),dtype=np.uint8); fi=0
                while fi<tw*th:
                    ib=tiles[pos]; pos+=1; idx=ib&0x7F
                    if ib&0x80:
                        run=1
                        while tiles[pos]==255: run+=255; pos+=1
                        run+=tiles[pos]+1; pos+=1
                    else: run=1
                    r,g,b=pal[idx,ri],pal[idx,gi],pal[idx,bi]
                    for _ in range(run):
                        if fi>=tw*th: break
                        row[fi//tw,fi%tw]=[r,g,b]; fi+=1
                fb[dy:dy+th,dx:dx+tw]=row
            cx+=tw
        cy+=th

# ---------------------------------------------------------------------------
# VNCBridge — single shared VNC connection
# ---------------------------------------------------------------------------
class VNCBridge:
    def __init__(self, cfg):
        self._cfg = cfg
        self._lock = threading.Lock()
        self._sock = None
        self._fb = None        # numpy RGB framebuffer
        self._fb_seq = 0       # incremented on every update
        self._fb_ms = 0        # Unix ms of last captured frame
        self._W = self._H = 0
        self._rs = self._gs = self._bs = 0
        self._input_q = []
        self._clip_q = []
        self.server_clipboard = None
        self.server_clipboard_seq = 0

    @property
    def dimensions(self):
        with self._lock:
            return self._W, self._H

    def get_frame_if_newer(self, known_seq):
        """Returns (copy, seq, capture_ms) if newer, else (None, known_seq, 0)."""
        with self._lock:
            if self._fb is None or self._fb_seq == known_seq:
                return None, known_seq, 0
            return self._fb.copy(), self._fb_seq, self._fb_ms

    def send_pointer(self, buttons, x, y):
        with self._lock:
            self._input_q.append(struct.pack("!BBHH", 5, buttons, x, y))

    def send_key(self, down, keysym):
        with self._lock:
            self._input_q.append(struct.pack("!BBxxI", 4, int(down), keysym))

    def send_clipboard(self, text):
        with self._lock:
            self._clip_q.append(text)

    def _flush_input(self):
        with self._lock:
            msgs = self._input_q[:]
            self._input_q.clear()
            clips = self._clip_q[:]
            self._clip_q.clear()
        if not self._sock:
            return
        data = b"".join(msgs)
        for text in clips:
            enc = text.encode("latin-1", errors="replace")
            data += struct.pack("!BBxxI", 6, 0, len(enc)) + enc
        if data:
            try: self._sock.sendall(data)
            except Exception: pass

    def _connect(self):
        cfg = self._cfg
        s = socket.socket()
        s.connect((cfg.vnc_host, cfg.vnc_port))
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        _recv(s, 12); s.send(b"RFB 003.008\n")
        n = _recv(s, 1)[0]; types = list(_recv(s, n))
        if cfg.macos_user and cfg.macos_pass and 30 in types:
            s.send(bytes([30]))
            if not _vnc_apple_dh(s, cfg.macos_user, cfg.macos_pass):
                s.close(); raise ConnectionError("Apple DH auth failed")
            log.info("Auth: Apple DH type-30 (full control)")
        elif 2 in types and cfg.vnc_pass:
            s.send(bytes([2]))
            s.send(_vnc_des(cfg.vnc_pass, _recv(s, 16)))
            if struct.unpack("!I", _recv(s, 4))[0] != 0:
                s.close(); raise ConnectionError("VNC password auth failed")
            log.info("Auth: VNC password type-2 (view-only on macOS 15+)")
        elif 1 in types:
            s.send(bytes([1])); log.info("Auth: none")
        else:
            s.close(); raise ConnectionError("No supported auth type, server offers: %s" % types)
        s.send(b"\x01")
        return s

    def _run(self):
        while True:
            try:
                s = self._connect()
                si = _recv(s, 24)
                W, H = struct.unpack("!HH", si[:4])
                bpp = si[4]
                rs, gs, bs = struct.unpack("!BBB", si[14:17])
                nl = struct.unpack("!I", si[20:24])[0]
                _recv(s, nl)
                log.info("VNC: %dx%d bpp=%d shifts=%d/%d/%d", W, H, bpp, rs, gs, bs)
                with self._lock:
                    self._W, self._H = W, H
                    self._rs, self._gs, self._bs = rs, gs, bs
                    self._fb = np.zeros((H, W, 3), dtype=np.uint8)
                    self._zd = zlib.decompressobj()
                self._sock = s
                s.send(struct.pack("!BBHi", 2, 0, 1, 16))   # SetEncodings: ZRLE
                s.send(struct.pack("!BBHHHH", 3, 0, 0, 0, W, H))  # FramebufferUpdateRequest
                while True:
                    self._flush_input()
                    r, _, _ = select.select([s], [], [], 0.05)
                    if not r:
                        continue
                    mt = _recv(s, 1)[0]
                    if mt == 0:  # FramebufferUpdate
                        _recv(s, 1)
                        nr = struct.unpack("!H", _recv(s, 2))[0]
                        with self._lock:
                            fb = self._fb; zd = self._zd
                            _rs, _gs, _bs = self._rs, self._gs, self._bs
                        for _ in range(nr):
                            rx, ry, rw, rh, enc = struct.unpack("!HHHHi", _recv(s, 12))
                            if enc == 16:  # ZRLE
                                dlen = struct.unpack("!I", _recv(s, 4))[0]
                                zdata = _recv(s, dlen)
                                try: _decode_zrle(fb, zd, rx, ry, rw, rh, zdata, _rs, _gs, _bs)
                                except Exception as e: log.debug("ZRLE err: %s", e)
                            elif enc == 0:  # Raw
                                raw = _recv(s, rw*rh*(bpp//8))
                                arr = np.frombuffer(raw, dtype=np.uint8).reshape(rh, rw, bpp//8)
                                with self._lock:
                                    if self._fb is not None:
                                        self._fb[ry:ry+rh,rx:rx+rw,0]=arr[:,:,rs//8]
                                        self._fb[ry:ry+rh,rx:rx+rw,1]=arr[:,:,gs//8]
                                        self._fb[ry:ry+rh,rx:rx+rw,2]=arr[:,:,bs//8]
                        now_ms = int(time.time() * 1000)
                        with self._lock:
                            self._fb_seq += 1
                            self._fb_ms = now_ms
                        s.send(struct.pack("!BBHHHH", 3, 1, 0, 0, W, H))
                    elif mt == 2:
                        pass  # Bell
                    elif mt == 3:  # ServerCutText
                        _recv(s, 3)
                        n = struct.unpack("!I", _recv(s, 4))[0]
                        if n:
                            txt = _recv(s, n).decode("latin-1", errors="replace")
                            self.server_clipboard = txt
                            self.server_clipboard_seq += 1
                    else:
                        log.warning("Unknown VNC msg %d", mt); break
            except Exception as e:
                log.warning("VNC error: %s — retry in 3s", e)
                self._sock = None
            time.sleep(3)

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

# ---------------------------------------------------------------------------
# EncoderPipeline — per-client H.264/H.265 with JPEG fallback
# ---------------------------------------------------------------------------
class EncoderPipeline:
    def __init__(self, target_codec, width, height, bitrate):
        self.target_codec = target_codec
        self.actual_codec = CODEC_JPEG
        self._cc = None
        self._last_pts = -1
        self._setup(width, height, bitrate)

    def _setup(self, width, height, bitrate):
        import fractions
        if not _AV_OK or self.target_codec == CODEC_JPEG:
            return
        candidates = {
            CODEC_H264: [
                ("h264_videotoolbox", {"realtime": "1", "allow_sw": "1"}),
                ("libx264", {"preset": "ultrafast", "tune": "zerolatency",
                             "x264-params": "bframes=0:rc-lookahead=0"}),
            ],
            CODEC_H265: [
                ("hevc_videotoolbox", {"realtime": "1", "allow_sw": "1"}),
                ("libx265", {"preset": "ultrafast", "tune": "zerolatency",
                             "x265-params": "bframes=0:rc-lookahead=0"}),
            ],
        }
        for name, opts in candidates.get(self.target_codec, []):
            try:
                cc = _av.CodecContext.create(name, "w")
                cc.width = width & ~1
                cc.height = height & ~1
                cc.pix_fmt = "yuv420p"
                cc.bit_rate = bitrate
                cc.time_base = fractions.Fraction(1, 1000)
                cc.options = opts
                cc.open()
                # Warm up hardware encoder — first frame is buffered, discard it
                dummy = _av.VideoFrame(cc.width, cc.height, "yuv420p")
                dummy.pts = 0
                list(cc.encode(dummy))
                self._last_pts = 0
                self._cc = cc
                self.actual_codec = self.target_codec
                log.info("Encoder: %s %dx%d @%dkbps", name, cc.width, cc.height, bitrate//1000)
                return
            except Exception as e:
                log.warning("Codec %s failed: %s", name, e)
        log.warning("No video codec available — JPEG fallback")

    def set_bitrate(self, bitrate):
        if self._cc is not None:
            try: self._cc.bit_rate = bitrate
            except Exception: pass

    def encode(self, rgb, capture_ms, jpeg_quality=65):
        """Returns (payload, is_keyframe, codec_byte) or (None, False, _) on skip."""
        if self._cc is None:
            return _encode_jpeg(rgb, jpeg_quality), True, CODEC_JPEG
        try:
            frame = _av.VideoFrame.from_ndarray(rgb, format="rgb24")
            frame = frame.reformat(format="yuv420p")
            pts = max(self._last_pts + 1, capture_ms)
            frame.pts = pts
            self._last_pts = pts
            pkts = list(self._cc.encode(frame))
            if not pkts:
                return None, False, self.actual_codec
            pkt = pkts[0]
            is_kf = bool(getattr(pkt, "is_keyframe", True))
            return bytes(pkt), is_kf, self.actual_codec
        except Exception as e:
            log.warning("Encode error: %s — JPEG fallback", e)
            self._cc = None
            self.actual_codec = CODEC_JPEG
            return _encode_jpeg(rgb, jpeg_quality), True, CODEC_JPEG

    def close(self):
        if self._cc:
            try: self._cc.close()
            except Exception: pass
            self._cc = None

# ---------------------------------------------------------------------------
# AdaptiveController — per-client fps + bitrate management
# ---------------------------------------------------------------------------
class AdaptiveController:
    _RESPONSIVE_FPS = 20.0   # minimum fps for 50ms input reaction

    def __init__(self, cfg):
        self.fps = float(cfg.fps)
        self.max_fps = float(cfg.max_fps)
        self.bitrate = 5_000_000
        self.jpeg_quality = 65
        self.client_w = 1920
        self.client_h = 1080
        self._min_br = 200_000
        self._max_br = 80_000_000
        self._last_slow = 0.0
        self._last_fast = 0.0
        self._lock = threading.Lock()

    @property
    def frame_interval(self):
        return 1.0 / max(1.0, self.fps)

    def _ceiling(self):
        # ~0.07 bits/pixel/frame is visually sufficient for H.264
        return min(self._max_br, int(self.client_w * self.client_h * 0.07 * self.fps))

    def on_resolution(self, w, h):
        with self._lock:
            self.client_w = max(1, w)
            self.client_h = max(1, h)

    def on_lag(self, age_ms, write_buf=0):
        with self._lock:
            now = time.monotonic()
            if now - self._last_slow < 0.3:
                return
            self._last_slow = now
            self._last_fast = 0.0  # reset fresh streak
            severe = age_ms > 500 or write_buf > 65536
            factor = 0.5 if severe else 0.75
            if self.fps > self._RESPONSIVE_FPS:
                new_fps = max(self._RESPONSIVE_FPS, self.fps * factor)
                # scale bitrate proportionally so per-frame budget stays stable
                self.bitrate = max(self._min_br, int(self.bitrate * new_fps / self.fps))
                self.fps = new_fps
            else:
                self.bitrate = max(self._min_br, int(self.bitrate * factor))
            self.jpeg_quality = max(10, int(self.jpeg_quality * factor))
            log.debug("slow: fps=%.1f br=%dk age=%dms", self.fps, self.bitrate//1000, int(age_ms))

    def on_fresh(self):
        with self._lock:
            now = time.monotonic()
            if now - self._last_fast < 2.0:
                return
            self._last_fast = now
            ceiling = self._ceiling()
            if self.bitrate < ceiling * 0.9:
                self.bitrate = min(ceiling, int(self.bitrate * 1.15))
                self.jpeg_quality = min(85, self.jpeg_quality + 2)
            elif self.fps < self.max_fps:
                self.fps = min(self.max_fps, self.fps + 2.0)
            log.debug("fast: fps=%.1f br=%dk", self.fps, self.bitrate//1000)

    def snapshot(self):
        with self._lock:
            return self.fps, self.bitrate, self.jpeg_quality

# ---------------------------------------------------------------------------
# WebSocket session (per client)
# ---------------------------------------------------------------------------
def _get_wbuf(ws):
    for attr in ("transport", ):
        try:
            t = getattr(ws, attr, None)
            if t: return t.get_write_buffer_size()
        except Exception: pass
    try:
        return ws.connection.transport.get_write_buffer_size()
    except Exception:
        return 0

async def client_session(ws, cfg, bridge):
    log.info("client connect: %s", ws.remote_address)

    # Wait for VNC to be ready
    for _ in range(30):
        W, H = bridge.dimensions
        if W and H: break
        await asyncio.sleep(0.2)
    else:
        log.warning("VNC not ready"); return

    ctrl = AdaptiveController(cfg)
    # Start with JPEG until client reports WebCodecs capability
    target_codec = CODEC_H264 if cfg.codec == "h264" else CODEC_H265
    encoder = EncoderPipeline(CODEC_JPEG, W, H, ctrl.bitrate)  # JPEG until caps received
    has_webcodecs = False

    seq_num = 0
    last_fb_seq = 0
    last_send_time = 0.0
    lag_history = []        # recent age_ms values from client
    no_lag_since = time.monotonic()

    def _upgrade_encoder():
        nonlocal encoder, has_webcodecs
        if not has_webcodecs:
            return
        if encoder.actual_codec != CODEC_JPEG:
            return  # already upgraded
        encoder.close()
        W2, H2 = bridge.dimensions
        encoder = EncoderPipeline(target_codec, W2, H2, ctrl.bitrate)
        if encoder.actual_codec != CODEC_JPEG:
            log.info("Upgraded to %s for %s", {CODEC_H264:"h264",CODEC_H265:"h265"}.get(encoder.actual_codec,"?"), ws.remote_address)
        else:
            log.warning("Video codec unavailable for %s — staying on JPEG", ws.remote_address)

    loop = asyncio.get_event_loop()

    async def input_reader():
        nonlocal has_webcodecs, no_lag_since
        cur_buttons = 0
        try:
            async for raw in ws:
                if not isinstance(raw, str):
                    continue
                try:
                    ev = json.loads(raw)
                    t = ev.get("t")
                    if t == "caps":
                        has_webcodecs = bool(ev.get("webcodecs", False))
                        w, h = int(ev.get("w", 1920)), int(ev.get("h", 1080))
                        ctrl.on_resolution(w, h)
                        _upgrade_encoder()
                    elif t == "resolution":
                        ctrl.on_resolution(int(ev.get("w",1920)), int(ev.get("h",1080)))
                    elif t == "lag":
                        age = float(ev.get("age_ms", 0))
                        wb = _get_wbuf(ws)
                        ctrl.on_lag(age, wb)
                        no_lag_since = 0.0
                    elif t == "fresh":
                        if no_lag_since == 0.0:
                            no_lag_since = time.monotonic()
                    elif t == "mm":
                        bridge.send_pointer(cur_buttons, int(ev["x"]), int(ev["y"]))
                    elif t == "md":
                        b = ev.get("b", 0)
                        cur_buttons |= (1<<b)
                        bridge.send_pointer(cur_buttons, int(ev["x"]), int(ev["y"]))
                    elif t == "mu":
                        b = ev.get("b", 0)
                        cur_buttons &= ~(1<<b)
                        bridge.send_pointer(cur_buttons, int(ev.get("x",0)), int(ev.get("y",0)))
                    elif t == "sc":
                        x, y = int(ev.get("x",0)), int(ev.get("y",0))
                        dx, dy = float(ev.get("dx",0)), float(ev.get("dy",0))
                        btn = (8 if dy<0 else 16) if abs(dy)>=abs(dx) else (32 if dx<0 else 64)
                        bridge.send_pointer(btn, x, y)
                        await asyncio.sleep(0.05)
                        bridge.send_pointer(0, x, y)
                    elif t in ("kd","ku"):
                        k = ev.get("k",""); code = ev.get("code","")
                        ks = KEYSYM.get(code) or KEYSYM.get(k) or (ord(k) if len(k)==1 else None)
                        if ks: bridge.send_key(t=="kd", ks)
                    elif t == "paste":
                        text = ev.get("text","")
                        if text:
                            bridge.send_clipboard(text)
                            # CMD+V (Meta+V) triggers paste in macOS apps
                            bridge.send_key(True,  KEYSYM["MetaLeft"])
                            bridge.send_key(True,  0x76)
                            bridge.send_key(False, 0x76)
                            bridge.send_key(False, KEYSYM["MetaLeft"])
                except Exception:
                    pass
        except Exception:
            pass

    async def frame_sender():
        nonlocal seq_num, last_fb_seq, last_send_time, no_lag_since
        known_clip = bridge.server_clipboard_seq
        last_encoder_codec = encoder.actual_codec
        try:
            while True:
                now = time.monotonic()
                fps, bitrate, jq = ctrl.snapshot()
                interval = 1.0 / max(1.0, fps)

                # Detect encoder codec switch (JPEG→H.264) — force re-encode current frame
                current_codec = encoder.actual_codec
                if current_codec != last_encoder_codec:
                    last_encoder_codec = current_codec
                    last_fb_seq = 0  # reset so we re-encode even on static screen

                # Write buffer check — immediate local backpressure
                wb = _get_wbuf(ws)
                if wb > 0:
                    ctrl.on_lag(0, wb)
                    await asyncio.sleep(0.01)
                    continue

                # Fresh streak → try to speed up
                if no_lag_since > 0 and (now - no_lag_since) > 2.0:
                    ctrl.on_fresh()

                # Rate limit
                elapsed = now - last_send_time
                if elapsed < interval:
                    await asyncio.sleep(interval - elapsed)
                    continue

                # Get framebuffer (only encode if changed)
                fb, fb_seq, cap_ms = bridge.get_frame_if_newer(last_fb_seq)
                if fb is None:
                    await asyncio.sleep(interval * 0.5)
                    continue
                last_fb_seq = fb_seq

                # Update encoder bitrate
                encoder.set_bitrate(bitrate)

                # Encode in thread (CPU-bound)
                try:
                    payload, is_kf, codec_byte = await loop.run_in_executor(
                        None, encoder.encode, fb, cap_ms, jq)
                except Exception as e:
                    log.debug("encode err: %s", e); continue

                if payload is None:
                    continue

                # Check again after encoding (encoding takes time)
                if _get_wbuf(ws) > 0:
                    ctrl.on_lag(0, _get_wbuf(ws))
                    continue  # drop this frame

                last_send_time = time.monotonic()
                seq_num += 1
                hdr = _hdr(seq_num, cap_ms, codec_byte, is_kf, len(payload))
                try:
                    await ws.send(hdr + payload)
                except Exception as e:
                    log.debug("send err: %s", e); break

                # Push Mac clipboard to client
                sc = bridge.server_clipboard_seq
                if sc != known_clip and bridge.server_clipboard:
                    known_clip = sc
                    try:
                        await ws.send(json.dumps({"t":"clipboard","text":bridge.server_clipboard}))
                    except Exception: pass

        except Exception as e:
            log.debug("sender exit: %s", e)
        finally:
            encoder.close()

    await asyncio.gather(frame_sender(), input_reader())
    log.info("client disconnect: %s", ws.remote_address)

# ---------------------------------------------------------------------------
# HTML/JS client
# ---------------------------------------------------------------------------
HTML = r"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>mac-vnc-stream</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:100%;height:100%;background:#111;overflow:hidden}
canvas{display:block;position:absolute;image-rendering:pixelated}
#hud{position:fixed;top:6px;right:10px;color:#0f0;font:11px monospace;
  background:rgba(0,0,0,.7);padding:2px 8px;border-radius:3px;pointer-events:none;z-index:9}
#cur{position:fixed;width:12px;height:12px;border:2px solid #fff;border-radius:50%;
  pointer-events:none;transform:translate(-50%,-50%);box-shadow:0 0 4px #000;z-index:9}
#cur.dn{border-color:#ff0}
#st{position:fixed;bottom:8px;left:50%;transform:translateX(-50%);color:#aaa;
  font:11px monospace;background:rgba(0,0,0,.7);padding:2px 10px;border-radius:3px;
  pointer-events:none;z-index:9}
/* Hidden textarea captures keyboard + paste without visible UI */
#ki{position:fixed;top:0;left:0;width:1px;height:1px;opacity:0;
  pointer-events:none;z-index:-1;resize:none;border:0;padding:0}
</style></head><body>
<canvas id="c"></canvas>
<div id="hud">connecting…</div>
<div id="cur"></div>
<div id="st"></div>
<textarea id="ki" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false"></textarea>
<script>
const canvas=document.getElementById('c'),ctx=canvas.getContext('2d',{alpha:false,desynchronized:true});
const hud=document.getElementById('hud'),cur=document.getElementById('cur');
const st=document.getElementById('st'),ki=document.getElementById('ki');

let imgW=1920,imgH=1080,scaleX=1,scaleY=1,ox=0,oy=0;
let ws,wsOpen=false,mBtn=0,fc=0,lastFpsT=performance.now();

// ---------------------------------------------------------------------------
// Layout
// ---------------------------------------------------------------------------
function resize(){
  const vw=window.innerWidth,vh=window.innerHeight;
  const ar=imgW/imgH,vr=vw/vh;
  let cw,ch;
  if(ar>vr){cw=vw;ch=vw/ar}else{ch=vh;cw=vh*ar}
  ox=(vw-cw)/2;oy=(vh-ch)/2;
  canvas.style.cssText='left:'+ox+'px;top:'+oy+'px;width:'+cw+'px;height:'+ch+'px;position:absolute;';
  scaleX=imgW/cw;scaleY=imgH/ch;
}
window.addEventListener('resize',()=>{resize();sendRes();});

function setDim(w,h){
  if(w===imgW&&h===imgH)return;
  imgW=w;imgH=h;canvas.width=w;canvas.height=h;resize();
}

// ---------------------------------------------------------------------------
// WebCodecs decoder
// ---------------------------------------------------------------------------
let useVideo=typeof VideoDecoder!=='undefined';
let decoder=null,decoderCodec=-1;

function initDecoder(codec){
  if(!useVideo)return;
  if(decoder&&decoderCodec===codec)return;
  if(decoder){try{decoder.close()}catch(e){}}
  const cs=codec===1?'avc1.640028':'hev1.1.6.L93.B0';
  try{
    decoder=new VideoDecoder({
      output:frame=>{
        setDim(frame.codedWidth,frame.codedHeight);
        ctx.drawImage(frame,0,0);frame.close();
        fc++;updateFps();
      },
      error:e=>{console.warn('VideoDecoder:',e);useVideo=false;decoder=null;}
    });
    decoder.configure({codec:cs,optimizeForLatency:true,hardwareAcceleration:'prefer-hardware'});
    decoderCodec=codec;
  }catch(e){console.warn('VideoDecoder configure:',e);useVideo=false;decoder=null;}
}

// ---------------------------------------------------------------------------
// Frame receive + lag reporting
// ---------------------------------------------------------------------------
let lagSamples=[],lagTimer=null;

function startLagReporter(){
  if(lagTimer)return;
  lagTimer=setInterval(()=>{
    if(!wsOpen)return;
    if(lagSamples.length===0){send({t:'fresh'});return;}
    const avg=lagSamples.reduce((a,b)=>a+b,0)/lagSamples.length;
    send({t:'lag',age_ms:Math.round(avg)});
    lagSamples=[];
  },500);
}

function handleBinary(buf){
  if(buf.byteLength<18)return;
  const v=new DataView(buf);
  const seq=v.getUint32(0);
  // capture_ms as two uint32 (avoids BigInt on older browsers)
  const cmsHi=v.getUint32(4),cmsLo=v.getUint32(8);
  const capMs=cmsHi*4294967296+cmsLo;
  const codec=v.getUint8(12),flags=v.getUint8(13);
  const payload=buf.slice(18);
  const age=Date.now()-capMs;
  if(age>0)lagSamples.push(age);

  if(codec===0||!useVideo){
    // JPEG path
    createImageBitmap(new Blob([payload],{type:'image/jpeg'})).then(bmp=>{
      setDim(bmp.width,bmp.height);
      ctx.drawImage(bmp,0,0);bmp.close();
      fc++;updateFps();
    }).catch(()=>{});
  }else{
    // H.264 / H.265 via WebCodecs
    initDecoder(codec);
    if(decoder&&decoder.state==='configured'){
      try{
        decoder.decode(new EncodedVideoChunk({
          type:(flags&1)?'key':'delta',
          timestamp:capMs*1000,
          data:new Uint8Array(payload)
        }));
      }catch(e){console.warn('decode:',e);}
    }
  }
}

function updateFps(){
  const now=performance.now();
  if(now-lastFpsT>=1000){
    hud.textContent=fc+' fps  '+imgW+'×'+imgH;
    fc=0;lastFpsT=now;
  }
}

// ---------------------------------------------------------------------------
// WebSocket
// ---------------------------------------------------------------------------
function send(obj){if(ws&&wsOpen)ws.send(JSON.stringify(obj));}
function sendRes(){send({t:'resolution',w:window.innerWidth,h:window.innerHeight});}

function connect(){
  const token=new URLSearchParams(location.search).get('token')||'';
  const url='ws://'+location.host+'/stream'+(token?'?token='+encodeURIComponent(token):'');
  ws=new WebSocket(url);
  ws.binaryType='arraybuffer';
  ws.onopen=()=>{
    wsOpen=true;
    st.textContent='connected';
    // Report capabilities and resolution
    send({t:'caps',webcodecs:typeof VideoDecoder!=='undefined',
          w:window.innerWidth,h:window.innerHeight});
    startLagReporter();
    ki.focus();
  };
  ws.onclose=()=>{
    wsOpen=false;hud.textContent='disconnected';
    st.textContent='reconnecting…';
    setTimeout(connect,2000);
  };
  ws.onerror=()=>{};
  ws.onmessage=e=>{
    if(e.data instanceof ArrayBuffer){
      handleBinary(e.data);
    }else{
      try{
        const msg=JSON.parse(e.data);
        if(msg.t==='clipboard'){
          // Mac clipboard → browser clipboard
          if(navigator.clipboard&&navigator.clipboard.writeText)
            navigator.clipboard.writeText(msg.text).catch(()=>{});
          st.textContent='📋 '+msg.text.substring(0,50);
          setTimeout(()=>{st.textContent='';},3000);
        }
      }catch(e){}
    }
  };
}

// ---------------------------------------------------------------------------
// Mouse
// ---------------------------------------------------------------------------
function toVNC(cx,cy){return[Math.round((cx-ox)*scaleX),Math.round((cy-oy)*scaleY)];}
function inBounds(vx,vy){return vx>=0&&vy>=0&&vx<imgW&&vy<imgH;}

document.body.addEventListener('mousemove',e=>{
  cur.style.left=e.clientX+'px';cur.style.top=e.clientY+'px';
  const[vx,vy]=toVNC(e.clientX,e.clientY);
  if(inBounds(vx,vy))send({t:'mm',x:vx,y:vy,b:mBtn});
});
canvas.addEventListener('mousedown',e=>{
  mBtn|=(1<<e.button);cur.classList.add('dn');
  const[vx,vy]=toVNC(e.clientX,e.clientY);
  send({t:'md',b:e.button,x:vx,y:vy});
  e.preventDefault();ki.focus();
});
canvas.addEventListener('mouseup',e=>{
  mBtn&=~(1<<e.button);cur.classList.remove('dn');
  const[vx,vy]=toVNC(e.clientX,e.clientY);
  send({t:'mu',b:e.button,x:vx,y:vy});
  e.preventDefault();
});
canvas.addEventListener('contextmenu',e=>e.preventDefault());
canvas.addEventListener('wheel',e=>{
  const[vx,vy]=toVNC(e.clientX,e.clientY);
  send({t:'sc',x:vx,y:vy,dx:e.deltaX,dy:e.deltaY});
  e.preventDefault();
},{passive:false});

// Touch
let lTouch=null;
canvas.addEventListener('touchstart',e=>{
  lTouch=e.touches[0];ki.focus();
  const[vx,vy]=toVNC(lTouch.clientX,lTouch.clientY);
  send({t:'md',b:0,x:vx,y:vy});e.preventDefault();
},{passive:false});
canvas.addEventListener('touchmove',e=>{
  lTouch=e.touches[0];
  const[vx,vy]=toVNC(lTouch.clientX,lTouch.clientY);
  send({t:'mm',x:vx,y:vy,b:1});e.preventDefault();
},{passive:false});
canvas.addEventListener('touchend',e=>{
  if(lTouch){const[vx,vy]=toVNC(lTouch.clientX,lTouch.clientY);send({t:'mu',b:0,x:vx,y:vy});}
  e.preventDefault();
},{passive:false});

// ---------------------------------------------------------------------------
// Keyboard — captured on hidden textarea so CTRL+V fires a paste event
// ---------------------------------------------------------------------------
ki.addEventListener('keydown',e=>{
  // Let CTRL+V / CMD+V fall through to paste event — don't preventDefault
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='v')return;
  send({t:'kd',k:e.key,code:e.code});
  e.preventDefault();
});
ki.addEventListener('keyup',e=>{
  send({t:'ku',k:e.key,code:e.code});
  e.preventDefault();
});

// Paste event — works on all browsers when textarea is focused
ki.addEventListener('paste',e=>{
  const text=(e.clipboardData||window.clipboardData).getData('text/plain');
  if(text&&wsOpen){
    send({t:'paste',text});  // server sets Mac clipboard + sends CMD+V
  }
  e.preventDefault();
  ki.value='';
});

// Refocus hidden textarea on any canvas interaction
canvas.addEventListener('click',()=>ki.focus());

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
canvas.width=imgW;canvas.height=imgH;resize();connect();
</script></body></html>
"""
HTML_BYTES = HTML.encode("utf-8")

# ---------------------------------------------------------------------------
# HTTP + WebSocket entry point
# ---------------------------------------------------------------------------
def _check_token(path, password):
    if not password:
        return True
    if "?" in path:
        for part in path.split("?",1)[1].split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k == "token" and v == password:
                    return True
    return False

def make_http_handler(cfg, bridge):
    async def handler(connection, request):
        from websockets.http11 import Response
        from websockets.datastructures import Headers
        if not _check_token(request.path, cfg.password):
            return Response(403, "Forbidden",
                            Headers([("Content-Type","text/plain")]), b"Invalid token.\n")
        if request.headers.get("Upgrade","").lower() != "websocket":
            hdrs = Headers([
                ("Content-Type","text/html; charset=utf-8"),
                ("Content-Length", str(len(HTML_BYTES))),
                ("Cache-Control","no-cache"),
            ])
            return Response(200, "OK", hdrs, HTML_BYTES)
    return handler

def make_ws_handler(cfg, bridge):
    async def handler(ws):
        if not _check_token(ws.request.path if hasattr(ws,'request') else "/", cfg.password):
            await ws.close(1008, "Forbidden")
            return
        await client_session(ws, cfg, bridge)
    return handler

# ---------------------------------------------------------------------------
# Arg parsing + main
# ---------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(description="mac-vnc-stream server")
    p.add_argument("--vnc-host", default=os.environ.get("VNC_HOST","127.0.0.1"))
    p.add_argument("--vnc-port", type=int, default=int(os.environ.get("VNC_PORT","5900")))
    p.add_argument("--vnc-pass", default=os.environ.get("VNC_PASS",""))
    p.add_argument("--macos-user", default=os.environ.get("MACOS_USER",""))
    p.add_argument("--macos-pass", default=os.environ.get("MACOS_PASS",""))
    p.add_argument("--listen", default=os.environ.get("LISTEN","127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT","6081")))
    p.add_argument("--fps", type=int, default=int(os.environ.get("FPS","20")),
                   help="Initial/max responsive fps (default 20)")
    p.add_argument("--max-fps", type=int, default=int(os.environ.get("MAX_FPS","60")),
                   help="Upper fps limit when bandwidth allows (default 60)")
    p.add_argument("--codec", choices=["h264","h265","jpeg"],
                   default=os.environ.get("CODEC","h264"),
                   help="Video codec (default h264)")
    p.add_argument("--password", default=os.environ.get("MVS_PASSWORD",""),
                   help="Optional access token for WebSocket URL (?token=...)")
    return p.parse_args()

async def _main(cfg):
    from websockets import serve
    bridge = VNCBridge(cfg)
    bridge.start()

    http_handler = make_http_handler(cfg, bridge)
    ws_handler = make_ws_handler(cfg, bridge)

    handler = lambda ws: ws_handler(ws)
    log.info("Listening %s:%d  codec=%s  max_fps=%d",
             cfg.listen, cfg.port, cfg.codec, cfg.max_fps)
    async with serve(handler, cfg.listen, cfg.port,
                     process_request=http_handler,
                     max_size=None, compression=None):
        await asyncio.Future()

def main():
    cfg = parse_args()
    if not cfg.vnc_pass and not (cfg.macos_user and cfg.macos_pass):
        print("Error: provide --vnc-pass or --macos-user + --macos-pass")
        raise SystemExit(1)
    target = CODEC_H264 if cfg.codec=="h264" else CODEC_H265 if cfg.codec=="h265" else CODEC_JPEG
    log.info("Target codec: %s (PyAV: %s)", cfg.codec, "yes" if _AV_OK else "NO — pip install av")
    asyncio.run(_main(cfg))

if __name__=="__main__":
    main()
