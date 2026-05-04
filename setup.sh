#!/usr/bin/env bash
# mac-vnc-stream/setup.sh
#
# Sets up mac-vnc-stream on a fresh macOS machine accessible only via SSH.
# Run this once; it is idempotent and safe to re-run.
#
# Usage (repo already cloned):
#   bash setup.sh [--port PORT] [--password TOKEN] [--listen ADDR]
#
# For curl-pipe install (no prior clone needed):
#   bash <(curl -fsSL https://raw.githubusercontent.com/reindertpelsma/mac-vnc-stream/main/install.sh)
#
# Typical flow:
#   1. Script prompts once for your macOS login password (used for sudo + VNC)
#   2. Installs Python deps, enables Screen Sharing, installs LaunchAgent
#   3. Server starts in VNC mode immediately — connect via SSH tunnel shown at end
#   4. Click Allow on the Screen Recording prompt visible in the web UI
#   5. Server auto-upgrades to 60 fps SCK capture within 30 seconds

set -euo pipefail

# ── Script location — works regardless of cwd ─────────────────────────────────
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_PY="$REPO_DIR/server.py"
PLIST_PATH="$HOME/Library/LaunchAgents/com.macvncstream.server.plist"
LOG_PATH="/tmp/macvncstream.log"
LABEL="com.macvncstream.server"

# ── Defaults ──────────────────────────────────────────────────────────────────
PORT=6081
LISTEN="127.0.0.1" # loopback-only; reach via SSH tunnel (--listen 0.0.0.0 only on trusted networks)
MVS_PASSWORD=""
MACOS_USER="$(whoami)"
MACOS_PASS=""
CODEC="h264"
MAX_FPS=60
SKIP_SCREEN_SHARING=0
VNC_PRESEEDED=0   # 1 = screensharingd already live on port 5900 (cloud Mac)

# ── Arg parsing ───────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)       PORT="$2";         shift 2 ;;
        --password)   MVS_PASSWORD="$2"; shift 2 ;;
        --listen)     LISTEN="$2";       shift 2 ;;
        --user)       MACOS_USER="$2";   shift 2 ;;
        --macos-pass) MACOS_PASS="$2";   shift 2 ;;
        --codec)      CODEC="$2";        shift 2 ;;
        --no-screen-sharing) SKIP_SCREEN_SHARING=1; shift ;;
        -h|--help)
            echo "Usage: $0 [options]"
            echo "  --port N          Web UI port (default 6081)"
            echo "  --password TOKEN  Web UI access token (random if omitted)"
            echo "  --listen ADDR     Bind address (default 127.0.0.1; use SSH tunnel to reach it)"
            echo "  --user USER       macOS username (default: current user)"
            echo "  --macos-pass PASS macOS login password — prompted if omitted"
            echo "  --no-screen-sharing  Skip enabling screensharingd"
            exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ── Helpers ───────────────────────────────────────────────────────────────────
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
red()    { printf '\033[31m%s\033[0m\n' "$*"; }
step()   { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die()    { red "ERROR: $*"; exit 1; }

# Run a sudo command using the cached password so we never prompt twice.
# Usage: sudo_s command [args...]
sudo_s() { echo "$MACOS_PASS" | sudo -S "$@" 2>/dev/null; }

# ── Step 1: Detect environment + conditional password prompt ──────────────────
#
# The macOS password serves two purposes:
#   (a) sudo — to enable/restart screensharingd via launchctl
#   (b) VNC input control — passed to the server as the VNC auth credential
#
# Both are only useful when screensharingd has Screen Recording TCC permission.
# We detect this via two independent signals (either is sufficient):
#
#   Signal A — port 5900 open: screensharingd is running and has TCC permission
#              (a daemon that lacks TCC starts but cannot bind for capture).
#
#   Signal B — TCC.db entry: screensharingd has permission but may not be
#              running yet (e.g. daemon was disabled; we can launchctl-load it).
#              Tries user DB first (no root needed), then system DB (no root on
#              some cloud images; silent failure otherwise).
#
# If neither fires → fresh physical Mac, VNC unusable → skip password, explain.

step "Detecting environment"

_VNC_REASON=""

# Signal A: daemon already listening — port 5900 open means running and has TCC
if nc -z 127.0.0.1 5900 2>/dev/null; then
    VNC_PRESEEDED=1
    _VNC_REASON="screensharingd running on port 5900"
fi

# Signal B: SIP disabled — TCC enforcement is bypassed, all screen capture APIs
# work directly regardless of what TCC.db says. Reading TCC.db would give false
# results here, so we skip it and trust the API will succeed.
if [[ "$VNC_PRESEEDED" -eq 0 ]]; then
    if /usr/bin/csrutil status 2>/dev/null | grep -q "disabled"; then
        VNC_PRESEEDED=1
        _VNC_REASON="SIP disabled — TCC bypassed, APIs work directly"
    fi
fi

# Signal C: TCC.db has screensharingd approved but daemon is stopped.
# Only reached when SIP is on (so TCC.db is authoritative) and port 5900 is
# closed. In that case we can launchctl-load screensharingd and it will work.
if [[ "$VNC_PRESEEDED" -eq 0 ]]; then
    _TCC_SIGNAL="$(python3 - <<'PYEOF' 2>/dev/null
import sqlite3, os, sys

def _approved(db):
    try:
        c = sqlite3.connect('file:' + db + '?mode=ro', uri=True)
        rows = c.execute(
            "SELECT auth_value FROM access "
            "WHERE service='kTCCServiceScreenCapture' "
            "AND (client='com.apple.screensharing' "
            "  OR client='com.apple.screensharing.agent' "
            "  OR client LIKE '%screensharing%')"
        ).fetchall()
        c.close()
        # auth_value 2 = allowed; 3 = limited (macOS 14+, also usable)
        return any(r[0] in (2, 3) for r in rows)
    except Exception:
        return False

# User-level TCC — always readable without root
if _approved(os.path.expanduser(
        '~/Library/Application Support/com.apple.TCC/TCC.db')):
    print("user_tcc"); sys.exit(0)

# System-level TCC — root-only on stock macOS; readable on some cloud images
if _approved('/Library/Application Support/com.apple.TCC/TCC.db'):
    print("system_tcc"); sys.exit(0)

sys.exit(1)
PYEOF
)"
    if [[ -n "$_TCC_SIGNAL" ]]; then
        VNC_PRESEEDED=1
        _VNC_REASON="screensharingd approved in TCC.db (${_TCC_SIGNAL}, daemon not yet running)"
    fi
fi

if [[ "$VNC_PRESEEDED" -eq 1 ]]; then
    green "  VNC usable — ${_VNC_REASON}"
else
    yellow "  screensharingd not detected (port 5900 closed, no TCC.db entry found)"
    yellow "  This is expected on a fresh physical Mac."
    yellow "  One-time fix: grant Screen Recording to Python in"
    yellow "  System Settings → Privacy & Security → Screen Recording"
    yellow "  then re-run setup.sh — VNC bootstrap will be available."
fi

echo "  User: $MACOS_USER"

if [[ "$VNC_PRESEEDED" -eq 1 ]]; then
    # Cloud Mac: password needed for sudo (enable/restart screensharingd) and VNC auth.
    if [[ -z "$MACOS_PASS" ]]; then
        echo
        yellow "  This Mac uses VNC capture (screensharingd is already running, or"
        yellow "  Screen Recording cannot be granted interactively over SSH)."
        yellow "  AppleDH authentication is required by macOS 15+ for full input"
        yellow "  control, and it needs your login password every time the server"
        yellow "  starts. The password will be stored in:"
        yellow "    ~/Library/LaunchAgents/com.macvncstream.server.plist  (mode 0600)"
        yellow "  Once Screen Recording is granted, switch to --api-only and remove"
        yellow "  MACOS_PASS from the plist (see README ▸ Security)."
        echo
        read -rsp "  macOS login password: " MACOS_PASS
        echo
    fi
    if echo "$MACOS_PASS" | sudo -S -v 2>/dev/null; then
        green "  Password verified"
    else
        yellow "  Could not validate password with sudo — screensharingd restart may fail"
    fi
else
    # Fresh Mac: no password needed — screensharingd cannot capture without TCC.
    # sudo still available via the system prompt if the user has passwordless sudo.
    if [[ -n "$MACOS_PASS" ]]; then
        # Honour explicit --macos-pass flag even on fresh Mac (user knows what they're doing).
        if echo "$MACOS_PASS" | sudo -S -v 2>/dev/null; then
            green "  Password provided and verified"
        fi
    fi
fi

# ── Step 2: Find the best Python binary ───────────────────────────────────────
step "Finding best Python for Screen Recording"

# Written to a temp file — bash 3.2 (macOS default) can't do heredoc inside $().
_PY_DETECT="$(mktemp /tmp/mvs_detect_XXXXXX.py)"
cat > "$_PY_DETECT" <<'PYEOF'
import os, sys, sqlite3, subprocess, shutil

def tcc_granted_pythons(service='kTCCServiceScreenCapture'):
    """Path-based TCC grants only — bundle-ID grants skipped.
    macOS applies the SCK grant to the specific binary that triggered the
    dialog, not all binaries sharing the same bundle ID.  mdfind-resolved
    bundle-ID targets often resolve to the wrong binary (e.g. CommandLineTools
    Python instead of Xcode Python), causing -3801 on stream start."""
    db = os.path.expanduser('~/Library/Application Support/com.apple.TCC/TCC.db')
    found = []
    try:
        conn = sqlite3.connect('file:' + db + '?mode=ro', uri=True)
        rows = conn.execute(
            "SELECT client, client_type FROM access WHERE service=? AND auth_value=2",
            (service,)).fetchall()
        conn.close()
    except Exception:
        return found
    for client, ctype in rows:
        if ctype == 1 and os.path.isfile(client) and os.access(client, os.X_OK):
            found.append(client)
    return found

def is_python(path):
    try:
        r = subprocess.run(
            [path, '-c', 'import sys; assert sys.version_info >= (3,9)'],
            capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False

candidates = []

# 1. Path-based TCC grant — already has Screen Recording, no dialog needed
candidates.extend(tcc_granted_pythons('kTCCServiceScreenCapture'))

# 2. Xcode Python.app — Apple-signed (com.apple.python3), has PyObjC built-in,
#    can receive Screen Recording permission via the standard macOS dialog.
#    Preferred over CommandLineTools Python even though both share the same
#    bundle ID: macOS appears to bind SCK permission to the specific binary
#    that triggered the grant dialog.
for p in [
    '/Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python',
    '/Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.11/Resources/Python.app/Contents/MacOS/Python',
    '/Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.12/Resources/Python.app/Contents/MacOS/Python',
]:
    if os.path.isfile(p) and p not in candidates:
        candidates.append(p)

# 3. Any python3 on PATH (homebrew, pyenv, CommandLineTools shim, etc.)
for name in ['python3', 'python3.12', 'python3.11', 'python3.10', 'python3.9']:
    p = shutil.which(name)
    if p and os.path.isfile(p) and p not in candidates:
        candidates.append(p)

for c in candidates:
    if is_python(c):
        print(c)
        sys.exit(0)

sys.exit(1)
PYEOF
PYTHON_BINARY="$(python3 "$_PY_DETECT" 2>/dev/null)"
rm -f "$_PY_DETECT"

if [[ -z "$PYTHON_BINARY" ]]; then
    die "No suitable Python 3.9+ binary found.
Install Python via: brew install python@3.11
Or install Xcode from the App Store (includes Python with PyObjC)."
fi

green "  Python: $PYTHON_BINARY"

if ! "$PYTHON_BINARY" -c 'import objc' 2>/dev/null; then
    yellow "  PyObjC not found in chosen Python — will install via pip"
fi

# ── Step 3: Install Python dependencies ───────────────────────────────────────
step "Installing Python dependencies"

# Detect PEP 668 (Homebrew Python on macOS 13+) and add --break-system-packages
# if needed. We still use --user so we don't touch the Homebrew-managed tree.
_PIP_FLAGS="--quiet --user"
if "$PYTHON_BINARY" -m pip install --quiet --user --dry-run pip 2>&1 \
        | grep -q "externally-managed-environment"; then
    _PIP_FLAGS="--quiet --user --break-system-packages"
    yellow "  PEP 668 detected — adding --break-system-packages"
fi

"$PYTHON_BINARY" -m pip install $_PIP_FLAGS \
    'websockets>=13.0' \
    'numpy>=1.24' \
    'Pillow>=10.0' \
    'cryptography>=41.0' \
    || die "pip install failed — check network and pip"

if "$PYTHON_BINARY" -m pip install $_PIP_FLAGS 'av>=12.0' 2>/dev/null; then
    green "  av (PyAV/H.264): installed"
else
    yellow "  av not installed — falling back to JPEG (lower quality)"
    CODEC="jpeg"
fi

yellow "  Installing PyObjC frameworks (5 packages, can take 5–10 min on fresh Mac)..."
# Drop --quiet for PyObjC: with old pip on fresh Macs the C-extension compile
# can take many minutes per package, and silent mode looks like a hang. Keep
# stderr visible so the user sees compile progress / wheel-building / errors.
_PYOBJC_FLAGS="${_PIP_FLAGS//--quiet/}"
if "$PYTHON_BINARY" -m pip install $_PYOBJC_FLAGS \
    pyobjc-core \
    pyobjc-framework-Cocoa \
    pyobjc-framework-Quartz \
    pyobjc-framework-AVFoundation \
    pyobjc-framework-ScreenCaptureKit; then
    green "  PyObjC: installed"
else
    yellow "  PyObjC partial install — SCK may be limited"
fi

green "  Dependencies ready"

# ── Step 4: Web UI access token ───────────────────────────────────────────────
if [[ -z "$MVS_PASSWORD" ]]; then
    MVS_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(16))')"
    yellow "  Generated web token: $MVS_PASSWORD"
    yellow "  (save this — you need it to open the web UI)"
fi

# ── Step 5: Enable Screen Sharing (screensharingd / VNC port 5900) ────────────
if [[ "$SKIP_SCREEN_SHARING" -eq 0 ]]; then
    step "Screen Sharing (screensharingd)"

    if [[ "$VNC_PRESEEDED" -eq 1 ]]; then
        green "  Already active on port 5900 — no action needed"
    elif [[ -n "$MACOS_PASS" ]]; then
        yellow "  Attempting to enable screensharingd..."

        # Method 1: launchctl (macOS 12+)
        sudo_s launchctl load -w \
            /System/Library/LaunchDaemons/com.apple.screensharing.plist && sleep 2 || true

        # Method 2: systemsetup fallback
        sudo_s /usr/sbin/systemsetup -setremotedesktop on || true

        if nc -z 127.0.0.1 5900 2>/dev/null; then
            green "  screensharingd started on port 5900"
            yellow "  Note: VNC capture requires Screen Recording TCC permission."
            yellow "  Without it, screensharingd runs but shows a blank screen."
            yellow "  Grant permission once physically, then re-run setup.sh."
        else
            yellow "  Could not start screensharingd — enable manually:"
            yellow "  System Settings → General → Sharing → Screen Sharing"
        fi
    else
        yellow "  Skipping screensharingd (no password provided and port 5900 not pre-seeded)"
        yellow "  On a fresh Mac, grant Screen Recording permission physically first."
    fi
fi

# ── Step 6: Install LaunchAgent ───────────────────────────────────────────────
step "Installing LaunchAgent: $PLIST_PATH"

mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON_BINARY}</string>
        <string>${SERVER_PY}</string>
        <string>--codec</string>
        <string>${CODEC}</string>
        <string>--max-fps</string>
        <string>${MAX_FPS}</string>
        <string>--listen</string>
        <string>${LISTEN}</string>
        <string>--port</string>
        <string>${PORT}</string>
    </array>

    <key>EnvironmentVariables</key>
    <dict>
        <key>MACOS_USER</key>
        <string>${MACOS_USER}</string>
        <key>MACOS_PASS</key>
        <string>${MACOS_PASS}</string>
        <key>MVS_PASSWORD</key>
        <string>${MVS_PASSWORD}</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>${LOG_PATH}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_PATH}</string>
</dict>
</plist>
PLIST

green "  Plist written (mode 0600)"
chmod 600 "$PLIST_PATH"
if [[ -n "$MACOS_PASS" ]]; then
    yellow "  Plist contains MACOS_PASS for VNC AppleDH auth (the only way macOS 15+"
    yellow "  permits full input control via VNC). To stop storing it: grant Screen"
    yellow "  Recording, then edit the plist to drop MACOS_PASS and add --api-only."
else
    green "  Plist contains no credentials — runtime uses SCK only"
fi

# ── Step 7: (Re)load the LaunchAgent ─────────────────────────────────────────
step "Starting mac-vnc-stream service"

# bootout + bootstrap ensures the plist is fully re-read (kickstart reuses
# the cached program path and ignores plist changes).
#
# Domain choice: gui/$UID requires an active Aqua console session. On a
# fresh-install cloud Mac with no console login (only SSH), gui/$UID does
# not exist and bootstrap fails with "125: Domain does not support specified
# action". user/$UID exists for any login session including SSH, and is
# enough for VNC capture/input — the cloud-Mac case. Try gui first (gives
# SCK access when available), fall back to user.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootout "user/$(id -u)/$LABEL" 2>/dev/null || true
sleep 1
LOAD_DOMAIN=""
if launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null; then
    LOAD_DOMAIN="gui/$(id -u)"
    green "  Loaded into $LOAD_DOMAIN (full GUI session — SCK/CGEvent available)"
elif launchctl bootstrap "user/$(id -u)" "$PLIST_PATH" 2>/dev/null; then
    LOAD_DOMAIN="user/$(id -u)"
    yellow "  No Aqua session detected — loaded into $LOAD_DOMAIN (VNC capture only)"
    yellow "  After you log in via VNC at least once, re-run setup.sh to switch to gui/\$UID"
    yellow "  for SCK capture and CGEvent input."
else
    die "launchctl bootstrap failed in both gui/$(id -u) and user/$(id -u). Run with -d for details:
  launchctl bootstrap user/$(id -u) $PLIST_PATH"
fi

echo -n "  Waiting for server"
WAITED=0
while [[ $WAITED -lt 15 ]]; do
    if nc -z 127.0.0.1 "$PORT" 2>/dev/null; then
        echo
        green "  Server is up on port $PORT"
        break
    fi
    sleep 1; WAITED=$((WAITED + 1)); echo -n "."
done
if [[ $WAITED -ge 15 ]]; then
    echo
    yellow "  Server did not respond within 15s — check log: tail -f $LOG_PATH"
fi

# ── Step 8: Trigger macOS permission prompts ──────────────────────────────────
#
# TCC permission dialogs appear in the GUI session on the Mac's display, NOT
# in the SSH terminal where you ran this script. How you see and click them
# depends on your setup:
#
#   Cloud Mac (VNC pre-seeded, e.g. Scaleway):
#     Connect via VNC on port 5900 right now — the dialogs will be visible.
#     URL: vnc://127.0.0.1:5900 (SSH-tunnel: ssh -L 5900:localhost:5900 ...)
#
#   Mac with display attached:
#     Look at the screen — the dialogs will appear there momentarily.
#
#   Headless fresh Mac with no prior permission:
#     Attach a display or KVM, grant Screen Recording to Python in
#     System Settings → Privacy & Security → Screen Recording, then
#     re-run setup.sh. That is a one-time step.
#
# We poke both APIs now so the dialogs surface immediately.
step "Requesting macOS permissions (Screen Recording + Accessibility)"

if [[ "$VNC_PRESEEDED" -eq 1 ]]; then
    yellow "  ┌─ ACTION REQUIRED ────────────────────────────────────────────────┐"
    yellow "  │ Connect via VNC to see and click the permission dialogs:         │"
    yellow "  │   ssh -L 5900:localhost:5900 ${MACOS_USER}@<mac-ip>             │"
    yellow "  │   then open:  vnc://127.0.0.1:5900                              │"
    yellow "  │ Click Allow on: Screen Recording  and  Accessibility            │"
    yellow "  └──────────────────────────────────────────────────────────────────┘"
else
    yellow "  ┌─ ACTION REQUIRED ────────────────────────────────────────────────┐"
    yellow "  │ The permission dialogs will appear on the Mac's display.         │"
    yellow "  │ If you have no display attached, you need physical/KVM access.   │"
    yellow "  │ Grant: Screen Recording  and  Accessibility  → click Allow       │"
    yellow "  │ This is a one-time step. Re-run setup.sh after granting.         │"
    yellow "  └──────────────────────────────────────────────────────────────────┘"
fi
"$PYTHON_BINARY" - <<'PYEOF' 2>/dev/null &
import sys, time
# Screen Recording — CGRequestScreenCaptureAccess() pops the system dialog
try:
    import Quartz
    Quartz.CGRequestScreenCaptureAccess()
except Exception:
    pass

# Accessibility — AXIsProcessTrusted(options:{prompt:True}) pops the dialog
try:
    import ctypes, ctypes.util
    ax = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    ax.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
    ax.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
    # kAXTrustedCheckOptionPrompt = True triggers the System Settings dialog
    try:
        from Foundation import NSDictionary
        opts = NSDictionary.dictionaryWithObject_forKey_(True, "AXTrustedCheckOptionPrompt")
        ax.AXIsProcessTrustedWithOptions(ctypes.c_void_p(id(opts)))
    except Exception:
        pass
except Exception:
    pass
# Keep the process alive briefly so the dialogs have time to render
time.sleep(3)
PYEOF
PERM_PID=$!
yellow "  Permission dialogs may appear on screen — click Allow for both."
yellow "  (Screen Recording enables 60fps capture; Accessibility enables smooth input)"
sleep 4
kill "$PERM_PID" 2>/dev/null || true

# ── Step 9: Connection info ────────────────────────────────────────────────────
MAC_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo '<mac-ip>')"

echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
green "  mac-vnc-stream is running!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo
if [[ "$LISTEN" == "127.0.0.1" ]]; then
    echo "  Access via SSH tunnel (server bound to loopback):"
    echo "    ssh -L ${PORT}:127.0.0.1:${PORT} ${MACOS_USER}@${MAC_IP}"
    echo "    then open: http://127.0.0.1:${PORT}/?token=${MVS_PASSWORD}"
else
    echo "  Direct URL (server bound to ${LISTEN}):"
    echo "    http://${MAC_IP}:${PORT}/?token=${MVS_PASSWORD}"
    echo
    echo "  SSH tunnel:"
    echo "    ssh -L ${PORT}:127.0.0.1:${PORT} ${MACOS_USER}@${MAC_IP}"
    echo "    then open: http://127.0.0.1:${PORT}/?token=${MVS_PASSWORD}"
fi
echo
echo "  Capture: SCK 60fps (or VNC fallback until Screen Recording is allowed)"
echo "  Input:   CGEvent native (or VNC fallback until Accessibility is allowed)"
echo "  The server upgrades automatically within 5s after permissions are granted."
echo
echo "  Python: $PYTHON_BINARY"
echo "  Log:    tail -f $LOG_PATH"
echo "  Restart: launchctl kickstart -k $LOAD_DOMAIN/$LABEL"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
