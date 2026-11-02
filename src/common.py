"""
Common constants, contract definitions, and utilities for GBA OS.
Adheres strictly to docs/CONTRACT.md and docs/DECISIONS.md.
"""

import os
import sys
import json
import hashlib
import re
import time
from typing import Optional

SYS_CONTRACT_VERSION = 1

# Launcher -> supervisor exit protocol (single source of truth; see CONTRACT.md S6).
# 0..9 are reserved for ordinary process failures so a signal death is never
# mistaken for a request.
EXIT_CODE_OK = 0
EXIT_CODE_LAUNCH_REQUEST = 10
EXIT_CODE_REBOOT = 100
EXIT_CODE_POWEROFF = 101
EXIT_CODE_RECOVERY = 102

# Log hygiene (Rule ST5): a runaway log must never be able to fill DATA.
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_MAX_ARCHIVES = 5

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

def _rotate_log_file(log_path: str) -> None:
    """
    Size-caps the active session log (Rule ST5).

    Rotation is done with a plain rename chain so it is safe on FAT-ish and
    journaling filesystems alike: boot_*.log -> boot_*.log.1 -> ... -> .N.
    The live file is kept small, so a chatty handler cannot fill DATA.
    """
    try:
        if os.path.getsize(log_path) < LOG_MAX_BYTES:
            return
    except OSError:
        return

    try:
        # Shift older archives first so the live file can become `.1`.
        for idx in range(LOG_MAX_ARCHIVES - 1, 0, -1):
            older = f"{log_path}.{idx}"
            if os.path.exists(older):
                os.replace(older, f"{log_path}.{idx + 1}")
        os.replace(log_path, f"{log_path}.1")
        # Drop anything past the cap.
        for name in os.listdir(os.path.dirname(log_path)):
            base = os.path.basename(log_path)
            if name.startswith(base + ".") and name[len(base) + 1:].isdigit():
                if int(name[len(base) + 1:]) > LOG_MAX_ARCHIVES:
                    try:
                        os.remove(os.path.join(os.path.dirname(log_path), name))
                    except OSError:
                        pass
    except Exception:
        pass


def prune_old_logs(keep: int = 25) -> None:
    """Removes the oldest boot logs beyond `keep` (Rule ST5 disk-safety cap)."""
    try:
        log_dir = get_logs_dir()
        latest = os.path.realpath(os.path.join(log_dir, "latest.log"))
        candidates = []
        for name in os.listdir(log_dir):
            if not name.startswith("boot_") or ".log" not in name:
                continue
            path = os.path.join(log_dir, name)
            if os.path.realpath(path) == latest:
                continue
            candidates.append((os.path.getmtime(path), path))
        candidates.sort()
        for _, path in candidates[: max(0, len(candidates) - keep)]:
            try:
                os.remove(path)
            except OSError:
                pass
    except Exception:
        pass


def setup_logging(name: str):
    """Configures centralized timestamped logging to file and console."""
    import logging
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    
    logger.setLevel(logging.DEBUG)
    log_file = get_current_log_file()
    _rotate_log_file(log_file)
    
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

def _clean_title(raw: str) -> str:
    """Normalizes a cartridge header title read from a ROM."""
    cleaned = " ".join(raw.replace("\x00", " ").split())
    cleaned = cleaned.strip(" \t\r\n-_:.|")
    if len(cleaned) < 2 or not all(c.isprintable() for c in cleaned):
        return ""
    if not any(c.isalnum() for c in cleaned):
        return ""
    # Reject filler bytes / uninitialized header space ("xxxxxxx", "_______").
    if len(cleaned) > 4 and len(set(cleaned.replace(" ", ""))) <= 2:
        return ""
    # Many official carts ship ALL-CAPS titles; keep short ones as-is (looks
    # intentional) but expand longer ones to Title Case for a friendlier list.
    if len(cleaned) > 3 and cleaned.isupper():
        cleaned = cleaned.title()
    return cleaned


def _title_from_filename(file_path: str) -> str:
    base = os.path.splitext(os.path.basename(file_path))[0]
    # Strip common ROM dump decorations: "(USA) (Rev 1) [b]" -> ""
    base = re.sub(r"\s*[\(\[].*?[\)\]]", "", base)
    base = base.replace("_", " ").replace("-", " ")
    return " ".join(base.split()) or os.path.basename(file_path)


def extract_gba_title(file_path: str) -> str:
    """
    Reads the cartridge header title, falling back to a cleaned filename.

    GBA titles live at 0xA0; original Game Boy (and GBC, for compatibility)
    titles live at 0x134. A mis-parsed header is ignored rather than shown.
    """
    ext = os.path.splitext(file_path)[1].lower()
    offsets = (0x134,) if ext in (".gb", ".gbc") else (0xA0, 0x134)
    for offset in offsets:
        try:
            with open(file_path, "rb") as f:
                f.seek(offset)
                raw = f.read(16 if offset == 0x134 else 12)
            title = _clean_title(raw.decode("ascii", errors="ignore"))
            if title:
                return title
        except Exception:
            continue
    return _title_from_filename(file_path)


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
                    if k in bindings and isinstance(v, int) and not isinstance(v, bool):
                        bindings[k] = v
                return sanitize_keybindings(bindings)
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
                return sanitize_keybindings(bindings)
        except Exception:
            pass

    return sanitize_keybindings(bindings)

def save_keybindings(bindings: dict) -> None:
    """Persists keybindings to both JSON config and mGBA config.ini."""
    import configparser
    bindings = sanitize_keybindings(bindings)
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
    paths = [os.path.expanduser("~/.config/mgba/config.ini")]
    # When the shell runs as root under a `su`/`sudo` session, keep the real
    # desktop user's mGBA profile in sync too instead of hard-coding a name.
    for user_env in ("SUDO_USER", "GBA_SESSION_USER"):
        user = os.environ.get(user_env)
        if not user or user == "root":
            continue
        try:
            import pwd
            home = pwd.getpwnam(user).pw_dir
        except Exception:
            continue
        candidate = os.path.join(home, ".config", "mgba", "config.ini")
        if candidate not in paths:
            paths.append(candidate)
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


# Keys the shell itself reserves for navigation/exit. Remapping a GBA button to
# one of these would make the controller unusable, so assignments skip them.
RESERVED_ACTION_KEYS = {
    27,      # Escape  -> exit game / back to library
    9,       # Tab     -> switch category
    1073741906, 1073741905, 1073741904, 1073741903,  # Arrow keys
    1073742048, 1073742049, 1073742050,              # L-CTRL / L-SHIFT / L-ALT
}

_RESERVED_MESSAGE = "That key is reserved by the shell (Esc, Tab, Arrows, Ctrl/Alt). Pick another."


def is_reserved_action_key(keycode: int) -> bool:
    """True when `keycode` cannot be bound to a GBA button."""
    return keycode in RESERVED_ACTION_KEYS


def sanitize_keybindings(bindings: dict) -> dict:
    """
    Returns a valid keybinding map (Rule I3: one profile, no conflicts).

    Invalid, reserved or duplicate assignments fall back to the shipped default
    so a hand-edited config can never leave the shell without working controls.
    """
    result = dict(DEFAULT_KEYBINDINGS)
    if not isinstance(bindings, dict):
        return result

    default_owner = {value: key for key, value in DEFAULT_KEYBINDINGS.items()}
    used = set()
    for key, _label in GBA_BUTTON_ORDER:
        value = bindings.get(key)
        valid = (
            isinstance(value, int)
            and not isinstance(value, bool)
            and value > 0
            and value not in RESERVED_ACTION_KEYS
            and value not in used
            # A value that is another button's default is almost always a
            # mistyped or stale config; prefer the shipped default.
            and default_owner.get(value, key) == key
        )
        if not valid:
            value = DEFAULT_KEYBINDINGS[key]
        result[key] = value
        used.add(value)
    return result


def keybindings_conflict_error(bindings: dict, key_id: str, keycode: int) -> "str | None":
    """Returns a human-readable rejection reason, or None if the binding is fine."""
    if keycode <= 0:
        return "Unsupported key."
    if is_reserved_action_key(keycode):
        return _RESERVED_MESSAGE
    for other_id, label in GBA_BUTTON_ORDER:
        if other_id != key_id and bindings.get(other_id) == keycode:
            return f"Already assigned to {label}."
    return None


# --- Play history -----------------------------------------------------------
# Rule S5 forbids the supervisor from touching save data. Launch history is
# therefore owned by the launcher and kept next to its own config.

HISTORY_MAX_ENTRIES = 20


def _history_path() -> str:
    return os.path.join(get_data_dir(), "config", "history.json")


def load_play_history() -> dict:
    """Returns {entry_id: {"last_played": epoch, "sessions": n, "seconds": n}}."""
    path = _history_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_play_history(history: dict) -> None:
    path = _history_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


def record_play_session(entry_id: str, duration_seconds: float = 0.0) -> None:
    """Marks an entry as recently played and accumulates play time."""
    if not entry_id:
        return
    history = load_play_history()
    record = history.get(entry_id, {})
    record["last_played"] = time.time()
    record["sessions"] = int(record.get("sessions", 0)) + 1
    record["seconds"] = int(record.get("seconds", 0) + max(0, int(duration_seconds)))
    history[entry_id] = record

    if len(history) > HISTORY_MAX_ENTRIES * 4:
        ordered = sorted(history.items(), key=lambda kv: kv[1].get("last_played", 0), reverse=True)
        history = dict(ordered[:HISTORY_MAX_ENTRIES])
    _save_play_history(history)


# --- Session metadata for the shell UI --------------------------------------

def format_duration(seconds: float) -> str:
    """Compact human duration: 42s, 12m, 3h 20m."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


def get_battery_status() -> Optional[dict]:
    """
    Reads the first battery-backed power supply from sysfs.

    Returns None on desktops without a battery so the shell can hide the gauge
    instead of drawing a fake full battery.
    """
    base = "/sys/class/power_supply"
    try:
        entries = os.listdir(base)
    except Exception:
        return None
    for name in entries:
        try:
            path = os.path.join(base, name)
            with open(os.path.join(path, "type"), "r") as f:
                if f.read().strip() != "Battery":
                    continue
            with open(os.path.join(path, "capacity"), "r") as f:
                capacity = int(f.read().strip())
            status = "Unknown"
            status_path = os.path.join(path, "status")
            if os.path.isfile(status_path):
                with open(status_path, "r") as f:
                    status = f.read().strip()
            return {"capacity": max(0, min(100, capacity)), "status": status}
        except Exception:
            continue
    return None


def get_uptime_seconds() -> float:
    try:
        with open("/proc/uptime", "r") as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0


def get_load_average() -> tuple:
    try:
        return os.getloadavg()
    except Exception:
        return (0.0, 0.0, 0.0)


def get_memory_usage() -> dict:
    """Returns {total_mb, used_mb, percent} from /proc/meminfo."""
    total = available = 0
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1]) // 1024
                elif line.startswith("MemAvailable:"):
                    available = int(line.split()[1]) // 1024
    except Exception:
        return {"total_mb": 0, "used_mb": 0, "percent": 0}
    used = max(0, total - available) if total else 0
    percent = min(100, int(used / total * 100)) if total else 0
    return {"total_mb": total, "used_mb": used, "percent": percent}


def file_age_seconds(path: str) -> Optional[float]:
    """How long ago a file was modified, or None when it does not exist."""
    try:
        return max(0.0, time.time() - os.path.getmtime(path))
    except Exception:
        return None


# --- Display preferences ----------------------------------------------------
# Stored in DATA so the choice survives reboots. The supervisor forwards it to
# handlers as SYS_DISPLAY_SCALING so emulators and the shell agree (Rule D2/I3).

DISPLAY_SCALING_MODES = ["integer", "stretch"]
_LEGACY_SCALING_ALIASES = {
    "Integer Aspect (Crisp)": "integer",
    "Fullscreen Stretched": "stretch",
}


def display_scaling_label(mode: str) -> str:
    return {
        "integer": "Integer Aspect (Crisp)",
        "stretch": "Fullscreen Stretched",
    }.get(mode, mode)


def load_display_config() -> dict:
    config = {"scaling": "integer"}
    path = os.path.join(get_data_dir(), "config", "display.json")
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                scaling = data.get("scaling", "integer")
                scaling = _LEGACY_SCALING_ALIASES.get(scaling, scaling)
                if scaling in DISPLAY_SCALING_MODES:
                    config["scaling"] = scaling
        except Exception:
            pass
    return config


def save_display_config(config: dict) -> None:
    path = os.path.join(get_data_dir(), "config", "display.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    scaling = config.get("scaling", "integer")
    scaling = _LEGACY_SCALING_ALIASES.get(scaling, scaling)
    if scaling not in DISPLAY_SCALING_MODES:
        scaling = "integer"
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"scaling": scaling}, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


# --- Storage layout names (Rule H1 / section 3.2) ---------------------------
SYS_SAVE_DIR_NAME = "saves"
SYS_HANDLERS_DIR_NAME = "handlers"
