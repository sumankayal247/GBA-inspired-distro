"""
Common constants, contract definitions, and utilities for GBA OS.
Adheres strictly to docs/CONTRACT.md and docs/DECISIONS.md.
"""

import os
import sys
import json
import hashlib
import time

SYS_CONTRACT_VERSION = 1

# System paths
DEFAULT_BASE_DIR = "/opt/gba-distro"
DEFAULT_DATA_DIR = "/data"
DEFAULT_GAMES_DIR = "/games"
DEFAULT_RUN_DIR = "/run/gba"

# Fallback paths for desktop/development testing
DEV_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEV_DATA_DIR = os.path.join(DEV_BASE_DIR, "data")
DEV_GAMES_DIR = "/games" if os.path.exists("/games") else os.path.join(DEV_BASE_DIR, "games")
DEV_RUN_DIR = "/tmp/gba-run"

def get_base_dir() -> str:
    if os.path.exists(DEFAULT_BASE_DIR):
        return DEFAULT_BASE_DIR
    return DEV_BASE_DIR

def get_data_dir() -> str:
    if os.path.exists(DEFAULT_DATA_DIR) and os.access(DEFAULT_DATA_DIR, os.W_OK):
        return DEFAULT_DATA_DIR
    os.makedirs(DEV_DATA_DIR, exist_ok=True)
    return DEV_DATA_DIR

def get_games_dir() -> str:
    if os.path.exists(DEFAULT_GAMES_DIR):
        return DEFAULT_GAMES_DIR
    os.makedirs(DEV_GAMES_DIR, exist_ok=True)
    return DEV_GAMES_DIR

def get_run_dir() -> str:
    run_dir = DEFAULT_RUN_DIR if os.path.exists("/run") and os.access("/run", os.W_OK) else DEV_RUN_DIR
    os.makedirs(run_dir, exist_ok=True)
    return run_dir

def compute_content_id(file_path: str) -> str:
    """Derives a stable cryptographic entry ID from file content (Rule L2)."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()[:16]

def extract_gba_title(file_path: str) -> str:
    """Reads the GBA internal header title (0xA0..0xAB), falls back to filename."""
    try:
        with open(file_path, "rb") as f:
            f.seek(0xA0)
            raw_title = f.read(12).decode("ascii", errors="ignore").strip("\x00 \t\r\n")
            if raw_title and len(raw_title) >= 2 and all(c.isprintable() for c in raw_title):
                # Clean up all-caps titles into Title Case if needed
                return raw_title
    except Exception:
        pass
    
    # Fallback to file base name without extension
    base = os.path.splitext(os.path.basename(file_path))[0]
    return base.replace("_", " ").replace("-", " ")
