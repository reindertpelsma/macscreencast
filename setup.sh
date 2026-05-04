#!/usr/bin/env bash
# setup.sh — install mac-vnc-stream from this git checkout.
#
# Policy: always build the .app bundle (~3–5 min first run, cached after)
# and install a LaunchAgent that launches the bundle binary. We run as
# com.macvncstream.server, NOT as the shared Python interpreter, so:
#
#   • TCC tracks grants against OUR bundle id, not com.apple.python3
#     (Tahoe explicitly refuses to honor Screen Recording grants for
#     interpreters; bundle id escapes that restriction)
#   • users grant permissions only to this app — not to the shared
#     interpreter that any other Python script on the system could exploit
#   • the same install path works on every macOS: SIP-on, SIP-off,
#     personal Mac, headless cloud Mac, GitHub macOS runner. SIP-off does
#     NOT disable TCC enforcement; running the interpreter directly is
#     fragile in all cases.
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
ASSUME_TCC=""              # "granted" | "denied" | "" (probe normally)
START_SSD_MODE=""          # "yes" | "lockdown" | "no" | "" (prompt)
NO_BOOTSTRAP_WAIT=0        # 1 = transition to production immediately, no Enter wait
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
        --assume-tcc-granted) ASSUME_TCC="granted"; shift ;;
        --no-tcc-probe|--assume-tcc-not-granted) ASSUME_TCC="denied"; shift ;;
        --start-screensharingd)
            START_SSD_MODE="$2"
            case "$START_SSD_MODE" in
                yes|lockdown|no) shift 2 ;;
                *) die "--start-screensharingd needs yes|lockdown|no, got: $START_SSD_MODE" ;;
            esac ;;
        --no-bootstrap-wait) NO_BOOTSTRAP_WAIT=1; shift ;;
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
  --no-launchagent   skip the LaunchAgent install — build bundle and write
                     the plist template, but don't bootstrap. Useful for audit
                     /preview, or when you'll launch the bundle manually.
  --assume-tcc-granted    skip the keep-path TCC probe; assume bundle has
                          valid TCC grants. Use when grants are known-warm
                          (e.g. just granted, re-running setup.sh shortly
                          after).
  --no-tcc-probe          skip the keep-path TCC probe; assume bundle does
                          NOT have valid grants. Forces VNC bootstrap path.
                          Safe default for headless / scripted use.
                          (Alias: --assume-tcc-not-granted)
  --start-screensharingd MODE   skip the screensharingd start prompt. MODE:
                          • yes      — start it, don't install pf rule
                          • lockdown — start it AND install pf rule blocking
                                       external :5900 (loopback only)
                          • no       — don't start it (skip VNC fallback)
  --no-bootstrap-wait     don't pause for the user-grants-then-Enter step.
                          After starting the bundle, transition to production
                          immediately. The server's runtime auto-upgrade loop
                          (server.py:170-194) detects late-arriving grants
                          within ~30s. Useful for fully-unattended installs;
                          loses the "probe confirms grants applied" feedback.

Reads MVS_HEADLESS, MACOS_PASS, MVS_PASSWORD from env if unset.

Headless defaults: --headless implies opinionated cloud-Mac defaults so
unattended installs don't get stuck:
  • --start-screensharingd=lockdown   (assume cloud Mac without security group)
  • --no-tcc-probe                    (probe is unreliable in headless contexts)
  • --no-bootstrap-wait               (no human to press Enter)
Override individually by passing the explicit flags after --headless.

TCC probe policy: --tcc-check is ONLY run on the keep path (existing
bundle whose grants might still be valid). After a rebuild the CDHash is
new → any prior grants are invalidated by csreq mismatch → no point
probing. setup.sh assumes "not granted" and offers VNC bootstrap. The
probe is also skipped on hosts where parent-shell inheritance fools it
(e.g. GitHub macos runners pre-grant /bin/bash); use the override flags
above to bypass.

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

# --headless implies opinionated cloud-Mac defaults — pick safe choices for
# every prompt setup.sh would otherwise show. User can override individually
# by passing the explicit flag after --headless. We only fill in defaults
# that haven't been explicitly set, so user intent always wins.
if [[ "$HEADLESS" -eq 1 ]]; then
    [[ -z "$ASSUME_TCC" ]]      && ASSUME_TCC="denied"      # rebuild path always; keep path: assume not granted (safer)
    [[ -z "$START_SSD_MODE" ]]  && START_SSD_MODE="lockdown" # cloud-Mac default
    NO_BOOTSTRAP_WAIT=1                                      # no human to press Enter
fi

step "mac-vnc-stream installer"
echo "  Repo:    $REPO_DIR"
echo "  User:    $MACOS_USER"
echo "  Headless: $([[ $HEADLESS -eq 1 ]] && echo yes || echo no)"

# ── Step 1: Detect macOS environment ──────────────────────────────────────────
# TCC enforcement is mandatory on every modern macOS regardless of SIP state.
# SIP-off does NOT disable TCC. The bundle path (com.macvncstream.server)
# is the only reliable way to grant Screen Recording / Accessibility on
# Sonoma+ and the only path that works at all on Tahoe (which refuses to
# honor grants for interpreters like com.apple.python3). So we don't branch
# on SIP — we always build and install the bundle.
step "Detecting environment"
green "  → will build .app bundle (com.macvncstream.server) so TCC tracks"
green "    grants against this app, not the shared Python interpreter"

# SSH-vs-local detection. Drives the VNC-fallback decision later: only
# offer VNC when running over SSH (no physical screen access). Local
# terminals on the Mac's own display assume the user can grant TCC at
# the keyboard.
RUNNING_FROM_SSH=0
if [[ -n "${SSH_CONNECTION:-}${SSH_CLIENT:-}${SSH_TTY:-}" ]]; then
    RUNNING_FROM_SSH=1
elif [[ -n "${TMUX:-}${TMATE_VERSION:-}" ]]; then
    # tmate/tmux sessions strip SSH_CONNECTION from the inner shell, but
    # if we're inside one of them on macOS it's almost certainly a remote
    # session (you don't run tmate locally for fun). Treat as remote.
    RUNNING_FROM_SSH=1
elif [[ "$(ps -o comm= -p "$PPID" 2>/dev/null || true)" == *"sshd"* ]]; then
    # Final fallback: parent process is sshd. Catches edge cases where the
    # shell was launched fresh inside an SSH session that scrubbed env.
    RUNNING_FROM_SSH=1
fi

# Display detection — used for the no-display + no-VNC frozen-screen
# warning. Best-effort; system_profiler is slow on first call so we cache
# the result.
DISPLAY_ATTACHED=0
if command -v system_profiler >/dev/null 2>&1 \
        && system_profiler SPDisplaysDataType 2>/dev/null | grep -q "Resolution:"; then
    DISPLAY_ATTACHED=1
fi

# screensharingd pre-decision. We only consider screensharingd at all when:
#   (a) the host is potentially headless — running over SSH with no
#       physical display attached. A physical display means SCK has its
#       own backend; we never need to start screensharingd just for that.
#   (b) screensharingd is actually installed on this Mac (its launchd
#       plist exists). Cloud Macs that ship without it can't use this
#       path regardless.
# screensharingd is needed on a headless Mac both as permission-grant
# viewer (so the SSH user can navigate System Settings to grant TCC) AND
# as display warmer (so SCK has a renderable display backend after grants
# land). Both roles apply identically across all macs — there is no
# SIP-state branch.
SCREENSHARINGD_PRESENT=0
DID_WE_CHECKED_SCREENSHARINGD=0
# screensharingd is needed when the user can't see the desktop locally:
#   • RUNNING_FROM_SSH=1 — user is remote even if a display is attached
#     (Scaleway / cloud Macs with HDMI dongles register a "display" that
#     shows up identically to a real monitor in system_profiler, but the
#     remote user can't see it; they still need the bundle's VNC bridge
#     as a browser viewer)
#   • DISPLAY_ATTACHED=0 — no display at all (truly headless)
# Either signal means VNC fallback path is the user's only route to
# System Settings; we need screensharingd as the local source.
if [[ -f /System/Library/LaunchDaemons/com.apple.screensharing.plist ]] \
   && { [[ "$RUNNING_FROM_SSH" -eq 1 ]] || [[ "$DISPLAY_ATTACHED" -eq 0 ]]; }; then
    SCREENSHARINGD_PRESENT=1
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

# py2app is always required — the bundle is the only install path.
"$PYTHON_BINARY" -m pip install $_PYOBJC_FLAGS 'py2app>=0.28' setuptools \
    || die "py2app install failed — required for the .app bundle build"
green "  Dependencies ready"

# ── Step 4: Web UI access token ───────────────────────────────────────────────
if [[ -z "$MVS_PASSWORD" ]]; then
    MVS_PASSWORD="$(${PYTHON_BINARY} -c 'import secrets; print(secrets.token_urlsafe(16))')"
    green "  Generated web token: $MVS_PASSWORD"
fi

# ── Authoritative TCC probe via TCC.db sudo query ────────────────────────────
# The bundle's --tcc-check probe has multiple known failure modes:
#   • PyObjC block-signature error on Tahoe ("Argument 4 is a block, but no
#     signature available") — fixed by importing pyobjc-framework-ScreenCaptureKit
#     properly, but only takes effect after rebuild
#   • parent-shell inheritance lying on hosts where the launching shell has
#     Screen Recording grant (GH macos-latest pre-grants /bin/bash)
#   • CGPreflight returning True optimistically on some macOS versions
#
# When we have MACOS_PASS, query TCC.db directly via sudo+sqlite3. That's the
# kernel's actual source of truth; not subject to any of the runtime quirks
# above. Returns:
#   echo "granted" | exit 0  — both rows have auth_value=2
#   echo "missing" | exit 1  — at least one row missing or auth_value!=2
#   echo "unknown" | exit 2  — no MACOS_PASS available (can't sudo)
tcc_probe() {
    if [[ -z "${MACOS_PASS:-}" ]]; then
        echo "unknown"
        return 2
    fi
    local _q="SELECT service, auth_value FROM access WHERE client='com.macvncstream.server' AND auth_value=2"
    local _sr=0 _ax=0
    # Query BOTH the system TCC.db and the per-user TCC.db. Apple's split
    # between the two has shifted across releases — Screen Recording is
    # typically system-wide on Sonoma+, but checking both costs nothing
    # and reduces false-negative risk if Apple moves it again or if the
    # user is on an older / quirky macOS. Take the max across the two.
    local _out_sys _out_user
    _out_sys="$(echo "$MACOS_PASS" | sudo -S sqlite3 \
        "/Library/Application Support/com.apple.TCC/TCC.db" \
        "$_q" 2>/dev/null || true)"
    _out_user="$(sqlite3 \
        "$HOME/Library/Application Support/com.apple.TCC/TCC.db" \
        "$_q" 2>/dev/null || true)"
    while IFS='|' read -r _svc _val; do
        [[ "$_svc" == "kTCCServiceScreenCapture" && "$_val" == "2" ]] && _sr=1
        [[ "$_svc" == "kTCCServiceAccessibility" && "$_val" == "2" ]] && _ax=1
    done <<< "$_out_sys"
    while IFS='|' read -r _svc _val; do
        [[ "$_svc" == "kTCCServiceScreenCapture" && "$_val" == "2" ]] && _sr=1
        [[ "$_svc" == "kTCCServiceAccessibility" && "$_val" == "2" ]] && _ax=1
    done <<< "$_out_user"
    if [[ "$_sr" -eq 1 && "$_ax" -eq 1 ]]; then
        echo "granted"
        return 0
    fi
    # MDM-pre-granted bundles aren't in either TCC.db — they live in
    # /var/db/ConfigurationProfiles. We do NOT query that here because:
    # (1) MDM detection is best handled by the existing 'sudo profiles show'
    #     check elsewhere in this script, and
    # (2) a false-negative here only causes setup.sh to trigger VNC
    #     bootstrap when it wasn't strictly needed — annoying but
    #     recoverable; far safer than the false-positive that would result
    #     from the bundle's broken --tcc-check probe (where the user gets
    #     trapped in production mode without working grants).
    # Bias toward "assume not granted unless clearly granted" — that's the
    # conservative direction.
    echo "missing sr=$_sr ax=$_ax"
    return 1
}

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
# (SCREENSHARINGD_PRESENT and DID_WE_CHECKED_SCREENSHARINGD declared in env-detection step.)

if [[ -d "$APP_DEST" ]]; then
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
        # TCC probe (keep-path only — rebuild path leaves TCC_GRANTED=0
        # because new CDHash invalidates any prior grants by csreq
        # mismatch, no point probing). Override with --assume-tcc-granted
        # or --no-tcc-probe when the probe is unreliable on this host.
        case "$ASSUME_TCC" in
            granted)
                TCC_GRANTED=1
                green "  --assume-tcc-granted → skipping probe, treating grants as valid"
                ;;
            denied)
                TCC_GRANTED=0
                NEEDS_TCC_RESET=1
                yellow "  --no-tcc-probe → skipping probe, treating grants as missing"
                ;;
            *)
                # Probe order:
                #   1. tcc_probe (sudo+sqlite3 TCC.db) — authoritative when
                #      MACOS_PASS available. Not subject to PyObjC quirks,
                #      parent-shell inheritance, or CGPreflight optimism.
                #   2. bundle's --tcc-check — fallback when no MACOS_PASS.
                #      Less reliable (Tahoe block-signature bug, GH-runner
                #      bash-inheritance lying), but better than nothing.
                #   3. Bundle predates --tcc-check entirely (no marker line):
                #      assume grants valid (user picked keep, trust them).
                # Capture rc + stdout WITHOUT triggering set -e on non-zero
                # return (bash 3.2 — macOS default — propagates the function's
                # non-zero exit code through `_var="$(fn)"; rc=$?` differently
                # than bash 4+, and set -euo pipefail aborts the script).
                # Calling inside `if` puts the function call in conditional
                # context where set -e is suppressed.
                _probe_rc=0
                if _probe_state="$(tcc_probe)"; then :; else _probe_rc=$?; fi
                if [[ "$_probe_rc" -eq 0 ]]; then
                    TCC_GRANTED=1
                    green "  Existing bundle has valid TCC grants (TCC.db sudo probe) — straight to production"
                elif [[ "$_probe_rc" -eq 1 ]]; then
                    yellow "  TCC.db says grants missing/stale — will re-grant via bootstrap"
                    NEEDS_TCC_RESET=1
                else
                    # _probe_rc == 2 — no MACOS_PASS, fall back to --tcc-check
                    _tcc_out="$("$LAUNCHAGENT_BINARY" --tcc-check 2>&1 || true)"
                    if echo "$_tcc_out" | grep -qE "screen_recording="; then
                        if echo "$_tcc_out" | grep -q "screen_recording=1" \
                                && echo "$_tcc_out" | grep -q "accessibility=1"; then
                            TCC_GRANTED=1
                            green "  Existing bundle has valid TCC grants (bundle --tcc-check) — straight to production"
                        else
                            yellow "  Bundle --tcc-check reports grants missing or stale"
                            NEEDS_TCC_RESET=1
                        fi
                    else
                        yellow "  Bundle predates --tcc-check (or probe failed on this OS version)."
                        yellow "  Cannot verify TCC state via probe; assuming grants are valid as-is."
                        yellow "  If the server doesn't work, re-run with --macos-pass for an"
                        yellow "  authoritative TCC.db query, or pick [r]ebuild."
                        TCC_GRANTED=1   # optimistic — user chose keep, trust them
                    fi
                    unset _tcc_out
                fi
                unset _probe_state _probe_rc
                ;;
        esac
    fi
else
    REBUILD_NEEDED=1   # no bundle yet — fresh install. TCC_GRANTED stays 0;
                       # the rebuild path will enforce that and skip the
                       # final-banner probe — see notes around final banner.
fi

# ── Step 6: ensure_screensharingd helper (memoized) ──────────────────────────
ensure_screensharingd() {
    # Returns 0 (true) if screensharingd is running on :5900, non-zero otherwise.
    # Memoized: subsequent calls return the cached result without re-prompting.
    # Early-return if the env-detection pre-check already decided we don't
    # NEED screensharingd at all (physical display attached, or running
    # locally, or screensharingd not even installed). The pre-check sets
    # SCREENSHARINGD_PRESENT=0 in those cases — never start screensharingd
    # on a Mac with its own physical display.
    local reason="${1:-keep the screen alive}"
    if [[ "$SCREENSHARINGD_PRESENT" -eq 0 && "$DID_WE_CHECKED_SCREENSHARINGD" -eq 1 ]]; then
        return 1  # already decided we don't need it
    fi
    if [[ "$DID_WE_CHECKED_SCREENSHARINGD" -eq 1 ]]; then
        return 0  # already decided yes
    fi
    if [[ "$SCREENSHARINGD_PRESENT" -eq 0 ]]; then
        # Pre-check said we don't want screensharingd. Mark checked + bail.
        DID_WE_CHECKED_SCREENSHARINGD=1
        return 1
    fi
    DID_WE_CHECKED_SCREENSHARINGD=1
    # Pre-check decided we DO want screensharingd. Now: is it actually up?
    if nc -z 127.0.0.1 5900 2>/dev/null; then
        # Already running — common on provisioned cloud Macs (provider
        # enables screensharingd for their own management). No start prompt
        # to fold the [y/N/l] choice into; offer the lockdown as a single
        # one-line prompt with cloud-heuristic default. On personal LAN
        # Macs default-N so we don't break Apple Screen Sharing.app over LAN.
        if [[ "$HEADLESS" -eq 0 ]]; then
            local _primary_ip _default_y _prompt
            _primary_ip="$(ipconfig getifaddr en0 2>/dev/null \
                          || ipconfig getifaddr en1 2>/dev/null || echo '')"
            _default_y=0
            if [[ -n "$_primary_ip" ]] \
               && [[ ! "$_primary_ip" =~ ^10\. ]] \
               && [[ ! "$_primary_ip" =~ ^192\.168\. ]] \
               && [[ ! "$_primary_ip" =~ ^172\.(1[6-9]|2[0-9]|3[01])\. ]] \
               && [[ ! "$_primary_ip" =~ ^169\.254\. ]]; then
                _default_y=1
            fi
            if [[ "$_default_y" -eq 1 ]]; then _prompt="[Y/n]"; else _prompt="[y/N]"; fi
            echo
            yellow "  screensharingd already running on 0.0.0.0:5900. The bundle"
            yellow "  only connects via 127.0.0.1; external :5900 is brute-force"
            yellow "  surface area. Primary IP: ${_primary_ip:-<none>} ($([[ $_default_y -eq 1 ]] && echo public-looking || echo LAN-internal))."
            read -rp "  Lock external :5900 via pf rule? $_prompt " _ans
            case "$_ans" in
                [Yy]*) WANT_PF_LOCKDOWN=1 ;;
                [Nn]*) ;;
                "")    [[ "$_default_y" -eq 1 ]] && WANT_PF_LOCKDOWN=1 ;;
            esac
            unset _ans _primary_ip _default_y _prompt
        fi
        return 0   # SCREENSHARINGD_PRESENT already 1 from pre-check
    fi
    # Port 5900 closed. Screensharingd is installed on this Mac (pre-check
    # checked /System/Library/LaunchDaemons/com.apple.screensharing.plist
    # exists), so we can offer to start it.
    if [[ "$HEADLESS" -eq 1 ]]; then
        yellow "  screensharingd configured but stopped. Headless — skipping."
        yellow "  Start manually: sudo launchctl kickstart -k system/com.apple.screensharing"
        return 1
    fi
    # --start-screensharingd flag short-circuits the prompt for headless /
    # scripted use. Three values: yes (start, no pf), lockdown (start + pf),
    # no (don't start). Mirrors the [y/N/l] interactive choices.
    case "$START_SSD_MODE" in
        no)       yellow "  --start-screensharingd=no → skipping screensharingd start"; return 1 ;;
        yes)      WANT_PF_LOCKDOWN=0; green "  --start-screensharingd=yes → starting (no pf rule)" ;;
        lockdown) WANT_PF_LOCKDOWN=1; green "  --start-screensharingd=lockdown → starting + pf lockdown" ;;
        "")
            echo
            yellow "  screensharingd is configured but stopped (port 5900 closed)."
            yellow "  Reason it's needed: $reason"
            yellow "  Will run: sudo launchctl kickstart -k system/com.apple.screensharing"
            yellow "  Note: screensharingd binds to 0.0.0.0:5900 (system default; macOS"
            yellow "  doesn't expose a bind-addr knob). Our bundle only connects via"
            yellow "  127.0.0.1, so external :5900 is just brute-force surface area."
            yellow "    y  = start it (rely on cloud-provider firewall to block external)"
            yellow "    l  = start AND lock down :5900 to localhost via pf (recommended"
            yellow "         on cloud Macs without a security group)"
            yellow "    N  = skip"
            # Default-to-Y on hosts where VNC is essential (no display + SSH). On
            # those hosts, pressing Enter to skip would trap the user in api-only
            # mode with no way to grant TCC remotely. Default-to-N on other hosts
            # (personal Mac with display) where the user has alternative paths.
            local _vnc_essential=0
            if [[ "$DISPLAY_ATTACHED" -eq 0 ]]; then
                _vnc_essential=1
            fi
            local _start_prompt
            if [[ "$_vnc_essential" -eq 1 ]]; then
                _start_prompt="[Y/n/l]   (default Y on this headless host)"
            else
                _start_prompt="[y/N/l]"
            fi
            read -rp "  Start screensharingd now? $_start_prompt " _ans
            case "$_ans" in
                [Ll]*) WANT_PF_LOCKDOWN=1 ;;
                [Yy]*) WANT_PF_LOCKDOWN=0 ;;
                [Nn]*) unset _ans _vnc_essential _start_prompt; return 1 ;;
                "")
                    if [[ "$_vnc_essential" -eq 1 ]]; then
                        WANT_PF_LOCKDOWN=0   # Enter = Y on essential-VNC hosts
                    else
                        unset _ans _vnc_essential _start_prompt; return 1
                    fi
                    ;;
                *) unset _ans _vnc_essential _start_prompt; return 1 ;;
            esac
            unset _vnc_essential _start_prompt
            ;;
    esac
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

# ── Optional :5900 lock-down via pf ──────────────────────────────────────────
# Set to 1 by ensure_screensharingd() when the user picks 'l' at the start
# prompt. apply_pf_lockdown_5900() is called once we have a verified password
# (sudo) — runs unconditionally if this flag is set, no second prompt.
# screensharingd doesn't expose a bind-address knob (its launchd plist is
# SIP-protected), so the clean way to restrict external access is a pf anchor:
#   pass  in quick proto tcp from 127.0.0.0/8 to any port 5900
#   block in quick proto tcp from any to any port 5900
# Order matters: 'quick' decides immediately. Loopback pass MUST come
# first or the block-quick line fires for everything including loopback,
# severing the bundle's VNC bridge to screensharingd.
# Our bundle's VNC bridge connects to 127.0.0.1:5900 anyway — losing nothing
# functionally, removing the cloud-Mac brute-force attack surface.
WANT_PF_LOCKDOWN=0
PF_ANCHOR_PATH="/etc/pf.anchors/com.macvncstream"
PF_CONF_MARKER="# anchor \"com.macvncstream\" -- mac-vnc-stream"
apply_pf_lockdown_5900() {
    [[ "$WANT_PF_LOCKDOWN" -eq 1 ]] || return 0
    command -v pfctl >/dev/null 2>&1 || { yellow "  pfctl missing — skipping pf rule"; return 0; }
    [[ -z "$MACOS_PASS" ]] && { yellow "  No password — skipping pf rule"; return 0; }

    yellow "  Writing ${PF_ANCHOR_PATH} (sudo) and updating /etc/pf.conf..."
    # pf rule ordering matters: 'quick' makes a decision and stops further
    # evaluation. With block-quick BEFORE pass-quick, ALL traffic to :5900
    # hits the block first and gets dropped — including loopback. The
    # bundle's VNC bridge then can't reach screensharingd and times out.
    # Pass loopback FIRST (quick → match-and-stop), then block everything
    # else. Verified live on Scaleway: wrong order broke VNC bridge.
    local _anchor_body=$'pass in quick proto tcp from 127.0.0.0/8 to any port 5900\nblock in quick proto tcp from any to any port 5900\n'
    if ! echo "$MACOS_PASS" | sudo -S tee "$PF_ANCHOR_PATH" >/dev/null <<<"$_anchor_body"; then
        yellow "  Failed to write anchor file — skipping"
        return 0
    fi
    if ! echo "$MACOS_PASS" | sudo -S grep -qF "$PF_CONF_MARKER" /etc/pf.conf 2>/dev/null; then
        echo "$MACOS_PASS" | sudo -S tee -a /etc/pf.conf >/dev/null <<EOF

${PF_CONF_MARKER}
anchor "com.macvncstream"
load anchor "com.macvncstream" from "${PF_ANCHOR_PATH}"
EOF
    fi
    if echo "$MACOS_PASS" | sudo -S pfctl -ef /etc/pf.conf 2>&1 | grep -qE "Token|enabled|already enabled|loaded"; then
        green "  pf rule installed — external :5900 now blocked, localhost still works"
    else
        yellow "  pfctl may have rejected the rule — check 'sudo pfctl -sr | grep 5900'"
        yellow "  Anchor file remains at ${PF_ANCHOR_PATH} for manual inspection."
    fi
}

# ── Step 7: VNC fallback decision ────────────────────────────────────────────
# VNC plays TWO different roles, both of which can independently require it:
#
#   ROLE 1: Permission-grant viewer.
#     TCC not yet granted + SSH session → user needs to view the desktop
#     in a browser so they can navigate System Settings and toggle the
#     grants. Without this, headless cloud Macs are stuck. Once grants
#     land, the running server auto-upgrades from VNC capture to SCK
#     within ~30 s (server.py polls CGPreflightScreenCaptureAccess and
#     hot-swaps the capture backend) — no setup.sh re-run needed.
#
#   ROLE 2: Display warmer.
#     macOS releases the virtual display backend when no client is
#     attached. SCK then captures stale or zero frames even if it's
#     fully permitted. On a host with NO physical display
#     (DISPLAY_ATTACHED=0, common on cloud Mac minis without a HDMI
#     dongle), our bundle's VNC connection IS what keeps the display
#     rendered.
#
# Either role triggers VNC_FALLBACK=1, which means setup.sh will
# (1) ensure screensharingd is running, (2) prompt for a password if
# we don't already have one, (3) include --enable-vnc-fallback in the
# server invocation so the bundle maintains a VNC connection.
NEEDS_VNC_FOR_GRANT=0
NEEDS_VNC_AS_DISPLAY_WARMER=0
# NEEDS_VNC_FOR_GRANT — fires when the user can't see the desktop locally
# AND grants are not yet valid:
#   • RUNNING_FROM_SSH=1 (user is remote — even a Mac with a display dongle
#     attached has its grants in a Settings UI the SSH user can't reach
#     directly; needs the bundle's VNC bridge to provide a browser view)
#   • OR DISPLAY_ATTACHED=0 (no display at all — user is necessarily remote)
# Either signal means the user needs the in-bundle VNC bridge as the path
# to System Settings. Earlier this gated on DISPLAY_ATTACHED alone, which
# silently skipped VNC for remote SSH users on Macs with a real display
# attached (Scaleway with HDMI dongle pattern).
if [[ "$TCC_GRANTED" -eq 0 ]] \
   && { [[ "$RUNNING_FROM_SSH" -eq 1 ]] || [[ "$DISPLAY_ATTACHED" -eq 0 ]]; }; then
    NEEDS_VNC_FOR_GRANT=1
fi
# NEEDS_VNC_AS_DISPLAY_WARMER — keeps an active VNC client connection to
# screensharingd so its virtual display backend stays awake. Fires when
# the user is remote (SSH'd in OR no display attached): even a Mac with a
# real-looking display attached often has it asleep when no one's at the
# keyboard, and a sleeping display means SCK captures stale frames =
# frozen browser. Verified live: Scaleway with AOC display attached, after
# bootstrap → production transition without VNC bridge, browser froze.
# Scaleway's display had `Display Asleep: Yes` in system_profiler.
# Same OR as NEEDS_VNC_FOR_GRANT — "remote user can't see screen locally"
# is the meaningful signal, not "is display hardware attached".
if [[ "$RUNNING_FROM_SSH" -eq 1 ]] || [[ "$DISPLAY_ATTACHED" -eq 0 ]]; then
    NEEDS_VNC_AS_DISPLAY_WARMER=1
fi
VNC_FALLBACK=0
if [[ "$NEEDS_VNC_FOR_GRANT" -eq 1 || "$NEEDS_VNC_AS_DISPLAY_WARMER" -eq 1 ]]; then
    _vnc_reason="to view the desktop while granting Screen Recording / Accessibility"
    if [[ "$NEEDS_VNC_AS_DISPLAY_WARMER" -eq 1 && "$NEEDS_VNC_FOR_GRANT" -eq 0 ]]; then
        _vnc_reason="to keep the virtual display alive (no physical display attached)"
    fi
    if ensure_screensharingd "$_vnc_reason"; then
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
                        apply_pf_lockdown_5900
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
                if [[ "$NEEDS_VNC_AS_DISPLAY_WARMER" -eq 1 ]]; then
                    yellow "  No password provided — but VNC is needed as a DISPLAY WARMER"
                    yellow "  on this host (no physical display detected). Without it, SCK"
                    yellow "  has no renderable display and the browser will be black."
                    yellow "  Server will TRY to connect to screensharingd anyway (some"
                    yellow "  VNC configs accept type-1 'no auth'); succeeds on GH runners,"
                    yellow "  may fail elsewhere. Re-run with --macos-pass if it doesn't work."
                    VNC_FALLBACK=1   # try anyway; bundle will negotiate auth or fail gracefully
                else
                    green "  No password — skipping VNC (assumes physical screen access)"
                fi
            fi
        fi
    fi
fi
unset _vnc_reason

# Optional MDM TCC profile detection (informational only — no auto-action).
if [[ -n "${MACOS_PASS:-}" ]]; then
    yellow "  About to run 'sudo profiles show' (read-only) to check for MDM TCC management..."
    if echo "$MACOS_PASS" | sudo -S profiles show 2>/dev/null \
            | grep -q "com.apple.TCC.configuration-profile-policy"; then
        yellow "  Note: MDM TCC profile installed. If grants don't take effect, ask"
        yellow "  admin to allowlist '${LABEL}', or remove enrollment as last resort:"
        yellow "    sudo profiles -R -p <enrollment-id-from 'sudo profiles show'>"
    fi
fi

# ── Step 8: Build/install bundle if needed ────────────────────────────────────
# Capture whether a prior bundle existed BEFORE we sudo-rm it. Determines
# whether the TCC reset is meaningful: on a fresh install (no prior bundle)
# TCC.db has no entries for our bundle id, so tccutil reset would error
# with "No such bundle identifier".
HAD_PRIOR_BUNDLE=0
[[ -d "$APP_DEST" ]] && HAD_PRIOR_BUNDLE=1
if [[ "$REBUILD_NEEDED" -eq 1 ]]; then
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
    # Only mark TCC reset needed when there were entries to invalidate.
    # Fresh install (HAD_PRIOR_BUNDLE=0) → TCC.db has no rows for our bundle
    # id; tccutil reset would fail with -10814 ("No such bundle identifier").
    if [[ "$HAD_PRIOR_BUNDLE" -eq 1 ]]; then
        NEEDS_TCC_RESET=1   # CDHash changed → grants invalidated
    fi
    TCC_GRANTED=0
fi

# ── Step 9: tccutil reset if needed (rebuild OR stale-CDHash-on-keep) ────────
if [[ "$NEEDS_TCC_RESET" -eq 1 ]]; then
    echo
    yellow "  Resetting TCC for com.macvncstream.server (CDHash mismatch)..."
    yellow "    sudo tccutil reset ScreenCapture com.macvncstream.server"
    yellow "    sudo tccutil reset Accessibility com.macvncstream.server"
    # Tolerate non-zero exit: tccutil errors with -10814 when the bundle id
    # has no entries in TCC.db (e.g. user manually wiped them between runs).
    # Reset semantics in that case are already satisfied — nothing to clear.
    if [[ -n "$MACOS_PASS" ]]; then
        echo "$MACOS_PASS" | sudo -S tccutil reset ScreenCapture com.macvncstream.server 2>&1 | head -1 || true
        echo "$MACOS_PASS" | sudo -S tccutil reset Accessibility com.macvncstream.server 2>&1 | head -1 || true
    else
        sudo tccutil reset ScreenCapture com.macvncstream.server 2>&1 | head -1 || true
        sudo tccutil reset Accessibility com.macvncstream.server 2>&1 | head -1 || true
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
    # Two independent inputs:
    #   $1 include_vnc_flag — pass --enable-vnc-fallback to the server. Set
    #                         when VNC_FALLBACK=1 (either grant-bootstrap or
    #                         permanent display-warmer for headless hosts).
    #   $2 include_password — store MACOS_PASS in the plist env. Set ONLY
    #                         during the transient bootstrap-grant window;
    #                         dropped after the transition so the password
    #                         doesn't survive in the plist on disk.
    #   If $2 omitted, defaults to $1 (legacy single-arg call sites).
    local include_vnc_flag="$1"
    local include_password="${2:-$1}"
    local args=(
        "$LAUNCHAGENT_BINARY"
    )
    args+=(
        --listen "$LISTEN"
        --port "$PORT"
        --password "$MVS_PASSWORD"
        --max-fps "$MAX_FPS"
        --codec "$CODEC"
    )
    [[ "$include_vnc_flag" -eq 1 ]] && args+=(--enable-vnc-fallback)

    local prog_xml=""
    for a in "${args[@]}"; do prog_xml+="        <string>${a}</string>
"; done

    local env_xml="        <key>MACOS_USER</key><string>${MACOS_USER}</string>
        <key>MVS_PASSWORD</key><string>${MVS_PASSWORD}</string>
"
    if [[ "$include_password" -eq 1 && -n "$MACOS_PASS" ]]; then
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
# Two args to write_plist: include_vnc_flag, include_password_env.
# vnc_flag: VNC_FALLBACK (bootstrap OR permanent display-warmer needs it)
# password: BOOTSTRAP_MODE only (transient — gone after grants)
write_plist "$VNC_FALLBACK" "$BOOTSTRAP_MODE"
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
if [[ "$BOOTSTRAP_MODE" -eq 1 && "$HEADLESS" -eq 0 && "$NO_BOOTSTRAP_WAIT" -eq 0 ]]; then
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
    # Probe via the bundle's --tcc-check, which prints
    # 'screen_recording=0/1' and 'accessibility=0/1' lines. The lines are the
    # primary signal — exit code alone is ambiguous because old bundles that
    # predate --tcc-check exit 2 (argparse error) on the flag and could
    # otherwise loop forever. Treat "no marker line in stdout" as "old
    # bundle, can't verify" and assume granted (user pressed Enter; trust
    # them).
    # Verification is precise — never trust user Enter alone. Three-stage
    # probe (any positive result is checked against runtime SCK afterward):
    #   1. tcc_probe: sudo+sqlite3 query of system & user TCC.db. Authoritative
    #      for whether the user actually toggled both services to auth_value=2.
    #      Bypasses every runtime quirk (PyObjC block-signature on Tahoe,
    #      parent-shell inheritance, CGPreflight optimism).
    #   2. Bundle's --tcc-check: only used if tcc_probe returns "unknown"
    #      (no MACOS_PASS available for sudo). Less reliable but still useful.
    #   3. If neither probe can confirm grants, REFUSE to transition. Print
    #      diagnostics and loop. Earlier this fell through to "trust user
    #      Enter" which left users trapped in production mode without grants.
    while true; do
        read -rp "  Press Enter when permissions are granted (Ctrl+C to leave in bootstrap mode): " _
        sleep 2  # give tccd a moment to commit the toggle
        # See keep-path comment: bash 3.2 + set -euo pipefail trips on
        # `var="$(fn)"; rc=$?` when fn returns non-zero. Use the if-form
        # to put the call in conditional context.
        _probe_rc=0
        if _probe_state="$(tcc_probe)"; then :; else _probe_rc=$?; fi
        case "$_probe_rc" in
            0)
                green "  Both grants confirmed via TCC.db (sudo) — switching to production mode."
                TCC_GRANTED=1
                break
                ;;
            1)
                echo
                yellow "  Not granted yet — TCC.db reports: ${_probe_state}"
                yellow "  Open System Settings ▸ Privacy & Security and toggle BOTH:"
                yellow "    • Screen Recording → 'mac-vnc-stream' ON"
                yellow "    • Accessibility    → 'mac-vnc-stream' ON"
                yellow "  IMPORTANT: Tahoe sometimes shows the toggle going ON then"
                yellow "  silently reverts it to OFF if you skip the password prompt."
                yellow "  Make sure Tahoe asks for your password and you actually enter it."
                yellow "  Then Cmd+Q the Settings app — Tahoe occasionally only commits"
                yellow "  the auth_value=2 row when Settings quits."
                yellow "  If 'mac-vnc-stream' isn't in the pane, click '+', Cmd+Shift+G:"
                yellow "    ${APP_BUILT}"
                yellow "  Then come back here and press Enter again. Or Ctrl+C to leave"
                yellow "  the bundle running in bootstrap (VNC) mode and finish later."
                echo
                ;;
            2)
                # No MACOS_PASS — fall back to bundle --tcc-check. If that also
                # can't give a clear answer, REFUSE to transition. Earlier this
                # branch fell through to "trust user Enter", which is what
                # caused users to land in production mode without grants on
                # Tahoe (where the bundle's --tcc-check is broken).
                _tcc_out="$("$LAUNCHAGENT_BINARY" --tcc-check 2>&1 || true)"
                if echo "$_tcc_out" | grep -q "screen_recording=1" \
                        && echo "$_tcc_out" | grep -q "accessibility=1"; then
                    green "  Both grants confirmed via bundle --tcc-check — switching to production mode."
                    TCC_GRANTED=1
                    break
                fi
                if echo "$_tcc_out" | grep -qE "screen_recording=0|accessibility=0"; then
                    _missing=""
                    if echo "$_tcc_out" | grep -q "screen_recording=0"; then _missing+=" Screen Recording"; fi
                    if echo "$_tcc_out" | grep -q "accessibility=0";    then _missing+=" Accessibility"; fi
                    echo
                    yellow "  Not granted yet — bundle probe says still missing:${_missing}"
                    yellow "  See instructions above. Re-toggle in Settings + Cmd+Q + Enter again."
                    echo
                else
                    # Bundle probe failed to produce a marker (Tahoe PyObjC
                    # block-signature error, etc). REFUSE to transition;
                    # surface the failure and how to fix it.
                    echo
                    red "  Cannot verify grants — both probes failed:"
                    red "    • TCC.db sudo probe: no MACOS_PASS available"
                    red "    • bundle --tcc-check: produced no parseable result"
                    echo "      bundle output: $(echo "$_tcc_out" | head -3)"
                    echo
                    yellow "  Two ways forward:"
                    yellow "  (a) Press Ctrl+C to leave the bundle running in bootstrap mode."
                    yellow "      Bundle is at /Applications/mac-vnc-stream.app and serves VNC."
                    yellow "      Once you grant in System Settings, the running server's"
                    yellow "      30s auto-upgrade loop picks SCK up automatically — no need"
                    yellow "      for setup.sh to verify the transition."
                    yellow "  (b) Re-run setup.sh with --macos-pass=<your-password> so this"
                    yellow "      script can sudo-query TCC.db directly. That's authoritative."
                    yellow "  Pressing Enter again WILL NOT transition — refusing without verification."
                    echo
                fi
                unset _tcc_out _missing
                ;;
        esac
        unset _probe_state _probe_rc
    done

    step "Switching to production mode"
    # Post-grant production plist:
    #  • include_vnc_flag: keep --enable-vnc-fallback if NEEDS_VNC_AS_DISPLAY_WARMER
    #    (remote/headless host still needs VNC connection to keep
    #    screensharingd's virtual display awake — a sleeping display means
    #    SCK captures stale frames, browser shows frozen image).
    #  • include_password: same as include_vnc_flag. The bundle's VNC bridge
    #    requires MACOS_PASS to authenticate to screensharingd; without it,
    #    --enable-vnc-fallback is set but VNC connection fails — bundle
    #    crash-loops on auth error. So password MUST persist whenever
    #    --enable-vnc-fallback persists. The security trade-off:
    #      • Personal Mac (DISPLAY=1, !SSH): include_vnc=0, password=0 — clean.
    #      • Remote/headless: include_vnc=1, password=1 — password persists in
    #        plist (mode 600, user-owned) for the lifetime of the install.
    #    Acceptable because the user is the only one with file access on
    #    a single-user Mac, and the alternative (frozen screen) is broken.
    write_plist "$NEEDS_VNC_AS_DISPLAY_WARMER" "$NEEDS_VNC_AS_DISPLAY_WARMER"
    if [[ "$NEEDS_VNC_AS_DISPLAY_WARMER" -eq 1 ]]; then
        yellow "  Plist rewritten — keeping --enable-vnc-fallback + MACOS_PASS for"
        yellow "  display warming (you're remote / no local display visibility)."
    else
        green "  Plist rewritten — credentials removed (local display, no VNC needed)"
    fi
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

# Final-banner TCC state: trust TCC_GRANTED (set by either the keep-path
# probe or the bootstrap-Enter loop). Re-running --tcc-check here would
# double-jeopardise on hosts where the probe is unreliable (e.g. GH macos
# runners pre-grant /bin/bash, parent-shell inheritance fools CG/SCSC
# probes into reporting "granted" even when our bundle's TCC.db row is
# auth_value=0 — the kernel SCStream call still fails -3801). After a
# rebuild without bootstrap-Enter, TCC_GRANTED stays 0 and the banner
# correctly steers the user to the manual-grant path.
TCC_OK="$TCC_GRANTED"

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

if [[ "$TCC_OK" -eq 1 ]]; then
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
    if [[ "$VNC_FALLBACK" -eq 1 ]]; then
        green "  VNC bootstrap is active — you can already see the desktop in your"
        green "  browser. Use it to grant the permissions above."
    elif [[ "$DISPLAY_ATTACHED" -eq 1 && "$RUNNING_FROM_SSH" -eq 0 ]]; then
        # Personal Mac with physical screen, user is at the keyboard
        # locally — grant directly via System Settings on the Mac.
        yellow "  Personal Mac path: grant the permissions on the Mac itself"
        yellow "  (we detected an attached display). The browser will be black"
        yellow "  until both grants are toggled on."
    elif [[ "$DISPLAY_ATTACHED" -eq 1 && "$RUNNING_FROM_SSH" -eq 1 ]]; then
        # Mac with display dongle but user is remote — they CAN'T see the
        # dongle's screen. They need VNC bridge (which wasn't enabled here)
        # or another way to reach Settings. This is the Scaleway pattern.
        red    "  ┌─ DISPLAY DETECTED BUT USER IS REMOTE — NO PATH TO SETTINGS ─────┐"
        yellow "  │  We detected an attached display, but you're SSH'd in — you    │"
        yellow "  │  can't see the desktop on that display. VNC bootstrap was       │"
        yellow "  │  needed but didn't activate (likely no macOS password provided).│"
        yellow "  │                                                                  │"
        yellow "  │  Re-run setup.sh and pass your macOS password:                  │"
        yellow "  │    bash setup.sh --macos-pass=<your-password>                   │"
        yellow "  │                                                                  │"
        yellow "  │  That enables the VNC bridge so you can grant TCC remotely.    │"
        red    "  └──────────────────────────────────────────────────────────────────┘"
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
