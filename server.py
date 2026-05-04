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
                            _request_accessibility, _check_screen_capture,
                            start_console_user_watcher, _trigger_sck_tcc_registration,
                            maybe_promote_to_launchagent)
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

    # Capture / input mode selection
    p.add_argument("--capture", choices=["auto","sck","vnc"],
                   default=os.environ.get("CAPTURE_MODE","auto"),
                   help="Screen capture mode: auto=SCK with VNC fallback, sck=SCK only, vnc=VNC only")
    p.add_argument("--input", choices=["auto","cgevent","vnc"],
                   default=os.environ.get("INPUT_MODE","auto"),
                   help="Input mode: auto=CGEvent with VNC fallback, cgevent=CGEvent only, vnc=VNC only")
    # Convenience shortcuts
    p.add_argument("--vnc-only", action="store_true",
                   help="Force VNC for both capture and input (sets --capture vnc --input vnc)")
    p.add_argument("--api-only", action="store_true",
                   help="Force native APIs only — no VNC at all (sets --capture sck --input cgevent)")
    # screensharingd lifecycle management
    p.add_argument("--manage-screensharingd", action="store_true", default=None,
                   help="Auto-restart screensharingd when VNC stalls (default: on when macos_pass is set and VNC is needed)")
    p.add_argument("--no-manage-screensharingd", action="store_true",
                   help="Disable auto-management of screensharingd")

    args = p.parse_args()

    # Apply shortcuts
    if args.vnc_only:
        args.capture = "vnc"; args.input = "vnc"
    if args.api_only:
        args.capture = "sck"; args.input = "cgevent"

    # Resolve manage_screensharingd: explicit flags first, then auto-enable
    # when VNC is needed and we have the macOS password to do the restart.
    if args.no_manage_screensharingd:
        args.manage_screensharingd = False
    elif args.manage_screensharingd is None:
        vnc_needed = (args.capture in ("auto","vnc") or args.input in ("auto","vnc"))
        args.manage_screensharingd = bool(vnc_needed and args.macos_pass)

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
        # LaunchDaemon → LaunchAgent self-promotion. When SCK is granted AND
        # gui/$UID accepts a bootstrap (i.e. user has logged in via VNC), the
        # daemon writes a LaunchAgent plist + spawns a detached helper to
        # migrate, then exits. No-op when not running as a daemon (see
        # maybe_promote_to_launchagent for preconditions).
        from mvs.keepalive import _find_console_uid
        uid = _find_console_uid()
        if uid is not None and maybe_promote_to_launchagent(uid):
            import sys as _sys
            log.info("Promotion scheduled — daemon exiting in 2s for clean migration")
            loop.call_later(1.5, lambda: _sys.exit(0))

    TCCWatcher(on_tcc_change=_on_tcc_change).start()

    # Console-user watcher: fires when a user logs in (e.g. VNC login on a
    # headless cloud Mac). On login, trigger an SCShareableContent probe via
    # `launchctl asuser <uid>` so TCC registers the Python binary in the
    # Screen Recording list — the user can then enable it with one click
    # instead of having to '+ Add' it by path.
    def _on_user_login(uid):
        _trigger_sck_tcc_registration()
    start_console_user_watcher(_on_user_login)

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
