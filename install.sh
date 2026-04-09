#!/usr/bin/env bash
# install.sh — quick setup for dog-cli
set -e
cd "$(dirname "$0")"

echo "📦  Creating virtual environment (.venv) ..."
python3 -m venv .venv

echo "📦  Installing dog-cli into .venv ..."
.venv/bin/pip install -e . -q

VENV_BIN="$(pwd)/.venv/bin"
DOG_BIN="$VENV_BIN/dog"

echo ""
echo "✅  Installed!  Binary: $DOG_BIN"
echo ""
echo "To use dog from anywhere, add it to your PATH — choose one option:"
echo ""
echo "  Option A — symlink to /usr/local/bin (requires sudo):"
echo "    sudo ln -sf $DOG_BIN /usr/local/bin/dog"
echo ""
echo "  Option B — add the venv bin directory to your shell profile:"
echo "    echo 'export PATH=\"$VENV_BIN:\$PATH\"' >> ~/.zshrc && source ~/.zshrc"
echo ""
echo "Quick test:"
echo "  dog --version"
echo "  dog claude --help"
