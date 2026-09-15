#!/usr/bin/env bash
# Flowing Curve Generator -- macOS/Linux launcher.
# Run with:  ./run.sh   (first run sets things up, later runs just start the app)
set -e
cd "$(dirname "$0")"

PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PYTHON="$candidate"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "Could not find Python 3 on your PATH."
    echo "Install it from https://www.python.org/downloads/ (or your OS's package manager), then run this again."
    exit 1
fi

if [ ! -f ".venv/bin/python" ]; then
    echo "Setting up a virtual environment in .venv the first time this runs ..."
    "$PYTHON" -m venv .venv
fi

echo "Installing/updating dependencies ..."
".venv/bin/python" -m pip install --upgrade pip >/dev/null
".venv/bin/python" -m pip install -r requirements.txt

echo "Starting Flowing Curve Generator ..."
".venv/bin/python" gui.py
