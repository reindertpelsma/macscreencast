#!/usr/bin/env bash
# install.sh — set up mac-vnc-stream dependencies on macOS
set -e

echo "==> mac-vnc-stream install"

# Ensure Python 3.9+
if ! command -v python3 &>/dev/null; then
  echo "Error: python3 not found. Install Python 3.9+ first."
  exit 1
fi
PY=$(python3 -c "import sys; print(sys.version_info[:2] >= (3,9))")
if [ "$PY" != "True" ]; then
  echo "Error: Python 3.9+ required."
  exit 1
fi

# Install libturbojpeg via Homebrew (optional but strongly recommended)
if command -v brew &>/dev/null; then
  echo "==> Installing jpeg-turbo (for fast JPEG encoding)..."
  brew install jpeg-turbo || true
else
  echo "==> Homebrew not found; skipping jpeg-turbo (will fall back to Pillow)"
fi

# Install Python dependencies
echo "==> Installing Python packages..."
python3 -m pip install --upgrade websockets numpy Pillow cryptography PyTurboJPEG

echo ""
echo "==> Done! Run the server with:"
echo "    python3 server.py --vnc-pass <your_vnc_password>"
echo ""
echo "Then from your laptop:"
echo "    ssh -L 6081:localhost:6081 user@your-mac"
echo "    open http://localhost:6081"
