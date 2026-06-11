#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "╔══════════════════════════════════════════════════════╗"
echo "║     Brikie — Modular Agentic Harness                ║"
echo "║     Baseplate Kernel Installation                   ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

PYTHON=""
for cmd in python3.11 python3; do
    if command -v "$cmd" &>/dev/null; then
        PYTHON="$cmd"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3.11+ is required but not found."
    exit 1
fi

echo "  Python: $("$PYTHON" --version)"
echo ""

if [ ! -d .venv ]; then
    echo "  Creating virtual environment..."
    "$PYTHON" -m venv .venv
fi

if [ -f .venv/bin/activate ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
else
    echo "ERROR: Virtual environment not found at .venv"
    exit 1
fi

pip install --quiet --upgrade pip setuptools wheel
pip install --quiet -e ".[dev]"
pip install --quiet httpx rich aiosqlite pydantic tiktoken PyYAML pytest pytest-asyncio

echo "  Done."
echo ""

echo "  Available Build Sets:"
for f in brikie/bricks/build/sets/*.json; do
    name=$(basename "$f" .json)
    desc=$("$PYTHON" -c "import json; print(json.load(open('$f')).get('description',''))" 2>/dev/null || echo "")
    printf "    %-12s %s\n" "$name" "$desc"
done
echo ""

read -rp "  Select set [local]: " SET_NAME
SET_NAME="${SET_NAME:-local}"
echo ""

echo "  Run brikie:"
echo "    brikie --set \"$SET_NAME\""
echo ""
echo "  Or with virtual environment:"
echo "    source .venv/bin/activate && brikie --set \"$SET_NAME\""
echo ""
