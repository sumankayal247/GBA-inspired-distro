#!/bin/bash
# ==============================================================================
# GBA OS — Desktop / Windowed Test Launcher
#
# Runs the full supervisor + shell inside a window on the current desktop, so
# the appliance can be exercised without rebooting.
# ==============================================================================

set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR" || exit 1

PYTHON="${PYTHON:-python3}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "error: $PYTHON not found. Install Python 3.8+ first." >&2
    exit 1
fi

if ! "$PYTHON" -c "import pygame" >/dev/null 2>&1; then
    echo "error: pygame is not installed." >&2
    echo "       Install it with:  $PYTHON -m pip install --user pygame" >&2
    echo "       or:               sudo apt install python3-pygame" >&2
    exit 1
fi

if [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
    echo "warning: no graphical session detected; the shell will fail to open a window." >&2
    echo "         Set DISPLAY=:0 if you are running this from a TTY." >&2
fi

export PYTHONPATH="$DIR/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$PYTHON" "$DIR/src/gba_supervisor.py" --desktop
