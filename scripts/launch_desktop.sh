#!/bin/bash
# ==============================================================================
# GBA OS — Desktop Testing Launcher
# Runs GBA OS inside a test window on your current desktop environment.
# ==============================================================================

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

export PYTHONPATH="$DIR/src:$PYTHONPATH"
python3 "$DIR/src/gba_supervisor.py" --desktop
