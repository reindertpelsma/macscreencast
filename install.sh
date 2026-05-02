#!/usr/bin/env bash
# mac-vnc-stream/install.sh
#
# One-command install from a fresh macOS machine (no prior clone needed).
# Clones the repo to ~/mac-vnc-stream and runs setup.sh.
#
# Usage:
#   bash <(curl -fsSL https://raw.githubusercontent.com/reindertpelsma/mac-vnc-stream/main/install.sh)
#
# With flags passed through to setup.sh:
#   bash <(curl -fsSL https://raw.githubusercontent.com/reindertpelsma/mac-vnc-stream/main/install.sh) \
#     --port 6081 --listen 0.0.0.0

set -euo pipefail

REPO_URL="https://github.com/reindertpelsma/mac-vnc-stream"
CLONE_DIR="$HOME/mac-vnc-stream"

# ── Helpers ───────────────────────────────────────────────────────────────────
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
red()    { printf '\033[31m%s\033[0m\n' "$*"; }
step()   { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die()    { red "ERROR: $*"; exit 1; }

# ── Ensure interactive stdin ───────────────────────────────────────────────────
# When piped through curl (curl ... | bash), stdin carries the script itself.
# Re-open /dev/tty so that read prompts in setup.sh reach the user's terminal.
if [ ! -t 0 ]; then
    exec < /dev/tty
fi

step "mac-vnc-stream installer"
echo "  Repo: $REPO_URL"
echo "  Clone target: $CLONE_DIR"

# ── Prerequisites ──────────────────────────────────────────────────────────────
if ! command -v git >/dev/null 2>&1; then
    die "git not found. Install Xcode Command Line Tools first:
  xcode-select --install
Then re-run this installer."
fi

if ! command -v python3 >/dev/null 2>&1; then
    die "python3 not found. Install Xcode Command Line Tools:
  xcode-select --install"
fi

# ── Clone or update ────────────────────────────────────────────────────────────
step "Fetching mac-vnc-stream"

if [[ -d "$CLONE_DIR/.git" ]]; then
    yellow "  Repo already at $CLONE_DIR — pulling latest..."
    git -C "$CLONE_DIR" pull --ff-only \
        || yellow "  Pull failed (local changes?). Continuing with existing code."
    green "  Up to date"
else
    git clone "$REPO_URL" "$CLONE_DIR"
    green "  Cloned to $CLONE_DIR"
fi

# ── Hand off to setup.sh ───────────────────────────────────────────────────────
exec bash "$CLONE_DIR/setup.sh" "$@"
