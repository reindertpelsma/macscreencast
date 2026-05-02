#!/usr/bin/env python3
"""
mac-vnc-stream: Adaptive H.264/H.265/JPEG macOS remote desktop in your browser over SSH.

python server.py --vnc-pass PASSWORD
python server.py --macos-user u --macos-pass p  # full control (macOS 15+)
ssh -L 6081:localhost:6081 user@mac && open http://localhost:6081
"""
import argparse, asyncio, hashlib, json, logging, os, select, socket, struct, sys
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
# SCK (ScreenCaptureKit) capture subprocess — Python script sent via -c to sys.executable.
# Requires the enhanced Screen Recording permission on macOS 15+ (System Settings >
# Privacy & Security > Screen Recording — toggle Python on).
# On macOS 26+, CGWindowListCreateImage only captures the desktop wallpaper;
# SCK is the only API that delivers full screen including application windows.
# Frame wire format: BVNC(4) + W(4LE uint32) + H(4LE uint32) + ts_ms(8LE uint64) + W*H*4 BGRA bytes.
# ---------------------------------------------------------------------------
_SCSTREAM_CAPTURE_SRC = r"""
# SCK capture subprocess via PyObjC ScreenCaptureKit.
# Runs a CoreFoundation run loop on the main thread; SCK delivers frames there.
# CVPixelBuffer raw bytes are extracted via ctypes CoreVideo.
import sys, os, struct, time, threading, ctypes
from Foundation import NSObject, NSRunLoop, NSDate, NSDefaultRunLoopMode
import ScreenCaptureKit as SCK
import CoreMedia

MAGIC = b'BVNC'
out   = sys.stdout.buffer
ppid  = os.getppid()

_cv = ctypes.CDLL('/System/Library/Frameworks/CoreVideo.framework/CoreVideo')
_cv.CVPixelBufferLockBaseAddress.restype    = ctypes.c_int32
_cv.CVPixelBufferLockBaseAddress.argtypes   = [ctypes.c_void_p, ctypes.c_uint64]
_cv.CVPixelBufferUnlockBaseAddress.restype  = ctypes.c_int32
_cv.CVPixelBufferUnlockBaseAddress.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
_cv.CVPixelBufferGetBaseAddress.restype     = ctypes.c_void_p
_cv.CVPixelBufferGetBaseAddress.argtypes    = [ctypes.c_void_p]
_cv.CVPixelBufferGetWidth.restype           = ctypes.c_size_t
_cv.CVPixelBufferGetWidth.argtypes          = [ctypes.c_void_p]
_cv.CVPixelBufferGetHeight.restype          = ctypes.c_size_t
_cv.CVPixelBufferGetHeight.argtypes         = [ctypes.c_void_p]
_cv.CVPixelBufferGetBytesPerRow.restype     = ctypes.c_size_t
_cv.CVPixelBufferGetBytesPerRow.argtypes    = [ctypes.c_void_p]

class _FrameOutput(NSObject):
    def stream_didOutputSampleBuffer_ofType_(self, stream, sampleBuffer, outputType):
        if outputType != 0:  # SCStreamOutputTypeScreen == 0
            return
        try:
            pb_obj = CoreMedia.CMSampleBufferGetImageBuffer(sampleBuffer)
            if pb_obj is None:
                return
            # PyObjC wraps CF objects; hash() returns the CF pointer for NSObject-bridged types.
            pb = hash(pb_obj)
            _cv.CVPixelBufferLockBaseAddress(pb, 1)  # kCVPixelBufferLock_ReadOnly = 1
            try:
                W   = int(_cv.CVPixelBufferGetWidth(pb))
                H   = int(_cv.CVPixelBufferGetHeight(pb))
                bpr = int(_cv.CVPixelBufferGetBytesPerRow(pb))
                if W <= 0 or H <= 0:
                    return
                base = _cv.CVPixelBufferGetBaseAddress(pb)
                if not base:
                    return
                ts_ms = int(time.time() * 1000)
                hdr = MAGIC + struct.pack('<IIQ', W, H, ts_ms)
                if bpr == W * 4:
                    pixel_data = ctypes.string_at(base, H * bpr)
                else:
                    pixel_data = b''.join(ctypes.string_at(base + r * bpr, W * 4) for r in range(H))
                out.write(hdr + pixel_data)
                out.flush()
            finally:
                _cv.CVPixelBufferUnlockBaseAddress(pb, 1)
        except BrokenPipeError:
            os._exit(0)
        except Exception as e:
            sys.stderr.write('SCKCapture frame: ' + str(e) + '\n')

_ready  = threading.Event()
_content = [None]
_cerr   = [None]

def _content_cb(content, error):
    _content[0] = content
    _cerr[0]    = error
    _ready.set()

try:
    SCK.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
        False, True, _content_cb)
except AttributeError:
    # Older macOS: try legacy method name
    SCK.SCShareableContent.getExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
        False, True, _content_cb)

t0 = time.time()
while not _ready.is_set() and time.time() - t0 < 10:
    NSRunLoop.mainRunLoop().runMode_beforeDate_(NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(0.1))

if _cerr[0] or _content[0] is None:
    sys.stderr.write('SCKCapture: content error: ' + str(_cerr[0]) + '\n')
    sys.exit(1)

displays = _content[0].displays()
if not displays:
    sys.stderr.write('SCKCapture: no displays\n')
    sys.exit(1)

display = displays[0]
W, H    = display.width(), display.height()
sys.stderr.write('SCKCapture: starting ' + str(W) + 'x' + str(H) + ' @ 60fps (BGRA/SCK)\n')
sys.stderr.flush()

filt   = SCK.SCContentFilter.alloc().initWithDisplay_excludingApplications_exceptingWindows_(display, [], [])
config = SCK.SCStreamConfiguration.alloc().init()
config.setWidth_(W)
config.setHeight_(H)
config.setPixelFormat_(0x42475241)  # kCVPixelFormatType_32BGRA
config.setShowsCursor_(True)
config.setCapturesAudio_(False)

writer = _FrameOutput.alloc().init()
stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(filt, config, writer)
stream.addStreamOutput_type_sampleHandlerQueue_error_(writer, 0, None, None)

_started = threading.Event()
_serr    = [None]
def _start_cb(e): _serr[0] = e; _started.set()
stream.startCaptureWithCompletionHandler_(_start_cb)

t0 = time.time()
while not _started.is_set() and time.time() - t0 < 8:
    NSRunLoop.mainRunLoop().runMode_beforeDate_(NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(0.1))

if _serr[0]:
    sys.stderr.write('SCKCapture: start error: ' + str(_serr[0]) + '\n')
    sys.exit(1)

sys.stderr.write('SCKCapture: stream active\n')
sys.stderr.flush()

# Run main loop; parent-death watchdog fires every ~5s
_ppid_check = 0
while True:
    NSRunLoop.mainRunLoop().runMode_beforeDate_(NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(0.01))
    _ppid_check += 1
    if _ppid_check >= 500:
        _ppid_check = 0
        try: os.kill(ppid, 0)
        except ProcessLookupError: break
"""

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

# Mac virtual key codes indexed by browser e.code.
# Allows CGEvent injection to bypass screensharingd's X11 keysym→VK translation,
# which becomes unreliable after VNC reconnects on newer macOS versions.
VK = {
    "KeyA":0,"KeyB":11,"KeyC":8,"KeyD":2,"KeyE":14,"KeyF":3,"KeyG":5,"KeyH":4,
    "KeyI":34,"KeyJ":38,"KeyK":40,"KeyL":37,"KeyM":46,"KeyN":45,"KeyO":31,"KeyP":35,
    "KeyQ":12,"KeyR":15,"KeyS":1,"KeyT":17,"KeyU":32,"KeyV":9,"KeyW":13,"KeyX":7,
    "KeyY":16,"KeyZ":6,
    "Digit0":29,"Digit1":18,"Digit2":19,"Digit3":20,"Digit4":21,
    "Digit5":23,"Digit6":22,"Digit7":26,"Digit8":28,"Digit9":25,
    "Space":49,"Enter":36,"Return":36,"Tab":48,"Backspace":51,"Delete":117,
    "Escape":53,"Home":115,"End":119,"PageUp":116,"PageDown":121,
    "ArrowLeft":123,"ArrowRight":124,"ArrowUp":126,"ArrowDown":125,
    "Equal":24,"Minus":27,"BracketLeft":33,"BracketRight":30,
    "Backslash":42,"Semicolon":41,"Quote":39,"Comma":43,"Period":47,"Slash":44,"Backquote":50,
    "MetaLeft":55,"MetaRight":54,
    "ShiftLeft":56,"ShiftRight":60,
    "ControlLeft":59,"ControlRight":62,
    "AltLeft":58,"AltRight":61,
    "CapsLock":57,
    "F1":122,"F2":120,"F3":99,"F4":118,"F5":96,"F6":97,
    "F7":98,"F8":100,"F9":101,"F10":109,"F11":103,"F12":111,
}
# e.key aliases: browser sends lowercase letter as k ("a") and code as "KeyA".
# Add both so CGEvent handles the letter without falling back to VNC.
for _c in "abcdefghijklmnopqrstuvwxyz":
    VK[_c] = VK[f"Key{_c.upper()}"]
    VK[_c.upper()] = VK[f"Key{_c.upper()}"]
for _d in "0123456789":
    VK[_d] = VK[f"Digit{_d}"]
VK[" "] = 49   # e.key for Space is " "
del _c, _d
# kCGEventFlag masks for each modifier VK code
_VK_FLAGS = {
    55:0x100000, 54:0x100000,  # MetaLeft/Right → Command
    56:0x020000, 60:0x020000,  # ShiftLeft/Right → Shift
    59:0x040000, 62:0x040000,  # ControlLeft/Right → Control
    58:0x080000, 61:0x080000,  # AltLeft/Right → Option
    57:0x010000,               # CapsLock → AlphaShift
}
_VK_MODS = frozenset(_VK_FLAGS)
# Global modifier-held state for CGEvent path (system-global, not per-session).
_cg_mod_held: set = set()
# True once AXIsProcessTrusted() and a test CGEventPost both succeed.
_cg_kb_ok: bool = False

def _check_cg_kb() -> bool:
    """Probe Accessibility permission and enable CGEvent keyboard injection if granted."""
    global _cg_kb_ok
    if _cg_kb_ok:
        return True
    try:
        import ctypes, Quartz as _Q
        ax = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        ax.AXIsProcessTrusted.restype = ctypes.c_bool
        if not ax.AXIsProcessTrusted():
            return False
        # Confirm CGEventPost works: post a key-up for a non-existent VK (no-op).
        evt = _Q.CGEventCreateKeyboardEvent(None, 0xFF, False)
        _Q.CGEventPost(_Q.kCGHIDEventTap, evt)
        _cg_kb_ok = True
        log.info("CGEvent keyboard: Accessibility granted — CGEvent input active")
        return True
    except Exception as e:
        log.debug("CGEvent keyboard probe: %s", e)
        return False

def _poll_cg_kb():
    """Background thread: poll until Accessibility is granted, then enable CGEvent input."""
    import time
    while not _cg_kb_ok:
        time.sleep(5)
        _check_cg_kb()

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
        self._nudge_schedule = []  # [(epoch_ms, x, y), ...] — scheduled compositor-wake nudges
        self._cached_fb = None    # copy made at last _fb_seq change
        self._cached_seq = -1     # seq corresponding to _cached_fb
        self._fb_hash = 0         # content hash for static-screen detection
        self._last_key_ms = 0     # epoch_ms of last keydown sent to Mac
        self._cg_fb = None           # CGImage override buffer (RGB, same shape as _fb)
        self._cg_override_until = 0  # epoch_ms: suppress stale VNC overwrites until this time
        self._stale_vnc_hash = 0     # hash screensharingd returned when CGImage capture was done

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
            lx, ly = self._last_ptr_x, self._last_ptr_y
            if down:
                self._input_q.append(struct.pack("!BBHH", 5, 0, lx + 1, ly))
                now_ms = int(time.time() * 1000)
                self._last_key_ms = now_ms
                self._nudge_schedule = [
                    (now_ms + i * 30, lx + (i % 2), ly) for i in range(1, 21)
                ]
            else:
                self._input_q.append(struct.pack("!BBHH", 5, 0, lx, ly))
        # Post a real HID-level event so screensharingd exits HID-idle immediately.
        # kCGSessionEventTap (what VNC injection uses) is invisible to screensharingd's
        # own HID-idle detector — only kCGHIDEventTap wakes it. Requires Accessibility
        # permission or root; falls back silently if unavailable.
        if down:
            try:
                import Quartz as _Q
                import AppKit as _AK
                mp = _AK.NSEvent.mouseLocation()
                sh = _AK.NSScreen.mainScreen().frame().size.height
                e = _Q.CGEventCreateMouseEvent(
                    None, _Q.kCGEventMouseMoved,
                    _Q.CGPoint(mp.x + 1, sh - mp.y), _Q.kCGMouseButtonLeft)
                _Q.CGEventPost(_Q.kCGHIDEventTap, e)
            except Exception:
                pass

    def send_clipboard(self, text):
        # Encode inline into _input_q so it's sent before subsequent key events.
        # _clip_q is flushed AFTER _input_q in _flush_input, which would cause
        # Cmd+V to fire before the clipboard is set — pasting the old content.
        enc = text.encode("latin-1", errors="replace")
        with self._lock:
            self._input_q.append(struct.pack("!BBxxI", 6, 0, len(enc)) + enc)

    def send_key_reset(self):
        """Release modifier keys and clear mouse buttons — guards against stuck state on reconnect."""
        with self._lock:
            # Release Shift/Ctrl/Alt/Meta (both sides)
            for ks in [0xFFE1, 0xFFE2, 0xFFE3, 0xFFE4, 0xFFE9, 0xFFEA, 0xFFE7, 0xFFE8]:
                self._input_q.append(struct.pack("!BBxxI", 4, 0, ks))
            # Pointer with all buttons released
            self._input_q.append(struct.pack("!BBHH", 5, 0, self._last_ptr_x, self._last_ptr_y))

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
        s.settimeout(10)  # screensharingd can hang after SCK restarts; detect fast
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
        s.settimeout(None)  # switch to blocking for the frame receive loop
        return s

    def _cg_direct_capture(self, W, H):
        """Capture the live screen via screencapture, bypassing screensharingd.
        Called when screensharingd is HID-idle. Takes ~150-250ms on M1.
        Uses the system screencapture tool which always returns the correctly
        composited scene (all windows + wallpaper, no premultiplied-alpha garbage)."""
        import subprocess, os, tempfile
        from PIL import Image as _PIL
        fname = None
        try:
            fd, fname = tempfile.mkstemp(suffix='.png', prefix='vnc_cap_', dir='/tmp')
            os.close(fd)
            subprocess.run(
                ['screencapture', '-x', fname],
                timeout=3, check=True, capture_output=True)
            pil = _PIL.open(fname).resize((W, H), _PIL.LANCZOS).convert('RGB')
            arr = np.array(pil, dtype=np.uint8)
            sh, sw = max(1, H // 32), max(1, W // 32)
            new_hash = hash(arr[::sh, ::sw, 0].tobytes())
            with self._lock:
                if new_hash == self._fb_hash:
                    return False
                self._stale_vnc_hash = self._fb_hash
                np.copyto(self._fb, arr)
                if self._cg_fb is None or self._cg_fb.shape != self._fb.shape:
                    self._cg_fb = np.empty_like(self._fb)
                np.copyto(self._cg_fb, self._fb)
                self._fb_hash = new_hash
                self._fb_seq += 1
                self._fb_ms = int(time.time() * 1000)
                self._cg_override_until = self._fb_ms + 2000
            return True
        except Exception as e:
            log.debug("cg_direct_capture failed: %s", e)
            return False
        finally:
            if fname:
                try: os.unlink(fname)
                except: pass

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
                _last_req_ms    = int(time.time() * 1000)
                _last_fbu_ms    = _last_req_ms  # last time ANY FBU was received
                _last_change_ms = _last_req_ms  # last time pixels actually changed
                _last_keepalive_ms = _last_req_ms
                _keepalive_idx  = 0
                _connect_ms     = _last_req_ms
                _force_reconnect = False
                while True:
                    self._flush_input()
                    r, _, _ = select.select([s], [], [], 0.005)  # 5ms: lower key-event pickup latency
                    if not r:
                        now_ms   = int(time.time() * 1000)
                        # screensharingd silently stops processing VNC input events
                        # after ~10 min while keeping the socket open — detect via age
                        # and force a clean reconnect before that window expires.
                        if now_ms - _connect_ms > 8 * 60 * 1000:
                            log.info("VNC: periodic reconnect to prevent screensharingd input stall")
                            _force_reconnect = True
                            break
                        stale_ms = now_ms - _last_fbu_ms
                        if not _pending_req:
                            # Use content staleness (time since last pixel change) not FBU
                            # staleness (time since last response). screensharingd answers
                            # _FBU_INC immediately with empty responses on a static screen,
                            # keeping stale_ms low while content_stale_ms reflects reality.
                            content_stale_ms = now_ms - _last_change_ms
                            req = _FBU_FULL if content_stale_ms > 25 else _FBU_INC
                            s.send(req)
                            _pending_req = True
                            _last_req_ms = now_ms
                        elif stale_ms > 25 and now_ms - _last_req_ms >= 25:
                            # Pending request unanswered for 50ms+ — override with full refresh.
                            s.send(_FBU_FULL)
                            _last_req_ms = now_ms
                        # Drain scheduled compositor-wake nudges — spaced 30ms apart so
                        # we don't overwhelm screensharingd's FBU scheduling.
                        if self._nudge_schedule:
                            with self._lock:
                                due = [t for t in self._nudge_schedule if now_ms >= t[0]]
                                for t in due:
                                    self._nudge_schedule.remove(t)
                            for _, nx, ny in due:
                                try:
                                    s.send(struct.pack("!BBHH", 5, 0, nx, ny))
                                except Exception:
                                    pass
                        # Compositor keepalive: every 100ms when screen is static, send a
                        # tiny pointer micro-move. Prevents macOS from throttling the display
                        # compositor's refresh rate on idle screens (which causes 500ms–3s
                        # first-keystroke latency). At 10Hz this is imperceptible on the Mac.
                        elif (now_ms - _last_change_ms > 500 and
                              now_ms - _last_keepalive_ms > 100):
                            _last_keepalive_ms = now_ms
                            lx, ly = self._last_ptr_x, self._last_ptr_y
                            nx = lx + (_keepalive_idx % 2)
                            _keepalive_idx += 1
                            try:
                                s.send(struct.pack("!BBHH", 5, 0, nx, ly))
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
                            sh = max(1, H // 32)
                            sw = max(1, W // 32)
                            new_hash = hash(fb[::sh, ::sw, 0].tobytes())
                            _do_cg = False
                            with self._lock:
                                self._fbu_count += 1
                                if now_ms < self._cg_override_until:
                                    # Inside CGImage override window: screensharingd is still
                                    # HID-idle and keeps serving the pre-keypress framebuffer.
                                    if new_hash == self._stale_vnc_hash:
                                        # Stale VNC data overwrote our CGImage pixels — restore.
                                        # Re-arm the window so we keep suppressing stale writes.
                                        if self._cg_fb is not None:
                                            np.copyto(self._fb, self._cg_fb)
                                        self._cg_override_until = int(time.time() * 1000) + 2000
                                    else:
                                        # Different hash — screensharingd woke up with real pixels.
                                        self._fb_hash = new_hash
                                        self._fb_seq += 1
                                        self._fb_ms = now_ms
                                        _last_change_ms = now_ms
                                        self._nudge_schedule = []
                                        self._cg_override_until = 0
                                elif new_hash != self._fb_hash:
                                    self._fb_hash = new_hash
                                    self._fb_seq += 1
                                    self._fb_ms = now_ms
                                    _last_change_ms = now_ms
                                    self._nudge_schedule = []
                                else:
                                    # screensharingd returned stale pixels. If a key was
                                    # recently pressed but no content arrived for 150ms,
                                    # screensharingd is HID-idle — bypass with CGDisplayCreateImage.
                                    lkm = self._last_key_ms
                                    if lkm > 0 and now_ms - lkm > 150 and now_ms - _last_change_ms > 150:
                                        _do_cg = True
                            # Call outside the lock — _cg_direct_capture takes the lock internally.
                            if _do_cg and self._cg_direct_capture(W, H):
                                _last_change_ms = now_ms
                        _last_fbu_ms = now_ms  # received FBU; reset response-staleness clock
                        # Choose next request type based on content staleness, not response
                        # staleness. If nothing has changed in the last 25ms, screensharingd
                        # is idle — send FULL to force a fresh capture that will pick up any
                        # terminal text that rendered since the last real pixel update.
                        content_stale_ms = now_ms - _last_change_ms
                        s.send(_FBU_FULL if content_stale_ms > 25 else _FBU_INC)
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
                _force_reconnect = False
            if not _force_reconnect:
                time.sleep(3)

    def _pbpaste_poll(self):
        """Poll pbpaste every second to detect Mac clipboard changes.
        VNC ClientCutText is silently ignored by screensharingd on macOS 15+,
        so pbpaste is the reliable path for Mac→browser clipboard sync."""
        import subprocess as _sp, hashlib as _hl
        last_hash = b''
        while True:
            time.sleep(1)
            try:
                text = _sp.run(['pbpaste'], capture_output=True, timeout=2).stdout.decode('utf-8', errors='replace')
                h = _hl.md5(text.encode()).digest()
                if h != last_hash:
                    last_hash = h
                    with self._lock:
                        self.server_clipboard = text
                        self.server_clipboard_seq += 1
            except Exception:
                pass

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._pbpaste_poll, daemon=True).start()

# ---------------------------------------------------------------------------
# DisplayStreamBridge — direct screen capture via SCK (ScreenCaptureKit) subprocess.
# Requires Screen Recording (kTCCServiceScreenCapture) granted to python3.
# Falls back gracefully: if no frame within 5s, is_running() returns False and
# BridgeProxy switches to VNCBridge for all capture calls.
# ---------------------------------------------------------------------------
class DisplayStreamBridge:
    _FRAME_HDR = 20  # magic(4) + W(4LE) + H(4LE) + ts_ms(8LE)

    def __init__(self):
        self._proc = None
        self._lock = threading.Lock()
        self._fb = None
        self._fb_seq = 0
        self._fb_ms = 0
        self._W = 0
        self._H = 0
        self._running = False

    def start(self):
        """Launch the capture subprocess. Returns True when first frame arrives (≤5s)."""
        import subprocess as _sp
        self._running = True
        self._proc = _sp.Popen(
            [sys.executable, "-c", _SCSTREAM_CAPTURE_SRC],
            stdout=_sp.PIPE, stderr=_sp.PIPE,
        )
        t = threading.Thread(target=self._read_loop, daemon=True)
        t.start()
        deadline = time.time() + 5.0
        while time.time() < deadline:
            with self._lock:
                if self._fb is not None:
                    log.info("DisplayStreamBridge: %dx%d capture active", self._W, self._H)
                    return True
            time.sleep(0.05)
        err = b""
        if self._proc and self._proc.poll() is not None:
            err = self._proc.stderr.read(200)
        log.warning("DisplayStreamBridge: no frame in 5s — %s",
                    err.decode(errors="replace").strip() or "Screen Recording permission needed")
        self._running = False
        return False

    def _read_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self._proc.stdout.read(n - len(buf))
            if not chunk:
                raise EOFError("capture helper exited")
            buf += chunk
        return bytes(buf)

    def _read_loop(self):
        try:
            while self._running:
                hdr = self._read_exact(self._FRAME_HDR)
                magic = hdr[:4]
                if magic not in (b'UVNC', b'BVNC'):
                    log.warning("DisplayStreamBridge: bad magic %r", magic)
                    break
                W     = struct.unpack_from('<I', hdr, 4)[0]
                H     = struct.unpack_from('<I', hdr, 8)[0]
                ts_ms = struct.unpack_from('<Q', hdr, 12)[0]
                if magic == b'BVNC':
                    # BGRA payload — 4 bytes/pixel; encoder uses format="bgra"
                    data = self._read_exact(W * H * 4)
                    frame = np.frombuffer(data, dtype=np.uint8).reshape(H, W, 4).copy()
                else:
                    data  = self._read_exact(W * H * 3)
                    frame = np.frombuffer(data, dtype=np.uint8).reshape(H, W, 3).copy()
                with self._lock:
                    self._fb      = frame
                    self._fb_seq += 1
                    self._fb_ms    = ts_ms if ts_ms else int(time.time() * 1000)
                    self._W, self._H = W, H
        except Exception as e:
            log.warning("DisplayStreamBridge: read loop stopped: %s", e)
        self._running = False

    @property
    def dimensions(self):
        with self._lock:
            return self._W, self._H

    def get_current_frame(self):
        with self._lock:
            if self._fb is None:
                return None, 0
            return self._fb, self._fb_ms

    def is_running(self):
        if not self._running:
            return False
        if self._proc and self._proc.poll() is not None:
            self._running = False
            return False
        return True

# ---------------------------------------------------------------------------
# InProcessSCKBridge — SCK capture running in the server process itself.
# On macOS 26, subprocess-spawned Python processes get -3801 even with a valid
# TCC grant; running SCK in the LaunchAgent process (GUI session, proper bundle
# context) avoids that.  Same API surface as DisplayStreamBridge.
# ---------------------------------------------------------------------------
class InProcessSCKBridge:
    _fo_class   = None  # ObjC class registered once; cached here after first use
    _sck_queue  = None  # GCD global queue for SCK frame delivery; set by _make_fo_class

    def __init__(self):
        self._lock   = threading.Lock()
        self._fb     = None
        self._fb_seq = 0
        self._fb_ms  = 0
        self._W = self._H = 0
        self._running = False
        self._stream  = None
        self._writer  = None

    @classmethod
    def _make_fo_class(cls):
        """Create and cache the NSObject frame-output delegate (ObjC class registration is once-only)."""
        if cls._fo_class is not None:
            return cls._fo_class
        from Foundation import NSObject
        import CoreMedia, ctypes, warnings, re as _re
        import objc as _objc
        warnings.filterwarnings('ignore', category=_objc.ObjCPointerWarning)
        _cv  = ctypes.CDLL('/System/Library/Frameworks/CoreVideo.framework/CoreVideo')
        _cm  = ctypes.CDLL('/System/Library/Frameworks/CoreMedia.framework/CoreMedia')
        _cm.CMSampleBufferGetImageBuffer.restype  = ctypes.c_void_p
        _cm.CMSampleBufferGetImageBuffer.argtypes = [ctypes.c_void_p]
        _gcd = ctypes.CDLL('/usr/lib/system/libdispatch.dylib')
        _gcd.dispatch_get_global_queue.restype  = ctypes.c_void_p
        _gcd.dispatch_get_global_queue.argtypes = [ctypes.c_long, ctypes.c_ulong]
        cls._sck_queue = _objc.objc_object(c_void_p=_gcd.dispatch_get_global_queue(21, 0))

        def _sb_to_ptr(sb):
            """Extract the ObjC CMSampleBufferRef pointer from a PyObjC proxy.
            Strategy 1: PyObjCPointer stores ptr at offset 16 in the CPython object.
            Strategy 2: Parse the first hex address from description (fragile but works
                        for CF-bridged types whose description starts with 'CMSampleBuffer 0x...')."""
            try:
                # In CPython+PyObjC, the Python object at id(sb) is laid out as:
                # [ob_refcnt:8][ob_type:8][objc_id:8]...
                return ctypes.cast(id(sb), ctypes.POINTER(ctypes.c_void_p))[2]
            except Exception:
                pass
            try:
                m = _re.search(r'\b0x([0-9a-fA-F]+)\b', str(sb))
                if m:
                    return int(m.group(1), 16)
            except Exception:
                pass
            return 0
        _cv.CVPixelBufferLockBaseAddress.restype    = ctypes.c_int32
        _cv.CVPixelBufferLockBaseAddress.argtypes   = [ctypes.c_void_p, ctypes.c_uint64]
        _cv.CVPixelBufferUnlockBaseAddress.restype  = ctypes.c_int32
        _cv.CVPixelBufferUnlockBaseAddress.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        _cv.CVPixelBufferGetBaseAddress.restype     = ctypes.c_void_p
        _cv.CVPixelBufferGetBaseAddress.argtypes    = [ctypes.c_void_p]
        _cv.CVPixelBufferGetWidth.restype           = ctypes.c_size_t
        _cv.CVPixelBufferGetWidth.argtypes          = [ctypes.c_void_p]
        _cv.CVPixelBufferGetHeight.restype          = ctypes.c_size_t
        _cv.CVPixelBufferGetHeight.argtypes         = [ctypes.c_void_p]
        _cv.CVPixelBufferGetBytesPerRow.restype     = ctypes.c_size_t
        _cv.CVPixelBufferGetBytesPerRow.argtypes    = [ctypes.c_void_p]

        class _FrameOutputInProc(NSObject):
            # _bridge_ref is a class variable set to the active InProcessSCKBridge before
            # the stream starts.  Using a class variable avoids closure/ObjC registration issues.
            _bridge_ref = None

            def stream_didOutputSampleBuffer_ofType_(self_obj, stream, sampleBuffer, outputType):
                if outputType != 0:
                    return
                bridge = _FrameOutputInProc._bridge_ref
                if bridge is None:
                    return
                try:
                    sb_ptr = _sb_to_ptr(sampleBuffer)
                    if not sb_ptr:
                        return
                    pb = _cm.CMSampleBufferGetImageBuffer(sb_ptr)
                    if not pb:
                        return
                    _cv.CVPixelBufferLockBaseAddress(pb, 1)
                    try:
                        W   = int(_cv.CVPixelBufferGetWidth(pb))
                        H   = int(_cv.CVPixelBufferGetHeight(pb))
                        bpr = int(_cv.CVPixelBufferGetBytesPerRow(pb))
                        base = _cv.CVPixelBufferGetBaseAddress(pb)
                        if not base or W <= 0 or H <= 0:
                            return
                        if bpr == W * 4:
                            data = bytes(ctypes.string_at(base, H * bpr))
                        else:
                            data = b''.join(ctypes.string_at(base + r * bpr, W * 4) for r in range(H))
                        frame = np.frombuffer(data, dtype=np.uint8).reshape(H, W, 4).copy()
                        with bridge._lock:
                            bridge._fb     = frame
                            bridge._fb_seq += 1
                            bridge._fb_ms  = int(time.time() * 1000)
                            bridge._W, bridge._H = W, H
                    finally:
                        _cv.CVPixelBufferUnlockBaseAddress(pb, 1)
                except Exception as e:
                    log.debug("InProcessSCK frame: %s", e)

        cls._fo_class = _FrameOutputInProc
        return cls._fo_class

    def start(self):
        """Initialize SCK capture in-process. Returns True when the first frame arrives (≤5s)."""
        done = threading.Event()
        ok   = [False]

        def _init():
            try:
                import ScreenCaptureKit as SCKmod
                FO = InProcessSCKBridge._make_fo_class()
                FO._bridge_ref = self

                _cnt_ev  = threading.Event()
                _content = [None]
                _cerr    = [None]

                def _cnt_cb(content, error):
                    _content[0] = content
                    _cerr[0]    = error
                    _cnt_ev.set()

                try:
                    SCKmod.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
                        False, True, _cnt_cb)
                except AttributeError:
                    SCKmod.SCShareableContent.getExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
                        False, True, _cnt_cb)

                # Give the user up to 60s to respond to the macOS consent dialog
                # (shown when no TCC entry exists for com.apple.python3).
                if not _cnt_ev.wait(60):
                    log.warning("InProcessSCK: content timeout — if a 'Python would like to record your screen' dialog appeared, click Allow then restart the server")
                    done.set(); return
                if _cerr[0] or not _content[0]:
                    log.warning("InProcessSCK: content error: %s — grant Screen Recording in System Settings > Privacy > Screen Recording, then restart", _cerr[0])
                    done.set(); return

                displays = _content[0].displays()
                if not displays:
                    log.warning("InProcessSCK: no displays")
                    done.set(); return

                display = displays[0]
                W, H = display.width(), display.height()

                filt = SCKmod.SCContentFilter.alloc().initWithDisplay_excludingApplications_exceptingWindows_(display, [], [])
                cfg  = SCKmod.SCStreamConfiguration.alloc().init()
                cfg.setWidth_(W)
                cfg.setHeight_(H)
                cfg.setPixelFormat_(0x42475241)  # kCVPixelFormatType_32BGRA
                cfg.setShowsCursor_(True)
                cfg.setCapturesAudio_(False)

                writer = FO.alloc().init()
                stream = SCKmod.SCStream.alloc().initWithFilter_configuration_delegate_(filt, cfg, writer)
                stream.addStreamOutput_type_sampleHandlerQueue_error_(
                    writer, 0, InProcessSCKBridge._sck_queue, None)

                _st_ev = threading.Event()
                _serr  = [None]
                def _st_cb(e): _serr[0] = e; _st_ev.set()
                stream.startCaptureWithCompletionHandler_(_st_cb)

                if not _st_ev.wait(10):
                    log.warning("InProcessSCK: start timeout")
                    done.set(); return
                if _serr[0]:
                    log.warning("InProcessSCK: start error: %s", _serr[0])
                    done.set(); return

                self._stream  = stream
                self._writer  = writer
                self._running = True
                ok[0] = True
                log.info("InProcessSCK: stream active %dx%d", W, H)
                done.set()
                # Hold ObjC refs alive until stopped.
                while self._running:
                    time.sleep(1)
            except Exception as e:
                log.warning("InProcessSCK: init failed: %s", e)
                done.set()

        threading.Thread(target=_init, daemon=True).start()
        done.wait(70)  # allows 60s for user to respond to macOS consent dialog + stream start
        if not ok[0]:
            return False

        deadline = time.time() + 5.0
        while time.time() < deadline:
            with self._lock:
                if self._fb is not None:
                    log.info("InProcessSCKBridge: %dx%d capture active", self._W, self._H)
                    return True
            time.sleep(0.05)

        log.warning("InProcessSCKBridge: no frame in 5s")
        self._running = False
        return False

    @property
    def dimensions(self):
        with self._lock:
            return self._W, self._H

    def get_current_frame(self):
        with self._lock:
            if self._fb is None:
                return None, 0
            return self._fb, self._fb_ms

    def is_running(self):
        return self._running

# ---------------------------------------------------------------------------
# BridgeProxy — routes capture calls to DisplayStreamBridge (if active) else VNCBridge,
# and always routes input (key/pointer/clipboard) to VNCBridge.
# ---------------------------------------------------------------------------
class BridgeProxy:
    def __init__(self, vnc, ds=None):
        self._v = vnc
        self._d = ds

    def _cap(self):
        return self._d if (self._d and self._d.is_running()) else self._v

    def get_current_frame(self):
        return self._cap().get_current_frame()

    @property
    def _fb_seq(self):
        return self._cap()._fb_seq

    @property
    def _fb_ms(self):
        return self._cap()._fb_ms

    @property
    def _fbu_count(self):
        return self._v._fbu_count

    @property
    def server_clipboard_seq(self):
        return self._v.server_clipboard_seq

    @property
    def server_clipboard(self):
        return self._v.server_clipboard

    @property
    def dimensions(self):
        return self._cap().dimensions

    def send_pointer(self, *a, **k):
        return self._v.send_pointer(*a, **k)

    def send_key(self, *a, **k):
        return self._v.send_key(*a, **k)

    def send_clipboard(self, *a, **k):
        return self._v.send_clipboard(*a, **k)

    def send_key_reset(self):
        return self._v.send_key_reset()

    def set_capture(self, ds):
        """Hot-swap the display capture backend (e.g., when SCK permission is granted later)."""
        self._d = ds

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
                cc.gop_size = 99999
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

    @staticmethod
    def _to_rgb(frame):
        """Convert BGRA or RGB frame to RGB (in-place if already RGB)."""
        if frame.ndim == 3 and frame.shape[2] == 4:
            return np.ascontiguousarray(frame[:, :, 2::-1])
        return frame

    def encode(self, rgb, capture_ms, jpeg_quality=65):
        """Returns (payload, is_keyframe, codec_byte) or (None, False, _) on skip."""
        if self._cc is None:
            return _encode_jpeg(self._to_rgb(rgb), jpeg_quality), True, CODEC_JPEG
        try:
            fmt = "bgra" if (rgb.ndim == 3 and rgb.shape[2] == 4) else "rgb24"
            frame = _av.VideoFrame.from_ndarray(rgb, format=fmt)
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
            return _encode_jpeg(self._to_rgb(rgb), jpeg_quality), True, CODEC_JPEG

    def encode_keyframe(self, rgb, capture_ms, quality):
        """Force an I-frame refresh — called after extended static period to sharpen quality.
        Attempts gop_size=1 + pict_type=I; VideoToolbox may ignore both, in which case
        the frame is still sent at the current (high) bitrate ceiling."""
        if self._cc is None:
            return _encode_jpeg(self._to_rgb(rgb), quality), True, CODEC_JPEG
        try:
            self._cc.gop_size = 1
        except Exception:
            pass
        pkts = []
        try:
            fmt = "bgra" if (rgb.ndim == 3 and rgb.shape[2] == 4) else "rgb24"
            frame = _av.VideoFrame.from_ndarray(rgb, format=fmt)
            frame = frame.reformat(format="yuv420p")
            pts = max(self._last_pts + 1, capture_ms)
            frame.pts = pts
            try:
                frame.pict_type = 1   # AV_PICTURE_TYPE_I
            except Exception:
                pass
            pkts = list(self._cc.encode(frame))
            self._last_pts = pts
        except Exception as e:
            log.debug("encode_keyframe err: %s", e)
        try:
            self._cc.gop_size = 99999
        except Exception:
            pass
        if not pkts:
            return None, False, self.actual_codec
        return bytes(pkts[0]), True, self.actual_codec

    def close(self):
        if self._cc:
            try: self._cc.close()
            except Exception: pass
            self._cc = None

# ---------------------------------------------------------------------------
# AdaptiveController — per-client fps + bitrate management
# ---------------------------------------------------------------------------
class AdaptiveController:

    def __init__(self, cfg):
        self.fps = float(cfg.max_fps)
        self.max_fps = float(cfg.max_fps)
        self.bitrate = 4_000_000     # start conservative — 1Mbps users reach stable in 2 halvings
        self.jpeg_quality = 85
        self.client_w = 1920
        self.client_h = 1080
        self._min_br = 300_000
        self._min_fps = 5.0          # fps floor — only reduced after bitrate hits minimum
        self._max_br = 50_000_000   # 50Mbps cap — plenty for any screenshare quality
        # Congestion ceiling: bitrate at the moment the last backoff fired.
        # 0 = not yet measured — on_fresh probes slowly until first congestion event.
        self._ceil_bitrate = 0
        self._last_slow = 0.0
        self._last_fast = 0.0
        self._lock = threading.Lock()
        self._ping_smooth = 0.0     # EWA-smoothed video ping RTT (jitter suppression)
        self._ping_history = []     # last 4 smoothed samples for gradient computation
        self._metric_rtt = 0.0      # EWA of unloaded metric-channel RTT; 0 = not measured yet

    @property
    def frame_interval(self):
        return 1.0 / max(1.0, self.fps)

    def on_resolution(self, w, h):
        with self._lock:
            self.client_w = max(1, w)
            self.client_h = max(1, h)

    def _backoff(self, severe):
        """Reduce quality. Must be called with _lock held; enforces 300ms debounce.

        Priority: cut bitrate (quality) first — preserves fps (input responsiveness).
        fps is only reduced as a last resort when bitrate is already at the floor,
        because lower fps means longer frame intervals which increases lag further."""
        now = time.monotonic()
        if now - self._last_slow < 0.3:
            return
        self._last_slow = now
        self._last_fast = 0.0
        factor = 0.5 if severe else 0.75
        if self.bitrate > self._min_br:
            # Save congestion point before reducing — this is the network ceiling (SSTHRESH).
            # On recovery, ramp fast back to here, probe slowly above.
            self._ceil_bitrate = self.bitrate
            self.bitrate = max(self._min_br, int(self.bitrate * factor))
            self.jpeg_quality = max(10, int(self.jpeg_quality * factor))
        elif self.fps > self._min_fps:
            self.fps = max(self._min_fps, self.fps * factor)
        log.debug("backoff: fps=%.1f br=%dk ceil=%dk severe=%s",
                  self.fps, self.bitrate // 1000, self._ceil_bitrate // 1000, severe)

    def lag_budget_ms(self):
        """Allowed in-flight delay per frame.

        1 frame interval for ≤20fps (low throughput — every extra ms is felt),
        naturally ~3 frames at 60fps (still ≤50ms reaction time), hard-capped
        at 500ms so a single slow frame never monopolises the buffer for a full
        second even at very low fps. Floor at 50ms for high-fps paths.
        """
        return max(50.0, min(1000.0 / max(1.0, self.fps), 500.0))

    def lag_wb_budget(self):
        """Write-buffer byte equivalent of lag_budget_ms at current bitrate.
        Floor is 2 average frame sizes — absorbs a keyframe burst without false backoff.
        At 1Mbps/20fps this is ~12KB (not 32KB); at 10Mbps/60fps it's ~42KB."""
        avg_frame = int(self.bitrate / max(1.0, self.fps) / 8)  # bytes per average frame
        return max(2 * avg_frame, 4 * 1024, int(self.lag_budget_ms() * self.bitrate / 8000))

    def on_lag(self, age_ms, write_buf=0):
        budget = self.lag_budget_ms()
        if age_ms > 0 and age_ms < budget and write_buf < self.lag_wb_budget():
            return
        if age_ms == 0 and write_buf < self.lag_wb_budget():
            return
        severe = age_ms > budget * 3 or write_buf > self.lag_wb_budget() * 6
        with self._lock:
            self._backoff(severe)

    def on_ping_rtt(self, rtt_ms):
        """Two-signal congestion detection via video-channel RTT.

        Signal 1 — gradient (primary): RTT rising means a buffer is FORMING right now.
        Fires early, before the queue is large, and requires no baseline or metric channel.
        Link-agnostic: RTT going up is RTT going up regardless of absolute value.

        Signal 2 — delta vs metric (secondary): RTT stable but elevated above the unloaded
        metric channel means a STATIC buffer exists. This catches the case where the gradient
        already fired and settled, or where we joined mid-congestion. A static buffer is an
        unstable equilibrium; slight backoff drains it quickly."""
        with self._lock:
            # Smooth to suppress per-sample jitter before computing gradient
            self._ping_smooth = (self._ping_smooth * 0.6 + rtt_ms * 0.4
                                 if self._ping_smooth > 0 else rtt_ms)
            s = self._ping_smooth
            self._ping_history.append(s)
            if len(self._ping_history) > 4:
                self._ping_history.pop(0)

            # Signal 1: gradient — buffer FORMING
            gradient_fired = False
            if len(self._ping_history) >= 3:
                prev_mean = sum(self._ping_history[:-1]) / len(self._ping_history[:-1])
                gradient = s - prev_mean
                if gradient > 15:       # rising >15ms per 2s sample = queue building
                    self._backoff(gradient > 40)
                    gradient_fired = True
                    log.debug("ping gradient=%.1fms rtt=%.1fms", gradient, s)

            # Signal 2: delta — buffer STATIC (only when gradient hasn't already fired)
            # 50ms tolerance = expected 1-frame-in-buffer offset; anything above is real queuing.
            if not gradient_fired and self._metric_rtt > 0:
                delta = s - self._metric_rtt
                if delta > 50:
                    self._backoff(delta > 150)
                    log.debug("ping delta=%.1fms rtt=%.1fms metric=%.1fms", delta, s, self._metric_rtt)

    def on_metric_rtt(self, rtt_ms):
        """RTT on the unloaded metric channel — pure link latency, no video queuing.
        Fast EWA (0.7/0.3) so link changes from WiFi↔5G roaming are reflected in ~4s."""
        with self._lock:
            if self._metric_rtt == 0.0:
                self._metric_rtt = rtt_ms
            else:
                self._metric_rtt = self._metric_rtt * 0.7 + rtt_ms * 0.3

    def on_fresh(self):
        with self._lock:
            now = time.monotonic()
            if now - self._last_fast < 2.0:
                return
            if now - self._last_slow < 2.0:
                return  # recent backoff — wait for stability before probing up
            self._last_fast = now
            if self.fps < self.max_fps:
                self.fps = self.max_fps
            elif self.bitrate < self._max_br:
                # Below ceiling: jump to 90% of it — the ceiling was the bitrate
                # that just triggered congestion, so landing slightly below avoids
                # an immediate re-trigger while still recovering fast.
                # _ceil_bitrate == 0 means no congestion measured yet; probe cautiously.
                if self._ceil_bitrate > 0 and self.bitrate < self._ceil_bitrate:
                    self.bitrate = max(self._min_br, int(self._ceil_bitrate * 0.90))
                elif self.bitrate < 20_000_000:
                    self.bitrate = min(self._max_br, int(self.bitrate * 1.10))
                else:
                    self.bitrate = min(self._max_br, int(self.bitrate * 1.05))
                self.jpeg_quality = min(95, self.jpeg_quality + 5)
            log.debug("fresh: fps=%.1f br=%dk ceil=%dk", self.fps, self.bitrate//1000, self._ceil_bitrate//1000)

    def on_screen_active(self):
        """Screen content changed after a static period — restore fps and jump toward last
        known stable bitrate. Uses 90% of the congestion ceiling (same as on_fresh recovery)
        to avoid immediately re-triggering congestion on every screen-active event."""
        with self._lock:
            self.fps = self.max_fps
            if self._ceil_bitrate > 0 and self._ceil_bitrate > self.bitrate:
                self.bitrate = max(self._min_br, int(self._ceil_bitrate * 0.90))
                self.jpeg_quality = min(95, self.jpeg_quality + 20)
            self._last_fast = time.monotonic()
            log.debug("screen active: fps=%.1f br=%dk ceil=%dk", self.fps, self.bitrate//1000, self._ceil_bitrate//1000)

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

    # Release any modifier keys / mouse buttons left over from a previous session.
    bridge.send_key_reset()

    ctrl = AdaptiveController(cfg)
    # Start with JPEG until client reports WebCodecs capability and supported codecs.
    # target_codec is updated when the client sends its caps (codec negotiation).
    target_codec = CODEC_H264 if cfg.codec == "h264" else CODEC_H265
    encoder = EncoderPipeline(CODEC_JPEG, W, H, ctrl.bitrate)  # JPEG until caps received
    has_webcodecs = False

    seq_num = 0
    last_send_time = time.monotonic()

    def _upgrade_encoder():
        nonlocal encoder, has_webcodecs
        if not has_webcodecs:
            return
        if encoder.actual_codec != CODEC_JPEG:
            return  # already upgraded
        encoder.close()
        W2, H2 = bridge.dimensions
        # Cascade: try target_codec first, then H.265, then H.264.
        # AV1 software encode (libaom/SVT) is too slow for real-time so we stop
        # at H.265 when hardware AV1 isn't available.
        seen = set()
        fallbacks = [target_codec, CODEC_H265, CODEC_H264]
        for codec in fallbacks:
            if codec == CODEC_AV1:
                continue  # skip software AV1 until hardware VT support lands
            if codec in seen:
                continue
            seen.add(codec)
            e = EncoderPipeline(codec, W2, H2, ctrl.bitrate)
            if e.actual_codec != CODEC_JPEG:
                encoder = e
                log.info("Upgraded to %s for %s",
                         {CODEC_H264:"h264",CODEC_H265:"h265",CODEC_AV1:"av1"}.get(encoder.actual_codec,"?"),
                         ws.remote_address)
                return
            e.close()
        encoder = EncoderPipeline(CODEC_JPEG, W2, H2, ctrl.bitrate)
        log.warning("Video codec unavailable for %s — staying on JPEG", ws.remote_address)

    loop = asyncio.get_event_loop()

    async def input_reader():
        nonlocal has_webcodecs, target_codec
        cur_buttons = 0
        try:
            async for raw in ws:
                if not isinstance(raw, str):
                    continue
                try:
                    ev = json.loads(raw)
                    t = ev.get("t")
                    if t == "reset":
                        cur_buttons = 0
                        bridge.send_key_reset()
                    elif t == "caps":
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
                        ctrl.on_lag(age, _get_wbuf(ws))
                    elif t == "metric_rtt":
                        ctrl.on_metric_rtt(float(ev.get("rtt_ms", 0)))
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
                        # dy/dx are pre-normalized click counts with sign by the browser
                        dx, dy = int(ev.get("dx",0)), int(ev.get("dy",0))
                        evts = []
                        if dy: evts.append((8 if dy < 0 else 16, abs(dy)))   # up/down
                        if dx: evts.append((32 if dx < 0 else 64, abs(dx)))  # left/right
                        async def _scroll(evts=evts, sx=x, sy=y):
                            for btn, n in evts:
                                for _ in range(n):
                                    bridge.send_pointer(btn, sx, sy)
                                    bridge.send_pointer(0, sx, sy)
                                    if n > 1:
                                        await asyncio.sleep(0.012)
                        asyncio.create_task(_scroll())
                    elif t in ("kd","ku"):
                        k = ev.get("k",""); code = ev.get("code","")
                        down = t == "kd"
                        vk = VK.get(code) if code else None
                        if vk is None and k:
                            vk = VK.get(k)
                        if vk is not None and _cg_kb_ok:
                            # CGEvent primary path — native VK codes, bypasses screensharingd
                            # keysym→VK translation which changes after VNC reconnects.
                            if vk in _VK_MODS:
                                if down: _cg_mod_held.add(vk)
                                else:    _cg_mod_held.discard(vk)
                            flags = 0
                            for mv in _cg_mod_held:
                                flags |= _VK_FLAGS.get(mv, 0)
                            try:
                                import Quartz as _Q
                                evt = _Q.CGEventCreateKeyboardEvent(None, vk, down)
                                _Q.CGEventSetFlags(evt, flags)
                                _Q.CGEventPost(_Q.kCGHIDEventTap, evt)
                            except Exception as _e:
                                log.debug("CGEvent key vk=%d: %s — VNC fallback", vk, _e)
                                ks = KEYSYM.get(code) or KEYSYM.get(k) or (ord(k) if len(k)==1 else None)
                                if ks: bridge.send_key(down, ks)
                        else:
                            ks = KEYSYM.get(code) or KEYSYM.get(k) or (ord(k) if len(k)==1 else None)
                            if ks: bridge.send_key(down, ks)
                    elif t in ("paste", "setclip"):
                        text = ev.get("text","")
                        if text:
                            # pbcopy is more reliable than VNC ClientCutText on macOS 15+
                            # (ClientCutText may be silently ignored by screensharingd)
                            try:
                                proc = await asyncio.create_subprocess_exec(
                                    'pbcopy', stdin=asyncio.subprocess.PIPE)
                                proc.stdin.write(text.encode('utf-8', errors='replace'))
                                proc.stdin.close()
                                await asyncio.wait_for(proc.wait(), timeout=2.0)
                            except Exception:
                                bridge.send_clipboard(text)  # fallback
                        if t == "paste" and text:
                            # Release any held modifiers, then send Cmd+V
                            if _cg_kb_ok:
                                try:
                                    import Quartz as _Q
                                    _cg_mod_held.clear()
                                    for _vk, _dn, _fl in [
                                        (55, True,  0x100000),
                                        (9,  True,  0x100000),
                                        (9,  False, 0x100000),
                                        (55, False, 0),
                                    ]:
                                        _e2 = _Q.CGEventCreateKeyboardEvent(None, _vk, _dn)
                                        _Q.CGEventSetFlags(_e2, _fl)
                                        _Q.CGEventPost(_Q.kCGHIDEventTap, _e2)
                                except Exception:
                                    pass
                            else:
                                for ks in [KEYSYM["ShiftLeft"], KEYSYM["ShiftRight"],
                                           KEYSYM["Control"], KEYSYM["ControlRight"],
                                           KEYSYM["Alt"], KEYSYM["AltRight"],
                                           KEYSYM["MetaLeft"], KEYSYM["MetaRight"]]:
                                    bridge.send_key(False, ks)
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
        finally:
            cur_buttons = 0
            bridge.send_key_reset()

    async def frame_sender():
        nonlocal seq_num, last_send_time
        known_clip = bridge.server_clipboard_seq
        # Send current Mac clipboard immediately on connect so side menu is populated.
        if bridge.server_clipboard:
            try:
                await ws.send(json.dumps({"t": "clipboard", "text": bridge.server_clipboard}))
            except Exception:
                pass
        last_encoder_codec = encoder.actual_codec
        _t_diag = time.monotonic(); _n_diag = 0; _n_drop = 0; _n_nosend = 0
        _last_vnc_fbu = bridge._fbu_count
        # Pipelined encode: start encoding during the rate-limit sleep so that
        # encode time doesn't add to the frame interval.
        _pipe_task = None       # concurrent encode future
        _pipe_cap_ms = 0        # cap_ms captured when pipe_task was started
        _last_encoded_seq = -1  # _fb_seq of last successfully sent frame
        _pipe_enc_seq = -1      # _fb_seq captured when current pipe was started
        _was_static = False     # True when screen has been unchanged this static period
        _static_since = 0.0     # monotonic time when current static period started
        _refresh_br = 0         # bitrate at which last I-frame quality refresh was sent
        _refresh_t = 0.0        # monotonic time of last I-frame quality refresh
        try:
            while True:
                now = time.monotonic()
                if now - _t_diag >= 5.0:
                    dt = now - _t_diag; _t_diag = now
                    _vnc_n = bridge._fbu_count
                    _vnc_fps = (_vnc_n - _last_vnc_fbu) / dt; _last_vnc_fbu = _vnc_n
                    _fb_age = int(time.time() * 1000) - bridge._fb_ms
                    await ws.send(json.dumps({"t": "stale", "ms": _fb_age}))
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

                # Write buffer check — immediate local backpressure.
                # Threshold is fps+bitrate-aware (lag_wb_budget) so a single
                # large frame draining doesn't trigger false congestion at low fps.
                wb = _get_wbuf(ws)
                if wb > 4 * 1024 * 1024:
                    log.warning("write buf %.1fMB — hard kill %s", wb / 1048576, ws.remote_address)
                    try: await ws.close()
                    except Exception: pass
                    break
                if wb > ctrl.lag_wb_budget():
                    ctrl.on_lag(0, wb)
                    _n_drop += 1
                    if _pipe_task is not None:
                        try: await _pipe_task
                        except Exception: pass
                        _pipe_task = None
                    await asyncio.sleep(0.01)
                    continue

                # Static-screen skip: no new content — poll at 1ms so the next
                # frame is detected and encoded within 1-2ms of the subprocess writing
                # it.  Shorter poll than the capture interval keeps encode latency low
                # and `last_send_time` drift from piling up between captures.
                cur_fb_seq = bridge._fb_seq
                if cur_fb_seq == _last_encoded_seq and _pipe_task is None:
                    if not _was_static:
                        _was_static = True
                        _static_since = now
                        _refresh_br = 0   # allow first refresh at any bitrate
                        _refresh_t = 0.0
                    else:
                        fps_s, br_s, jq_s = ctrl.snapshot()
                        # Heartbeat: send a frame every 2s when static so the client
                        # sees cursor movement and confirms the stream is alive (0fps
                        # on a static screen feels broken even when latency is fine).
                        # Also send a higher-quality refresh when bitrate improved >25%.
                        last_refresh_age = now - _refresh_t
                        quality = 95 if br_s > _refresh_br * 1.25 else 75
                        if now - _static_since > 1.0 and last_refresh_age > 2.0:
                            _refresh_t = now
                            if br_s > _refresh_br * 1.25:
                                _refresh_br = br_s
                            fb_s, cms_s = bridge.get_current_frame()
                            if fb_s is not None:
                                encoder.set_bitrate(br_s)
                                try:
                                    payload_s, is_kf_s, codec_s = await loop.run_in_executor(
                                        None, encoder.encode_keyframe, fb_s, cms_s, quality)
                                    if payload_s:
                                        seq_num += 1
                                        hdr_s = _hdr(seq_num, int(time.time() * 1000),
                                                     codec_s, True, len(payload_s))
                                        await ws.send(hdr_s + payload_s)
                                        _n_diag += 1
                                        log.debug("static heartbeat: %dkbps q=%d", br_s // 1000, quality)
                                except Exception as e:
                                    log.warning("heartbeat frame err: %s", e)
                    await asyncio.sleep(0.001)
                    continue

                # Screen just changed — jump to peak bitrate immediately
                if _was_static:
                    _was_static = False
                    _refresh_br = 0
                    ctrl.on_screen_active()
                    last_send_time = time.monotonic() - interval  # skip rate-limit delay

                # Probe quality up — gated internally on _last_slow (no recent backoff)
                ctrl.on_fresh()

                # Pipeline: start encode NOW so it runs concurrently with the rate-limit sleep.
                # Encode takes ~4.5ms; sleep is ~16.7ms — encode finishes well before we wake.
                if _pipe_task is None:
                    fb, cap_ms = bridge.get_current_frame()
                    if fb is not None:
                        encoder.set_bitrate(bitrate)
                        _pipe_cap_ms = cap_ms
                        _pipe_enc_seq = cur_fb_seq
                        _pipe_task = loop.run_in_executor(None, encoder.encode, fb, cap_ms, jq)

                # Rate limit using deadline: last_send_time advances by interval each frame
                # so encode + send time is absorbed and doesn't compound into the next sleep.
                target = last_send_time + interval
                to_sleep = target - time.monotonic()
                if to_sleep > 0.001:
                    await asyncio.sleep(to_sleep)
                    wb = _get_wbuf(ws)
                    if wb > ctrl.lag_wb_budget():
                        ctrl.on_lag(0, wb)
                        _n_drop += 1
                        if _pipe_task is not None:
                            try: await _pipe_task
                            except Exception: pass
                            _pipe_task = None
                        continue

                # Collect encode result — encode ran during sleep, so this is near-instant
                if _pipe_task is None:
                    _pipe_enc_seq = bridge._fb_seq
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

                if _get_wbuf(ws) > ctrl.lag_wb_budget():
                    ctrl.on_lag(0, _get_wbuf(ws))
                    _n_drop += 1
                    continue

                last_send_time = target
                _last_encoded_seq = _pipe_enc_seq
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

    async def ping_monitor():
        """RFC 6455 WebSocket pings as congestion signal.
        Ping frames queue behind video data frames, so rising RTT means the
        TCP send buffer is building — earlier warning than JS age_ms reports."""
        while True:
            await asyncio.sleep(2.0)
            t0 = time.monotonic()
            try:
                pong_waiter = await ws.ping()
                await asyncio.wait_for(pong_waiter, timeout=5.0)
                rtt_ms = (time.monotonic() - t0) * 1000
                ctrl.on_ping_rtt(rtt_ms)
                log.debug("ping rtt=%.1fms metric=%.1fms", rtt_ms, ctrl._metric_rtt)
            except asyncio.TimeoutError:
                log.warning("ping timeout %s — closing stale connection", ws.remote_address)
                try: await ws.close()
                except Exception: pass
                break
            except Exception as e:
                log.debug("ping err: %s", e)
                break

    try:
        await asyncio.gather(frame_sender(), input_reader(), dbg_sender(), ping_monitor())
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
  background:rgba(0,0,0,.7);padding:2px 8px;border-radius:3px;z-index:9;
  cursor:pointer;user-select:none}
#hud.dim{opacity:0.25}
#hud.hide{opacity:0;pointer-events:none}
#cur{position:fixed;width:12px;height:12px;border:2px solid #fff;border-radius:50%;
  pointer-events:none;transform:translate(-50%,-50%);box-shadow:0 0 4px #000;z-index:9}
#cur.dn{border-color:#ff0}
#st{position:fixed;bottom:8px;left:50%;transform:translateX(-50%);color:#aaa;
  font:11px monospace;background:rgba(0,0,0,.7);padding:2px 10px;border-radius:3px;
  pointer-events:none;z-index:9}
#ki{position:fixed;top:0;left:0;width:1px;height:1px;opacity:0;
  pointer-events:none;z-index:-1;resize:none;border:0;padding:0}
/* Dock */
#dock{position:fixed;left:0;top:50%;transform:translateY(-50%);
  background:rgba(0,0,0,.82);color:#eee;font:12px/1.5 monospace;
  border-radius:0 6px 6px 0;z-index:20;user-select:none;overflow:hidden}
#dock-tab{width:16px;min-height:56px;display:flex;align-items:center;
  justify-content:center;cursor:pointer;font-size:10px}
#dock.open #dock-tab{display:none}
#dock-head{display:none;padding:4px 8px 3px;border-bottom:1px solid rgba(255,255,255,.1);
  font-size:10px;color:#888}
#dock.open #dock-head{display:flex;justify-content:space-between}
#dock-body{display:none;padding:5px 8px 8px;min-width:148px}
#dock.open #dock-body{display:block}
.dock-btn{display:block;width:100%;margin:3px 0;padding:5px 7px;
  background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.14);
  border-radius:3px;color:#ccc;cursor:pointer;text-align:left;font:12px monospace}
.dock-btn:hover{background:rgba(255,255,255,.16);color:#fff}
.dock-btn.active{background:rgba(70,180,70,.22);border-color:rgba(70,200,70,.4);color:#8f8}
#sk-menu{display:none;margin-top:3px;padding:3px 0 0;
  border-top:1px solid rgba(255,255,255,.09)}
#sk-menu.show{display:block}
.sk-btn{display:block;width:100%;margin:2px 0;padding:3px 6px;
  background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.09);
  border-radius:2px;color:#aaa;cursor:pointer;text-align:left;font:11px monospace}
.sk-btn:hover{background:rgba(255,255,255,.13);color:#ddd}
#clip-section{margin-top:5px;padding-top:5px;border-top:1px solid rgba(255,255,255,.09)}
#clip-ta{width:100%;height:54px;margin:3px 0;background:rgba(255,255,255,.07);
  border:1px solid rgba(255,255,255,.14);border-radius:3px;color:#ccc;
  font:11px monospace;resize:vertical;padding:3px 5px}
#clip-ta:focus{outline:1px solid rgba(100,180,255,.5);color:#fff}
.clip-row{display:flex;gap:4px;margin:2px 0}
.clip-row button{flex:1;padding:3px 0;font:11px monospace;
  background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.14);
  border-radius:3px;color:#ccc;cursor:pointer}
.clip-row button:hover{background:rgba(255,255,255,.18);color:#fff}
#sk-custom{margin-top:4px;padding-top:4px;border-top:1px solid rgba(255,255,255,.07)}
.mod-row{display:flex;gap:4px;flex-wrap:wrap;margin:3px 0}
.mod-cb{display:flex;align-items:center;gap:2px;color:#aaa;font:11px monospace;cursor:pointer}
.mod-cb input{cursor:pointer;accent-color:#6b6}
#sk-key-input{width:100%;margin:2px 0;padding:3px 5px;background:rgba(255,255,255,.07);
  border:1px solid rgba(255,255,255,.14);border-radius:3px;color:#ccc;font:11px monospace}
#sk-key-input:focus{outline:1px solid rgba(100,180,255,.5);color:#fff}
</style></head><body>
<canvas id="c"></canvas>
<div id="hud">connecting...</div>
<div id="cur"></div>
<div id="st"></div>
<textarea id="ki" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false"></textarea>
<div id="dock" class="closed">
  <div id="dock-tab">&gt;</div>
  <div id="dock-head"><span>Menu</span><span id="dock-close" style="cursor:pointer">[x]</span></div>
  <div id="dock-body">
    <button class="dock-btn" id="btn-fs">Fullscreen</button>
    <button class="dock-btn" id="btn-fit">Fit screen</button>
    <button class="dock-btn active" id="btn-ctrl">Ctrl=Cmd [on]</button>
    <button class="dock-btn" id="btn-sk">Special keys ▾</button>
    <div id="sk-menu">
      <button class="sk-btn" id="sk-spotlight">Cmd+Space (Spotlight)</button>
      <button class="sk-btn" id="sk-appsw">Cmd+Tab (App switch)</button>
      <button class="sk-btn" id="sk-quit">Cmd+Q (Quit)</button>
      <button class="sk-btn" id="sk-esc">Escape</button>
      <button class="sk-btn" id="sk-tab">Tab</button>
      <button class="sk-btn" id="sk-f11">F11</button>
      <button class="sk-btn" id="sk-f12">F12</button>
      <button class="sk-btn" id="sk-cad">Ctrl+Alt+Del</button>
      <button class="sk-btn" id="sk-ctrlc">Ctrl+C (interrupt)</button>
      <button class="sk-btn" id="sk-ctrlz">Ctrl+Z (suspend)</button>
      <button class="sk-btn" id="sk-ctrld">Ctrl+D (EOF)</button>
      <button class="sk-btn" id="sk-zoom-in">Cmd+= (zoom in)</button>
      <button class="sk-btn" id="sk-zoom-out">Cmd+- (zoom out)</button>
      <button class="sk-btn" id="sk-redo">Cmd+Shift+Z (redo)</button>
      <div id="sk-custom">
        <div class="mod-row">
          <label class="mod-cb"><input type="checkbox" id="mod-shift">Shift</label>
          <label class="mod-cb"><input type="checkbox" id="mod-ctrl">Ctrl</label>
          <label class="mod-cb"><input type="checkbox" id="mod-alt">Alt</label>
          <label class="mod-cb"><input type="checkbox" id="mod-cmd">Cmd</label>
        </div>
        <input id="sk-key-input" placeholder="key or F1…F12 then Enter" autocomplete="off" spellcheck="false">
      </div>
    </div>
    <div id="clip-section">
      <div style="color:#666;font:10px monospace;margin-bottom:2px">Clipboard (Mac→here)</div>
      <textarea id="clip-ta" placeholder="Mac clipboard appears here. Edit then Paste↓" spellcheck="false"></textarea>
      <div class="clip-row">
        <button id="clip-paste">Paste on Mac</button>
        <button id="clip-clear">Clear</button>
      </div>
    </div>
  </div>
</div>
<script>
const canvas=document.getElementById('c'),ctx=canvas.getContext('2d',{alpha:false,desynchronized:true});
const hud=document.getElementById('hud'),cur=document.getElementById('cur');
const st=document.getElementById('st'),ki=document.getElementById('ki');

let imgW=1920,imgH=1080,scaleX=1,scaleY=1,ox=0,oy=0;
let ws,wsOpen=false,mBtn=0,fc=0,lastFpsT=performance.now();
let fitMode='fit';       // 'fit' = letterbox, 'cover' = fill/crop
// Default ctrlToMeta ON for Windows/Linux (Ctrl+C/V → Cmd+C/V on Mac).
// Default OFF on macOS browsers — user has a physical Cmd key, no remap needed.
let ctrlToMeta=!navigator.userAgent.includes('Macintosh');
let _ctrlRemapped={};    // tracks in-flight ctrl→meta remap for keyup pairing
const _suppressedKd=new Set(); // keys whose keydown was intercepted; suppress matching keyup
let _suppressReconnect=false,_hiddenTimer=null;  // tab visibility disconnect

// Metrics
let rxBytes=0,rxBps=0;         // WebSocket receive bandwidth
let codecName='';               // current codec name
let ageWindow=[];               // rolling 5s frame-age samples [{ts,age}]
let worstAge5s=0;               // worst-case frame age in last 5s
let staleMs=0;                  // server-reported screen content age (ms)
let hudMode=0;                  // 0=full 1=dim 2=hidden (cycles on click)

hud.addEventListener('click',()=>{
  hudMode=(hudMode+1)%3;
  hud.className=hudMode===1?'dim':hudMode===2?'hide':'';
});

// ---------------------------------------------------------------------------
// Layout
// ---------------------------------------------------------------------------
function resize(){
  const vw=window.innerWidth,vh=window.innerHeight;
  const ar=imgW/imgH,vr=vw/vh;
  let cw,ch;
  if(fitMode==='cover'){
    // Fill window — canvas may extend beyond edges (clipped by overflow:hidden)
    if(ar>vr){ch=vh;cw=vh*ar}else{cw=vw;ch=vw/ar}
  }else{
    // Letterbox — entire image visible with black bars
    if(ar>vr){cw=vw;ch=vw/ar}else{ch=vh;cw=vh*ar}
  }
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
// Lag is reported per received frame, throttled to ≤10/s.
// No 'fresh' message when idle — connection liveness is covered by WS ping/pong.
let _lastLagReport=0;
function startLagReporter(){} // no-op; reporter is inline in handleBinary

const CODEC_NAMES={0:'jpeg',1:'h264',2:'h265',3:'av1'};

function handleBinary(buf){
  if(buf.byteLength<18)return;
  rxBytes+=buf.byteLength;  // bandwidth tracking
  const v=new DataView(buf);
  const seq=v.getUint32(0);
  // capture_ms as two uint32 (avoids BigInt on older browsers)
  const cmsHi=v.getUint32(4),cmsLo=v.getUint32(8);
  const capMs=cmsHi*4294967296+cmsLo;
  const codec=v.getUint8(12),flags=v.getUint8(13);
  const payload=buf.slice(18);
  const age=Date.now()-capMs;
  codecName=CODEC_NAMES[codec]||'?';
  if(age>0){
    // Rolling 5s worst-case age window (for HUD display)
    const now=Date.now();
    ageWindow.push({ts:now,age});
    ageWindow=ageWindow.filter(e=>now-e.ts<5000);
    worstAge5s=ageWindow.length?Math.max(...ageWindow.map(e=>e.age)):0;
    // Per-frame lag report, throttled to ≤10/s
    if(now-_lastLagReport>=100){
      _lastLagReport=now;
      send({t:'lag',age_ms:Math.round(age)});
    }
  }

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
  const dt=(now-lastFpsT)/1000;
  if(dt>=1){
    rxBps=rxBytes*8/dt;  // bits per second
    rxBytes=0;
    const bw=rxBps>=1e6?(rxBps/1e6).toFixed(1)+'Mbps':(rxBps/1e3).toFixed(0)+'Kbps';
    const lag=worstAge5s>0?'lag:'+worstAge5s+'ms ':'';
    const codec=codecName?codecName+' ':'';
    const net=worstMetricRtt5s>0?'net:'+worstMetricRtt5s+'ms ':'';
    const stl=staleMs>2000?'stale:'+(staleMs/1000).toFixed(0)+'s ':'';
    hud.textContent=fc+'fps '+codec+bw+' '+lag+net+stl+imgW+'×'+imgH;
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
    send({t:'reset'});  // release any stuck keys/buttons from previous session
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
    wsOpen=false;mBtn=0;hud.textContent='disconnected';
    if(_suppressReconnect){
      st.textContent='paused (tab hidden)';
    }else{
      st.textContent='reconnecting...';
      setTimeout(connect,2000);
    }
  };
  ws.onerror=()=>{};
  ws.onmessage=e=>{
    if(e.data instanceof ArrayBuffer){
      handleBinary(e.data);
    }else{
      try{
        const msg=JSON.parse(e.data);
        if(msg.t==='stale'){
          staleMs=msg.ms||0;
        }else if(msg.t==='clipboard'){
          // Mac clipboard → dock textarea + browser clipboard
          clipTA.value=msg.text;
          _writeClipboard(msg.text);
          st.textContent='[clipboard] '+msg.text.substring(0,50);
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
// Metric WebSocket — unloaded channel; echoes pings so client measures baseline RTT.
// video_ping_rtt - metric_rtt = pure queuing delay, independent of link latency/roaming.
// ---------------------------------------------------------------------------
let metricWs=null,metricOpen=false,metricRtt=0;
let metricRttWindow=[],worstMetricRtt5s=0;  // rolling 5s worst-case metric RTT

function connectMetric(){
  const token=new URLSearchParams(location.search).get('token')||'';
  const url='ws://'+location.host+'/metric'+(token?'?token='+encodeURIComponent(token):'');
  metricWs=new WebSocket(url);
  metricWs.onopen=()=>{
    metricOpen=true;
    // Send first ping immediately so metric RTT is known before the video channel needs it
    sendMetricPing();
  };
  metricWs.onclose=()=>{metricOpen=false;setTimeout(connectMetric,1000);};
  metricWs.onerror=()=>{};
  metricWs.onmessage=e=>{
    try{
      const msg=JSON.parse(e.data);
      if(msg.t==='ping'&&msg.ts){
        metricRtt=Date.now()-msg.ts;
        const mnow=Date.now();
        metricRttWindow.push({ts:mnow,rtt:metricRtt});
        metricRttWindow=metricRttWindow.filter(e=>mnow-e.ts<5000);
        worstMetricRtt5s=metricRttWindow.length?Math.max(...metricRttWindow.map(e=>e.rtt)):metricRtt;
        send({t:'metric_rtt',rtt_ms:metricRtt});
      }
    }catch(e){}
  };
}
function sendMetricPing(){
  if(metricOpen&&metricWs)metricWs.send(JSON.stringify({t:'ping',ts:Date.now()}));
}
setInterval(sendMetricPing,2000);

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
// window-level mouseup catches releases that happen outside the canvas (drag-out, right-click menus, etc.)
window.addEventListener('mouseup',e=>{
  if(!(mBtn&(1<<e.button)))return;
  mBtn&=~(1<<e.button);cur.classList.remove('dn');
  const[vx,vy]=toVNC(e.clientX,e.clientY);
  send({t:'mu',b:e.button,x:vx,y:vy});
});
canvas.addEventListener('contextmenu',e=>e.preventDefault());
canvas.addEventListener('wheel',e=>{
  const[vx,vy]=toVNC(e.clientX,e.clientY);
  // Normalize to integer click-counts: deltaMode 0=pixels, 1=lines(×40), 2=page(×800)
  const mul=e.deltaMode===1?40:e.deltaMode===2?800:1;
  const norm=v=>v===0?0:Math.sign(v)*Math.max(1,Math.min(8,Math.round(Math.abs(v*mul)/80)));
  const cy=norm(e.deltaY),cx=norm(e.deltaX);
  if(cy||cx)send({t:'sc',x:vx,y:vy,dy:cy,dx:cx});
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
// Keyboard — captured on hidden textarea
// ---------------------------------------------------------------------------

// Send text to Mac by typing it character-by-character (bypasses clipboard sync)
function sendPasteText(text){
  if(text&&wsOpen)send({t:'paste',text});
}

// Write text to the browser's local clipboard.
// Tries the modern async API first; falls back to the deprecated execCommand path
// which works without permission in all major browsers.
function _writeClipboard(text){
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(text).catch(()=>_legacyCopy(text));
  }else{
    _legacyCopy(text);
  }
}
function _legacyCopy(text){
  const ta=document.createElement('textarea');
  ta.value=text;
  ta.style.cssText='position:fixed;left:-9999px;top:-9999px;width:1px;height:1px;opacity:0;';
  document.body.appendChild(ta);
  ta.focus();ta.select();
  try{document.execCommand('copy');}catch(e){}
  document.body.removeChild(ta);
  ki.focus(); // restore VNC keyboard focus
}

// Read browser clipboard and paste to Mac.
// Uses navigator.clipboard.readText() (requires focus+permission or HTTPS).
// Falls back to showing the clipboard textarea so user can paste manually.
async function pasteFromBrowserClipboard(){
  let text='';
  try{
    if(navigator.clipboard&&navigator.clipboard.readText)
      text=await navigator.clipboard.readText();
  }catch(e){}
  if(text){sendPasteText(text);return;}
  // Clipboard API blocked — open dock clipboard textarea for manual paste
  dockOpen(true);
  clipTA.focus();
  clipTA.select();
  st.textContent='Paste your text into the box, then click "Paste on Mac"';
}

ki.addEventListener('keydown',async e=>{
  let code=e.code,key=e.key;
  // Ctrl→Cmd: remap ControlLeft/Right → MetaLeft (Ctrl+A=Cmd+A, Ctrl+C=Cmd+C, etc.)
  if(ctrlToMeta&&!e.metaKey&&(code==='ControlLeft'||code==='ControlRight')){
    _ctrlRemapped[code]='MetaLeft';
    code=key='MetaLeft';
    send({t:'kd',k:key,code:code});
    e.preventDefault(); // prevent browser Ctrl+key shortcuts (zoom, find, etc.)
    return;
  }
  // Ctrl+V / Cmd+V: let the browser fire a native paste event on ki (no permission needed).
  // document paste listener captures e.clipboardData and sends {t:"paste"} to Mac.
  // Crucially: do NOT call e.preventDefault() here — that would block the paste event.
  if((e.ctrlKey||e.metaKey)&&key.toLowerCase()==='v'){
    _suppressedKd.add(e.code); // suppress matching keyup
    return; // don't send V to VNC; paste event handles the rest
  }
  send({t:'kd',k:key,code:code});
  e.preventDefault();
});
ki.addEventListener('keyup',e=>{
  let code=e.code,key=e.key;
  if(_ctrlRemapped[e.code]){code=key=_ctrlRemapped[e.code];delete _ctrlRemapped[e.code];}
  // Suppress keyup for any key whose keydown was intercepted (e.g. V after Ctrl+V paste).
  // Must check e.code (physical key) not key, because modifier state may have changed.
  if(_suppressedKd.has(e.code)){_suppressedKd.delete(e.code);e.preventDefault();return;}
  // Belt-and-suspenders: also suppress if modifier still held (catches cases where
  // the keydown wasn't tracked but modifier is clearly still active).
  if((e.ctrlKey||e.metaKey)&&key.toLowerCase()==='v'){e.preventDefault();return;}
  send({t:'ku',k:key,code:code});
  e.preventDefault();
});

// Document-level paste fallback (when dock or other UI has focus)
document.addEventListener('paste',e=>{
  if(document.activeElement===clipTA)return; // let user paste into clipboard textarea
  const text=(e.clipboardData||window.clipboardData||{}).getData('text/plain');
  if(text&&wsOpen){sendPasteText(text);e.preventDefault();}
});

// Refocus hidden textarea on canvas click and window focus so keyboard events route correctly
canvas.addEventListener('click',()=>ki.focus());
window.addEventListener('focus',()=>ki.focus());

// ---------------------------------------------------------------------------
// Dock UI
// ---------------------------------------------------------------------------
const dock=document.getElementById('dock');
const dockTab=document.getElementById('dock-tab');
function dockOpen(o){
  dock.classList.toggle('open',o);
  dock.classList.toggle('closed',!o);
  dockTab.textContent=o?'<':'>';
}
dockTab.addEventListener('click',()=>dockOpen(true));
document.getElementById('dock-close').addEventListener('click',()=>{dockOpen(false);ki.focus();});

const _LOCK_KEYS=['Escape','Tab','F1','F2','F3','F4','F5','F6','F7','F8','F9','F10','F11','F12'];
document.addEventListener('fullscreenchange',async()=>{
  if(document.fullscreenElement){
    try{if(navigator.keyboard)await navigator.keyboard.lock(_LOCK_KEYS);}catch(e){}
    ki.focus();
  }else{
    try{if(navigator.keyboard)navigator.keyboard.unlock();}catch(e){}
  }
});
document.getElementById('btn-fs').addEventListener('click',()=>{
  if(!document.fullscreenElement)document.documentElement.requestFullscreen().catch(()=>{});
  else document.exitFullscreen().catch(()=>{});
  ki.focus();
});

const btnFit=document.getElementById('btn-fit');
btnFit.addEventListener('click',()=>{
  fitMode=fitMode==='fit'?'cover':'fit';
  btnFit.textContent=fitMode==='cover'?'Fill screen':'Fit screen';
  btnFit.classList.toggle('active',fitMode==='cover');
  resize();ki.focus();
});

const btnCtrl=document.getElementById('btn-ctrl');
// Sync button label to initial auto-detected state
btnCtrl.textContent='Ctrl=Cmd ['+(ctrlToMeta?'on':'off')+']';
btnCtrl.classList.toggle('active',ctrlToMeta);
btnCtrl.addEventListener('click',()=>{
  ctrlToMeta=!ctrlToMeta;
  _ctrlRemapped={};  // clear any in-flight remaps
  _suppressedKd.clear();
  btnCtrl.textContent='Ctrl=Cmd ['+(ctrlToMeta?'on':'off')+']';
  btnCtrl.classList.toggle('active',ctrlToMeta);
  ki.focus();
});

const btnSk=document.getElementById('btn-sk');
const skMenu=document.getElementById('sk-menu');
const clipTA=document.getElementById('clip-ta');
btnSk.addEventListener('click',()=>{
  const show=skMenu.classList.toggle('show');
  btnSk.classList.toggle('active',show);
});

function sendSpecial(pairs){
  for(const[k,down]of pairs)send({t:down?'kd':'ku',k,code:k});
}
document.getElementById('sk-spotlight').addEventListener('click',()=>{
  sendSpecial([['MetaLeft',true],[' ',true],[' ',false],['MetaLeft',false]]);ki.focus();
});
document.getElementById('sk-appsw').addEventListener('click',()=>{
  sendSpecial([['MetaLeft',true],['Tab',true],['Tab',false],['MetaLeft',false]]);ki.focus();
});
document.getElementById('sk-quit').addEventListener('click',()=>{
  sendSpecial([['MetaLeft',true],['q',true],['q',false],['MetaLeft',false]]);ki.focus();
});
document.getElementById('sk-esc').addEventListener('click',()=>{
  sendSpecial([['Escape',true],['Escape',false]]);ki.focus();
});
document.getElementById('sk-tab').addEventListener('click',()=>{
  sendSpecial([['Tab',true],['Tab',false]]);ki.focus();
});
document.getElementById('sk-f11').addEventListener('click',()=>{
  sendSpecial([['F11',true],['F11',false]]);ki.focus();
});
document.getElementById('sk-f12').addEventListener('click',()=>{
  sendSpecial([['F12',true],['F12',false]]);ki.focus();
});
document.getElementById('sk-cad').addEventListener('click',()=>{
  sendSpecial([['ControlLeft',true],['AltLeft',true],['Delete',true],
               ['Delete',false],['AltLeft',false],['ControlLeft',false]]);ki.focus();
});
document.getElementById('sk-ctrlc').addEventListener('click',()=>{
  sendSpecial([['ControlLeft',true],['c',true],['c',false],['ControlLeft',false]]);ki.focus();
});
document.getElementById('sk-ctrlz').addEventListener('click',()=>{
  sendSpecial([['ControlLeft',true],['z',true],['z',false],['ControlLeft',false]]);ki.focus();
});
document.getElementById('sk-ctrld').addEventListener('click',()=>{
  sendSpecial([['ControlLeft',true],['d',true],['d',false],['ControlLeft',false]]);ki.focus();
});
document.getElementById('sk-zoom-in').addEventListener('click',()=>{
  sendSpecial([['MetaLeft',true],['=',true],['=',false],['MetaLeft',false]]);ki.focus();
});
document.getElementById('sk-zoom-out').addEventListener('click',()=>{
  sendSpecial([['MetaLeft',true],['-',true],['-',false],['MetaLeft',false]]);ki.focus();
});
document.getElementById('sk-redo').addEventListener('click',()=>{
  sendSpecial([['MetaLeft',true],['ShiftLeft',true],['z',true],['z',false],['ShiftLeft',false],['MetaLeft',false]]);ki.focus();
});

// Custom key sender: modifier checkboxes + key input
document.getElementById('sk-key-input').addEventListener('keydown',e=>{
  if(e.key!=='Enter')return;
  e.preventDefault();
  const raw=e.target.value.trim();
  if(!raw)return;
  const mods=[];
  if(document.getElementById('mod-shift').checked)mods.push('ShiftLeft');
  if(document.getElementById('mod-ctrl').checked)mods.push('ControlLeft');
  if(document.getElementById('mod-alt').checked)mods.push('AltLeft');
  if(document.getElementById('mod-cmd').checked)mods.push('MetaLeft');
  const dn=mods.map(m=>[m,true]);
  const up=mods.slice().reverse().map(m=>[m,false]);
  sendSpecial([...dn,[raw,true],[raw,false],...up]);
  e.target.value='';
  ki.focus();
});

// Clipboard textarea — paste on Mac button
document.getElementById('clip-paste').addEventListener('click',()=>{
  const text=clipTA.value;
  if(text&&wsOpen){sendPasteText(text);st.textContent='Pasted '+text.length+' chars';}
  ki.focus();
});
document.getElementById('clip-clear').addEventListener('click',()=>{clipTA.value='';ki.focus();});

// Bidirectional sync: when user edits clipTA, update Mac clipboard in real-time (debounced 400ms)
let _clipTATimer=null;
clipTA.addEventListener('input',()=>{
  clearTimeout(_clipTATimer);
  _clipTATimer=setTimeout(()=>{
    if(wsOpen)send({t:'setclip',text:clipTA.value});
  },400);
});

// ---------------------------------------------------------------------------
// Init + visibility-based disconnect
// ---------------------------------------------------------------------------
document.addEventListener('visibilitychange',()=>{
  if(document.hidden){
    // Disconnect after 30s hidden to save bandwidth
    _hiddenTimer=setTimeout(()=>{
      if(document.hidden){
        _suppressReconnect=true;
        if(ws&&wsOpen)ws.close(1000,'tab-idle');
      }
    },30000);
  }else{
    clearTimeout(_hiddenTimer);_hiddenTimer=null;
    if(_suppressReconnect){
      _suppressReconnect=false;
      connect();
    }else if(wsOpen){
      send({t:'reset'});
    }
    ki.focus();
  }
});
window.addEventListener('blur',()=>{
  if(wsOpen)send({t:'reset'});
});
canvas.width=imgW;canvas.height=imgH;resize();connect();connectMetric();
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

async def metric_session(ws):
    """Unloaded ping channel — echoes JSON messages immediately, no video data.
    Client sends {t:'ping',ts:N}, measures round-trip, reports delta to video WS."""
    try:
        async for msg in ws:
            if isinstance(msg, str):
                try:
                    await ws.send(msg)
                except Exception:
                    break
    except Exception:
        pass

def make_ws_handler(cfg, bridge):
    async def handler(ws):
        path = (ws.request.path if hasattr(ws, 'request') else "/").split("?")[0]
        if not _check_token(ws.request.path if hasattr(ws, 'request') else "/", cfg.password):
            await ws.close(1008, "Forbidden")
            return
        if path == "/metric":
            await metric_session(ws)
        else:
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

async def _main(cfg, ds=None):
    from websockets import serve
    vnc = VNCBridge(cfg)
    vnc.start()
    bridge = BridgeProxy(vnc, ds)

    http_handler = make_http_handler(cfg, bridge)
    ws_handler = make_ws_handler(cfg, bridge)

    cap_mode = "SCK" if (ds and ds.is_running()) else "VNC"
    handler = lambda ws: ws_handler(ws)
    log.info("Listening %s:%d  codec=%s  max_fps=%d  capture=%s",
             cfg.listen, cfg.port, cfg.codec, cfg.max_fps, cap_mode)

    async def _sck_retry_loop():
        """Background task: retry InProcessSCKBridge every 30s.
        Activates 60fps capture automatically after user grants Screen Recording permission."""
        await asyncio.sleep(30)
        while True:
            if bridge._d is None or not bridge._d.is_running():
                try:
                    loop = asyncio.get_event_loop()
                    ip = InProcessSCKBridge()
                    ok = await loop.run_in_executor(None, ip.start)
                    if ok:
                        bridge.set_capture(ip)
                        log.info("SCK capture activated via retry — now 60fps")
                except Exception as e:
                    log.debug("SCK retry: %s", e)
            await asyncio.sleep(30)

    async with serve(handler, cfg.listen, cfg.port,
                     process_request=http_handler,
                     max_size=None, compression=None):
        asyncio.create_task(_sck_retry_loop())
        await asyncio.Future()

_COMPOSITOR_KEEPALIVE_SCRIPT = """\
# Keeps macOS's display compositor running at the display refresh rate.
# Without this, WindowServer throttles to ~3Hz on idle screens, causing
# 500ms-3s first-keystroke latency for keyboard echo.
#
# Mechanism: a CVDisplayLink fires at every vblank; each callback commits a
# CALayer change on a near-invisible topmost window. CoreAnimation propagates
# these commits to the WindowServer render server, which must then composite
# the full scene (including Terminal and other windows) on every vblank.
# Position (3,25): between VNC sample-grid points, so the server's content
# hash never changes (no false "screen is updating" signals to the encoder).
import sys, ctypes, time
try:
    import AppKit, Quartz
except ImportError:
    sys.exit(0)

try:
    # --- CVDisplayLink via ctypes (PyObjC closure type unsupported) ---
    cv = ctypes.CDLL("/System/Library/Frameworks/CoreVideo.framework/CoreVideo")
    CBTYPE = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.c_void_p, ctypes.c_uint64,
                               ctypes.POINTER(ctypes.c_uint64), ctypes.c_void_p)
    _i = [0]
    _layer = [None]

    def _vblank(dl, now, out, fin, fout, ctx):
        layer = _layer[0]
        if layer is None:
            return 0
        _i[0] ^= 1
        # Commit a 1-unit opacity change to force render server compositing.
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        layer.setOpacity_(0.002 if _i[0] else 0.001)
        Quartz.CATransaction.commit()
        return 0  # kCVReturnSuccess

    _cb = CBTYPE(_vblank)
    _link = ctypes.c_void_p()
    cv.CVDisplayLinkCreateWithActiveCGDisplays(ctypes.byref(_link))
    cv.CVDisplayLinkSetOutputCallback(_link, _cb, None)

    # --- NSWindow with a CALayer ---
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyProhibited)
    win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        AppKit.NSMakeRect(3, 25, 2, 2),
        AppKit.NSWindowStyleMaskBorderless,
        AppKit.NSBackingStoreBuffered,
        False)
    win.setAlphaValue_(0.002)
    win.setIgnoresMouseEvents_(True)
    win.setOpaque_(False)
    win.setBackgroundColor_(AppKit.NSColor.clearColor())
    win.contentView().setWantsLayer_(True)
    _layer[0] = win.contentView().layer()
    _layer[0].setBackgroundColor_(AppKit.NSColor.whiteColor().CGColor())
    _layer[0].setOpacity_(0.001)
    win.orderFrontRegardless()

    # NSActivityLatencyCritical signals to WindowServer that this process
    # has precision timing needs — prevents compositor throttling for our window.
    _activity = AppKit.NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
        AppKit.NSActivityLatencyCritical | AppKit.NSActivityIdleDisplaySleepDisabled,
        "VNC compositor keepalive"
    )

    cv.CVDisplayLinkStart(_link)

    # Background thread: post a HID-level mouse micro-move every 25 seconds.
    # screensharingd's HID-idle detector only responds to kCGHIDEventTap events
    # (real hardware level), not the kCGSessionEventTap events that VNC injection
    # uses. Without this, screensharingd enters HID-idle after ~30s of no physical
    # activity and stops updating its framebuffer cache.
    import threading as _threading
    def _hid_keepalive():
        while True:
            time.sleep(25)
            try:
                mp = AppKit.NSEvent.mouseLocation()
                sh = AppKit.NSScreen.mainScreen().frame().size.height
                cy = sh - mp.y
                e1 = Quartz.CGEventCreateMouseEvent(
                    None, Quartz.kCGEventMouseMoved,
                    Quartz.CGPoint(mp.x, cy + 1), Quartz.kCGMouseButtonLeft)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, e1)
                e2 = Quartz.CGEventCreateMouseEvent(
                    None, Quartz.kCGEventMouseMoved,
                    Quartz.CGPoint(mp.x, cy), Quartz.kCGMouseButtonLeft)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, e2)
            except Exception:
                pass
    _threading.Thread(target=_hid_keepalive, daemon=True).start()

    app.run()
    cv.CVDisplayLinkStop(_link)
    AppKit.NSProcessInfo.processInfo().endActivity_(_activity)
except Exception:
    sys.exit(0)
"""

def _start_compositor_keepalive():
    """Spawn a tiny subprocess that keeps macOS's display compositor warm at 30fps.
    Falls back silently if the display or tkinter is unavailable (headless, SSH-only)."""
    try:
        import subprocess, threading, time as _time
        proc = subprocess.Popen(
            [sys.executable, "-c", _COMPOSITOR_KEEPALIVE_SCRIPT],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        log.info("compositor keepalive started (PID %d)", proc.pid)
        def _watch():
            _time.sleep(2)
            rc = proc.poll()
            if rc is not None:
                err = proc.stderr.read(500)
                log.warning("compositor keepalive exited rc=%d: %s", rc, err.decode(errors="replace"))
        threading.Thread(target=_watch, daemon=True).start()
    except Exception as e:
        log.warning("compositor keepalive unavailable: %s", e)

def _request_screen_capture_access():
    """Request Screen Recording permission on macOS 13+.
    Initializes NSApp so the system consent dialog can appear from a LaunchAgent context.
    Returns True if already granted; False if the dialog was shown (user must click Allow)
    or if permission is denied."""
    try:
        import ctypes
        cg = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
        cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        cg.CGRequestScreenCaptureAccess.restype   = ctypes.c_bool
        if cg.CGPreflightScreenCaptureAccess():
            return True
        # Initialize NSApp so the system dialog can be presented from a LaunchAgent.
        try:
            from AppKit import NSApplication
            NSApplication.sharedApplication()
        except Exception:
            pass
        result = cg.CGRequestScreenCaptureAccess()
        if not result:
            log.info(
                "Screen Recording: permission not yet granted. "
                "To enable 60fps SCK capture: open System Settings → Privacy & Security → "
                "Screen Recording, enable Python (com.apple.python3), then the server will "
                "automatically switch to 60fps within ~30 seconds."
            )
        return bool(result)
    except Exception as e:
        log.debug("screen capture access request: %s", e)
        return False

def _request_accessibility():
    """Open System Settings → Accessibility if Python isn't trusted yet."""
    try:
        import ctypes
        ax = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        ax.AXIsProcessTrusted.restype = ctypes.c_bool
        if ax.AXIsProcessTrusted():
            log.info("Accessibility: granted")
            return
        log.warning("Accessibility: not granted — open System Settings → Privacy → Accessibility "
                    "and enable Python (com.apple.python3)")
        import subprocess
        subprocess.Popen([
            "open",
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
        ])
    except Exception as e:
        log.debug("accessibility check: %s", e)

def _check_screen_capture():
    """Probe whether SCK (ScreenCaptureKit) screen capture is available and permitted.

    On macOS 15+, CGWindowListCreateImage only captures the desktop wallpaper —
    application windows require SCK with the enhanced Screen Recording permission.
    This probe tests SCK directly: if it can enumerate displays and start a stream,
    full-screen capture (including all windows) is available via DisplayStreamBridge.

    If denied: log guidance and return False so BridgeProxy falls back to VNCBridge.
    If granted: return True and DisplayStreamBridge uses the SCK capture subprocess.
    """
    import subprocess
    _probe = r"""
import sys, time, threading
try:
    from Foundation import NSRunLoop, NSDate, NSDefaultRunLoopMode
    import ScreenCaptureKit as SCK
    _ready = threading.Event()
    _err   = [None]
    def _cb(content, error): _err[0] = error; _ready.set()
    try:
        SCK.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
            False, True, _cb)
    except AttributeError:
        SCK.SCShareableContent.getExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
            False, True, _cb)
    t0 = time.time()
    while not _ready.is_set() and time.time() - t0 < 7:
        NSRunLoop.mainRunLoop().runMode_beforeDate_(NSDefaultRunLoopMode,
                                                   NSDate.dateWithTimeIntervalSinceNow_(0.1))
    sys.exit(1 if _err[0] else 0)
except Exception as e:
    sys.stderr.write(str(e) + '\n')
    sys.exit(2)
"""
    try:
        p = subprocess.run([sys.executable, "-c", _probe], timeout=12, capture_output=True)
        if p.returncode == 0:
            log.info("Screen Recording: SCK permission granted — window capture active")
            return True
        reason = p.stderr.decode(errors='replace').strip()
        if p.returncode == 1:
            log.warning(
                "Screen Recording: SCK permission denied — displaying via VNC fallback (21fps). "
                "To enable 60fps window capture: System Settings > Privacy & Security > "
                "Screen Recording > enable Python (com.apple.python3), then restart the server.")
        else:
            log.warning("Screen Recording: SCK unavailable (%s) — VNC fallback", reason or "unknown")
        return False
    except Exception as e:
        log.debug("SCK screen capture probe failed: %s", e)
        return False

def main():
    cfg = parse_args()
    if not cfg.vnc_pass and not (cfg.macos_user and cfg.macos_pass):
        print("Error: provide --vnc-pass or --macos-user + --macos-pass")
        raise SystemExit(1)
    log.info("Target codec: %s (PyAV: %s)", cfg.codec, "yes" if _AV_OK else "NO — pip install av")
    _request_screen_capture_access()
    _request_accessibility()
    _start_compositor_keepalive()
    if not _check_cg_kb():
        log.warning("CGEvent keyboard: Accessibility not granted — using VNC keysym fallback. "
                    "Enable in System Settings > Privacy > Accessibility.")
        threading.Thread(target=_poll_cg_kb, daemon=True).start()
    ds = None
    # Try in-process SCK first: avoids subprocess permission/session issues on macOS 26.
    ip = InProcessSCKBridge()
    if ip.start():
        ds = ip
        log.info("capture=SCK-inproc")
    elif _check_screen_capture():
        ds = DisplayStreamBridge()
        if not ds.start():
            log.warning("DisplayStreamBridge failed — falling back to VNC capture")
            ds = None
    asyncio.run(_main(cfg, ds))

if __name__=="__main__":
    main()
