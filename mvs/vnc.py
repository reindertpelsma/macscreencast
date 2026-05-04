import hashlib
import logging
import os
import select
import socket
import struct
import subprocess
import threading
import time
import zlib

import numpy as np

log = logging.getLogger("macvnc")

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
# _active_clients is read inside _run() below; import at module level so the
# name resolves in module globals (not a closure).
import mvs.handler as _handler_mod


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

    def _restart_screensharingd(self) -> bool:
        """Kill and restart screensharingd using the macOS login password from config.
        Blocks until port 5900 reopens (≤15s) or gives up. Returns True on success."""
        pw = getattr(self._cfg, 'macos_pass', '')
        if not pw:
            log.warning("screensharingd restart: no macos_pass configured — skipping")
            return False
        log.warning("Restarting screensharingd…")
        try:
            subprocess.run(
                ['sudo', '-S', 'launchctl', 'kickstart', '-k',
                 'system/com.apple.screensharing'],
                input=(pw + '\n').encode(),
                capture_output=True, timeout=10)
        except Exception as e:
            log.warning("screensharingd restart command failed: %s", e)
            return False
        host = getattr(self._cfg, 'vnc_host', '127.0.0.1')
        port = getattr(self._cfg, 'vnc_port', 5900)
        for _ in range(15):
            time.sleep(1)
            try:
                s = socket.socket(); s.settimeout(1)
                s.connect((host, port)); s.close()
                log.info("screensharingd restarted — port %d open", port)
                return True
            except Exception:
                pass
        log.warning("screensharingd restart: port %d still closed after 15s", port)
        return False

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
                    if _handler_mod._active_clients == 0:
                        time.sleep(0.2)  # no viewers — stop polling screensharingd to save CPU/battery
                        self._flush_input()
                        continue
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
                        # Watchdog: if screensharingd stopped sending FBUs for 30s,
                        # treat it as a hard hang and restart if manage_screensharingd=True.
                        if getattr(self._cfg, 'manage_screensharingd', False):
                            if now_ms - _last_fbu_ms > 30_000:
                                log.warning("VNC: no FBU for 30s — screensharingd appears hung")
                                self._restart_screensharingd()
                                break  # reconnect outer loop will re-connect VNC
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

    def _screensharingd_pid_watcher(self):
        """Poll screensharingd process existence every 5s.
        If the process dies, restart screensharingd immediately — much faster
        than waiting for the 30s FBU-stall watchdog in _run().
        Only runs when manage_screensharingd=True."""
        while True:
            time.sleep(5)
            if not getattr(self._cfg, 'manage_screensharingd', False):
                continue
            try:
                r = subprocess.run(['pgrep', '-x', 'screensharingd'],
                                   capture_output=True, timeout=2)
                if r.returncode != 0:
                    log.warning("screensharingd process not found — restarting")
                    self._restart_screensharingd()
            except Exception:
                pass

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._pbpaste_poll, daemon=True).start()
        threading.Thread(target=self._screensharingd_pid_watcher,
                         daemon=True, name="ssd-pid-watcher").start()
        threading.Thread(target=self._vnc_keepwarm,
                         daemon=True, name="vnc-keepwarm").start()

    def _vnc_keepwarm(self):
        """Send a no-op VNC pointer move every 25s to keep screensharingd's
        framebuffer cache warm.

        screensharingd has an HID-idle path that stops updating its internal
        framebuffer cache after ~30s of no input. When that happens, the
        first user input takes 500ms-3s to wake it up, and the captured
        frame is stale for that whole window. The compositor-keepalive
        subprocess (mvs.keepalive) handles this via kCGHIDEventTap from a
        GUI session, but in LaunchDaemon mode (no Aqua session) that
        subprocess can't run.

        This thread sends pointer events through the VNC protocol itself
        — same connection screensharingd already accepts input on — every
        25s. Move pointer +1px, then back. screensharingd treats it as
        real input and resets its HID-idle timer. No visible cursor jump
        because both moves happen in the same frame from screensharingd's
        perspective."""
        import time as _time
        while True:
            _time.sleep(25)
            try:
                with self._lock:
                    if not self._input_q and self._sock is not None:
                        # Only nudge when no real input is pending — don't
                        # interleave with active user typing/clicking.
                        x, y = self._last_ptr_x or 1, self._last_ptr_y or 1
                        self._input_q.append(struct.pack("!BBHH", 5, 0, x + 1, y))
                        self._input_q.append(struct.pack("!BBHH", 5, 0, x, y))
            except Exception as e:
                log.debug("vnc keepwarm: %s", e)
