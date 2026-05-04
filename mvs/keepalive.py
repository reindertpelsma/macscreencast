import logging
import os
import sys
import threading

log = logging.getLogger("macvnc")

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

    # NOTE: a previous version posted an HID-level mouse micro-move every 25s
    # to keep screensharingd's HID-idle detector awake. That was removed
    # because (a) it teleported the cursor to (0,0) on headless cloud Macs
    # where NSEvent.mouseLocation() returns origin, and (b) the same job is
    # done more reliably by VNCBridge._vnc_keepwarm in mvs/vnc.py, which
    # uses VNC pointer events that don't move the visible cursor.

    app.run()
    cv.CVDisplayLinkStop(_link)
    AppKit.NSProcessInfo.processInfo().endActivity_(_activity)
except Exception:
    sys.exit(0)
"""


def _start_compositor_keepalive():
    """Spawn a tiny subprocess that keeps macOS's display compositor warm at 30fps.
    Falls back silently if the display or tkinter is unavailable (headless, SSH-only).

    SKIPS spawn entirely if gui/$UID isn't available: on Macs without an
    Aqua session (cloud-Mac fresh-install pattern), Launch Services fails
    to spawn this subprocess and surfaces a "Launch failed — see the
    py2app website for debugging launch issues" alert dialog in the
    macOS GUI, which spams the user's VNC view. The keepalive is a
    nice-to-have for SCK compositor throttling — when it can't run, just
    silently skip it instead of producing user-visible alert noise.
    """
    try:
        import subprocess, threading, time as _time
        # gui/$UID precheck — skip if the domain doesn't exist. The
        # subprocess we'd spawn needs Aqua services (NSWindow, CALayer);
        # without gui/$UID, launchd refuses to launch it AND surfaces a
        # user-visible alert in the GUI for each retry.
        try:
            uid = os.getuid()
            r = subprocess.run(
                ["launchctl", "print", f"gui/{uid}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=2,
            )
            if r.returncode != 0:
                log.info("compositor keepalive: skipping (gui/%d not available — "
                         "Aqua session not created yet)", uid)
                return
        except Exception:
            # If the precheck itself fails (timeout, etc), proceed
            # optimistically rather than blocking the bundle's startup.
            pass
        # Inside a py2app .app bundle, sys.executable is .app/Contents/MacOS/python
        # whose dyld dependencies use @executable_path-relative references to
        # the embedded Python3.framework. When spawned with no cwd, dyld may
        # resolve those against the parent's cwd instead of the binary's own
        # location and fail with 'Library not loaded: @executable_path/...'.
        # Pinning cwd to the binary's own directory keeps the relative paths
        # consistent. No effect outside a bundle.
        proc = subprocess.Popen(
            [sys.executable, "-c", _COMPOSITOR_KEEPALIVE_SCRIPT],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=os.path.dirname(sys.executable) or None,
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
            import sys as _sys, os as _os
            py = _sys.executable
            log.info(
                "Screen Recording: permission not yet granted.\n"
                "  To enable 60 fps SCK capture, open System Settings → Privacy & Security →\n"
                "  Screen Recording. If 'Python' is already listed, toggle it ON.\n"
                "  If Python is NOT listed (common when the server runs as a LaunchDaemon —\n"
                "  the system context can't auto-register Python with TCC), click the '+'\n"
                "  button at the bottom-left of the pane and navigate to the Python binary:\n"
                "    %s\n"
                "  Hint: in the file picker press Cmd+Shift+G and paste the path above.\n"
                "  After granting, the server detects the change within ~30 s and switches\n"
                "  to 60 fps. No restart needed.",
                py,
            )
            # Belt-and-suspenders: open the Privacy → Screen Recording pane via
            # `open`. CGRequestScreenCaptureAccess()'s in-process dialog needs
            # an active GUI session to render — it silently no-ops in
            # LaunchDaemon / non-Aqua contexts, which is exactly the cloud-Mac
            # case where this matters most. `open` triggers a UI process via
            # launchd; the pane appears on the Mac's display and is visible to
            # whoever's connected via VNC.
            #
            # The URL anchor changed across macOS releases:
            #   - macOS 12-14: Privacy_ScreenCapture
            #   - macOS 15-26: Privacy_ScreenCapture *or* Privacy_ScreenRecording
            #     depending on minor version
            # Try both anchors, then fall back to the top-level Privacy &
            # Security pane (no anchor) — that works on every macOS version
            # but requires the user to scroll to find Screen Recording.
            try:
                import subprocess
                for anchor in ("Privacy_ScreenCapture",
                               "Privacy_ScreenRecording",
                               ""):  # final fallback: pane root, no anchor
                    url = "x-apple.systempreferences:com.apple.preference.security"
                    if anchor:
                        url += "?" + anchor
                    subprocess.Popen(["open", url])
            except Exception:
                pass
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
        p = subprocess.run([sys.executable, "-c", _probe], timeout=12,
                           capture_output=True,
                           cwd=os.path.dirname(sys.executable) or None)
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
