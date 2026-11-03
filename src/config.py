"""
Configuration, display setup and hardware helpers for GBA OS.

Everything the shell can customise lives in one JSON document under
`DATA/config/settings.json` so settings are inspectable, diffable and safe to
reset. Hardware helpers degrade silently on machines without the relevant
sysfs/debugfs interfaces.
"""

import glob
import json
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from common import get_data_dir

SETTINGS_VERSION = 1

# The shell is designed against a 320-unit reference height. Every coordinate
# and font size is multiplied by (screen_height / 320), so the UI is drawn at
# native resolution and fills the whole display without letterboxing.
DESIGN_HEIGHT = 320

# ---------------------------------------------------------------------------
# In-game actions (see src/gba_ingame.py and the mGBA handler).
#
# Shortcuts are expressed as *GBA button combinations*, not raw keyboard
# codes: the controller stays the primary device and the shared keybinding
# profile (common.load_keybindings) supplies the keyboard fallback.  A combo
# is a pair of distinct button ids from GBA_BUTTON_ORDER.
# ---------------------------------------------------------------------------
ACTION_CHEATS = "cheats"
ACTION_FAST_FORWARD = "fast_forward"
ACTION_SAVE_STATES = "save_states"

SHORTCUT_ACTIONS = (ACTION_CHEATS, ACTION_FAST_FORWARD, ACTION_SAVE_STATES)

SHORTCUT_SETTING_FOR_ACTION = {
    ACTION_CHEATS: "shortcut_cheats",
    ACTION_FAST_FORWARD: "shortcut_fast_forward",
    ACTION_SAVE_STATES: "shortcut_save_states",
}

SHORTCUT_LABELS = {
    ACTION_CHEATS: "Cheats Overlay",
    ACTION_FAST_FORWARD: "Fast-Forward Cycle",
    ACTION_SAVE_STATES: "Save States Overlay",
}

DEFAULT_SHORTCUTS: Dict[str, List[str]] = {
    # Arrow Up + Arrow Left (D-Pad up + D-Pad left on a controller)
    "shortcut_cheats": ["keyUp", "keyLeft"],
    # L + R pressed together
    "shortcut_fast_forward": ["keyL", "keyR"],
    # Arrow Left + Arrow Right
    "shortcut_save_states": ["keyLeft", "keyRight"],
}

# The button ids are the same ones the shared keybinding profile uses (I3).
SHORTCUT_BUTTON_IDS = (
    "keyUp", "keyDown", "keyLeft", "keyRight", "keySelect",
    "keyA", "keyB", "keyL", "keyR", "keyStart",
)

SHORTCUT_BUTTON_LABELS = {
    "keyUp": "UP", "keyDown": "DOWN", "keyLeft": "LEFT", "keyRight": "RIGHT",
    "keySelect": "SELECT", "keyA": "A", "keyB": "B", "keyL": "L",
    "keyR": "R", "keyStart": "START",
}

DEFAULT_SETTINGS: Dict[str, Any] = {
    "version": SETTINGS_VERSION,
    # Display
    "scaling": "fit",          # fit (fill the screen) | integer (crisp, may letterbox)
    "theme": "indigo",         # indigo | violet | amber | mint
    "clock_24h": True,
    "show_clock": True,
    # Library
    "view_mode": "grid",       # grid (cartridges) | list
    "usb_autoscan": True,
    "usb_copy_subfolders": True,
    # Audio
    "volume": 85,
    "muted": False,
    # Misc
    "confirm_launch": True,
    "browser_home": "about:welcome",
    # In-game action shortcuts (controller buttons, keyboard fallback via I3)
    **{key: list(value) for key, value in DEFAULT_SHORTCUTS.items()},
}


def normalise_shortcut(value: Any, default: Optional[List[str]] = None) -> List[str]:
    """Validates a hand-edited combo, falling back to the shipped default.

    Accepts the JSON form (`["keyL", "keyR"]`) and the convenient text form
    (`"keyL+keyR"`); anything else — a single button, a duplicate, an unknown
    id, a three-button combo — is rejected so a broken file can never leave
    the emulator without a working shortcut.
    """
    fallback = list(default or DEFAULT_SHORTCUTS["shortcut_fast_forward"])
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace(",", "+").split("+")]
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        return fallback
    parts = [p for p in parts if isinstance(p, str)]
    if len(parts) != 2 or parts[0] == parts[1] or \
            any(p not in SHORTCUT_BUTTON_IDS for p in parts):
        return fallback
    return parts


def shortcut_label(combo) -> str:
    """Human-readable combo, e.g. `L + R`."""
    parts = normalise_shortcut(combo)
    return " + ".join(SHORTCUT_BUTTON_LABELS.get(p, p.upper()) for p in parts)


def load_shortcuts(settings: Optional[Dict[str, Any]] = None) -> Dict[str, Tuple[str, ...]]:
    """Returns {action: (button, button)} for the three in-game actions."""
    settings = load_settings() if settings is None else settings
    result = {}
    for action in SHORTCUT_ACTIONS:
        key = SHORTCUT_SETTING_FOR_ACTION[action]
        combo = normalise_shortcut(settings.get(key), DEFAULT_SHORTCUTS[key])
        result[action] = tuple(combo)
    return result


def save_shortcuts(shortcuts: Dict[str, Any],
                   settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Persists action combos through the single settings store (settings.json)."""
    settings = dict(load_settings() if settings is None else settings)
    for action in SHORTCUT_ACTIONS:
        key = SHORTCUT_SETTING_FOR_ACTION[action]
        if action in shortcuts:
            settings[key] = normalise_shortcut(shortcuts[action], DEFAULT_SHORTCUTS[key])
    save_settings(settings)
    return settings


SCALING_ALIASES = {
    "integer": "integer",
    "fit": "fit",
    "Integer Aspect (Crisp)": "integer",
    "Fullscreen Stretched": "fit",
    "fullscreen": "fit",
    "stretch": "fit",
}


def normalise_scaling(value: Any) -> str:
    """Maps current and legacy labels onto the two supported modes (D2)."""
    return SCALING_ALIASES.get(str(value), "fit")


def settings_path() -> str:
    return os.path.join(get_data_dir(), "config", "settings.json")


def _legacy_display_path() -> str:
    return os.path.join(get_data_dir(), "config", "display.json")


def _migrate_legacy_display(settings: Dict[str, Any]) -> bool:
    """
    One-time import of the pre-settings.json display config.

    Earlier builds kept only `config/display.json`; a user upgrading must not
    lose their scaling choice, so the legacy file is folded in the first time
    settings.json is created.
    """
    path = _legacy_display_path()
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("scaling"):
            settings["scaling"] = normalise_scaling(data["scaling"])
            return True
    except Exception:
        pass
    return False


def load_settings() -> Dict[str, Any]:
    """Returns the persisted settings merged over the defaults."""
    settings = dict(DEFAULT_SETTINGS)
    path = settings_path()
    if not os.path.isfile(path):
        _migrate_legacy_display(settings)
        return settings
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for key, value in data.items():
                if key in DEFAULT_SHORTCUTS:
                    # Combos also accept the text form "keyL+keyR", so they
                    # cannot go through the generic type check below.
                    settings[key] = normalise_shortcut(value, DEFAULT_SHORTCUTS[key])
                elif key in DEFAULT_SETTINGS and isinstance(value, type(DEFAULT_SETTINGS[key])):
                    settings[key] = value
    except Exception:
        pass
    settings["scaling"] = normalise_scaling(settings.get("scaling"))
    if settings.get("theme") not in ("indigo", "violet", "amber", "mint"):
        settings["theme"] = "indigo"
    if settings.get("view_mode") not in ("grid", "list"):
        settings["view_mode"] = "grid"
    settings["volume"] = max(0, min(100, int(settings.get("volume", 85))))
    # Combos are lists in settings.json: validate them explicitly so a broken
    # file cannot disable the in-game actions or inject unknown button ids.
    for shortcut_key, default in DEFAULT_SHORTCUTS.items():
        settings[shortcut_key] = normalise_shortcut(settings.get(shortcut_key), default)
    return settings


def save_settings(settings: Dict[str, Any]) -> None:
    """Atomically persists settings, keeping only known keys."""
    path = settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {k: settings.get(k, v) for k, v in DEFAULT_SETTINGS.items()}
    payload["version"] = SETTINGS_VERSION
    payload["scaling"] = normalise_scaling(payload.get("scaling"))
    for shortcut_key, default in DEFAULT_SHORTCUTS.items():
        payload[shortcut_key] = normalise_shortcut(payload.get(shortcut_key), default)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        pass


# --- display geometry -------------------------------------------------------

def canvas_size(display_w: int, display_h: int) -> Tuple[int, int]:
    """
    Returns the drawing surface size for a display.

    We draw at the display's own resolution (so text is rendered crisply at
    native size) and derive a uniform UI scale from the height.
    """
    display_w = max(320, int(display_w))
    display_h = max(240, int(display_h))
    return display_w, display_h


def ui_scale_for(height: int) -> float:
    return max(0.75, min(6.0, height / float(DESIGN_HEIGHT)))


def design_width(canvas_w: int, canvas_h: int) -> float:
    """Width of the layout in design units for the current surface."""
    return canvas_w / ui_scale_for(canvas_h)


# --- SDL / video ------------------------------------------------------------

def configure_sdl_video(force: Optional[str] = None) -> str:
    """
    Chooses an SDL video driver that can actually present to the user.

    A KMS/DRM driver left in the environment while an X11 session is running is
    the classic cause of a "black screen" appliance: SDL grabs a DRM master it
    cannot use, renders nothing and every child inherits the same mistake.
    This function normalises the environment for both the shell and mGBA.
    """
    requested = force or os.environ.get("GBA_VIDEO_DRIVER")
    if requested:
        os.environ["SDL_VIDEODRIVER"] = requested
        return requested

    if os.environ.get("SDL_VIDEODRIVER") == "dummy" or os.environ.get("GBA_HEADLESS") == "1":
        os.environ["SDL_VIDEODRIVER"] = "dummy"
        return "dummy"

    has_x11 = bool(os.environ.get("DISPLAY"))
    has_wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
    current = os.environ.get("SDL_VIDEODRIVER", "")

    if has_x11:
        if current.lower() in ("kmsdrm", "directfb", ""):
            os.environ["SDL_VIDEODRIVER"] = "x11"
    elif has_wayland:
        if current.lower() in ("kmsdrm", ""):
            os.environ["SDL_VIDEODRIVER"] = "wayland"
    else:
        # Bare console: KMS/DRM is the right backend when a DRM device exists.
        if current.lower() in ("", "x11", "wayland"):
            if glob.glob("/dev/dri/card*") or glob.glob("/dev/dri/renderD*"):
                os.environ["SDL_VIDEODRIVER"] = "kmsdrm"
    return os.environ.get("SDL_VIDEODRIVER", "")


def _x11_socket(display: str) -> bool:
    """True when $DISPLAY actually points at a live X server."""
    try:
        host, _, rest = display.partition(":")
        if host and host != "unix":
            return True                      # remote/TCP display: trust it
        number = rest.split(".")[0] or "0"
        return os.path.exists(f"/tmp/.X11-unix/X{number}")
    except Exception:
        return False


def display_session(env: Optional[Dict[str, str]] = None) -> str:
    """Classifies the machine as x11, wayland, console or headless."""
    env = os.environ if env is None else env
    display = env.get("DISPLAY", "")
    if display and _x11_socket(display):
        return "x11"
    wayland = env.get("WAYLAND_DISPLAY", "")
    if wayland:
        runtime = env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        if wayland.startswith("/") or os.path.exists(os.path.join(runtime, wayland)):
            return "wayland"
    if glob.glob("/dev/dri/card*"):
        return "console"
    return "headless"


def prepare_child_environment(base: Optional[Dict[str, str]] = None,
                              force_video: Optional[str] = None) -> Dict[str, str]:
    """
    Builds an environment for a full-screen child (mGBA) that cannot end up on
    a driver with no display behind it.

    A black screen after launching a game is almost always a poisoned
    environment: a stale `DISPLAY` pointing at a dead X server, or
    `SDL_VIDEODRIVER=kmsdrm` inherited into a running desktop, which makes SDL
    take a DRM master it cannot present. Rather than inherit blindly we resolve
    the session first, then pin the matching driver (Rules D1, D4).

    Returns the new environment dict; the caller passes it to `Popen`.
    """
    env = dict(os.environ if base is None else base)
    session = display_session(env)

    if session == "x11":
        env["SDL_VIDEODRIVER"] = force_video or "x11"
        env.pop("SDL_VIDEO_KMSDRM_DEVICE", None)
    elif session == "wayland":
        env["SDL_VIDEODRIVER"] = force_video or "wayland"
        env.pop("SDL_VIDEO_KMSDRM_DEVICE", None)
    elif session == "console":
        env["SDL_VIDEODRIVER"] = force_video or "kmsdrm"
        # Two GPUs (hybrid laptops, discrete cards) make SDL pick whichever it
        # finds first, which may be a card with no outputs: a black screen with
        # a perfectly healthy machine. Pin the first card explicitly.
        if not env.get("SDL_VIDEO_KMSDRM_DEVICE"):
            cards = sorted(glob.glob("/dev/dri/card*"))
            if cards:
                env["SDL_VIDEO_KMSDRM_DEVICE"] = cards[0]
    else:
        # No display at all: SDL must not try to open one, or every child
        # (and the emulator) dies with a black screen.
        env["SDL_VIDEODRIVER"] = force_video or "dummy"

    # A stale display variable is worse than none: drop it when it is dead.
    if "DISPLAY" in env and not _x11_socket(env["DISPLAY"]):
        env.pop("DISPLAY")
    if session in ("console", "headless"):
        env.pop("DISPLAY", None)
        env.pop("WAYLAND_DISPLAY", None)

    # Audio must never be able to abort video initialisation.
    has_audio = bool(glob.glob("/dev/snd/pcm*")) or bool(env.get("PULSE_SERVER"))
    runtime = env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    has_audio = has_audio or os.path.exists(os.path.join(runtime, "pulse", "native"))
    has_audio = has_audio or os.path.exists(os.path.join(runtime, "pipewire-0"))
    if not has_audio:
        env["SDL_AUDIODRIVER"] = "dummy"
    return env


def configure_sdl_audio() -> str:
    """Falls back to the dummy audio driver when no device is available."""
    if os.environ.get("SDL_AUDIODRIVER"):
        return os.environ["SDL_AUDIODRIVER"]
    has_device = bool(glob.glob("/dev/snd/pcm*")) or bool(os.environ.get("PULSE_SERVER"))
    if not has_device and os.environ.get("GBA_HEADLESS") == "1":
        os.environ["SDL_AUDIODRIVER"] = "dummy"
    return os.environ.get("SDL_AUDIODRIVER", "")


# --- physical display geometry ----------------------------------------------

# Connector types that are built-in panels: on a laptop/handheld appliance the
# internal screen is the one the emulator window must be measured against.
INTERNAL_CONNECTORS = ("eDP", "LVDS", "DSI", "DPI")


def _parse_mode_line(line: str) -> Optional[Tuple[int, int]]:
    """'1366x768' -> (1366, 768). Rejects interlaced, garbage and absurd values."""
    parts = line.split()
    if not parts:
        return None
    width, sep, height = parts[0].partition("x")
    if not sep or not width.isdigit() or not height.isdigit():
        return None
    w, h = int(width), int(height)
    if 320 <= w <= 8192 and 200 <= h <= 8192:
        return (w, h)
    return None


def display_mode_size(env: Optional[Dict[str, str]] = None,
                      drm_root: str = "/sys/class/drm") -> Optional[Tuple[int, int]]:
    """
    The physical display resolution as (w, h), or None when unknown.

    This reads `/sys/class/drm/<card>-<connector>/{status,modes}` and never
    opens the display itself — the handler must not take a DRM master or an
    X connection that the emulator needs next (D-031/D4). The first mode line
    of a connected connector is its preferred (native) mode. Internal panels
    win over external outputs, and only the card named by a pinned
    `SDL_VIDEO_KMSDRM_DEVICE` is considered when one is set (D8). Falls back
    to xrandr's current mode under X11 when DRM sysfs says nothing.
    """
    env = os.environ if env is None else env
    pinned = os.path.basename(env.get("SDL_VIDEO_KMSDRM_DEVICE", "") or "")
    best: Optional[Tuple[int, int, Tuple[int, int]]] = None
    try:
        names = sorted(os.listdir(drm_root))
    except OSError:
        names = []
    for name in names:
        card, _, connector = name.partition("-")
        if not connector or (pinned and card != pinned):
            continue
        base = os.path.join(drm_root, name)
        try:
            with open(os.path.join(base, "status"), "r", encoding="utf-8") as f:
                if f.read().strip() != "connected":
                    continue
            with open(os.path.join(base, "modes"), "r", encoding="utf-8") as f:
                mode = _parse_mode_line(f.readline())
        except OSError:
            continue
        if not mode:
            continue
        # Prefer the internal panel; among equals prefer the larger mode.
        rank = 0 if connector.startswith(INTERNAL_CONNECTORS) else 1
        candidate = (rank, -(mode[0] * mode[1]), mode)
        if best is None or candidate < best:
            best = candidate
    if best:
        return best[2]
    if env.get("DISPLAY"):
        return _xrandr_mode_size()
    return None


def _xrandr_mode_size() -> Optional[Tuple[int, int]]:
    """The current ('*'-marked) mode according to xrandr; fallback only."""
    xrandr = shutil.which("xrandr")
    if not xrandr:
        return None
    try:
        out = subprocess.run([xrandr], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=4.0,
                             check=False).stdout.decode("utf-8", "ignore")
    except Exception:
        return None
    for line in out.splitlines():
        if "*" not in line:
            continue
        mode = _parse_mode_line(line.strip())
        if mode:
            return mode
    return None


# --- audio volume -----------------------------------------------------------

def _run(cmd: List[str], timeout: float = 4.0) -> bool:
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=timeout, check=False)
        return True
    except Exception:
        return False


def audio_available() -> bool:
    return shutil.which("amixer") is not None


def apply_volume(volume: int, muted: bool) -> bool:
    """Applies the volume through ALSA when possible. Returns True on success."""
    volume = max(0, min(100, int(volume)))
    if not audio_available():
        return False
    ok = _run(["amixer", "-q", "sset", "Master", "mute" if muted else "unmute"])
    ok = _run(["amixer", "-q", "sset", "Master", f"{volume}%"]) and ok
    # Some images expose PCM instead of Master.
    _run(["amixer", "-q", "sset", "PCM", "mute" if muted else "unmute"])
    _run(["amixer", "-q", "sset", "PCM", f"{volume}%"])
    return ok


# --- backlight --------------------------------------------------------------

def backlight_device() -> Optional[str]:
    devices = sorted(glob.glob("/sys/class/backlight/*"))
    return devices[0] if devices else None


def get_brightness() -> Optional[int]:
    device = backlight_device()
    if not device:
        return None
    try:
        with open(os.path.join(device, "brightness")) as f:
            current = int(f.read().strip())
        with open(os.path.join(device, "max_brightness")) as f:
            maximum = max(1, int(f.read().strip()))
        return int(round(current / maximum * 100))
    except Exception:
        return None


def set_brightness(percent: int) -> bool:
    device = backlight_device()
    if not device:
        return False
    try:
        with open(os.path.join(device, "max_brightness")) as f:
            maximum = max(1, int(f.read().strip()))
        value = max(1, int(maximum * max(0, min(100, percent)) / 100.0))
        with open(os.path.join(device, "brightness"), "w") as f:
            f.write(str(value))
        return True
    except Exception:
        return False


# --- storage ----------------------------------------------------------------

def storage_info(path: str) -> Optional[Dict[str, float]]:
    try:
        usage = shutil.disk_usage(path)
        return {
            "total_gb": usage.total / (1024 ** 3),
            "used_gb": usage.used / (1024 ** 3),
            "free_gb": usage.free / (1024 ** 3),
            "percent": int(usage.used / usage.total * 100) if usage.total else 0,
        }
    except Exception:
        return None


def other_drm_devices(exclude: str = "") -> List[str]:
    """The remaining DRM cards, for retrying when the first one presents nothing."""
    cards = sorted(glob.glob("/dev/dri/card*"))
    return [card for card in cards if card != exclude]
