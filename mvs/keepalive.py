import logging
import os
import sys
import threading

log = logging.getLogger("macvnc")


def _os_geteuid_is_root():
    try:
        return os.geteuid() == 0
    except Exception:
        return False


def _find_console_uid():
    """Return the uid of the currently-logged-in console user, or None.
    On a cloud Mac with VNC login, this is whichever uid screensharingd
    let in. Falls back to scanning /dev/console ownership."""
    try:
        import subprocess
        # `stat -f %u /dev/console` returns the owning uid of the console.
        out = subprocess.run(["stat", "-f", "%u", "/dev/console"],
                             capture_output=True, text=True, timeout=2)
        uid = int(out.stdout.strip())
        if uid >= 500:  # skip system uids
            return uid
    except Exception:
        pass
    return None


# SCShareableContent probe script. Runs in user context via `launchctl asuser`
# to make TCC register the calling Python binary in the Screen Recording list.
# CGRequestScreenCaptureAccess does NOT trigger TCC registration on Tahoe;
# only the actual SCK API does. The script blocks briefly so TCC has time to
# write the entry, then exits.
_SCK_TCC_REG_SCRIPT = r"""
import sys, time, threading
try:
    import ScreenCaptureKit as SCK
    from Foundation import NSRunLoop, NSDate, NSDefaultRunLoopMode
    _done = threading.Event()
    def _cb(content, error): _done.set()
    try:
        SCK.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(False, True, _cb)
    except AttributeError:
        SCK.SCShareableContent.getExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(False, True, _cb)
    t0 = time.time()
    while not _done.is_set() and time.time() - t0 < 5:
        NSRunLoop.mainRunLoop().runMode_beforeDate_(NSDefaultRunLoopMode,
                                                   NSDate.dateWithTimeIntervalSinceNow_(0.1))
except Exception as e:
    sys.stderr.write(str(e) + chr(10))
"""


def _trigger_sck_tcc_registration():
    """Force TCC to register the current Python binary in the Screen Recording
    list by invoking SCShareableContent from the user's Aqua context.

    From a LaunchDaemon (system context, root), neither
    CGRequestScreenCaptureAccess() nor SCShareableContent registers the binary
    — TCC treats system-context callers differently. Spawning the same call
    via `launchctl asuser <uid>` routes it through the user's session domain,
    where TCC does register the binary.

    Result: Python appears in the Screen Recording toggle list, so the user
    enables it with one click instead of having to '+' add it by path.

    Quietly no-op when not running as root (LaunchAgent path already runs
    in user context) or when no console user is logged in (TCC reg can't
    land in that case anyway)."""
    if not _os_geteuid_is_root():
        return
    uid = _find_console_uid()
    if uid is None:
        return
    try:
        import subprocess as _sp, sys as _sys
        _sp.Popen(["launchctl", "asuser", str(uid),
                   _sys.executable, "-c", _SCK_TCC_REG_SCRIPT],
                  cwd=os.path.dirname(_sys.executable) or None)
        log.info("TCC: triggered Screen Recording registration via launchctl asuser %d", uid)
    except Exception as e:
        log.debug("asuser SCK TCC reg: %s", e)


def start_console_user_watcher(on_user_login):
    """Watch for a console user becoming available (e.g. via VNC login on a
    headless cloud Mac). Calls on_user_login(uid) once per detected login.

    The daemon starts before any user is logged in. We need to detect the
    moment a user logs in via VNC so we can fire the SCK TCC registration
    via launchctl asuser — and later, the LaunchDaemon → LaunchAgent
    self-promotion when both Screen Recording and Accessibility are granted.

    Implementation: poll _find_console_uid() every 5s. On transition from
    None → uid, fire the callback. Best-effort; runs as a daemon thread."""
    def _loop():
        import time as _t
        last = None
        while True:
            try:
                uid = _find_console_uid()
                if uid is not None and uid != last:
                    log.info("Console user detected: uid=%d", uid)
                    try:
                        on_user_login(uid)
                    except Exception as e:
                        log.debug("console-user callback: %s", e)
                    last = uid
                elif uid is None:
                    last = None
            except Exception:
                pass
            _t.sleep(5)
    threading.Thread(target=_loop, daemon=True, name="console-user-watcher").start()

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
    Falls back silently if the display or tkinter is unavailable (headless, SSH-only)."""
    try:
        import subprocess, threading, time as _time
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
            _trigger_sck_tcc_registration()
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


def _gui_domain_accepts_bootstrap(uid):
    """Probe whether gui/<uid> accepts a no-op LaunchAgent bootstrap. Used to
    decide if the daemon can self-promote into a user-context LaunchAgent."""
    import subprocess, tempfile, os as _os
    f = tempfile.NamedTemporaryFile(mode='w', suffix='.plist', delete=False)
    try:
        f.write(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>\n'
            '  <key>Label</key><string>com.macvncstream.probe</string>\n'
            '  <key>ProgramArguments</key><array><string>/usr/bin/true</string></array>\n'
            '  <key>RunAtLoad</key><false/>\n'
            '</dict></plist>\n')
        f.close()
        subprocess.run(["launchctl", "asuser", str(uid),
                        "launchctl", "bootout", f"gui/{uid}/com.macvncstream.probe"],
                       capture_output=True, timeout=3)
        r = subprocess.run(["launchctl", "asuser", str(uid),
                            "launchctl", "bootstrap", f"gui/{uid}", f.name],
                           capture_output=True, timeout=5)
        subprocess.run(["launchctl", "asuser", str(uid),
                        "launchctl", "bootout", f"gui/{uid}/com.macvncstream.probe"],
                       capture_output=True, timeout=3)
        return r.returncode == 0
    except Exception:
        return False
    finally:
        try: _os.unlink(f.name)
        except Exception: pass


def maybe_promote_to_launchagent(uid, label="com.macvncstream.server"):
    """If running as a LaunchDaemon (root), Screen Recording is granted, and
    gui/<uid> accepts a bootstrap, write a clean LaunchAgent plist and spawn
    a detached helper that boots the daemon out and the agent in.

    Returns True if promotion was scheduled (current process should exit
    shortly); False if any precondition isn't met."""
    if not _os_geteuid_is_root():
        return False
    daemon_plist = f"/Library/LaunchDaemons/{label}.plist"
    if not os.path.exists(daemon_plist):
        return False  # not a LaunchDaemon install — already an Agent
    try:
        import ctypes
        cg = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
        cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        if not cg.CGPreflightScreenCaptureAccess():
            return False
    except Exception:
        return False
    if not _gui_domain_accepts_bootstrap(uid):
        return False

    log.info("Promotion: SCK granted + gui/%d ready — migrating LaunchDaemon → LaunchAgent", uid)

    # Build the LaunchAgent plist from the daemon plist, stripping the
    # daemon-only keys. Reading the live daemon plist preserves whatever
    # custom args the user passed to setup.sh.
    try:
        import plistlib, getpass
        with open(daemon_plist, 'rb') as f:
            d = plistlib.load(f)
        d.pop("UserName", None)
        d.pop("GroupName", None)
        d.pop("WorkingDirectory", None)
        # Drop the VNC-only flags so the agent boots with auto-mode.
        args = d.get("ProgramArguments", [])
        cleaned = []
        skip = False
        for a in args:
            if skip: skip = False; continue
            if a in ("--capture", "--input"):
                skip = True
                continue
            if a == "vnc":  # bare 'vnc' that escaped the --capture/--input pair
                continue
            cleaned.append(a)
        d["ProgramArguments"] = cleaned
        # Also drop MACOS_PASS — LaunchAgent doesn't need it (no VNC fallback
        # post-promotion since SCK works). Keep MACOS_USER and MVS_PASSWORD.
        env = d.get("EnvironmentVariables", {})
        env.pop("MACOS_PASS", None)
        env.pop("HOME", None)  # LaunchAgent inherits HOME from user session
        env.pop("USER", None)
        d["EnvironmentVariables"] = env

        # Write to a per-user LaunchAgents dir.
        import pwd
        home = pwd.getpwuid(uid).pw_dir
        agents_dir = os.path.join(home, "Library/LaunchAgents")
        os.makedirs(agents_dir, exist_ok=True)
        agent_plist = os.path.join(agents_dir, f"{label}.plist")
        with open(agent_plist, 'wb') as f:
            plistlib.dump(d, f)
        os.chown(agent_plist, uid, _gid_for_uid(uid))
        os.chmod(agent_plist, 0o600)
    except Exception as e:
        log.warning("Promotion: failed to build agent plist: %s", e)
        return False

    # Spawn the migration helper. It runs detached so the current daemon
    # process can exit cleanly first (otherwise launchctl bootout deadlocks
    # waiting for us). Helper has 30s to complete; if anything fails it
    # leaves the daemon plist intact so KeepAlive restarts it.
    helper = f"""\
#!/bin/bash
set -e
sleep 2
launchctl bootout system/{label} 2>&1 || true
sleep 1
rm -f {daemon_plist}
launchctl asuser {uid} launchctl bootstrap gui/{uid} {agent_plist}
"""
    try:
        import subprocess
        helper_path = f"/tmp/{label}.promote.sh"
        with open(helper_path, 'w') as f:
            f.write(helper)
        os.chmod(helper_path, 0o755)
        # nohup + setsid so the helper survives our exit.
        subprocess.Popen(["/usr/bin/nohup", "/bin/bash", helper_path],
                         stdout=open("/tmp/macvncstream.promote.log", "a"),
                         stderr=subprocess.STDOUT,
                         start_new_session=True)
        log.info("Promotion: helper scheduled, daemon exiting in 2s")
        return True
    except Exception as e:
        log.warning("Promotion: helper spawn failed: %s", e)
        return False


def _gid_for_uid(uid):
    try:
        import pwd
        return pwd.getpwuid(uid).pw_gid
    except Exception:
        return 20  # 'staff' on macOS


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
