#!/usr/bin/env bash
# install.sh — convenience installer for macscreencast.
#
# Two paths, attempted in order:
#
#   1. Pre-built release bundle (FAST PATH, ~10 s, requires a published
#      GitHub release with a matching .app.tar.gz asset). The bundle has its
#      own bundle id (com.macscreencast.server) so it works on Tahoe + on
#      MDM-managed Macs without granting the Python interpreter. After this
#      lands, the user just toggles permissions and is done.
#
#   2. From-source build (FALLBACK, ~5–10 min). Clones the repo, runs
#      setup.sh which installs Python deps + builds the .app bundle locally
#      via py2app + ad-hoc signs it. Used when no matching release asset
#      exists, when the user passes --build-from-source, or when the fast
#      path fails (e.g. offline). Source-build is also the right path for
#      audit-conscious / airgapped users who don't want a downloaded binary.
#
# Both paths converge on the same end state: a LaunchAgent in gui/$UID
# pointing at /Applications/macscreencast.app/Contents/MacOS/macscreencast
# (SIP-enabled hosts) or at python3 server.py directly (SIP-disabled hosts).
#
# Usage:
#   bash <(curl -fsSL https://raw.githubusercontent.com/reindertpelsma/macscreencast/main/install.sh)
#
# With flags forwarded to setup.sh:
#   bash <(curl ...) --port 6081 --listen 0.0.0.0 --headless

set -euo pipefail

green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
red()    { printf '\033[31m%s\033[0m\n' "$*"; }
step()   { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die()    { red "ERROR: $*"; exit 1; }

# Root-user handling: same logic as setup.sh — if running under sudo,
# re-exec as the real user. If direct root login, refuse.
if [[ "$(id -u)" -eq 0 ]]; then
    if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
        exec sudo -u "$SUDO_USER" -E -H bash "$0" "$@"
    else
        red "ERROR: install.sh should not be run as root directly."
        yellow "Run as your regular user account; sudo is invoked internally."
        yellow "If you need to run via sudo for some reason, do:"
        yellow "  sudo -u <your-user> bash install.sh"
        exit 1
    fi
fi

# ── OS guard ─────────────────────────────────────────────────────────────────
# This is the macOS-only repo. Linux/Windows users get a friendly
# redirect to the (forthcoming) sibling repo instead of confusing
# error messages from py2app / sudo / launchctl on a non-macOS host.
_OS="$(uname -s 2>/dev/null || echo unknown)"
if [[ "$_OS" != "Darwin" ]]; then
    red "  This repo is macOS only — detected: $_OS"
    yellow "  For Linux / Windows browser remote-desktop, use the sibling repo:"
    yellow "    bash <(curl -fsSL https://raw.githubusercontent.com/reindertpelsma/browser-screencast/main/install.sh)"
    exit 1
fi
unset _OS

REPO_URL="https://github.com/reindertpelsma/macscreencast"
RELEASES_API="https://api.github.com/repos/reindertpelsma/macscreencast/releases/latest"
CLONE_DIR="$HOME/macscreencast"
APP_DEST="/Applications/macscreencast.app"

# Headless / opt-out parsing.
HEADLESS="${MACSCREENCAST_HEADLESS:-0}"
FORCE_BUILD_FROM_SOURCE=0
PASSTHROUGH_ARGS=()
for arg in "$@"; do
    case "$arg" in
        --headless)            HEADLESS=1; PASSTHROUGH_ARGS+=("$arg") ;;
        --build-from-source)   FORCE_BUILD_FROM_SOURCE=1 ;;
        *)                     PASSTHROUGH_ARGS+=("$arg") ;;
    esac
done

# Re-open /dev/tty for interactive prompts when piped via curl|bash.
if [[ ! -t 0 ]] && [[ -e /dev/tty ]] && [[ "$HEADLESS" -eq 0 ]]; then
    exec </dev/tty
fi

step "macscreencast installer"
echo "  Repo:           $REPO_URL"
echo "  Headless:       $([[ $HEADLESS -eq 1 ]] && echo yes || echo no)"
echo "  Force source:   $([[ $FORCE_BUILD_FROM_SOURCE -eq 1 ]] && echo yes || echo no)"

# ── Prerequisites common to both paths ────────────────────────────────────────
command -v curl >/dev/null 2>&1 || die "curl not found"

# ── Path 1: try the pre-built release ─────────────────────────────────────────
if [[ "$FORCE_BUILD_FROM_SOURCE" -eq 0 ]]; then
    step "Looking for a pre-built release"
    ARCH="$(uname -m)"   # arm64 or x86_64
    ASSET_NAME="macscreencast-${ARCH}.app.tar.gz"
    RELEASE_TAG=""
    ASSET_URL=""

    if RELEASE_JSON="$(curl -fsSL "$RELEASES_API" 2>/dev/null)"; then
        RELEASE_TAG="$(printf '%s' "$RELEASE_JSON" \
            | sed -n 's/.*"tag_name":[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)"
        ASSET_URL="$(printf '%s' "$RELEASE_JSON" \
            | tr ',' '\n' \
            | grep -E "browser_download_url.*${ASSET_NAME//\./\\.}" \
            | sed -n 's/.*"browser_download_url":[[:space:]]*"\([^"]*\)".*/\1/p' \
            | head -1)"
    fi

    if [[ -n "$ASSET_URL" ]]; then
        green "  Found release ${RELEASE_TAG:-?} → ${ASSET_NAME}"

        # Existing-bundle detection — never auto-overwrite. A user running
        # install.sh without knowing the install state of the Mac (e.g.
        # they're not sure if they ran it before, or this is shared host)
        # should NOT have their working bundle destroyed silently. Default
        # to keep; explicit opt-in for reinstall.
        DO_REINSTALL=1
        if [[ -d "$APP_DEST" ]]; then
            echo
            yellow "  Existing bundle at $APP_DEST"
            yellow "  Latest release: $RELEASE_TAG"
            if [[ "$HEADLESS" -eq 1 ]]; then
                green "  Headless mode — keeping existing bundle (default)"
                DO_REINSTALL=0
            else
                read -rp "  [k]eep existing or [r]einstall from release? [K/r] " _ans
                if [[ "$_ans" =~ ^[Rr]$ ]]; then
                    DO_REINSTALL=1
                else
                    DO_REINSTALL=0
                fi
                unset _ans
            fi
        fi

        if [[ "$DO_REINSTALL" -eq 0 ]]; then
            green "  Keeping existing bundle — skipping download (~53 MB saved)"
        else
            yellow "  Downloading..."
            TMP_DIR="$(mktemp -d /tmp/macscreencast.XXXXXX)"
            TMP_TAR="$TMP_DIR/$ASSET_NAME"
            if ! curl -fsSL "$ASSET_URL" -o "$TMP_TAR"; then
                yellow "  Download failed — falling through to from-source install"
                rm -rf "$TMP_DIR"
                # Drop out of the fast-path block; the from-source path
                # below handles git clone + build.
                ASSET_URL=""
            else
                green "  Downloaded $(du -sh "$TMP_TAR" | awk '{print $1}')"

                step "Installing $APP_DEST"
                sudo rm -rf "$APP_DEST"
                sudo tar -xzf "$TMP_TAR" -C /Applications/ \
                    || die "tar extract failed"
                rm -rf "$TMP_DIR"
                # Strip Gatekeeper quarantine — we ad-hoc sign at build time
                # rather than Apple-notarize. Without this, macOS would refuse
                # to launch the bundle ("Apple cannot check it for malicious
                # software"). Removing the quarantine attribute tells macOS
                # this didn't come over the network for Gatekeeper purposes.
                sudo xattr -dr com.apple.quarantine "$APP_DEST" 2>/dev/null || true
                green "  $APP_DEST installed (ad-hoc signed, quarantine cleared)"
            fi
        fi

        # Hand off to setup.sh — same code path for keep + reinstall.
        # The bundle in $APP_DEST is now either:
        #   • the freshly-downloaded latest release (DO_REINSTALL=1), OR
        #   • the previously-installed bundle (DO_REINSTALL=0; user kept it)
        # Setup.sh's job is identical in both cases: write+load the
        # LaunchAgent. ASSET_URL gets blanked on download failure (above)
        # so we drop out to from-source path in that case.
        if [[ -n "$ASSET_URL" ]]; then
            # Fetch setup.sh at the SAME tag as the bundle artifact rather
            # than `main`. Tag content is immutable so CDN caching is
            # guaranteed-correct, and setup.sh is in-sync with the bundle
            # artifact's expectations.
            SETUP_TMP="$(mktemp -d /tmp/macscreencast-setup.XXXXXX)"
            curl -fsSL "$REPO_URL/raw/${RELEASE_TAG}/setup.sh" -o "$SETUP_TMP/setup.sh" \
                || die "failed to fetch setup.sh from ${RELEASE_TAG}"
            chmod +x "$SETUP_TMP/setup.sh"
            export MACSCREENCAST_PREBUILT_APP="$APP_DEST"
            exec bash "$SETUP_TMP/setup.sh" "${PASSTHROUGH_ARGS[@]+"${PASSTHROUGH_ARGS[@]}"}"
        fi
    else
        yellow "  No matching release asset (${ASSET_NAME}) — using from-source install"
    fi
fi

# ── Path 2: from-source install (clone + setup.sh) ────────────────────────────
command -v git    >/dev/null 2>&1 || die "git not found. Run: xcode-select --install"
command -v python3 >/dev/null 2>&1 || die "python3 not found. Run: xcode-select --install"

step "Fetching macscreencast source"
if [[ -d "$CLONE_DIR/.git" ]]; then
    yellow "  Repo already at $CLONE_DIR — pulling latest"
    git -C "$CLONE_DIR" pull --ff-only \
        || yellow "  Pull failed (local changes?). Continuing with existing tree."
    green "  Up to date"
else
    git clone "$REPO_URL" "$CLONE_DIR"
    green "  Cloned to $CLONE_DIR"
fi

exec bash "$CLONE_DIR/setup.sh" "${PASSTHROUGH_ARGS[@]+"${PASSTHROUGH_ARGS[@]}"}"
