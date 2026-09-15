#!/usr/bin/env bash
# Flowing Curve Generator -- macOS/Linux one-click installer.
#
# Meant to be downloaded and run on its own (e.g. straight from the
# project's docs page) -- it fetches the whole project into a folder next
# to itself, then hands off to that copy's own run.sh to finish setting up
# a virtual environment, install dependencies, and start the app. Safe to
# run again later: it updates the existing copy instead of re-downloading
# everything.
set -e
cd "$(dirname "$0")"

REPO_URL="https://github.com/Shane-Stevens-Git/Curvy_Line.git"
ZIP_URL="https://github.com/Shane-Stevens-Git/Curvy_Line/archive/refs/heads/main.zip"
TARGET="FlowingCurveGenerator"

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

if command -v git >/dev/null 2>&1; then
    if [ -d "$TARGET/.git" ]; then
        echo "Updating the existing copy in $TARGET ..."
        git -C "$TARGET" pull --ff-only
    else
        echo "Downloading Flowing Curve Generator into $TARGET ..."
        git clone --depth 1 "$REPO_URL" "$TARGET"
    fi
else
    echo "git was not found, so downloading a zip of the project instead ..."
    if [ -d "$TARGET" ]; then
        echo "A \"$TARGET\" folder already exists here -- using it as-is."
        echo "(Delete that folder first if you want a completely fresh copy.)"
    else
        curl -L -o curvyline.zip "$ZIP_URL"
        unzip -q curvyline.zip
        mv "Curvy_Line-main" "$TARGET"
        rm curvyline.zip
    fi
fi

if [ ! -f "$TARGET/run.sh" ]; then
    echo "Something went wrong -- $TARGET/run.sh was not found after downloading."
    exit 1
fi

chmod +x "$TARGET/run.sh"
echo ""
echo "Handing off to $TARGET/run.sh to finish setup and start the app ..."
echo ""
cd "$TARGET"
exec ./run.sh
