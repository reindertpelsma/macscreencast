#!/usr/bin/env python3
"""
mac-vnc-stream: Adaptive H.264/H.265/JPEG macOS remote desktop in your browser over SSH.

python server.py --vnc-pass PASSWORD
python server.py --macos-user u --macos-pass p  # full control (macOS 15+)
ssh -L 6081:localhost:6081 user@mac && open http://localhost:6081
"""
import argparse
import asyncio
import logging
import os
import sys
import threading

log = logging.getLogger("macvnc")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from mvs.vnc import VNCBridge
from mvs.sck import InProcessSCKBridge, DisplayStreamBridge, TCCWatcher
from mvs.capture import BridgeProxy
from mvs.handler import make_http_handler, make_ws_handler
from mvs.cgevent import _check_cg_kb, _poll_cg_kb
import mvs.cgevent as _cge
from mvs.keepalive import (_start_compositor_keepalive, _request_screen_capture_access,
                            _request_accessibility, _check_screen_capture)
from mvs.codec import _AV_OK


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

    # Capture / input mode selection.
    #
    # Default = native APIs only (sck + cgevent), no screensharingd contact.
    # This matches the production --api-only behaviour: people running
    # `python3 server.py` directly almost always want SCK and have grants set
    # up; they do NOT want a daemon silently establishing a VNC connection
    # to screensharingd in the background. To opt back into the VNC fallback
    # for the headless-cloud-Mac bootstrap case (or to use the bundle as a
    # display warmer), pass --enable-vnc-fallback explicitly. setup.sh adds
    # that flag automatically when the user provides a macOS password.
    p.add_argument("--capture", choices=["auto","sck","vnc"],
                   default=os.environ.get("CAPTURE_MODE","sck"),
                   help="Screen capture mode (default: sck — VNC requires --enable-vnc-fallback)")
    p.add_argument("--input", choices=["auto","cgevent","vnc"],
                   default=os.environ.get("INPUT_MODE","cgevent"),
                   help="Input mode (default: cgevent — VNC requires --enable-vnc-fallback)")
    # Convenience shortcuts
    p.add_argument("--vnc-only", action="store_true",
                   help="Force VNC for both capture and input (sets --capture vnc --input vnc)")
    p.add_argument("--api-only", action="store_true",
                   help="(Now the default; kept as a no-op alias for backwards compatibility)")
    p.add_argument("--enable-vnc-fallback", action="store_true",
                   help="Opt-in: try SCK/CGEvent first, fall back to VNC. setup.sh sets this "
                        "automatically when the user provides a macOS password (headless cloud Macs).")
    # screensharingd lifecycle management
    p.add_argument("--manage-screensharingd", action="store_true", default=None,
                   help="Auto-restart screensharingd when VNC stalls (default: on when macos_pass is set and VNC is needed)")
    p.add_argument("--no-manage-screensharingd", action="store_true",
                   help="Disable auto-management of screensharingd")
    p.add_argument("--tcc-check", action="store_true",
                   help="Probe Screen Recording + Accessibility TCC for this bundle "
                        "and exit 0 if both granted, 1 otherwise. Used by setup.sh "
                        "to decide whether the keep-existing-bundle path needs the "
                        "VNC bootstrap fallback (= grants missing).")
    p.add_argument("--vnc-prime", action="store_true",
                   help="Brief VNC handshake against 127.0.0.1:5900 with --macos-user "
                        "and --macos-pass. screensharingd authenticates the user and, "
                        "as a side effect, creates the gui/$UID Aqua session — needed "
                        "before launchctl bootstrap gui/$UID can succeed on a Mac with "
                        "no active console session. Exits cleanly after handshake; does "
                        "not open ports or start the server. Used by setup.sh on "
                        "first-install on remote-only Macs (Scaleway pattern).")

    args = p.parse_args()

    # --tcc-check short-circuit. Runs before any heavy initialization so it's
    # cheap to invoke and doesn't open ports / start threads / contact
    # screensharingd. The mere act of running this binary registers our bundle
    # with TCC, which is exactly what we want as a side effect — first-run
    # registration so the bundle id appears in System Settings.
    if args.tcc_check:
        import sys as _sys, ctypes as _ct, time as _time, threading as _th
        try:
            ax = _ct.cdll.LoadLibrary(
                "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
            ax.AXIsProcessTrusted.restype = _ct.c_bool

            # Screen Recording: probe via SCShareableContent — the SAME kernel
            # TCC path that SCK uses at runtime. CGPreflightScreenCaptureAccess
            # and CGWindowListCreateImage walk the responsible-app chain (on GH
            # macos-latest runners /bin/bash is pre-granted, so those return
            # True via inheritance even though our bundle's row is auth_value=0
            # → kernel SCStream still fails -3801). SCShareableContent uses the
            # strict path that ignores parent inheritance.
            #
            # Tahoe note: importing via objc.loadBundle does NOT register the
            # completion-handler block signature, so the call fails with
            # "Argument 4 is a block, but no signature available". The
            # pyobjc-framework-ScreenCaptureKit Python module includes the
            # auto-generated bridging metadata (block signatures included),
            # so we import that way instead. py2app bundles this framework
            # already (see setup.sh dependencies).
            from Foundation import NSRunLoop, NSDate, NSDefaultRunLoopMode
            from ScreenCaptureKit import SCShareableContent  # noqa: F401

            _ev = _th.Event()
            _err = [None]
            _content = [None]
            def _cb(content, err):
                _content[0] = content
                _err[0] = err
                _ev.set()
            try:
                SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
                    False, True, _cb)
            except (NameError, AttributeError):
                SCShareableContent.getExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
                    False, True, _cb)
            t0 = _time.time()
            while not _ev.is_set() and _time.time() - t0 < 5.0:
                NSRunLoop.mainRunLoop().runMode_beforeDate_(
                    NSDefaultRunLoopMode,
                    NSDate.dateWithTimeIntervalSinceNow_(0.1))
            sr_ok = (_ev.is_set() and _err[0] is None and _content[0] is not None)

            ax_ok = bool(ax.AXIsProcessTrusted())
            print("screen_recording=" + ("1" if sr_ok else "0"))
            print("accessibility=" + ("1" if ax_ok else "0"))
            _sys.exit(0 if (sr_ok and ax_ok) else 1)
        except Exception as e:
            print("tcc_check_error=" + str(e))
            _sys.exit(2)

    # --vnc-prime short-circuit. Authenticates against screensharingd's
    # AppleVNC server using Apple Diffie-Hellman (RFB security type 30).
    # Side effect: screensharingd creates the gui/$UID Aqua session for the
    # authenticated user, which is the prerequisite for setup.sh's later
    # `launchctl bootstrap gui/$UID` to succeed on a Mac with no active
    # console session (Scaleway / cloud-Mac fresh-install pattern, where
    # the user has only SSH access — no physical login, no prior VNC
    # client, so gui/$UID didn't exist yet).
    #
    # Exits cleanly after handshake. Does not open ports, does not start
    # the server, does not change capture/input mode. Pure session-prime.
    if args.vnc_prime:
        import sys as _sys, socket as _sock, struct as _st, hashlib as _h, os as _os
        try:
            user = args.macos_user
            pw = args.macos_pass
            if not user or not pw:
                print("vnc_prime_error=missing_macos_user_or_macos_pass")
                _sys.exit(2)

            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.backends import default_backend

            def _recv_all(s, n):
                buf = b""
                while len(buf) < n:
                    c = s.recv(n - len(buf))
                    if not c:
                        raise ConnectionError("VNC closed mid-handshake")
                    buf += c
                return buf

            s = _sock.create_connection(("127.0.0.1", 5900), timeout=10)
            s.settimeout(10)
            _recv_all(s, 12)                      # server protocol version
            s.send(b"RFB 003.008\n")              # client protocol version
            n = _recv_all(s, 1)[0]
            if n == 0:
                msg_len = _st.unpack("!I", _recv_all(s, 4))[0]
                reason = _recv_all(s, msg_len).decode("utf-8", "replace")
                print(f"vnc_prime_error=server_rejected: {reason}")
                _sys.exit(1)
            types = list(_recv_all(s, n))

            # Apple DH (type 30) — full-control auth, creates gui/$UID
            # for the authenticated user. Required on macOS 15+; the
            # legacy VNC password (type 2) is read-only on modern macOS.
            if 30 not in types:
                print(f"vnc_prime_error=apple_dh_unavailable: {types}")
                _sys.exit(1)
            s.send(bytes([30]))
            g = _st.unpack("!H", _recv_all(s, 2))[0]
            kl = _st.unpack("!H", _recv_all(s, 2))[0]
            prime = int.from_bytes(_recv_all(s, kl), "big")
            spub = int.from_bytes(_recv_all(s, kl), "big")
            cpriv = int.from_bytes(_os.urandom(kl), "big") % (prime - 2) + 1
            cpub = pow(g, cpriv, prime)
            shared = pow(spub, cpriv, prime)
            aes_key = _h.md5(shared.to_bytes(kl, "big")).digest()
            payload = (user.encode("utf-8")[:64].ljust(64, b"\x00")
                       + pw.encode("utf-8")[:64].ljust(64, b"\x00"))
            enc = Cipher(algorithms.AES(aes_key), modes.ECB(),
                         backend=default_backend()).encryptor()
            ciphertext = enc.update(payload) + enc.finalize()
            s.send(ciphertext + cpub.to_bytes(kl, "big"))
            status = _st.unpack("!I", _recv_all(s, 4))[0]
            if status != 0:
                print(f"vnc_prime_error=auth_failed: status={status}")
                _sys.exit(1)
            # Auth succeeded. Send ClientInit and briefly receive ServerInit
            # to ensure screensharingd has fully spawned AppleVNCServer for
            # this user (the spawn is what creates gui/$UID).
            s.send(b"\x01")  # shared=1
            # Receive ServerInit (24+ bytes: framebuffer dims, pixel format, name length + name).
            try:
                fb_dims = _recv_all(s, 4)
                _recv_all(s, 16)  # pixel format
                name_len = _st.unpack("!I", _recv_all(s, 4))[0]
                if 0 < name_len < 1024:
                    _recv_all(s, name_len)
            except Exception:
                pass
            # Send SetEncodings + FramebufferUpdateRequest so screensharingd
            # treats us as a real client, not a connect-and-disconnect probe.
            try:
                # SetEncodings (msg=2): Raw (0), CopyRect (1) — minimal but valid
                s.send(_st.pack("!BBHii", 2, 0, 2, 0, 1))
                # FramebufferUpdateRequest (msg=3): incremental=0, x=0,y=0,w=64,h=64
                s.send(_st.pack("!BBHHHH", 3, 0, 0, 0, 64, 64))
            except Exception:
                pass
            print("vnc_prime_ok=apple_dh_auth")
            _sys.stdout.flush()
            # Stay connected so screensharingd has time to fully spawn
            # AppleVNCServer + register gui/$UID with launchd. Disconnecting
            # immediately after auth was insufficient — observed live on
            # Scaleway, gui/$UID still wasn't available 2 seconds after
            # vnc_prime_ok, causing the subsequent launchctl bootstrap retry
            # to fail with the same rc=125 error.
            #
            # Daemon mode: setup.sh runs us in the background and reads
            # vnc_prime_ok from our stdout, then proceeds with bootstrap
            # while we keep the VNC session alive. Once the bundle's own
            # LaunchAgent is up with --enable-vnc-fallback, ITS VNC bridge
            # takes over and we can exit. Setup.sh kills us on success.
            #
            # SIGTERM handling: when setup.sh kills us, exit cleanly.
            import signal as _sig
            def _bye(*_):
                try: s.close()
                except Exception: pass
                _sys.exit(0)
            _sig.signal(_sig.SIGTERM, _bye)
            _sig.signal(_sig.SIGINT, _bye)
            # Keep draining incoming frames so screensharingd's send buffer
            # doesn't fill up and stall. 5 minute hard cap as safety —
            # setup.sh's bootstrap should finish in <30s on any sane Mac.
            import time as _t
            _deadline = _t.time() + 300
            s.settimeout(2)
            while _t.time() < _deadline:
                try:
                    chunk = s.recv(65536)
                    if not chunk:
                        break  # server closed
                except _sock.timeout:
                    # Periodically poke screensharingd to keep the session alive
                    try:
                        s.send(_st.pack("!BBHHHH", 3, 1, 0, 0, 64, 64))
                    except Exception:
                        break
                except Exception:
                    break
            try: s.close()
            except Exception: pass
            _sys.exit(0)
        except Exception as e:
            print("vnc_prime_error=" + str(e).replace("\n", " "))
            _sys.exit(2)

    # Apply shortcuts.
    if args.vnc_only:
        args.capture = "vnc"; args.input = "vnc"
    elif args.enable_vnc_fallback:
        # Opt-in: enable the SCK→VNC and CGEvent→VNC auto-fallback paths.
        # Without this flag we run native-API-only; with it we behave like
        # the legacy "auto" defaults.
        args.capture = "auto"; args.input = "auto"
    # --api-only is now a no-op (default behaviour). Honour it harmlessly:
    if args.api_only:
        args.capture = "sck"; args.input = "cgevent"

    # Resolve manage_screensharingd: explicit flags first, then auto-enable
    # when VNC is needed and we have the macOS password to do the restart.
    if args.no_manage_screensharingd:
        args.manage_screensharingd = False
    elif args.manage_screensharingd is None:
        vnc_needed = (args.capture in ("auto","vnc") or args.input in ("auto","vnc"))
        args.manage_screensharingd = bool(vnc_needed and args.macos_pass)

    # api_only attribute is still consumed by elsewhere in the codebase
    # (server.py:_start_compositor_keepalive guard, etc.). Set it based on
    # the resolved capture/input modes.
    args.api_only = (args.capture == "sck" and args.input == "cgevent")

    return args


async def _main(cfg, ds=None, vnc=None):
    from websockets import serve

    if vnc is None:
        vnc = VNCBridge(cfg)
        vnc.start()
    bridge = BridgeProxy(vnc, ds)

    http_handler = make_http_handler(cfg, bridge)
    ws_handler = make_ws_handler(cfg, bridge)

    cap_mode = "SCK" if (ds and ds.is_running()) else "VNC"
    handler = lambda ws: ws_handler(ws)
    log.info("Listening %s:%d  codec=%s  max_fps=%d  capture=%s  input=%s  manage_ssd=%s",
             cfg.listen, cfg.port, cfg.codec, cfg.max_fps, cap_mode,
             "CGEvent" if _cge._cg_kb_ok else "VNC",
             cfg.manage_screensharingd)

    loop = asyncio.get_event_loop()

    async def _sck_upgrade():
        """Try to activate SCK capture. Called from TCC watcher or retry loop."""
        if bridge._d is not None and bridge._d.is_running():
            return
        try:
            ip = InProcessSCKBridge()
            ok = await loop.run_in_executor(None, ip.start)
            if ok:
                bridge.set_capture(ip)
                log.info("SCK capture activated — now 60fps")
        except Exception as e:
            log.debug("SCK upgrade attempt: %s", e)

    async def _sck_retry_loop():
        """Retry SCK every 30s — activates automatically after Screen Recording is granted."""
        await asyncio.sleep(30)
        while True:
            if cfg.capture != "vnc":
                await _sck_upgrade()
            await asyncio.sleep(30)

    # TCC watcher: fires on TCC.db mtime change. Does not interpret the DB —
    # we re-probe each capability live and act only when the result changes.
    was_cg_ok = _cge._cg_kb_ok

    def _on_tcc_change():
        nonlocal was_cg_ok
        # Accessibility / CGEvent input
        if cfg.input != "vnc":
            now_cg = _check_cg_kb()
            if now_cg and not was_cg_ok:
                log.info("TCC: Accessibility granted — CGEvent keyboard+mouse now active")
            elif not now_cg and was_cg_ok:
                log.warning("TCC: Accessibility revoked — input falling back to VNC")
            was_cg_ok = now_cg
        # Screen Recording / SCK capture — only try upgrade if not already running
        if cfg.capture != "vnc" and (bridge._d is None or not bridge._d.is_running()):
            asyncio.run_coroutine_threadsafe(_sck_upgrade(), loop)

    TCCWatcher(on_tcc_change=_on_tcc_change).start()

    async with serve(handler, cfg.listen, cfg.port,
                     process_request=http_handler,
                     max_size=None, compression=None):
        asyncio.create_task(_sck_retry_loop())
        await asyncio.Future()


def main():
    cfg = parse_args()
    log.info("Target codec: %s (PyAV: %s)", cfg.codec, "yes" if _AV_OK else "NO — pip install av")
    log.info("Mode: capture=%s  input=%s  manage_screensharingd=%s",
             cfg.capture, cfg.input, cfg.manage_screensharingd)

    if cfg.password:
        log.info("─" * 60)
        log.info("Token:  %s", cfg.password)
        log.info("URL:    http://localhost:%d/?token=%s", cfg.port, cfg.password)
        log.info("SSH:    ssh -L %d:localhost:%d user@<host>", cfg.port, cfg.port)
        log.info("─" * 60)

    # SIP status — informational only. With SIP disabled TCC enforcement is
    # bypassed: SCK and CGEvent APIs work regardless of TCC.db contents, and
    # TCCWatcher may never fire (DB doesn't change). The 30s retry loop and the
    # direct API probes below handle this correctly without any special casing.
    try:
        import subprocess as _sp
        _sip = _sp.run(["/usr/bin/csrutil", "status"],
                       capture_output=True, text=True, timeout=3)
        if "disabled" in _sip.stdout:
            log.info("SIP disabled — TCC enforcement bypassed; "
                     "probing APIs directly without consulting TCC.db")
    except Exception:
        pass

    _request_screen_capture_access()
    _request_accessibility()
    # Compositor keepalive only matters for the VNC capture path (it keeps
    # screensharingd's framebuffer cache warm). When running --api-only with
    # SCK, SCStream delivers frames on its own schedule independent of the
    # compositor refresh — the keepalive is unnecessary, AND it can't run at
    # all from inside a py2app bundle (the embedded python binary's
    # @executable_path-relative rpath breaks when subprocess.Popen'd).
    if not cfg.api_only:
        _start_compositor_keepalive()

    # Resolve initial CGEvent availability
    if not _check_cg_kb():
        if cfg.input == "cgevent":
            log.warning("CGEvent input requested but Accessibility not granted — "
                        "will retry automatically when permission is granted.")
        else:
            log.warning("Accessibility not granted — input falls back to VNC. "
                        "Enable in System Settings > Privacy > Accessibility.")
        if cfg.input != "vnc":
            threading.Thread(target=_poll_cg_kb, daemon=True).start()

    # Resolve initial screen capture
    ds = None
    if cfg.capture != "vnc":
        ip = InProcessSCKBridge()
        if ip.start():
            ds = ip
            log.info("capture=SCK-inproc")
        elif _check_screen_capture():
            ds = DisplayStreamBridge()
            if not ds.start():
                log.warning("DisplayStreamBridge failed — falling back to VNC capture")
                ds = None

    # Start VNC only if it might be needed for capture or input.
    # In --api-only mode (SCK + CGEvent) we skip VNCBridge entirely so
    # screensharingd is never touched.
    vnc = None
    vnc_needed = (
        (cfg.capture in ("auto", "vnc") and ds is None) or
        (cfg.input in ("auto", "vnc") and not _cge._cg_kb_ok)
    )
    if vnc_needed or cfg.capture == "vnc" or cfg.input == "vnc":
        if not cfg.vnc_pass and not (cfg.macos_user and cfg.macos_pass):
            log.error("VNC needed but no credentials — provide --vnc-pass or "
                      "--macos-user + --macos-pass")
            raise SystemExit(1)
        vnc = VNCBridge(cfg)
        vnc.start()
    else:
        log.info("VNC not needed — screensharingd will not be contacted")
        # Still need a VNCBridge for clipboard polling (pbpaste) even if
        # not used for capture/input — create a minimal one without _run().
        vnc = VNCBridge(cfg)
        threading.Thread(target=vnc._pbpaste_poll, daemon=True).start()

    asyncio.run(_main(cfg, ds, vnc))


if __name__ == "__main__":
    main()
