#!/usr/bin/env bash
# install.sh — convenience installer for mac-vnc-stream.
#
# Two paths, attempted in order:
#
#   1. Pre-built release bundle (FAST PATH, ~10 s, requires a published
#      GitHub release with a matching .app.tar.gz asset). The bundle has its
#      own bundle id (com.macvncstream.server) so it works on Tahoe + on
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
# pointing at /Applications/mac-vnc-stream.app/Contents/MacOS/mac-vnc-stream
# (SIP-enabled hosts) or at python3 server.py directly (SIP-disabled hosts).
#
# Usage:
#   bash <(curl -fsSL https://raw.githubusercontent.com/reindertpelsma/mac-vnc-stream/main/install.sh)
#
# With flags forwarded to setup.sh:
#   bash <(curl ...) --port 6081 --listen 0.0.0.0 --headless

set -euo pipefail

REPO_URL="https://github.com/reindertpelsma/mac-vnc-stream"
RELEASES_API="https://api.github.com/repos/reindertpelsma/mac-vnc-stream/releases/latest"
CLONE_DIR="$HOME/mac-vnc-stream"
APP_DEST="/Applications/mac-vnc-stream.app"

green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
red()    { printf '\033[31m%s\033[0m\n' "$*"; }
step()   { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die()    { red "ERROR: $*"; exit 1; }

# Headless / opt-out parsing.
HEADLESS="${MVS_HEADLESS:-0}"
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

step "mac-vnc-stream installer"
echo "  Repo:           $REPO_URL"
echo "  Headless:       $([[ $HEADLESS -eq 1 ]] && echo yes || echo no)"
echo "  Force source:   $([[ $FORCE_BUILD_FROM_SOURCE -eq 1 ]] && echo yes || echo no)"

# ── Prerequisites common to both paths ────────────────────────────────────────
command -v curl >/dev/null 2>&1 || die "curl not found"

# ── Path 1: try the pre-built release ─────────────────────────────────────────
if [[ "$FORCE_BUILD_FROM_SOURCE" -eq 0 ]]; then
    step "Looking for a pre-built release"
    ARCH="$(uname -m)"   # arm64 or x86_64
    ASSET_NAME="mac-vnc-stream-${ARCH}.app.tar.gz"
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
        yellow "  Downloading..."
        TMP_DIR="$(mktemp -d /tmp/mvs.XXXXXX)"
        TMP_TAR="$TMP_DIR/$ASSET_NAME"
        if curl -fsSL "$ASSET_URL" -o "$TMP_TAR"; then
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

            # Hand off to setup.sh in install-only mode (it'll detect the
            # existing $APP_DEST and just write+load the LaunchAgent without
            # rebuilding). For now we still need the source for setup.sh
            # itself, so curl it directly into a small temp dir.
            SETUP_TMP="$(mktemp -d /tmp/mvs-setup.XXXXXX)"
            curl -fsSL "$REPO_URL/raw/main/setup.sh" -o "$SETUP_TMP/setup.sh"
            chmod +x "$SETUP_TMP/setup.sh"
            export MVS_PREBUILT_APP="$APP_DEST"
            exec bash "$SETUP_TMP/setup.sh" "${PASSTHROUGH_ARGS[@]+"${PASSTHROUGH_ARGS[@]}"}"
        else
            yellow "  Download failed — falling through to from-source install"
        fi
    else
        yellow "  No matching release asset (${ASSET_NAME}) — using from-source install"
    fi
fi

# ── Path 2: from-source install (clone + setup.sh) ────────────────────────────
command -v git    >/dev/null 2>&1 || die "git not found. Run: xcode-select --install"
command -v python3 >/dev/null 2>&1 || die "python3 not found. Run: xcode-select --install"

step "Fetching mac-vnc-stream source"
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
