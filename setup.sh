#!/usr/bin/env bash
# mac-vnc-stream/setup.sh
#
# Sets up mac-vnc-stream on a fresh macOS machine accessible only via SSH.
# Run this once; it is idempotent and safe to re-run.
#
# Usage:
#   bash setup.sh [--port PORT] [--password TOKEN] [--listen ADDR]
#
# Typical flow:
#   1. SSH into Mac, clone repo, run this script
#   2. Script starts server in VNC-quality mode (30 fps) immediately
#   3. Connect to the web UI via SSH tunnel or direct URL shown at end
#   4. Click Allow on the Screen Recording permission prompt that appears
#      in the GUI session (visible via your cloud provider's VNC or our web UI)
#   5. Server auto-upgrades to 60 fps SCK within 30 seconds — no restart needed

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_PY="$REPO_DIR/server.py"
PLIST_PATH="$HOME/Library/LaunchAgents/com.macvncstream.server.plist"
LOG_PATH="/tmp/macvncstream.log"
LABEL="com.macvncstream.server"

# ── Defaults ──────────────────────────────────────────────────────────────────
PORT=6081
LISTEN="127.0.0.1" # loopback-only by default; use SSH tunnel to access remotely (--listen 0.0.0.0 only if network is trusted)
MVS_PASSWORD=""
MACOS_USER="$(whoami)"
MACOS_PASS=""
CODEC="h264"
MAX_FPS=60
SKIP_SCREEN_SHARING=0

# ── Arg parsing ───────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)       PORT="$2";       shift 2 ;;
        --password)   MVS_PASSWORD="$2"; shift 2 ;;
        --listen)     LISTEN="$2";     shift 2 ;;
        --user)       MACOS_USER="$2"; shift 2 ;;
        --macos-pass) MACOS_PASS="$2"; shift 2 ;;
        --codec)      CODEC="$2";      shift 2 ;;
        --no-screen-sharing) SKIP_SCREEN_SHARING=1; shift ;;
        -h|--help)
            echo "Usage: $0 [--port N] [--password TOKEN] [--listen ADDR] [--user USER] [--macos-pass PASS] [--no-screen-sharing]"
            echo "  --listen defaults to 127.0.0.1 (loopback). Use SSH tunnel to reach it remotely."
            echo "  --macos-pass  macOS login password (for VNC input control). Prompted if omitted."
            exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ── Helpers ───────────────────────────────────────────────────────────────────
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
red()    { printf '\033[31m%s\033[0m\n' "$*"; }
step()   { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

die() { red "ERROR: $*"; exit 1; }

# ── Step 1: Find the best Python binary ───────────────────────────────────────
step "Finding the best Python binary for Screen Recording permission"

# Embedded Python helper — queries TCC DB and probes known paths.
# Written to a temp file to avoid bash 3.2 (macOS default) heredoc-in-$() bugs.
_PY_DETECT="$(mktemp /tmp/mvs_detect_XXXXXX.py)"
cat > "$_PY_DETECT" <<'PYEOF'
import os, sys, sqlite3, subprocess, shutil

# ── TCC query ──
def tcc_granted_pythons(service='kTCCServiceScreenCapture'):
    """Return Python binaries that have an explicit PATH-based TCC grant.
    Bundle-ID grants (client_type=0) are intentionally skipped here because
    mdfind resolves them to whichever .app it indexes first, which may not be
    the binary macOS will actually honour for SCK (macOS appears to apply the
    grant to the specific binary that triggered the dialog, not all binaries
    sharing the same bundle ID)."""
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
        if ctype == 1:  # path-based grant — reliable
            if os.path.isfile(client) and os.access(client, os.X_OK):
                found.append(client)
        # ctype==0 (bundle-ID) skipped: mdfind may resolve to wrong binary
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

# 1. Already TCC-granted (best: no dialog needed)
candidates.extend(tcc_granted_pythons('kTCCServiceScreenCapture'))

# 2. Xcode Python.app — Apple-signed com.apple.python3, has PyObjC built-in,
#    can receive Screen Recording permission via the standard macOS dialog.
xcode_pythons = [
    '/Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python',
    '/Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.11/Resources/Python.app/Contents/MacOS/Python',
    '/Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.12/Resources/Python.app/Contents/MacOS/Python',
]
for p in xcode_pythons:
    if os.path.isfile(p) and p not in candidates:
        candidates.append(p)

# 3. Any python3 on PATH (homebrew, pyenv, etc.) — path grants work too
for name in ['python3', 'python3.12', 'python3.11', 'python3.10', 'python3.9']:
    p = shutil.which(name)
    if p and os.path.isfile(p) and p not in candidates:
        candidates.append(p)

for candidate in candidates:
    if is_python(candidate):
        print(candidate)
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

# Check PyObjC (required for SCK and display info)
if ! "$PYTHON_BINARY" -c 'import objc' 2>/dev/null; then
    yellow "  PyObjC not found in chosen Python — will install via pip"
fi

# ── Step 2: Install Python dependencies ───────────────────────────────────────
step "Installing Python dependencies"

# Install into user site-packages of the chosen Python so it works system-wide
# without a venv (LaunchAgent runs outside any activated venv).
"$PYTHON_BINARY" -m pip install --quiet --user \
    'websockets>=13.0' \
    'numpy>=1.24' \
    'Pillow>=10.0' \
    'cryptography>=41.0' \
    || die "pip install failed — check network and pip availability"

# av (PyAV, H.264/H.265 via VideoToolbox) — optional but strongly recommended
if "$PYTHON_BINARY" -m pip install --quiet --user 'av>=12.0' 2>/dev/null; then
    green "  av (PyAV/H.264): installed"
else
    yellow "  av (PyAV) not installed — will fall back to JPEG codec (lower quality)"
    CODEC="jpeg"
fi

# PyObjC frameworks (needed for SCK in-process capture)
# Install core + only the frameworks we actually import
"$PYTHON_BINARY" -m pip install --quiet --user \
    pyobjc-core \
    pyobjc-framework-Cocoa \
    pyobjc-framework-Quartz \
    pyobjc-framework-AVFoundation \
    pyobjc-framework-ScreenCaptureKit \
    2>/dev/null && green "  PyObjC: installed" || yellow "  PyObjC: partial install (SCK upgrade may be limited)"

green "  Dependencies ready"

# ── Step 3: macOS credentials for VNC input ───────────────────────────────────
step "macOS credentials (used for full-control VNC input)"

echo "  macOS user: $MACOS_USER"
if [[ -z "$MACOS_PASS" ]]; then
    read -rsp "  macOS password (for this user, used for VNC control): " MACOS_PASS
    echo
fi

# ── Step 4: Web UI access token ───────────────────────────────────────────────
if [[ -z "$MVS_PASSWORD" ]]; then
    # Generate a random token if not provided
    MVS_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(16))')"
    yellow "  Generated access token: $MVS_PASSWORD"
    yellow "  (save this — you will need it to open the web UI)"
fi

# ── Step 5: Enable Screen Sharing (screensharingd / VNC port 5900) ────────────
if [[ "$SKIP_SCREEN_SHARING" -eq 0 ]]; then
    step "Checking Screen Sharing (VNC)"

    if nc -z 127.0.0.1 5900 2>/dev/null; then
        green "  Screen Sharing already active on port 5900"
    else
        yellow "  Screen Sharing not detected — attempting to enable..."

        # Method 1: launchctl load (works on macOS 12+)
        if sudo launchctl load -w \
                /System/Library/LaunchDaemons/com.apple.screensharing.plist 2>/dev/null; then
            sleep 2
        fi

        # Method 2: systemsetup (older fallback)
        sudo /usr/sbin/systemsetup -setremotedesktop on 2>/dev/null || true

        if nc -z 127.0.0.1 5900 2>/dev/null; then
            green "  Screen Sharing enabled"
        else
            yellow "  Could not confirm Screen Sharing on port 5900."
            yellow "  Enable it manually: System Settings → General → Sharing → Screen Sharing"
            yellow "  The server will still start; VNC input will connect once it is on."
        fi
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

green "  Plist written"

# ── Step 7: (Re)load the LaunchAgent ─────────────────────────────────────────
step "Starting mac-vnc-stream service"

# bootout first (no-op if not loaded) then bootstrap — ensures the plist
# is fully re-read and the new Python binary is picked up.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
sleep 1
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"

# Wait up to 15s for the server to start listening
echo -n "  Waiting for server..."
WAITED=0
while [[ $WAITED -lt 15 ]]; do
    if nc -z 127.0.0.1 "$PORT" 2>/dev/null; then
        echo
        green "  Server is up on port $PORT"
        break
    fi
    sleep 1
    WAITED=$((WAITED + 1))
    echo -n "."
done
if [[ $WAITED -ge 15 ]]; then
    echo
    yellow "  Server did not respond within 15s — check log: tail -f $LOG_PATH"
fi

# ── Step 8: Show connection info ──────────────────────────────────────────────
MAC_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo '<mac-ip>')"

echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
green "  mac-vnc-stream is running!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo
if [[ "$LISTEN" == "127.0.0.1" ]]; then
    echo "  Server bound to loopback — access via SSH tunnel:"
    echo "    ssh -L ${PORT}:127.0.0.1:${PORT} ${MACOS_USER}@${MAC_IP}"
    echo "    then open: http://127.0.0.1:${PORT}/?token=${MVS_PASSWORD}"
else
    echo "  Web UI (direct — bound to ${LISTEN}):"
    echo "    http://${MAC_IP}:${PORT}/?token=${MVS_PASSWORD}"
    echo
    echo "  Or via SSH tunnel:"
    echo "    ssh -L ${PORT}:127.0.0.1:${PORT} ${MACOS_USER}@${MAC_IP}"
    echo "    then open: http://127.0.0.1:${PORT}/?token=${MVS_PASSWORD}"
fi
echo
echo "  Current capture mode: VNC (~30 fps)"
echo "  To upgrade to 60 fps SCK capture:"
echo
yellow "    1. Open the web UI above — the screen will already be visible"
yellow "    2. A 'Python would like to record your screen' dialog will appear"
yellow "       in the GUI session (visible in the web UI itself)"
yellow "    3. Click Allow — the server auto-upgrades to 60 fps within 30s"
echo
echo "  Python binary: $PYTHON_BINARY"
echo "  Log: tail -f $LOG_PATH"
echo "  Restart: launchctl kickstart -k gui/\$(id -u)/$LABEL"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
