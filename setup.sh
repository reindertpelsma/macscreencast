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
BUILD_FROM_SOURCE=0
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
        --build-from-source) BUILD_FROM_SOURCE=1; shift ;;
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

# ── Step 5: Bundle decision ───────────────────────────────────────────────────
# Done BEFORE the VNC decision because the bundle's TCC state determines
# whether VNC bootstrap is even needed. Keep + valid grants → trivial happy
# path: skip VNC entirely, just (re-)bootstrap the LaunchAgent.
LAUNCHAGENT_BINARY=""
APP_BUILT=""
APP_DEST="/Applications/mac-vnc-stream.app"
REBUILD_NEEDED=0
TCC_GRANTED=0
NEEDS_TCC_RESET=0
DID_WE_CHECKED_SCREENSHARINGD=0
SCREENSHARINGD_PRESENT=0

if [[ "$SIP_DISABLED" -eq 1 ]]; then
    # SIP off: TCC isn't enforcing. Run server.py directly, no bundle.
    TCC_GRANTED=1
    REBUILD_NEEDED=0
    LAUNCHAGENT_BINARY="$PYTHON_BINARY"
    green "  SIP off → no bundle needed, raw LaunchAgent path"
elif [[ -d "$APP_DEST" ]]; then
    step "Existing bundle"
    yellow "  Found: $APP_DEST"
    yellow "  Keep preserves grants (CDHash unchanged); rebuild picks up source"
    yellow "  changes but invalidates grants."
    if [[ "$HEADLESS" -eq 1 ]]; then
        green "  Headless mode — keeping (default)"
        REBUILD_NEEDED=0
    elif [[ "$BUILD_FROM_SOURCE" -eq 1 ]]; then
        yellow "  --build-from-source given — forcing rebuild"
        REBUILD_NEEDED=1
    else
        read -rp "  [k]eep or [r]ebuild? [K/r] " _ans
        [[ "$_ans" =~ ^[Rr]$ ]] && REBUILD_NEEDED=1
        unset _ans
    fi
    if [[ "$REBUILD_NEEDED" -eq 0 ]]; then
        APP_BUILT="$APP_DEST"
        LAUNCHAGENT_BINARY="$APP_DEST/Contents/MacOS/mac-vnc-stream"
        # Probe TCC. --tcc-check exit 0 = both grants valid AND CDHash matches.
        if "$LAUNCHAGENT_BINARY" --tcc-check >/dev/null 2>&1; then
            TCC_GRANTED=1
            green "  Existing bundle has valid TCC grants — straight to production mode"
        else
            yellow "  Existing bundle is missing or has stale TCC grants"
            NEEDS_TCC_RESET=1   # stale-CDHash-on-keep → reset before re-grant
        fi
    fi
else
    REBUILD_NEEDED=1   # no bundle yet — fresh install
fi

# ── Step 6: ensure_screensharingd helper (memoized) ──────────────────────────
ensure_screensharingd() {
    # Returns 0 (true) if screensharingd is running on :5900, non-zero otherwise.
    # Memoized: subsequent calls return the cached result without re-prompting.
    # If port 5900 is closed but screensharingd is configured (TCC-known), prompts
    # the user to start it, with a one-line $1 reason.
    local reason="${1:-keep the screen alive}"
    if [[ "$DID_WE_CHECKED_SCREENSHARINGD" -eq 1 ]]; then
        return $((1 - SCREENSHARINGD_PRESENT))
    fi
    DID_WE_CHECKED_SCREENSHARINGD=1
    if nc -z 127.0.0.1 5900 2>/dev/null; then
        SCREENSHARINGD_PRESENT=1; return 0
    fi
    # Configured-but-stopped detection requires sudo to read TCC.db.
    local ssd_known=0
    if [[ -n "${MACOS_PASS:-}" ]] && \
       echo "$MACOS_PASS" | sudo -S sqlite3 \
           /Library/Application\ Support/com.apple.TCC/TCC.db \
           "SELECT 1 FROM access WHERE client='com.apple.screensharing.agent' LIMIT 1" \
           2>/dev/null | grep -q 1; then
        ssd_known=1
    fi
    if [[ "$ssd_known" -eq 0 ]]; then
        return 1   # not configured, can't start
    fi
    if [[ "$HEADLESS" -eq 1 ]]; then
        yellow "  screensharingd configured but stopped. Headless — skipping."
        yellow "  Start manually: sudo launchctl kickstart -k system/com.apple.screensharing"
        return 1
    fi
    echo
    yellow "  screensharingd is configured but stopped (port 5900 closed)."
    yellow "  Reason it's needed: $reason"
    yellow "  Will run: sudo launchctl kickstart -k system/com.apple.screensharing"
    yellow "  IMPORTANT: respects on-disk bind config (typically 0.0.0.0:5900)."
    yellow "  Cloud-provider firewalls should block external 5900 if you rely on that."
    read -rp "  Start screensharingd now? [y/N] " _ans
    if [[ ! "$_ans" =~ ^[Yy]$ ]]; then
        unset _ans
        return 1
    fi
    unset _ans
    if [[ -n "$MACOS_PASS" ]]; then
        echo "$MACOS_PASS" | sudo -S launchctl kickstart -k system/com.apple.screensharing 2>&1 | head -2
    else
        sudo launchctl kickstart -k system/com.apple.screensharing 2>&1 | head -2
    fi
    sleep 3
    if nc -z 127.0.0.1 5900 2>/dev/null; then
        SCREENSHARINGD_PRESENT=1
        green "  screensharingd is now listening on :5900"
        return 0
    fi
    yellow "  screensharingd didn't come up — skipping"
    return 1
}

# ── Step 7: VNC fallback decision (only when needed) ─────────────────────────
# VNC fallback is needed when ALL THREE are true:
#   • SIP enabled (TCC is enforcing)
#   • TCC not granted yet (so we need a way for the user to grant)
#   • Running over SSH (no physical screen access)
# Otherwise the user can just grant at the keyboard.
VNC_FALLBACK=0
if [[ "$SIP_DISABLED" -eq 0 && "$TCC_GRANTED" -eq 0 && "$RUNNING_FROM_SSH" -eq 1 ]]; then
    if ensure_screensharingd "to view the desktop while granting Screen Recording / Accessibility"; then
        echo
        yellow "  Optional VNC bootstrap. Provides a live desktop view in your browser"
        yellow "  while you grant TCC permissions. Skip with empty password if you'll"
        yellow "  grant via another method (physical screen, Apple Screen Sharing.app)."
        if [[ "$HEADLESS" -eq 1 ]]; then
            [[ -n "$MACOS_PASS" ]] && VNC_FALLBACK=1
        else
            if [[ -z "$MACOS_PASS" ]]; then
                read -rsp "  macOS login password (Enter to skip): " MACOS_PASS
                echo
            fi
            if [[ -n "$MACOS_PASS" ]]; then
                _attempts=0
                while true; do
                    if echo "$MACOS_PASS" | sudo -S -v 2>/dev/null; then
                        VNC_FALLBACK=1
                        green "  Password verified — VNC bootstrap enabled"
                        break
                    fi
                    _attempts=$((_attempts + 1))
                    if [[ $_attempts -ge 3 ]]; then
                        yellow "  Three attempts failed — skipping VNC"
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
                green "  No password — skipping VNC (assumes physical screen access)"
            fi
        fi
    fi
fi

# Optional MDM TCC profile detection (informational only — no auto-action).
if [[ "$SIP_DISABLED" -eq 0 && -n "${MACOS_PASS:-}" ]]; then
    yellow "  About to run 'sudo profiles show' (read-only) to check for MDM TCC management..."
    if echo "$MACOS_PASS" | sudo -S profiles show 2>/dev/null \
            | grep -q "com.apple.TCC.configuration-profile-policy"; then
        yellow "  Note: MDM TCC profile installed. If grants don't take effect, ask"
        yellow "  admin to allowlist '${LABEL}', or remove enrollment as last resort:"
        yellow "    sudo profiles -R -p <enrollment-id-from 'sudo profiles show'>"
    fi
fi

# ── Step 8: Build/install bundle if needed ────────────────────────────────────
if [[ "$REBUILD_NEEDED" -eq 1 && "$SIP_DISABLED" -eq 0 ]]; then
    step "Building .app bundle (com.macvncstream.server)"
    rm -rf "$REPO_DIR/build" "$REPO_DIR/dist"
    (cd "$REPO_DIR" && "$PYTHON_BINARY" build_app.py py2app 2>&1 | tail -10)
    [[ -d "$REPO_DIR/dist/mac-vnc-stream.app" ]] \
        || die "py2app did not produce dist/mac-vnc-stream.app"
    echo
    yellow "  About to install bundle to /Applications/ — REQUIRES SUDO:"
    yellow "    sudo rm -rf $APP_DEST"
    yellow "    sudo cp -R $REPO_DIR/dist/mac-vnc-stream.app $APP_DEST"
    if [[ -n "$MACOS_PASS" ]]; then
        echo "$MACOS_PASS" | sudo -S rm -rf "$APP_DEST" 2>/dev/null || true
        echo "$MACOS_PASS" | sudo -S cp -R "$REPO_DIR/dist/mac-vnc-stream.app" "$APP_DEST"
    else
        sudo rm -rf "$APP_DEST" || true
        sudo cp -R "$REPO_DIR/dist/mac-vnc-stream.app" "$APP_DEST"
    fi
    APP_BUILT="$APP_DEST"
    LAUNCHAGENT_BINARY="$APP_BUILT/Contents/MacOS/mac-vnc-stream"
    green "  Bundle installed at $APP_DEST (com.macvncstream.server, ad-hoc signed)"
    NEEDS_TCC_RESET=1   # CDHash changed → grants invalidated
    TCC_GRANTED=0
fi

# ── Step 9: tccutil reset if needed (rebuild OR stale-CDHash-on-keep) ────────
if [[ "$NEEDS_TCC_RESET" -eq 1 && "$SIP_DISABLED" -eq 0 ]]; then
    echo
    yellow "  Resetting TCC for com.macvncstream.server (CDHash mismatch)..."
    yellow "    sudo tccutil reset ScreenCapture com.macvncstream.server"
    yellow "    sudo tccutil reset Accessibility com.macvncstream.server"
    if [[ -n "$MACOS_PASS" ]]; then
        echo "$MACOS_PASS" | sudo -S tccutil reset ScreenCapture com.macvncstream.server 2>&1 | head -1
        echo "$MACOS_PASS" | sudo -S tccutil reset Accessibility com.macvncstream.server 2>&1 | head -1
    else
        sudo tccutil reset ScreenCapture com.macvncstream.server 2>&1 | head -1
        sudo tccutil reset Accessibility com.macvncstream.server 2>&1 | head -1
    fi
    green "  TCC reset — next toggle in Settings records current CDHash"
fi

# ── Step 10: Bootstrap mode flag ─────────────────────────────────────────────
# Bootstrap = transient plist with --enable-vnc-fallback + MACOS_PASS env.
# Used only when TCC is not granted AND VNC fallback is enabled. Otherwise
# straight to production plist (and if TCC is also not granted, the user
# grants manually after install — see final banner).
BOOTSTRAP_MODE=0
if [[ "$VNC_FALLBACK" -eq 1 && "$TCC_GRANTED" -eq 0 ]]; then
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
    yellow "  │  In System Settings ▸ Privacy & Security, you need TWO grants:"
    yellow "  │    • Screen Recording → toggle ON for 'mac-vnc-stream'"
    yellow "  │    • Accessibility    → toggle ON for 'mac-vnc-stream'"
    yellow "  │"
    yellow "  │  ── If 'mac-vnc-stream' is in the list ──────────────────────────"
    yellow "  │  Just toggle it ON in each pane."
    yellow "  │"
    yellow "  │  ── If 'mac-vnc-stream' is NOT in the list (Tahoe Screen "
    yellow "  │     Recording sometimes doesn't auto-register, even though"
    yellow "  │     Accessibility does) ───────────────────────────────────────"
    yellow "  │  1. Click '+' at the bottom-left of the pane."
    yellow "  │  2. Press  Cmd+Shift+G  (file picker shortcut for 'Go to Folder')."
    yellow "  │  3. Paste this exact path and click Open:"
    yellow "  │"
    yellow "  │       ${APP_BUILT}"
    yellow "  │"
    yellow "  │  4. Toggle the new 'mac-vnc-stream' entry ON."
    yellow "  │  5. Repeat for the other pane (Screen Recording AND Accessibility"
    yellow "  │     are TWO separate grants — both must be on)."
    yellow "  │"
    yellow "  │  When done, press Enter to switch to production mode:"
    yellow "  │    • drops --enable-vnc-fallback from the plist"
    yellow "  │    • removes MACOS_PASS from the plist env"
    yellow "  │    • restarts the bundle in pure --api-only (SCK + CGEvent)"
    yellow "  └────────────────────────────────────────────────────────────────────┘"
    echo
    # Loop until grants are actually applied OR the user Ctrl+C's out.
    # Probe via the bundle's --tcc-check (exit 0 = both grants valid for
    # com.macvncstream.server with current CDHash, exit 1 = something
    # missing). Pressing Enter without granting just gives them another
    # chance — no destructive transition unless TCC actually verifies.
    while true; do
        read -rp "  Press Enter when permissions are granted (Ctrl+C to leave in bootstrap mode): " _
        sleep 2  # give tccd a moment to commit the toggle
        if "$LAUNCHAGENT_BINARY" --tcc-check >/dev/null 2>&1; then
            green "  Both grants confirmed — switching to production mode."
            break
        fi
        # Try to give the user actionable detail about which grant is missing
        # by reading --tcc-check's stdout (it prints screen_recording=0/1 +
        # accessibility=0/1 lines).
        _tcc_out="$("$LAUNCHAGENT_BINARY" --tcc-check 2>&1 || true)"
        _missing=""
        if echo "$_tcc_out" | grep -q "screen_recording=0"; then _missing+=" Screen Recording"; fi
        if echo "$_tcc_out" | grep -q "accessibility=0";    then _missing+=" Accessibility"; fi
        echo
        yellow "  Not granted yet — still missing:${_missing:-' (TCC error — see log)'}"
        yellow "  Open System Settings ▸ Privacy & Security and toggle the missing"
        yellow "  panes ON for 'mac-vnc-stream'. If the entry isn't in the list,"
        yellow "  click '+', press Cmd+Shift+G, paste:"
        yellow "    ${APP_BUILT}"
        yellow "  Then come back here and press Enter again. Or Ctrl+C to leave"
        yellow "  the bundle running in bootstrap (VNC) mode and finish later."
        echo
        unset _tcc_out _missing
    done

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

# Detect if SCK + AX are granted FOR THIS BUNDLE. Use the bundle's own
# --tcc-check exclusively — running CGPreflight via the host's python3 here
# would check setup.sh's identity (bash) which doesn't match com.macvncstream.server,
# so the result would always be False even when the bundle's grants ARE valid.
TCC_OK=0
if [[ "$SIP_DISABLED" -eq 1 ]]; then
    TCC_OK=1
elif [[ -n "$APP_BUILT" ]] && "$APP_BUILT/Contents/MacOS/mac-vnc-stream" --tcc-check 2>/dev/null; then
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
    elif [[ "$DISPLAY_ATTACHED" -eq 1 ]]; then
        # Personal Mac with physical screen — user grants at the keyboard.
        yellow "  Personal Mac path: grant the permissions on the Mac itself"
        yellow "  (we detected an attached display). The browser will be black"
        yellow "  until both grants are toggled on."
    else
        # Headless cloud Mac without VNC and without a display: the browser
        # WILL show a permissions-needed page (server.py serves that when
        # SCK has no displays), but SCK ultimately can't activate without
        # either a dongle or VNC. Tell the user the way out is to re-run
        # setup.sh and provide the macOS password (which enables VNC
        # bootstrap, which keeps the virtual display alive).
        red    "  ┌─ NO DISPLAY + NO VNC = SERVER WILL STAY FROZEN ────────────────┐"
        yellow "  │  The bundle is installed and the LaunchAgent is running, but   │"
        yellow "  │  there's no way to capture frames: no physical display is      │"
        yellow "  │  attached, and you didn't enable VNC bootstrap (no MACOS_PASS  │"
        yellow "  │  was provided). Pick one to recover:                            │"
        yellow "  │                                                                  │"
        yellow "  │  1. Attach a 'headless display dongle' (~\$10) and re-run setup. │"
        yellow "  │  2. Re-run setup.sh and provide your macOS password — the VNC  │"
        yellow "  │     fallback will keep the virtual display alive while you     │"
        yellow "  │     grant permissions, then drop to pure SCK production.       │"
        yellow "  │  3. Manually enable Screen Sharing on the Mac (System Settings │"
        yellow "  │     ▸ Sharing) and re-run setup.sh — that re-detects 5900 and  │"
        yellow "  │     prompts for the password.                                   │"
        red    "  └──────────────────────────────────────────────────────────────────┘"
    fi
fi

echo
echo "  Log:    tail -f $LOG_PATH"
echo "  Restart: launchctl kickstart -k ${LOAD_DOMAIN}/${LABEL}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
