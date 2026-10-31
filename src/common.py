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

def get_logs_dir() -> str:
    log_dir = os.path.join(get_data_dir(), "logs")
    os.makedirs(log_dir, exist_ok=True)
    return log_dir

def get_current_log_file() -> str:
    """Returns the path to the current boot session log file (Rule D-LOG)."""
    env_log = os.environ.get("GBA_LOG_FILE")
    if env_log:
        return env_log
    
    log_dir = get_logs_dir()
    latest_symlink = os.path.join(log_dir, "latest.log")
    
    # If GBA-tty1-wrapper already created a latest.log symlink pointing to current boot log
    if os.path.islink(latest_symlink):
        try:
            target = os.path.realpath(latest_symlink)
            if os.path.exists(target):
                os.environ["GBA_LOG_FILE"] = target
                return target
        except Exception:
            pass

    # Create new timestamped log
    ts = time.strftime("%Y-%m-%d_%H-%M-%S")
    log_path = os.path.join(log_dir, f"boot_{ts}.log")
    os.environ["GBA_LOG_FILE"] = log_path
    try:
        if os.path.lexists(latest_symlink):
            os.remove(latest_symlink)
        os.symlink(log_path, latest_symlink)
    except Exception:
        pass
    return log_path

def setup_logging(name: str):
    """Configures centralized timestamped logging to file and console."""
    import logging
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    
    logger.setLevel(logging.DEBUG)
    log_file = get_current_log_file()
    
    formatter = logging.Formatter(
        "[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    
    try:
        fh = logging.FileHandler(log_file, mode="a", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    except Exception:
        pass
        
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    
    return logger

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

DEFAULT_KEYBINDINGS = {
    "keyUp": 1073741906,     # Up Arrow
    "keyDown": 1073741905,   # Down Arrow
    "keyLeft": 1073741904,   # Left Arrow
    "keyRight": 1073741903,  # Right Arrow
    "keySelect": 8,          # Backspace (GBA SELECT)
    "keyA": 120,             # X key (GBA A)
    "keyB": 122,             # Z key (GBA B)
    "keyL": 97,              # A key (GBA L)
    "keyR": 115,             # S key (GBA R)
    "keyStart": 13           # Enter / Return (GBA START)
}

GBA_BUTTON_ORDER = [
    ("keyUp", "D-PAD UP"),
    ("keyDown", "D-PAD DOWN"),
    ("keyLeft", "D-PAD LEFT"),
    ("keyRight", "D-PAD RIGHT"),
    ("keySelect", "SELECT"),
    ("keyA", "A BUTTON"),
    ("keyB", "B BUTTON"),
    ("keyL", "L TRIGGER"),
    ("keyR", "R TRIGGER"),
    ("keyStart", "START")
]

def load_keybindings() -> dict:
    """Loads active keybindings from config files, falling back to defaults."""
    import configparser
    bindings = dict(DEFAULT_KEYBINDINGS)
    
    # 1. Try JSON config in data_dir
    json_path = os.path.join(get_data_dir(), "config", "keybindings.json")
    if os.path.isfile(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                for k, v in data.items():
                    if k in bindings and isinstance(v, int):
                        bindings[k] = v
                return bindings
        except Exception:
            pass

    # 2. Try mGBA config.ini
    mgba_cfg = os.path.expanduser("~/.config/mgba/config.ini")
    if os.path.isfile(mgba_cfg):
        try:
            cp = configparser.ConfigParser()
            cp.optionxform = str
            cp.read(mgba_cfg)
            if "input.SDLK" in cp:
                for k in bindings.keys():
                    if k in cp["input.SDLK"]:
                        bindings[k] = int(cp["input.SDLK"][k])
                return bindings
        except Exception:
            pass

    return bindings

def save_keybindings(bindings: dict) -> None:
    """Persists keybindings to both JSON config and mGBA config.ini."""
    import configparser
    # 1. Save JSON config in data_dir
    config_dir = os.path.join(get_data_dir(), "config")
    os.makedirs(config_dir, exist_ok=True)
    json_path = os.path.join(config_dir, "keybindings.json")
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(bindings, f, indent=2)
    except Exception:
        pass

    # 2. Save to user mGBA config.ini
    paths = [
        os.path.expanduser("~/.config/mgba/config.ini"),
        "/home/suman/.config/mgba/config.ini"
    ]
    for p in paths:
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            cp = configparser.ConfigParser()
            cp.optionxform = str
            if os.path.isfile(p):
                cp.read(p)
            if "ports.sdl" not in cp:
                cp["ports.sdl"] = {
                    "fullscreen": "1",
                    "lockAspectRatio": "1",
                    "lockIntegerScaling": "0"
                }
            if "input.SDLK" not in cp:
                cp["input.SDLK"] = {}
            for k, v in bindings.items():
                cp["input.SDLK"][k] = str(v)
            with open(p, "w", encoding="utf-8") as f:
                cp.write(f)
        except Exception:
            pass

