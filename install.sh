#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Brikie — Modular Agentic Harness
# Install script: sets up the Baseplate kernel and prompts for initial bricks.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "╔══════════════════════════════════════════════════════╗"
echo "║     Brikie — Modular Agentic Harness                ║"
echo "║     Baseplate Kernel Installation                   ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# ── Check Python ─────────────────────────────────────────────────────────
PYTHON=""
for cmd in python3.11 python3; do
    if command -v "$cmd" &>/dev/null; then
        PYTHON="$cmd"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3.11+ is required but not found."
    echo "Install it from https://www.python.org/downloads/"
    exit 1
fi

PY_VER=$("$PYTHON" --version 2>&1 | grep -oP '\d+\.\d+')
echo "  Python version: $("$PYTHON" --version)"
echo ""

# ── Create virtual environment (optional) ────────────────────────────────
if [ ! -d .venv ]; then
    echo "  Creating virtual environment (.venv)..."
    "$PYTHON" -m venv .venv
    echo "  Done."
    echo ""
fi

source .venv/bin/activate 2>/dev/null || true
PIP="pip3"

# ── Install dependencies ─────────────────────────────────────────────────
echo "  Installing dependencies..."
"$PIP" install --quiet --upgrade pip setuptools wheel
"$PIP" install --quiet -e .
echo "  Done."
echo ""

# ── Verify ───────────────────────────────────────────────────────────────
echo "  Verifying installation..."
if "$PYTHON" -c "from brikie.kernel.registry import BrickRegistry; print('  Kernel OK')" 2>/dev/null; then
    echo "  Baseplate kernel verified."
else
    echo "  WARNING: Kernel verification failed."
fi
echo ""

# ── Configuration ────────────────────────────────────────────────────────
echo "╔══════════════════════════════════════════════════════╗"
echo "║     Brick Selection                                 ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

echo "  Brikie needs at least one Provider Brick and one Interface Brick."
echo ""

# Provider selection
echo "  Available Provider Bricks:"
echo "    1) HTTPProvider (OpenAI / compatible API — default)"
echo "    2) Skip (run 'brikie --provider <path>' later)"
echo ""
read -rp "  Select provider [1]: " PROVIDER_CHOICE
PROVIDER_CHOICE="${PROVIDER_CHOICE:-1}"
echo ""

# Interface selection
echo "  Available Interface Bricks:"
echo "    1) CLIBrick (terminal stdin/stdout — default)"
echo "    2) Skip (run 'brikie --interface <path>' later)"
echo ""
read -rp "  Select interface [1]: " INTERFACE_CHOICE
INTERFACE_CHOICE="${INTERFACE_CHOICE:-1}"
echo ""

# ── LLM endpoint configuration ────────────────────────────────────────────
echo "  LLM Endpoint Configuration"
echo ""
read -rp "  Model name [gpt-4o]: " MODEL
MODEL="${MODEL:-gpt-4o}"
read -rp "  API base URL [https://api.openai.com/v1]: " BASE_URL
BASE_URL="${BASE_URL:-https://api.openai.com/v1}"
read -rsp "  API key (leave blank to skip): " API_KEY
API_KEY="${API_KEY:-}"
echo ""
echo ""

# ── Summary ──────────────────────────────────────────────────────────────
echo "╔══════════════════════════════════════════════════════╗"
echo "║     Installation Complete                           ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "  Run brikie:"
echo "    brikie --model \"$MODEL\" --base-url \"$BASE_URL\""
echo ""
echo "  Or with virtual environment:"
echo "    source .venv/bin/activate && brikie --model \"$MODEL\" --base-url \"$BASE_URL\""
echo ""
echo "  Documentation: https://github.com/VeelaCleave/brikie"
