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

# Per-session queues for server→browser JS eval (debug channel)
_dbg_eval_sessions: set = set()

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
CODEC_JPEG, CODEC_H264, CODEC_H265, CODEC_AV1 = 0, 1, 2, 3

# Ordered best→fallback. Server tries these in order; first one that both sides
# support AND the server can hardware-encode is used.
_CODEC_PREFERENCE = [CODEC_AV1, CODEC_H265, CODEC_H264]
_CLIENT_CODEC_MAP = {
    "av1": CODEC_AV1,
    "h265": CODEC_H265, "hevc": CODEC_H265,
    "h264": CODEC_H264, "avc": CODEC_H264,
}

def _select_codec(client_codecs):
    """Given the client's supported codec list (ordered best→worst), return the
    best CODEC_* constant we should target.  Caller still needs to verify that
    an encoder for that codec can actually be opened on this machine."""
    client_set = {_CLIENT_CODEC_MAP[c] for c in client_codecs if c in _CLIENT_CODEC_MAP}
    for c in _CODEC_PREFERENCE:
        if c in client_set:
            return c
    return CODEC_H264

def _hdr(seq, capture_ms, codec, keyframe, plen):
    return struct.pack(">IQBBI", seq, capture_ms, codec, 1 if keyframe else 0, plen)

# ---------------------------------------------------------------------------
# VNC helpers
# ---------------------------------------------------------------------------
KEYSYM = {
    # X11 names (used internally / legacy)
    "BackSpace":0xFF08,"Return":0xFF0D,
    # Browser e.key values (modern browsers)
    "Backspace":0xFF08,"Enter":0xFF0D,
    "Tab":0xFF09,"Escape":0xFF1B,
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
        self._fbu_count = 0   # VNC FramebufferUpdates with actual pixels received
        self._last_ptr_x = 0
        self._last_ptr_y = 0
        self._hover_after_ms = 0  # schedule a delayed nudge at this epoch-ms
        self._cached_fb = None    # copy made at last _fb_seq change
        self._cached_seq = -1     # seq corresponding to _cached_fb

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

    def get_current_frame(self):
        """Always returns the latest frame (continuous stream mode).

        Caches the copy so we only memcpy 6MB when VNC sends new pixels (~1fps on
        idle) rather than on every encoder call (60fps). Cuts steady-state CPU ~10%.
        """
        with self._lock:
            if self._fb is None:
                return None, 0
            if self._fb_seq != self._cached_seq:
                self._cached_fb = self._fb.copy()
                self._cached_seq = self._fb_seq
            return self._cached_fb, int(time.time() * 1000)

    def send_pointer(self, buttons, x, y):
        with self._lock:
            self._last_ptr_x = x
            self._last_ptr_y = y
            self._input_q.append(struct.pack("!BBHH", 5, buttons, x, y))

    def send_key(self, down, keysym):
        with self._lock:
            self._input_q.append(struct.pack("!BBxxI", 4, int(down), keysym))
            if not down:
                # screensharingd batches damage and only flushes on pointer events.
                # Immediate nudge to x+1 triggers first damage scan; the 30ms delayed
                # nudge returns to x (two distinct movement events, maximising coverage
                # of fast renders AND slower terminal async renders).
                lx, ly = self._last_ptr_x, self._last_ptr_y
                self._input_q.append(struct.pack("!BBHH", 5, 0, lx + 1, ly))
                self._hover_after_ms = int(time.time() * 1000) + 30

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
                # _FBU_INC: ask screensharingd for changed tiles only (efficient, used when active)
                # _FBU_FULL: force screensharingd to send the complete current screen,
                #   bypassing its damage-detection entirely — used when it goes quiet.
                #   H.264 P-frames handle the static-content case (≈0 bits for no change).
                _FBU_INC  = struct.pack("!BBHHHH", 3, 1, 0, 0, W, H)
                _FBU_FULL = struct.pack("!BBHHHH", 3, 0, 0, 0, W, H)
                s.send(_FBU_FULL)  # full initial request
                _pending_req = True
                _last_req_ms  = int(time.time() * 1000)
                _last_fbu_ms  = _last_req_ms  # last time ANY FBU was received
                while True:
                    self._flush_input()
                    r, _, _ = select.select([s], [], [], 0.010)  # 10ms: flush input every 10ms
                    if not r:
                        now_ms   = int(time.time() * 1000)
                        stale_ms = now_ms - _last_fbu_ms
                        if not _pending_req:
                            # Choose request type: if screensharingd has been quiet for >50ms,
                            # force a full refresh so we own the update cadence, not it.
                            req = _FBU_FULL if stale_ms > 50 else _FBU_INC
                            s.send(req)
                            _pending_req = True
                            _last_req_ms = now_ms
                        elif stale_ms > 50 and now_ms - _last_req_ms >= 50:
                            # Pending incremental request has been sitting unanswered for 50ms+
                            # and the framebuffer is stale — screensharingd's damage detection
                            # missed the update. Override with a forced full refresh.
                            s.send(_FBU_FULL)
                            _last_req_ms = now_ms
                        # Delayed nudge from key event: return cursor to (x, y) 30ms after
                        # key-up, giving the terminal time to render before we re-scan.
                        if self._hover_after_ms > 0 and now_ms >= self._hover_after_ms:
                            self._hover_after_ms = 0
                            lx, ly = self._last_ptr_x, self._last_ptr_y
                            try:
                                s.send(struct.pack("!BBHH", 5, 0, lx, ly))
                            except Exception:
                                pass
                        continue
                    mt = _recv(s, 1)[0]
                    if mt == 0:  # FramebufferUpdate
                        _pending_req = False
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
                        if nr > 0:
                            with self._lock:
                                self._fb_seq += 1
                                self._fb_ms = now_ms
                                self._fbu_count += 1
                        _last_fbu_ms = now_ms  # received FBU; reset staleness clock
                        # Immediately re-request for active content (video, animations).
                        # For a stale screen this is still an incremental=1; the escalation
                        # to incremental=0 happens in the timeout branch if screensharingd
                        # doesn't respond within 50ms.
                        s.send(_FBU_INC)
                        _pending_req = True
                        _last_req_ms = now_ms
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
        # VideoToolbox VBR options: constant_bit_rate=0 lets the encoder use more
        # bits for complex scenes and fewer for static content — better quality
        # consistency than CBR at the same average bitrate.
        _vt_opts = {"realtime": "1", "allow_sw": "1", "constant_bit_rate": "0"}
        candidates = {
            CODEC_H264: [
                ("h264_videotoolbox", _vt_opts),
                ("libx264", {"preset": "fast", "tune": "zerolatency",
                             "x264-params": "bframes=0:rc-lookahead=0:aq-mode=1"}),
            ],
            CODEC_H265: [
                ("hevc_videotoolbox", _vt_opts),
                ("libx265", {"preset": "fast", "tune": "zerolatency",
                             "x265-params": "bframes=0:rc-lookahead=0:aq-mode=1"}),
            ],
            CODEC_AV1: [
                ("av1_videotoolbox", {"realtime": "1", "allow_sw": "0"}),
                ("libsvtav1", {"preset": "10",
                               "svtav1-params": "film-grain=0:irefresh-type=2"}),
                ("libaom-av1", {"cpu-used": "10", "usage": "realtime"}),
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
                # Large GOP: one I-frame per 5 seconds at max fps. Static screens
                # produce near-zero P-frames; a short GOP would flood with large I-frames.
                cc.gop_size = 300
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
                log.debug("Codec %s failed: %s", name, e)
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
        self.fps = float(cfg.max_fps)  # start at ceiling; adaptive controller reduces on congestion
        self.max_fps = float(cfg.max_fps)
        self.bitrate = 2_000_000  # 2Mbps start — static screens use near-zero bits; ramps up via on_fresh
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

    # Bytes in the asyncio write buffer that we treat as real congestion.
    # A single 30KB I-frame transiently in the transport buffer is NOT congestion.
    _WB_THRESH = 131072   # 128KB: ~4 large I-frames queued before we act

    def on_lag(self, age_ms, write_buf=0):
        # Ignore sub-100ms ages (normal encode+network pipeline latency) and
        # transient write-buffer spikes smaller than one GOP worth of data.
        if age_ms < 100 and write_buf < self._WB_THRESH:
            return
        with self._lock:
            now = time.monotonic()
            if now - self._last_slow < 0.3:
                return
            self._last_slow = now
            self._last_fast = 0.0  # reset fresh streak
            severe = age_ms > 500 or write_buf > 524288  # 512KB = truly severe
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
    # Start with JPEG until client reports WebCodecs capability and supported codecs.
    # target_codec is updated when the client sends its caps (codec negotiation).
    target_codec = CODEC_H264 if cfg.codec == "h264" else CODEC_H265
    encoder = EncoderPipeline(CODEC_JPEG, W, H, ctrl.bitrate)  # JPEG until caps received
    has_webcodecs = False

    seq_num = 0
    last_fb_seq = 0
    last_send_time = time.monotonic()
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
        nonlocal has_webcodecs, no_lag_since, target_codec
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
                        client_codecs = ev.get("codecs", [])
                        w, h = int(ev.get("w", 1920)), int(ev.get("h", 1080))
                        ctrl.on_resolution(w, h)
                        # Negotiate codec: pick best that client supports.
                        # If the client sent an explicit codec list, use that to override
                        # the server's configured default. If the client only said
                        # webcodecs=true without a list, keep the configured default.
                        if client_codecs and has_webcodecs:
                            target_codec = _select_codec(client_codecs)
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
                    elif t == "dbg_result":
                        log.info("DBG[%s]: %s", ev.get("id","?"), ev.get("result",""))
                except Exception:
                    pass
        except Exception:
            pass

    async def frame_sender():
        nonlocal seq_num, last_fb_seq, last_send_time, no_lag_since
        known_clip = bridge.server_clipboard_seq
        last_encoder_codec = encoder.actual_codec
        _t_diag = time.monotonic(); _n_diag = 0; _n_drop = 0; _n_nosend = 0
        _last_vnc_fbu = bridge._fbu_count
        # Pipelined encode: start encoding during the rate-limit sleep so that
        # encode time doesn't add to the frame interval.
        _pipe_task = None   # concurrent encode future
        _pipe_cap_ms = 0    # cap_ms captured when pipe_task was started
        try:
            while True:
                now = time.monotonic()
                if now - _t_diag >= 5.0:
                    dt = now - _t_diag; _t_diag = now
                    _vnc_n = bridge._fbu_count
                    _vnc_fps = (_vnc_n - _last_vnc_fbu) / dt; _last_vnc_fbu = _vnc_n
                    _fb_age = int(time.time() * 1000) - bridge._fb_ms
                    log.info("DIAG: sent=%d/%.1fs=%.1ffps drop_wb=%d nosend=%d ctrl_fps=%.1f vnc=%.1ffps fb_age=%dms",
                             _n_diag, dt, _n_diag/dt, _n_drop, _n_nosend, ctrl.fps, _vnc_fps, _fb_age)
                    _n_diag = _n_drop = _n_nosend = 0
                fps, bitrate, jq = ctrl.snapshot()
                interval = 1.0 / max(1.0, fps)

                # Detect encoder codec switch (JPEG→H.264) — drain in-flight encode first
                current_codec = encoder.actual_codec
                if current_codec != last_encoder_codec:
                    last_encoder_codec = current_codec
                    if _pipe_task is not None:
                        try: await _pipe_task
                        except Exception: pass
                        _pipe_task = None

                # Write buffer check — immediate local backpressure
                wb = _get_wbuf(ws)
                if wb > 0:
                    ctrl.on_lag(0, wb)
                    _n_drop += 1
                    if _pipe_task is not None:
                        try: await _pipe_task
                        except Exception: pass
                        _pipe_task = None
                    await asyncio.sleep(0.01)
                    continue

                # Fresh streak → try to speed up
                if no_lag_since > 0 and (now - no_lag_since) > 2.0:
                    ctrl.on_fresh()

                # Pipeline: start encode NOW so it runs concurrently with the rate-limit sleep.
                # Encode takes ~4.5ms; sleep is ~16.7ms — encode finishes well before we wake.
                if _pipe_task is None:
                    fb, cap_ms = bridge.get_current_frame()
                    if fb is not None:
                        encoder.set_bitrate(bitrate)
                        _pipe_cap_ms = cap_ms
                        _pipe_task = loop.run_in_executor(None, encoder.encode, fb, cap_ms, jq)

                # Rate limit using deadline: last_send_time advances by interval each frame
                # so encode + send time is absorbed and doesn't compound into the next sleep.
                target = last_send_time + interval
                to_sleep = target - time.monotonic()
                if to_sleep > 0.001:
                    await asyncio.sleep(to_sleep)
                    wb = _get_wbuf(ws)
                    if wb > 0:
                        ctrl.on_lag(0, wb)
                        _n_drop += 1
                        if _pipe_task is not None:
                            try: await _pipe_task
                            except Exception: pass
                            _pipe_task = None
                        continue

                # Collect encode result — encode ran during sleep, so this is near-instant
                if _pipe_task is None:
                    fb, cap_ms = bridge.get_current_frame()
                    if fb is None:
                        await asyncio.sleep(0.01)
                        continue
                    encoder.set_bitrate(bitrate)
                    try:
                        payload, is_kf, codec_byte = await loop.run_in_executor(
                            None, encoder.encode, fb, cap_ms, jq)
                    except Exception as e:
                        log.debug("encode err: %s", e); continue
                else:
                    cap_ms = _pipe_cap_ms
                    try:
                        payload, is_kf, codec_byte = await _pipe_task
                    except Exception as e:
                        log.debug("encode err: %s", e)
                        _pipe_task = None; continue
                    _pipe_task = None

                if payload is None:
                    _n_nosend += 1
                    last_send_time = target
                    continue

                if _get_wbuf(ws) > 0:
                    ctrl.on_lag(0, _get_wbuf(ws))
                    _n_drop += 1
                    continue

                last_send_time = target
                _n_diag += 1
                seq_num += 1
                # Use current wall-clock time for cap_ms in the header — the browser
                # uses this to measure transport age. The encoder's PTS (cap_ms passed
                # to encode()) can be older (encode-start time) without affecting the
                # lag reporter. This keeps age_ms ≈ SSH-tunnel RTT / 2 ≈ 18ms,
                # not encode_interval + SSH_latency, preventing false congestion signals.
                hdr = _hdr(seq_num, int(time.time() * 1000), codec_byte, is_kf, len(payload))
                try:
                    await ws.send(hdr + payload)
                except Exception as e:
                    log.debug("send err: %s", e); break

                sc = bridge.server_clipboard_seq
                if sc != known_clip and bridge.server_clipboard:
                    known_clip = sc
                    try:
                        await ws.send(json.dumps({"t":"clipboard","text":bridge.server_clipboard}))
                    except Exception: pass

        except Exception as e:
            log.debug("sender exit: %s", e)
        finally:
            if _pipe_task is not None:
                try: await _pipe_task
                except Exception: pass
            encoder.close()

    dbg_q: asyncio.Queue = asyncio.Queue()
    _dbg_eval_sessions.add(dbg_q)
    _dbg_seq = [0]

    async def dbg_sender():
        while True:
            js = await dbg_q.get()
            _dbg_seq[0] += 1
            try:
                await ws.send(json.dumps({"t": "eval", "js": js, "id": _dbg_seq[0]}))
            except Exception:
                break

    try:
        await asyncio.gather(frame_sender(), input_reader(), dbg_sender())
    finally:
        _dbg_eval_sessions.discard(dbg_q)
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

// Codec byte → WebCodecs codec string
const CODEC_STRINGS={
  1:'avc1.640028',       // H.264 High Profile Level 4.0
  2:'hev1.1.6.L93.B0',  // H.265 Main Profile
  3:'av01.0.08M.08',    // AV1 Main Profile Level 4.0
};

// Probe which codecs the browser can decode via WebCodecs (hardware preferred).
// Returns ordered list best→worst, e.g. ['h265','h264'].
async function probeSupportedCodecs(){
  if(!useVideo)return[];
  const probes=[
    {name:'h265',codec:'hev1.1.6.L93.B0'},
    {name:'h264',codec:'avc1.640028'},
    {name:'av1', codec:'av01.0.08M.08'},
  ];
  const out=[];
  for(const p of probes){
    try{
      const r=await VideoDecoder.isConfigSupported(
        {codec:p.codec,hardwareAcceleration:'prefer-hardware'});
      if(r.supported)out.push(p.name);
    }catch(e){}
  }
  return out.length>0?out:['h264'];
}

function initDecoder(codec){
  if(!useVideo)return;
  if(decoder&&decoderCodec===codec)return;
  if(decoder){try{decoder.close()}catch(e){}}
  const cs=CODEC_STRINGS[codec];
  if(!cs){console.warn('Unknown codec byte',codec);useVideo=false;return;}
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
    startLagReporter();
    ki.focus();
    // Probe codec support async; send caps once probing is done so server picks
    // the best codec this browser can actually decode (H.265 > H.264 > JPEG).
    const haswc=typeof VideoDecoder!=='undefined';
    if(haswc){
      probeSupportedCodecs().then(codecs=>{
        send({t:'caps',webcodecs:true,codecs,
              w:window.innerWidth,h:window.innerHeight});
      });
    }else{
      send({t:'caps',webcodecs:false,codecs:[],
            w:window.innerWidth,h:window.innerHeight});
    }
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
        }else if(msg.t==='eval'){
          let result='';
          try{result=String(eval(msg.js))}catch(e){result='ERR:'+String(e);}
          send({t:'dbg_result',id:msg.id,result});
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
        path = request.path

        # Debug eval endpoint: GET /dbg?js=<url-encoded-JS>  (localhost only)
        if path.startswith("/dbg"):
            from urllib.parse import parse_qs
            qs = parse_qs(path.split("?", 1)[1] if "?" in path else "")
            js = qs.get("js", [""])[0]  # parse_qs already URL-decodes values
            n = 0
            if js:
                for q in list(_dbg_eval_sessions):
                    q.put_nowait(js)
                    n += 1
            body = ("sent to %d session(s)\n" % n).encode()
            return Response(200, "OK", Headers([("Content-Type","text/plain")]), body)

        if not _check_token(path, cfg.password):
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
