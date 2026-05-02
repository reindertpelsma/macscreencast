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

# ── Step 1: macOS login password ──────────────────────────────────────────────
# Prompted once here; reused for (a) sudo and (b) VNC input control.
step "macOS login password"

echo "  User: $MACOS_USER"
if [[ -z "$MACOS_PASS" ]]; then
    read -rsp "  Password (used for sudo + VNC input control): " MACOS_PASS
    echo
fi

# Validate and cache sudo credentials so later commands don't prompt again.
if echo "$MACOS_PASS" | sudo -S -v 2>/dev/null; then
    green "  Password verified"
else
    yellow "  Could not validate password with sudo — Screen Sharing enable may fail"
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

# User site-packages: no venv needed; LaunchAgent inherits user environment.
"$PYTHON_BINARY" -m pip install --quiet --user \
    'websockets>=13.0' \
    'numpy>=1.24' \
    'Pillow>=10.0' \
    'cryptography>=41.0' \
    || die "pip install failed — check network and pip"

if "$PYTHON_BINARY" -m pip install --quiet --user 'av>=12.0' 2>/dev/null; then
    green "  av (PyAV/H.264): installed"
else
    yellow "  av not installed — falling back to JPEG (lower quality)"
    CODEC="jpeg"
fi

"$PYTHON_BINARY" -m pip install --quiet --user \
    pyobjc-core \
    pyobjc-framework-Cocoa \
    pyobjc-framework-Quartz \
    pyobjc-framework-AVFoundation \
    pyobjc-framework-ScreenCaptureKit \
    2>/dev/null && green "  PyObjC: installed" \
               || yellow "  PyObjC partial install — SCK may be limited"

green "  Dependencies ready"

# ── Step 4: Web UI access token ───────────────────────────────────────────────
if [[ -z "$MVS_PASSWORD" ]]; then
    MVS_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(16))')"
    yellow "  Generated web token: $MVS_PASSWORD"
    yellow "  (save this — you need it to open the web UI)"
fi

# ── Step 5: Enable Screen Sharing (screensharingd / VNC port 5900) ────────────
if [[ "$SKIP_SCREEN_SHARING" -eq 0 ]]; then
    step "Enabling Screen Sharing (VNC for input control)"

    if nc -z 127.0.0.1 5900 2>/dev/null; then
        green "  Screen Sharing already active on port 5900"
    else
        yellow "  Not detected on port 5900 — attempting to enable..."

        # Method 1: launchctl (macOS 12+)
        sudo_s launchctl load -w \
            /System/Library/LaunchDaemons/com.apple.screensharing.plist && sleep 2 || true

        # Method 2: systemsetup fallback
        sudo_s /usr/sbin/systemsetup -setremotedesktop on || true

        if nc -z 127.0.0.1 5900 2>/dev/null; then
            green "  Screen Sharing enabled"
        else
            yellow "  Could not confirm port 5900 is open."
            yellow "  Enable manually: System Settings > General > Sharing > Screen Sharing"
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

# bootout + bootstrap ensures the plist is fully re-read (kickstart reuses
# the cached program path and ignores plist changes).
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
sleep 1
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"

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

# ── Step 8: Connection info ────────────────────────────────────────────────────
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
echo "  Current capture mode: VNC (~30 fps)"
echo "  To upgrade to 60 fps SCK capture:"
echo
yellow "    1. Open the web UI above — screen is already visible"
yellow "    2. A 'Python wants to record your screen' dialog will appear"
yellow "       in the GUI session (visible in the web UI itself)"
yellow "    3. Click Allow — server auto-upgrades to 60 fps within 30s"
echo
echo "  Python: $PYTHON_BINARY"
echo "  Log:    tail -f $LOG_PATH"
echo "  Restart: launchctl kickstart -k gui/\$(id -u)/$LABEL"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
