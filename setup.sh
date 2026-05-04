#!/usr/bin/env bash
# setup.sh — install mac-vnc-stream from this git checkout.
#
# Policy:
#   SIP disabled  → install raw LaunchAgent → server.py
#                   (TCC bypassed, no .app bundle needed; fastest path,
#                    used by GitHub macOS runners and custom-imaged dev Macs)
#
#   SIP enabled   → build .app bundle via py2app (~5–10 min on first run)
#                   → install LaunchAgent → bundle binary
#                   (TCC enforces grants by bundle id; we run as
#                    com.macvncstream.server, NOT as the Python interpreter,
#                    so users grant permissions only to this app — never to
#                    the shared interpreter that any other Python script
#                    could exploit)
#
# Headless / scripted use:
#   Pass --headless or set MVS_HEADLESS=1 to skip all interactive prompts.
#   Empty / unset MACOS_PASS is treated as "no VNC fallback wanted".
#
# Flags forwarded to the server (see server.py for full list):
#   --port PORT        web listen port (default 6081)
#   --listen ADDR      web bind addr (default 127.0.0.1)
#   --password TOKEN   web access token (random if omitted)
#   --max-fps N        encoder fps cap (default 60)
#   --codec NAME       h264 | h265 | jpeg (default h264)

set -euo pipefail

# ── Helpers ───────────────────────────────────────────────────────────────────
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
red()    { printf '\033[31m%s\033[0m\n' "$*"; }
step()   { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die()    { red "ERROR: $*"; exit 1; }

# ── Defaults / arg parsing ────────────────────────────────────────────────────
PORT=6081
LISTEN="127.0.0.1"
MAX_FPS=60
CODEC="h264"
MVS_PASSWORD=""
MACOS_PASS=""
MACOS_USER="$(whoami)"
HEADLESS="${MVS_HEADLESS:-0}"
LABEL="com.macvncstream.server"
LOG_PATH="/tmp/macvncstream.log"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NO_LAUNCHAGENT=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)        PORT="$2"; shift 2 ;;
        --listen)      LISTEN="$2"; shift 2 ;;
        --password)    MVS_PASSWORD="$2"; shift 2 ;;
        --macos-pass)  MACOS_PASS="$2"; shift 2 ;;
        --max-fps)     MAX_FPS="$2"; shift 2 ;;
        --codec)       CODEC="$2"; shift 2 ;;
        --headless)    HEADLESS=1; shift ;;
        --no-launchagent) NO_LAUNCHAGENT=1; shift ;;
        -h|--help)
            cat <<HELP
setup.sh — install mac-vnc-stream from this git checkout.

  --port PORT        web listen port (default 6081)
  --listen ADDR      web bind addr (default 127.0.0.1)
  --password TOKEN   web access token (random if omitted)
  --macos-pass PASS  macOS login password (only stored in plist if VNC
                     bootstrap fallback is wanted; empty = no fallback)
  --max-fps N        encoder fps cap (default 60)
  --codec NAME       h264 | h265 | jpeg (default h264)
  --headless         no prompts, sensible defaults (or MVS_HEADLESS=1)
  --no-launchagent   skip the LaunchAgent install — build bundle (if SIP on)
                     and write the plist template, but don't bootstrap. Useful
                     for audit/preview, or when you'll launch the bundle manually.

Reads MVS_HEADLESS, MACOS_PASS, MVS_PASSWORD from env if unset.

Privileged-action policy: before every sudo command setup.sh announces what
it's about to do (e.g. "Installing bundle to /Applications/ — requires sudo").
You can Ctrl+C to abort at any point. The only actions that need root are
(1) writing /Applications/mac-vnc-stream.app and (2) reading the MDM profile
list for the informational TCC-policy detection.
HELP
            exit 0 ;;
        *) die "unknown arg: $1" ;;
    esac
done

# Re-open /dev/tty so prompts work when piped via curl|bash.
if [[ ! -t 0 ]] && [[ -e /dev/tty ]] && [[ "$HEADLESS" -eq 0 ]]; then
    exec </dev/tty
fi

step "mac-vnc-stream installer"
echo "  Repo:    $REPO_DIR"
echo "  User:    $MACOS_USER"
echo "  Headless: $([[ $HEADLESS -eq 1 ]] && echo yes || echo no)"

# ── Step 1: Detect macOS protection state ─────────────────────────────────────
# SIP enabled → TCC is enforcing → we need a real bundle id (com.macvncstream.server)
# SIP disabled → TCC is bypassed → can run server.py directly as the Python interpreter
step "Detecting environment"
SIP_DISABLED=0
if csrutil status 2>&1 | grep -qi disabled; then
    SIP_DISABLED=1
    green "  SIP disabled — TCC enforcement bypassed (GH runner / custom-image Mac)"
    green "  → will install raw LaunchAgent, no .app bundle needed"
else
    green "  SIP enabled — TCC will enforce permissions by bundle id"
    green "  → will build .app bundle (com.macvncstream.server) so users grant"
    green "    permissions to this app only, not the Python interpreter"
fi

# ── Step 2: Find the best Python ──────────────────────────────────────────────
step "Finding Python 3.9+"

# Pure-bash candidate scan — works even when there's no `python3` on PATH yet
# (fresh macOS without Xcode CLT). Each candidate is invoked directly with a
# version-check stdin program; first one that returns 0 wins.
_PYTHON_VER_CHECK='import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)'
PYTHON_BINARY=""
PY_CANDIDATES=(
    # Apple-signed Xcode CLT Python.app paths (preferred — has PyObjC built-in,
    # is what TCC has historically tracked, ad-hoc-sign-friendly via py2app).
    /Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python
    /Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.11/Resources/Python.app/Contents/MacOS/Python
    /Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.12/Resources/Python.app/Contents/MacOS/Python
    /Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.13/Resources/Python.app/Contents/MacOS/Python
    # Homebrew / pyenv / system pythons in PATH order.
    "$(command -v python3.13 2>/dev/null)"
    "$(command -v python3.12 2>/dev/null)"
    "$(command -v python3.11 2>/dev/null)"
    "$(command -v python3.10 2>/dev/null)"
    "$(command -v python3.9 2>/dev/null)"
    "$(command -v python3 2>/dev/null)"
)
for cand in "${PY_CANDIDATES[@]}"; do
    [[ -z "$cand" || ! -x "$cand" ]] && continue
    if "$cand" -c "$_PYTHON_VER_CHECK" 2>/dev/null; then
        PYTHON_BINARY="$cand"
        break
    fi
done

if [[ -z "$PYTHON_BINARY" ]]; then
    # Most likely Xcode Command Line Tools isn't installed yet (common on a
    # fresh-image cloud Mac). xcode-select --install pops a GUI dialog —
    # useless when we're SSH-only with no Aqua session yet. Use the
    # `softwareupdate` headless install path instead: works over plain SSH.
    yellow "  No Python 3.9+ found on this Mac."
    yellow "  Most likely: Xcode Command Line Tools isn't installed."
    echo
    if [[ "$HEADLESS" -eq 1 || "$RUNNING_FROM_SSH" -eq 1 ]]; then
        # Headless / SSH path: use softwareupdate. xcode-select --install
        # would just hang on a GUI dialog nobody can click.
        yellow "  Installing Command Line Tools headlessly via softwareupdate."
        yellow "  This needs sudo and downloads ~700 MB. Takes ~5–10 min."
        yellow ""
        yellow "  About to run (REQUIRES SUDO):"
        yellow "    sudo touch /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress"
        yellow "    sudo softwareupdate --list  (find CLT label)"
        yellow "    sudo softwareupdate --install '<CLT label>'"
        echo
        if [[ "$HEADLESS" -ne 1 ]]; then
            read -rp "  Proceed? [Y/n] " _ans
            [[ "$_ans" =~ ^[Nn]$ ]] && die "User declined CLT install — re-run with python3 already installed."
            unset _ans
        fi
        # The sentinel file makes CLT appear in `softwareupdate --list` output.
        # Without it, CLT is hidden from the list (Apple expects xcode-select
        # --install to be the entry point on GUI Macs).
        if [[ -n "$MACOS_PASS" ]]; then
            echo "$MACOS_PASS" | sudo -S touch /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress
            CLT_LABEL=$(echo "$MACOS_PASS" | sudo -S softwareupdate --list 2>&1 \
                | grep -E "^\s*\* (Label|Title): Command Line Tools" \
                | head -1 | sed -E 's/.*Command Line Tools[^"]*"?([^"]*)"?.*/\1/' \
                | sed -E 's/^.*: //; s/^\s+//; s/\s+$//')
        else
            sudo touch /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress
            CLT_LABEL=$(sudo softwareupdate --list 2>&1 \
                | grep -E "^\s*\* (Label|Title): Command Line Tools" \
                | head -1 | sed -E 's/.*Command Line Tools[^"]*"?([^"]*)"?.*/\1/' \
                | sed -E 's/^.*: //; s/^\s+//; s/\s+$//')
        fi
        if [[ -z "$CLT_LABEL" ]]; then
            yellow "  Couldn't auto-detect CLT label. softwareupdate output:"
            (echo "${MACOS_PASS:-}" | sudo -S softwareupdate --list 2>&1 || sudo softwareupdate --list 2>&1) | head -20
            die "Find the 'Command Line Tools' label above and run manually:
  sudo softwareupdate --install '<that label>'
Then re-run setup.sh."
        fi
        yellow "  Found CLT package label: $CLT_LABEL"
        yellow "  Installing... (this is the long one — ~5–10 min)"
        if [[ -n "$MACOS_PASS" ]]; then
            echo "$MACOS_PASS" | sudo -S softwareupdate --install "$CLT_LABEL" --verbose
        else
            sudo softwareupdate --install "$CLT_LABEL" --verbose
        fi
        # Cleanup the sentinel.
        (echo "${MACOS_PASS:-}" | sudo -S rm -f /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress 2>/dev/null) \
            || sudo rm -f /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress
        green "  CLT install reports done. Re-running Python detection..."
        # Re-scan candidates now that CLT is installed.
        for cand in "${PY_CANDIDATES[@]}" "$(command -v python3 2>/dev/null)"; do
            [[ -z "$cand" || ! -x "$cand" ]] && continue
            if "$cand" -c "$_PYTHON_VER_CHECK" 2>/dev/null; then
                PYTHON_BINARY="$cand"; break
            fi
        done
        if [[ -z "$PYTHON_BINARY" ]]; then
            die "CLT install ran but Python still not found. Try a manual install:
  • Homebrew:  brew install python@3.12
  • Or check:  xcode-select -p  (should print the CLT install path)
Then re-run setup.sh."
        fi
    else
        # Local terminal path: GUI installer dialog will actually be visible.
        yellow "  About to run:  xcode-select --install"
        yellow "  Pops the official Apple installer dialog on this Mac's display."
        yellow "  Click Install, agree, wait ~5 min, then re-run setup.sh."
        read -rp "  Trigger the CLT installer now? [Y/n] " _ans
        if [[ ! "$_ans" =~ ^[Nn]$ ]]; then
            xcode-select --install 2>&1 | head -5 || true
            yellow "  CLT installer dialog launched. After it finishes, re-run:"
            yellow "    bash setup.sh"
            exit 0
        fi
        die "No Python — install CLT (xcode-select --install), Homebrew Python,
or any python3 ≥3.9, then re-run setup.sh."
    fi
fi
green "  Python: $PYTHON_BINARY"

# ── Step 3: pip install dependencies ──────────────────────────────────────────
step "Installing Python dependencies"

_PIP_FLAGS="--quiet --user"
if "$PYTHON_BINARY" -m pip install --quiet --user --dry-run pip 2>&1 \
        | grep -q "externally-managed-environment"; then
    _PIP_FLAGS="--quiet --user --break-system-packages"
fi

"$PYTHON_BINARY" -m pip install $_PIP_FLAGS \
    'websockets>=13.0' 'numpy>=1.24' 'Pillow>=10.0' 'cryptography>=41.0' \
    || die "pip install failed — check network and pip"

if "$PYTHON_BINARY" -m pip install $_PIP_FLAGS 'av>=12.0' 2>/dev/null; then
    green "  av (PyAV/H.264): installed"
else
    yellow "  av not installed — falling back to JPEG (lower quality)"
    CODEC="jpeg"
fi

yellow "  Installing PyObjC frameworks (5 packages, can take 5–10 min on a fresh Mac)..."
_PYOBJC_FLAGS="${_PIP_FLAGS//--quiet/}"
"$PYTHON_BINARY" -m pip install $_PYOBJC_FLAGS \
    pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-Quartz \
    pyobjc-framework-AVFoundation pyobjc-framework-ScreenCaptureKit \
    || yellow "  PyObjC partial install — SCK may be limited"

# Build deps only when SIP-enabled bundle path is taken.
if [[ "$SIP_DISABLED" -eq 0 ]]; then
    "$PYTHON_BINARY" -m pip install $_PYOBJC_FLAGS 'py2app>=0.28' setuptools \
        || die "py2app install failed — required for the .app bundle build"
fi
green "  Dependencies ready"

# ── Step 4: Web UI access token ───────────────────────────────────────────────
if [[ -z "$MVS_PASSWORD" ]]; then
    MVS_PASSWORD="$(${PYTHON_BINARY} -c 'import secrets; print(secrets.token_urlsafe(16))')"
    green "  Generated web token: $MVS_PASSWORD"
fi

# ── Step 5: Decide on optional VNC bootstrap fallback ─────────────────────────
# VNC fallback serves two roles, both important on headless cloud Macs:
#
#   1. BOOTSTRAP — let the user view the desktop via VNC long enough to
#      grant Screen Recording / Accessibility in System Settings.
#   2. DISPLAY WARMER — on a Mac with NO physical display attached
#      (Scaleway, AWS EC2 Mac without HDMI dongle), SCK reports "no
#      displays" unless something is keeping screensharingd's virtual
#      display rendered. Our own VNC connection counts. Without it,
#      SCK frames go stale even when TCC is granted ("frozen screen"
#      pattern verified 2026-05-04 on Scaleway M2 Tahoe).
#
# Logic: if screensharingd is listening on :5900 AND user provides a
# password (or MACOS_PASS is set in env), enable VNC. Empty password =
# "I have a physical display, no VNC needed" — bundle runs --api-only.
#
# Personal Macs with physical display: empty password → --api-only.
# Cloud Macs (any cloud provider): provide password → bundle keeps VNC
# alive permanently as the display warmer; SCK becomes the capture
# path once granted.
#
# Short-circuit when SIP is disabled: TCC isn't enforcing, no bundle is being
# built, no permissions need granting. The simplest possible path: skip VNC
# entirely, write production plist, start. This is the GitHub-runner /
# custom-image-Mac case — out-of-the-box working with zero prompts.
WANTS_VNC=0
# SSH-vs-local detection. If the user is running setup.sh from a terminal
# open ON the Mac's actual display (not via SSH), they have physical screen
# access and can grant TCC at the keyboard — no VNC bootstrap needed.
RUNNING_FROM_SSH=0
if [[ -n "${SSH_CONNECTION:-}${SSH_CLIENT:-}${SSH_TTY:-}" ]]; then
    RUNNING_FROM_SSH=1
fi

# Detect "screensharingd is installed/configured but currently stopped"
# (e.g. cloud-Mac providers that ship it disabled for security). Distinct
# from "screensharingd not installed at all" — the former is recoverable
# with `sudo launchctl kickstart -k system/com.apple.screensharing` if the
# user explicitly opts into VNC bootstrap.
SSD_INSTALLED_BUT_OFF=0
if [[ "$SIP_DISABLED" -eq 0 ]] && ! nc -z 127.0.0.1 5900 2>/dev/null; then
    if [[ -r /Library/Application\ Support/com.apple.TCC/TCC.db ]] \
            && [[ -n "${MACOS_PASS:-}" ]]; then
        yellow "  About to read /Library/Application Support/com.apple.TCC/TCC.db (read-only, requires sudo) to check screensharingd state..."
        if echo "$MACOS_PASS" | sudo -S sqlite3 \
                /Library/Application\ Support/com.apple.TCC/TCC.db \
                "SELECT 1 FROM access WHERE client='com.apple.screensharing.agent' LIMIT 1" \
                2>/dev/null | grep -q 1; then
            SSD_INSTALLED_BUT_OFF=1
        fi
    fi
fi

if [[ "$SIP_DISABLED" -eq 1 ]]; then
    green "  Skipping VNC bootstrap (SIP off — TCC isn't enforcing, raw API path is fine)"
elif [[ "$RUNNING_FROM_SSH" -eq 0 ]]; then
    green "  Skipping VNC bootstrap (running from local terminal — physical display assumed)"
    green "  You'll grant Screen Recording / Accessibility at the keyboard after install."
elif [[ "$SSD_INSTALLED_BUT_OFF" -eq 1 ]]; then
    if [[ "$HEADLESS" -eq 1 ]]; then
        yellow "  screensharingd appears configured but stopped (port 5900 is closed)."
        yellow "  Headless mode — skipping VNC bootstrap. To enable manually:"
        yellow "    sudo launchctl kickstart -k system/com.apple.screensharing"
    else
        echo
        yellow "  screensharingd is configured but stopped (port 5900 is closed)."
        yellow "  Cloud Mac providers sometimes disable it for security. Without it,"
        yellow "  there's no way to grant Screen Recording on a headless cloud Mac"
        yellow "  unless you have physical access to the keyboard."
        yellow ""
        yellow "  About to start screensharingd via:"
        yellow "    sudo launchctl kickstart -k system/com.apple.screensharing"
        yellow ""
        yellow "  IMPORTANT: this restarts the service with whatever bind config"
        yellow "  was already on disk — typically 0.0.0.0:5900. Make sure your"
        yellow "  cloud provider's firewall blocks external 5900 access if you"
        yellow "  rely on that for security. (We connect to 127.0.0.1:5900 only,"
        yellow "  but other clients on the network could also connect once it's up.)"
        echo
        read -rp "  Start screensharingd now? [y/N] " _ans
        if [[ "$_ans" =~ ^[Yy]$ ]]; then
            yellow "  About to run sudo to start screensharingd..."
            if [[ -n "$MACOS_PASS" ]]; then
                echo "$MACOS_PASS" | sudo -S launchctl kickstart -k system/com.apple.screensharing 2>&1 | head -2
            else
                sudo launchctl kickstart -k system/com.apple.screensharing 2>&1 | head -2
            fi
            sleep 3
            if nc -z 127.0.0.1 5900 2>/dev/null; then
                green "  screensharingd is now listening on :5900"
            else
                yellow "  screensharingd didn't come up. Skipping VNC bootstrap for this run."
            fi
        else
            yellow "  Skipping. Run this command manually if you want VNC bootstrap:"
            yellow "    sudo launchctl kickstart -k system/com.apple.screensharing"
        fi
        unset _ans
    fi
fi
# After potentially starting screensharingd above, re-check the port. If it's
# now up (or was up to begin with), enter the regular VNC-bootstrap prompt.
if nc -z 127.0.0.1 5900 2>/dev/null && [[ "$WANTS_VNC" -eq 0 ]] \
        && [[ "$SIP_DISABLED" -eq 0 ]] \
        && [[ "$RUNNING_FROM_SSH" -eq 1 ]]; then
    if [[ "$HEADLESS" -eq 1 ]]; then
        # Headless: only enable VNC if MACOS_PASS came in via env.
        if [[ -n "$MACOS_PASS" ]]; then
            WANTS_VNC=1
            green "  VNC fallback: enabled (MACOS_PASS provided in env)"
        else
            green "  VNC fallback: skipped (headless, no MACOS_PASS in env)"
        fi
    else
        echo
        yellow "  Optional VNC bootstrap — needed only if you have NO physical access"
        yellow "  to this Mac and will grant Screen Recording via a VNC viewer."
        yellow "  Skip with empty password if you have physical screen access."
        echo
        if [[ -z "$MACOS_PASS" ]]; then
            read -rsp "  macOS login password (Enter to skip VNC bootstrap): " MACOS_PASS
            echo
        fi
        if [[ -n "$MACOS_PASS" ]]; then
            # Validate up to 3 attempts.
            _attempts=0
            while true; do
                if echo "$MACOS_PASS" | sudo -S -v 2>/dev/null; then
                    WANTS_VNC=1
                    green "  Password verified — VNC bootstrap enabled"
                    break
                fi
                _attempts=$((_attempts + 1))
                if [[ $_attempts -ge 3 ]]; then
                    yellow "  Three attempts failed — installing without VNC fallback"
                    MACOS_PASS=""
                    break
                fi
                yellow "  Password rejected. Try again, or press Enter to skip."
                read -rsp "  macOS login password: " MACOS_PASS
                echo
                [[ -z "$MACOS_PASS" ]] && { yellow "  Skipping VNC bootstrap"; break; }
            done
            unset _attempts
        else
            green "  No password — VNC bootstrap skipped (assumes physical screen access)"
        fi
    fi
fi

# Optional friendly note about MDM TCC (informational only — no auto-removal).
# With our own bundle id, MDM TCC policies generally don't block us by default
# (most policies are subtractive against named apps, not allowlists). If a
# specific MDM is locked-down enough to require an allowlist that omits our
# bundle, the user will see the grant fail to take effect; this note tells
# them what to do.
if [[ "$SIP_DISABLED" -eq 0 && -n "${MACOS_PASS:-}" ]]; then
    yellow "  About to run 'sudo profiles show' (read-only, requires sudo) to check for MDM TCC management..."
    if echo "$MACOS_PASS" | sudo -S profiles show 2>/dev/null \
            | grep -q "com.apple.TCC.configuration-profile-policy"; then
        yellow "  Note: an MDM TCC profile is installed. If your grants for"
        yellow "  mac-vnc-stream don't take effect, the MDM policy may need updating."
        yellow "  Some MDMs allowlist by bundle id — ask your admin to allowlist"
        yellow "  '${LABEL}'. As a last resort: 'sudo profiles -R -p <enrollment-id>'"
        yellow "  removes the MDM entirely (reversible — provider re-enrolls)."
    fi
fi

# ── Step 6: Build .app bundle (SIP-enabled only) ──────────────────────────────
LAUNCHAGENT_BINARY=""
APP_DEST="/Applications/mac-vnc-stream.app"
APP_BUILT=""
DID_REBUILD=0   # 1 if we just built/installed a fresh bundle (CDHash changed)

if [[ "$SIP_DISABLED" -eq 0 ]]; then
    # Phase A: install.sh's release-download fast path may have already placed
    # a pre-built bundle and exported MVS_PREBUILT_APP. Treat that as a fresh
    # install (CDHash unknown to TCC).
    if [[ -n "${MVS_PREBUILT_APP:-}" && -d "$MVS_PREBUILT_APP" ]]; then
        APP_BUILT="$MVS_PREBUILT_APP"
        DID_REBUILD=1
        green "  Using pre-built bundle (install.sh release path): $APP_BUILT"
    elif [[ -d "$APP_DEST" ]]; then
        # Phase B: existing bundle in /Applications/. Prompt to keep or rebuild.
        # Keep is the right default in headless mode (preserves existing TCC
        # grants — bundle CDHash unchanged → grants stay valid).
        if [[ "$HEADLESS" -eq 1 ]]; then
            green "  Existing bundle at $APP_DEST — keeping (headless default)"
            APP_BUILT="$APP_DEST"
            DID_REBUILD=0
        else
            echo
            yellow "  An existing mac-vnc-stream.app is installed at:"
            yellow "    $APP_DEST"
            yellow "  Keeping it preserves your existing TCC grants (CDHash unchanged)."
            yellow "  Rebuilding picks up source changes but invalidates grants —"
            yellow "  you'll need to re-toggle Screen Recording / Accessibility."
            echo
            read -rp "  [k]eep or [r]ebuild? [K/r] " _ans
            if [[ "$_ans" =~ ^[Rr]$ ]]; then
                DID_REBUILD=1
            else
                APP_BUILT="$APP_DEST"
                DID_REBUILD=0
            fi
            unset _ans
        fi
    else
        DID_REBUILD=1   # no bundle yet — fresh install
    fi

    if [[ "$DID_REBUILD" -eq 1 && -z "$APP_BUILT" ]]; then
        step "Building .app bundle (com.macvncstream.server)"
        rm -rf "$REPO_DIR/build" "$REPO_DIR/dist"
        (cd "$REPO_DIR" && "$PYTHON_BINARY" build_app.py py2app 2>&1 | tail -10)
        [[ -d "$REPO_DIR/dist/mac-vnc-stream.app" ]] \
            || die "py2app did not produce dist/mac-vnc-stream.app"
        # Install to /Applications/ so the bundle has a stable, system-level path.
        # /Applications/ is owned by root; sudo via cached creds (or interactive
        # prompt if MACOS_PASS isn't set, e.g. personal-Mac local install).
        echo
        yellow "  About to install bundle to /Applications/ — this REQUIRES SUDO:"
        yellow "    sudo rm -rf $APP_DEST"
        yellow "    sudo cp -R $REPO_DIR/dist/mac-vnc-stream.app $APP_DEST"
        yellow "  (Press Ctrl+C to abort if you'd rather install elsewhere manually.)"
        echo
        if [[ -n "$MACOS_PASS" ]]; then
            echo "$MACOS_PASS" | sudo -S rm -rf "$APP_DEST" 2>/dev/null || true
            echo "$MACOS_PASS" | sudo -S cp -R "$REPO_DIR/dist/mac-vnc-stream.app" "$APP_DEST"
        else
            sudo rm -rf "$APP_DEST" || true
            sudo cp -R "$REPO_DIR/dist/mac-vnc-stream.app" "$APP_DEST"
        fi
        APP_BUILT="$APP_DEST"
        green "  Bundle installed at $APP_DEST (com.macvncstream.server, ad-hoc signed)"

        # CDHash changed → any prior TCC grants are tied to the OLD CDHash and
        # will be silently denied by tccd even though System Settings shows
        # "Allowed". Resetting forces a fresh registration on the new bundle's
        # next SCK / AX call, and the user's grant after that records the
        # current CDHash. Without this, we end up in the loop the user just
        # hit: Settings says granted, but every actual SCK call returns -3801.
        echo
        yellow "  About to reset stale TCC grants for com.macvncstream.server"
        yellow "  (CDHash just changed — old grants don't apply to the new bundle):"
        yellow "    sudo tccutil reset ScreenCapture com.macvncstream.server"
        yellow "    sudo tccutil reset Accessibility com.macvncstream.server"
        if [[ -n "$MACOS_PASS" ]]; then
            echo "$MACOS_PASS" | sudo -S tccutil reset ScreenCapture com.macvncstream.server 2>&1 | head -1
            echo "$MACOS_PASS" | sudo -S tccutil reset Accessibility com.macvncstream.server 2>&1 | head -1
        else
            sudo tccutil reset ScreenCapture com.macvncstream.server 2>&1 | head -1
            sudo tccutil reset Accessibility com.macvncstream.server 2>&1 | head -1
        fi
        green "  TCC reset — your next toggle in Settings will record the new CDHash"
    fi

    LAUNCHAGENT_BINARY="$APP_BUILT/Contents/MacOS/mac-vnc-stream"
else
    step "SIP disabled — skipping .app bundle build"
    LAUNCHAGENT_BINARY="$PYTHON_BINARY"
fi

# ── Step 6b: Decide bootstrap vs production mode ──────────────────────────────
# BOOTSTRAP mode (only when DID_REBUILD=1 AND VNC is available): write a
# transient plist with --enable-vnc-fallback + MACOS_PASS in env. After the
# user grants permissions, we rewrite the plist as production (no flag, no
# password) and kickstart-restart. Production restarts thereafter never have
# the password in the env.
#
# Production mode: clean plist, no --enable-vnc-fallback, no MACOS_PASS env.
# Used when bundle is unchanged (grants still valid) OR when no VNC is wanted.
BOOTSTRAP_MODE=0
if [[ "$DID_REBUILD" -eq 1 && "$WANTS_VNC" -eq 1 ]]; then
    BOOTSTRAP_MODE=1
fi

# ── Step 7: write_plist function (called once or twice depending on mode) ─────
write_plist() {
    local include_vnc="$1"   # 1 = include --enable-vnc-fallback + MACOS_PASS env
    local args=(
        "$LAUNCHAGENT_BINARY"
    )
    [[ "$SIP_DISABLED" -eq 1 ]] && args+=("$REPO_DIR/server.py")
    args+=(
        --listen "$LISTEN"
        --port "$PORT"
        --password "$MVS_PASSWORD"
        --max-fps "$MAX_FPS"
        --codec "$CODEC"
    )
    [[ "$include_vnc" -eq 1 ]] && args+=(--enable-vnc-fallback)

    local prog_xml=""
    for a in "${args[@]}"; do prog_xml+="        <string>${a}</string>
"; done

    local env_xml="        <key>MACOS_USER</key><string>${MACOS_USER}</string>
        <key>MVS_PASSWORD</key><string>${MVS_PASSWORD}</string>
"
    if [[ "$include_vnc" -eq 1 && -n "$MACOS_PASS" ]]; then
        env_xml+="        <key>MACOS_PASS</key><string>${MACOS_PASS}</string>
"
    fi

    mkdir -p "$HOME/Library/LaunchAgents"
    local tmp; tmp="$(mktemp /tmp/mvs_plist_XXXXXX.plist)"
    cat > "$tmp" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
${prog_xml}    </array>
    <key>EnvironmentVariables</key>
    <dict>
${env_xml}    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>${LOG_PATH}</string>
    <key>StandardErrorPath</key><string>${LOG_PATH}</string>
</dict>
</plist>
PLIST
    mv "$tmp" "$PLIST_PATH"
    chmod 600 "$PLIST_PATH"
}

# ── Step 8: Write the appropriate plist + (re)load ────────────────────────────
step "Installing LaunchAgent: $PLIST_PATH"
write_plist "$BOOTSTRAP_MODE"
if [[ "$BOOTSTRAP_MODE" -eq 1 ]]; then
    yellow "  Plist: BOOTSTRAP mode — temporarily includes --enable-vnc-fallback"
    yellow "  and MACOS_PASS env. Both are removed after you grant permissions."
else
    green "  Plist: production mode — no VNC flag, no stored password"
fi

if [[ "$NO_LAUNCHAGENT" -eq 1 ]]; then
    step "Skipping LaunchAgent bootstrap (--no-launchagent)"
    green "  Plist written to $PLIST_PATH but not loaded."
    green "  Start it manually with:  launchctl bootstrap gui/\$(id -u) $PLIST_PATH"
    green "  Or run the bundle directly:  $LAUNCHAGENT_BINARY"
    LOAD_DOMAIN=""
else
    step "Starting mac-vnc-stream service"
    # Belt-and-suspenders cleanup: bootout the LaunchAgent if loaded, then
    # pkill any stray bundle processes (could be from a prior `open -a`,
    # direct binary launch, or a bootout that didn't fully tear down).
    # Without this, the new bootstrap can race with a stale process for
    # port 6081 and fail with EADDRINUSE.
    launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
    pkill -9 -f "/Applications/mac-vnc-stream.app/Contents/MacOS/mac-vnc-stream" 2>/dev/null || true
    pkill -9 -f "${REPO_DIR}/dist/mac-vnc-stream.app/Contents/MacOS/mac-vnc-stream" 2>/dev/null || true
    sleep 2
    if launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH" 2>&1; then
        LOAD_DOMAIN="gui/$(id -u)"
        green "  Loaded into ${LOAD_DOMAIN}"
    else
        die "launchctl bootstrap into gui/$(id -u) failed.
This usually means there's no active console (Aqua) session yet. Either:
  • Log in via VNC at vnc://127.0.0.1:5900 once, then re-run setup.sh
  • Or attach a display + login locally"
    fi

    echo -n "  Waiting for server"
    WAITED=0
    while [[ $WAITED -lt 15 ]]; do
        if nc -z 127.0.0.1 "$PORT" 2>/dev/null; then echo; green "  Server up on :$PORT"; break; fi
        sleep 1; WAITED=$((WAITED + 1)); echo -n "."
    done
    [[ $WAITED -ge 15 ]] && yellow "  Server slow to start — check $LOG_PATH"
fi

# ── Step 9b: Bootstrap → production transition (interactive only) ────────────
# When we just rebuilt the bundle AND VNC is acting as the bootstrap path,
# pause here so the user can grant permissions to the new bundle (CDHash
# changed → grants invalidated even if previously granted). Once they
# confirm, rewrite the plist as production (no VNC flag, no MACOS_PASS env)
# and kickstart-restart. Production restarts thereafter never expose the
# password.
#
# Headless mode: skip the interactive wait. The user re-runs setup.sh when
# they're ready to transition (or the bootstrap state runs indefinitely
# until they do — VNC stays available).
if [[ "$BOOTSTRAP_MODE" -eq 1 && "$HEADLESS" -eq 0 ]]; then
    MAC_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo '<mac-ip>')"
    URL="http://localhost:${PORT}/?token=${MVS_PASSWORD}"
    if [[ "$LISTEN" == "127.0.0.1" ]]; then
        TUNNEL_HINT="  ssh -L ${PORT}:127.0.0.1:${PORT} ${MACOS_USER}@${MAC_IP}"
    else
        TUNNEL_HINT=""
    fi
    echo
    yellow "  ┌─ BOOTSTRAP MODE: GRANT PERMISSIONS THEN PRESS ENTER ──────────────┐"
    yellow "  │  The bundle is running with VNC fallback so you can view the      │"
    yellow "  │  desktop while you grant TCC permissions to the (new-CDHash)      │"
    yellow "  │  bundle. Open the URL in your browser:                            │"
    yellow "  │"
    yellow "  │    $URL"
    if [[ -n "$TUNNEL_HINT" ]]; then
        yellow "  │  Via SSH tunnel:"
        yellow "  │$TUNNEL_HINT"
    fi
    yellow "  │"
    yellow "  │  In System Settings ▸ Privacy & Security:"
    yellow "  │    • Screen Recording → toggle ON for 'mac-vnc-stream'"
    yellow "  │    • Accessibility    → toggle ON for 'mac-vnc-stream'"
    yellow "  │"
    yellow "  │  When done, press Enter to switch to production mode:"
    yellow "  │    • drops --enable-vnc-fallback from the plist"
    yellow "  │    • removes MACOS_PASS from the plist env"
    yellow "  │    • restarts the bundle in pure --api-only (SCK + CGEvent)"
    yellow "  └────────────────────────────────────────────────────────────────────┘"
    echo
    read -rp "  Press Enter when permissions are granted (or Ctrl+C to leave in bootstrap mode): " _

    step "Switching to production mode"
    write_plist 0   # production: no --enable-vnc-fallback, no MACOS_PASS env
    green "  Plist rewritten — credentials removed"
    # bootout + bootstrap (NOT kickstart -k). kickstart -k just SIGTERMs the
    # process; KeepAlive then restarts it with the CACHED service definition,
    # so the previous --enable-vnc-fallback flag and MACOS_PASS env survive
    # even though the plist on disk no longer has them. bootout + bootstrap
    # forces launchd to re-parse the plist from disk.
    launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
    pkill -9 -f "/Applications/mac-vnc-stream.app/Contents/MacOS/mac-vnc-stream" 2>/dev/null || true
    sleep 2
    launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH" 2>&1 | head -2
    sleep 4
    if grep -E "capture=SCK|InProcessSCK: stream active" "$LOG_PATH" 2>/dev/null | tail -1 | grep -q "."; then
        green "  Production mode active — SCK capture confirmed"
    else
        yellow "  Switched to production plist; SCK status unconfirmed in log."
        yellow "  Check $LOG_PATH — if it still says 'no displays' you may need"
        yellow "  to re-run setup.sh with VNC fallback to keep the display alive."
    fi
fi

# ── Step 10: TCC state check + final banner ───────────────────────────────────
step "Connection info"
MAC_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo '<mac-ip>')"

# Detect if SCK + AX are already granted.
TCC_OK=0
if [[ "$SIP_DISABLED" -eq 1 ]]; then
    TCC_OK=1
elif [[ -n "$APP_BUILT" ]] && "$APP_BUILT/Contents/MacOS/mac-vnc-stream" --tcc-check 2>/dev/null; then
    TCC_OK=1
elif "$PYTHON_BINARY" -c '
import ctypes, sys
cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
ax = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
ax.AXIsProcessTrusted.restype = ctypes.c_bool
sys.exit(0 if (cg.CGPreflightScreenCaptureAccess() and ax.AXIsProcessTrusted()) else 1)
' 2>/dev/null; then
    TCC_OK=1
fi

echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
green "  mac-vnc-stream is running"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo
if [[ "$LISTEN" == "127.0.0.1" ]]; then
    echo "  Access via SSH tunnel (server is loopback-only):"
    echo "    ssh -L ${PORT}:127.0.0.1:${PORT} ${MACOS_USER}@${MAC_IP}"
    echo "    then open: http://127.0.0.1:${PORT}/?token=${MVS_PASSWORD}"
else
    echo "  Direct: http://${MAC_IP}:${PORT}/?token=${MVS_PASSWORD}"
fi
echo

if [[ "$SIP_DISABLED" -eq 1 ]]; then
    green "  Mode: raw LaunchAgent → server.py (SIP disabled, TCC bypassed)"
elif [[ "$TCC_OK" -eq 1 ]]; then
    green "  Mode: SCK 60fps + CGEvent input via signed bundle"
    green "  Bundle id: ${LABEL} — TCC has honored your grants. Production path."
else
    yellow "  Mode: signed bundle awaiting TCC grants"
    yellow "  ┌─ Grant permissions in System Settings ▸ Privacy & Security ──────┐"
    yellow "  │  • Screen Recording  → toggle ON for 'mac-vnc-stream'             │"
    yellow "  │  • Accessibility     → toggle ON for 'mac-vnc-stream'             │"
    yellow "  │                                                                    │"
    yellow "  │  The app should already be in both lists (we ran a TCC probe).    │"
    yellow "  │  If it isn't, click '+', press Cmd+Shift+G in the file picker,    │"
    yellow "  │  paste this path, and click Open:                                 │"
    yellow "  │    ${APP_BUILT}"
    yellow "  │                                                                    │"
    yellow "  │  Once granted, the server picks up the change within ~30 s — no   │"
    yellow "  │  restart needed. The browser will auto-switch from the placeholder │"
    yellow "  │  message to the live screen.                                      │"
    yellow "  └────────────────────────────────────────────────────────────────────┘"
    if [[ "$WANTS_VNC" -eq 1 ]]; then
        green "  VNC bootstrap is active — you can already see the desktop in your"
        green "  browser. Use it to grant the permissions above."
    else
        yellow "  No VNC bootstrap was set up. The browser will show a 'permissions"
        yellow "  needed' message until you grant Screen Recording at the keyboard."
    fi
fi

echo
echo "  Log:    tail -f $LOG_PATH"
echo "  Restart: launchctl kickstart -k ${LOAD_DOMAIN}/${LABEL}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
