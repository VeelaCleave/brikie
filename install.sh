#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "  ▀▄▀▄▀▄  b r i k i e  ▄▀▄▀▄▀"
echo "  build your agent · brick by brick"
echo ""

PYTHON=""
for cmd in python3.12 python3.11 python3; do
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

if [ ! -d .venv ]; then
    echo "  Creating virtual environment..."
    "$PYTHON" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "  Installing the Baseplate kernel..."
pip install --quiet --upgrade pip
pip install --quiet -e ".[dev]"

echo "  Done."

# Hand over to the interactive brick picker.
python3 -m brikie.install

echo ""
echo "  (activate the environment first:  source .venv/bin/activate)"
echo ""
