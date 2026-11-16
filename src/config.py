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
import re
import shutil
import subprocess
import threading
import time
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
    # Which real browser the "Web Browser" app opens: auto | netsurf | dillo
    "browser_engine": "auto",
    # Where the full browser opens when the app starts.
    "browser_start_page": "https://en.wikipedia.org/wiki/Game_Boy_Advance",
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
                              force_video: Optional[str] = None,
                              drm_root: str = "/sys/class/drm") -> Dict[str, str]:
    """
    Builds an environment for a full-screen child (mGBA) that cannot end up on
    a driver with no display behind it.

    A black screen after launching a game is almost always a poisoned
    environment: a stale `DISPLAY` pointing at a dead X server, or
    `SDL_VIDEODRIVER=kmsdrm` inherited into a running desktop, which makes SDL
    take a DRM master it cannot present. Rather than inherit blindly we resolve
    the session first, then pin the matching driver (Rules D1, D4).

    On a bare console the KMS/DRM card is chosen by `ranked_drm_cards`, which
    prefers a card that actually has a connected output: pinning the first card
    is how a hybrid-GPU machine ends up emulating perfectly onto a display that
    does not exist (Rule D8, contract test T-DISPLAY-2). `drm_root` is only for
    tests; it is the sysfs directory the ranking reads.

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
        # a perfectly healthy machine - and healthy audio, because nothing in
        # the emulator notices that no connector is wired to it. Pin a card
        # that actually drives a display (T-DISPLAY-2 / D8).
        if not env.get("SDL_VIDEO_KMSDRM_DEVICE"):
            cards = ranked_drm_cards(drm_root=drm_root)
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


def _connector_entries(drm_root: str) -> List[Tuple[str, str]]:
    """(card, connector) pairs from sysfs, e.g. ('card0', 'eDP-1')."""
    try:
        names = sorted(os.listdir(drm_root))
    except OSError:
        return []
    entries = []
    for name in names:
        card, _, connector = name.partition("-")
        if card and connector:
            entries.append((card, connector))
    return entries


def _connector_connected(card: str, connector: str, drm_root: str) -> bool:
    try:
        with open(os.path.join(drm_root, f"{card}-{connector}", "status"),
                  "r", encoding="utf-8") as f:
            return f.read().strip() == "connected"
    except OSError:
        return False


def ranked_drm_cards(drm_root: str = "/sys/class/drm",
                     devices: Optional[List[str]] = None) -> List[str]:
    """Every DRM card, best-first, for pinning SDL's KMS/DRM device (D8).

    Pinning the *first* card is what produces the classic appliance black
    screen: on a hybrid-GPU machine `card0` is usually the integrated card with
    nothing wired to it, and an emulator on that card runs perfectly - audio,
    input, frame timing all correct - while presenting nothing at all. It also
    explains a shelf that renders and a game that does not: the pin is applied
    only to the child, so the launcher keeps SDL's own (working) choice.

    Contract test T-DISPLAY-2 requires "no black screen from an output-less
    card", so the order is:

      1. a card with a connected internal panel (eDP/LVDS/DSI/DPI);
      2. any other card with a connected connector;
      3. everything else in device order - the previous behaviour, so a machine
         whose sysfs cannot be read is pinned exactly as it was before.
    """
    cards = sorted(glob.glob("/dev/dri/card*") if devices is None else devices)
    if not cards:
        return []
    best: Dict[str, int] = {}
    for card, connector in _connector_entries(drm_root):
        if f"/dev/dri/{card}" not in cards:
            continue
        if not _connector_connected(card, connector, drm_root):
            continue
        rank = 0 if connector.startswith(INTERNAL_CONNECTORS) else 1
        if rank < best.get(card, len(INTERNAL_CONNECTORS) + 1):
            best[card] = rank
    with_output = [path for path in cards if os.path.basename(path) in best]
    with_output.sort(key=lambda path: (best[os.path.basename(path)], path))
    without = [path for path in cards if os.path.basename(path) not in best]
    return with_output + without


def cards_without_output(drm_root: str = "/sys/class/drm",
                         devices: Optional[List[str]] = None) -> List[str]:
    """Cards that sysfs says drive nothing, for the log's benefit only."""
    ranked = ranked_drm_cards(drm_root=drm_root, devices=devices)
    entries = _connector_entries(drm_root)
    if not entries:
        return []                  # no evidence either way: claim nothing
    connected = {card for card, connector in entries
                 if _connector_connected(card, connector, drm_root)}
    return [path for path in ranked if os.path.basename(path) not in connected]


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
    The active X11 size or preferred DRM size as (w, h), or None.

    This reads `/sys/class/drm/<card>-<connector>/{status,modes}` and never
    opens the display itself — the handler must not take a DRM master or an
    connection that becomes the emulator's render surface (D-031/D4). The first mode line
    of a connected connector is its preferred (native) mode. Internal panels
    win over external outputs, and only the card named by a pinned
    `SDL_VIDEO_KMSDRM_DEVICE` is considered when one is set (D8). Falls back
    to DRM sysfs when X11's active mode cannot be queried.
    """
    env = os.environ if env is None else env
    # X11's active mode, not a connector's advertised preferred mode, is
    # the geometry a screen-sized unmanaged window must actually cover.
    if env.get("DISPLAY"):
        current = _xrandr_mode_size()
        if current:
            return current
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


def other_drm_devices(exclude: str = "", drm_root: str = "/sys/class/drm") -> List[str]:
    """The remaining DRM cards, best-first, for retrying when one presents nothing.

    Ordered by `ranked_drm_cards`, so the D-047 ladder retries onto a card that
    can actually present instead of onto a second output-less one.
    """
    return [card for card in ranked_drm_cards(drm_root=drm_root) if card != exclude]


# --- network / WiFi ---------------------------------------------------------

def get_wifi_status() -> Optional[Dict[str, Any]]:
    """Detect WiFi connection status via /proc/net/wireless or nmcli/ip.

    Returns None if no WiFi hardware is found, otherwise a dict with:
      - connected: bool
      - ssid: str (empty if not connected)
      - signal: int (0-100, approximate quality)
    """
    import glob as _glob
    # Check for wireless interfaces
    wireless_ifaces = []
    try:
        for iface in os.listdir("/sys/class/net"):
            phy_path = f"/sys/class/net/{iface}/wireless"
            if os.path.exists(phy_path):
                wireless_ifaces.append(iface)
    except Exception:
        pass

    if not wireless_ifaces:
        return None

    result: Dict[str, Any] = {"connected": False, "ssid": "", "signal": 0}

    # Try nmcli first (NetworkManager)
    nmcli = shutil.which("nmcli")
    if nmcli:
        try:
            out = subprocess.run(
                [nmcli, "-t", "-f", "active,ssid,signal", "dev", "wifi"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=3.0, check=False
            ).stdout.decode("utf-8", "ignore").strip()
            for line in out.splitlines():
                parts = line.split(":")
                if len(parts) >= 3 and parts[0].strip().lower() == "yes":
                    result["connected"] = True
                    result["ssid"] = parts[1].strip()
                    try:
                        result["signal"] = int(parts[2].strip())
                    except (ValueError, IndexError):
                        result["signal"] = 50
                    break
            return result
        except Exception:
            pass

    # Fallback: read /proc/net/wireless
    try:
        with open("/proc/net/wireless", "r") as f:
            lines = f.readlines()
        for line in lines[2:]:  # Skip header lines
            parts = line.split()
            if len(parts) >= 4:
                iface_name = parts[0].rstrip(":")
                if iface_name in wireless_ifaces:
                    try:
                        quality = int(parts[2].rstrip("."))
                        result["connected"] = quality > 0
                        result["signal"] = min(100, max(0, quality * 2))
                    except (ValueError, IndexError):
                        pass
                    break
    except Exception:
        pass

    return result


def get_wifi_networks() -> List[Dict[str, str]]:
    """List available WiFi networks via nmcli."""
    nmcli = shutil.which("nmcli")
    if not nmcli:
        return []
    try:
        out = subprocess.run(
            [nmcli, "-t", "-f", "ssid,signal,security,in-use", "dev", "wifi"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=5.0, check=False
        ).stdout.decode("utf-8", "ignore").strip()
        networks = []
        seen = set()
        for line in out.splitlines():
            parts = line.split(":")
            if len(parts) >= 3:
                ssid = parts[0].strip()
                if not ssid or ssid in seen:
                    continue
                seen.add(ssid)
                try:
                    signal = int(parts[1].strip())
                except (ValueError, IndexError):
                    signal = 0
                security = parts[2].strip() if len(parts) > 2 else ""
                in_use = parts[3].strip().lower() == "yes" if len(parts) > 3 else False
                networks.append({
                    "ssid": ssid,
                    "signal": signal,
                    "security": security,
                    "connected": in_use,
                })
        networks.sort(key=lambda n: n["signal"], reverse=True)
        return networks[:20]
    except Exception:
        return []


def connect_wifi(ssid: str, password: str = "") -> Tuple[bool, str]:
    """Connect to a WiFi network. Returns (success, message)."""
    nmcli = shutil.which("nmcli")
    if not nmcli:
        return False, "NetworkManager not available"
    try:
        cmd = [nmcli, "dev", "wifi", "connect", ssid]
        if password:
            cmd.extend(["--ask"])
        # Try with password via stdin if provided
        if password:
            proc = subprocess.run(
                [nmcli, "dev", "wifi", "connect", ssid,
                 "wifi-sec.key-mgmt", "wpa-psk",
                 "wifi-sec.psk", password],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=15.0, check=False
            )
        else:
            proc = subprocess.run(
                [nmcli, "dev", "wifi", "connect", ssid],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=15.0, check=False
            )
        if proc.returncode == 0:
            return True, f"Connected to {ssid}"
        err = proc.stderr.decode("utf-8", "ignore").strip()
        return False, err or "Connection failed"
    except Exception as exc:
        return False, str(exc)


def toggle_wifi(enabled: bool) -> bool:
    """Enable or disable WiFi radio."""
    nmcli = shutil.which("nmcli")
    if not nmcli:
        return False
    state = "on" if enabled else "off"
    try:
        subprocess.run(
            [nmcli, "radio", "wifi", state],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=3.0, check=False
        )
        return True
    except Exception:
        return False


# --- Bluetooth --------------------------------------------------------------
#
# Bluetooth is the only hardware path in the shell that talks to a daemon
# instead of a device file, and that daemon answers asynchronously:
# `bluetoothctl connect` prints "Attempting to connect to <MAC>" and only
# later prints "Connection successful" or a BlueZ error. The old code ran
# one-shot commands, believed the exit code and pasted the raw D-Bus error
# into the toast, so a headset that was merely busy looked like a crash:
#
#     Attempting to connect to 01:02:03:04:D2:A1 | Failed to connect:
#     org.bluez.Error.Failed br-connection-profile-unavailable
#
# br-connection-profile-unavailable means BlueZ had no profile it could
# connect for that device. In practice that is one of two things:
#
#   * no audio service is running, so there is no A2DP sink endpoint and the
#     headset has nothing to attach to (the GBA OS session ships without a
#     sound server by default), or
#   * the headset is still linked to the phone it was last used with and
#     refuses a second link.
#
# The helpers below therefore pair and trust before connecting, treat the
# device list - not the exit code - as the truth about a connection,
# translate BlueZ error codes into one actionable sentence, and report
# whether an audio service and an A2DP sink profile exist at all.

BT_CONNECT_TIMEOUT = 30.0
BT_PAIR_TIMEOUT = 30.0
BT_VERIFY_TIMEOUT = 12.0
BT_POLL_INTERVAL = 0.5

# BlueZ error text -> one actionable sentence. Matched case-insensitively as a
# substring, longest key first, so "device not available" wins over
# "not available".
BT_ERROR_HINTS: Dict[str, str] = {
    "br-connection-profile-unavailable":
        "No usable BT profile: headset busy, or no audio service",
    "br-connection-already-connected": "Already connected",
    "br-connection-failed": "Connection failed",
    "authenticationfailed": "Pairing failed: the PIN was rejected",
    "authenticationrejected": "Pairing was rejected by the headset",
    "authentication canceled": "Pairing was cancelled",
    "org.bluez.error.inprogress": "Another connection is already running",
    "org.bluez.error.notready": "Adapter not ready: toggle Bluetooth off/on",
    "org.bluez.error.nosuchdevice": "The device is gone: rescan",
    "device not available": "Out of range: rescan and keep the headset close",
    "not available": "Out of range: rescan and keep the headset close",
    "no such device": "The device is gone: rescan",
    "not connected": "The headset refused the connection",
}

# BlueZ object names -> the profile names people know them by.
BLUEZ_PROFILE_NAMES: Dict[str, str] = {
    "a2dpsink": "a2dp-sink",
    "a2dpsource": "a2dp-source",
    "hspag": "hsp-ag",
    "hsphs": "hsp-hs",
    "outputcontrol": "output-control",
    "inputcontrol": "input-control",
}


def _btctl(args: List[str], timeout: float = 5.0) -> str:
    """Run bluetoothctl once and return everything it printed (stdout+stderr).

    BlueZ prints its errors on stderr, so both streams are merged: an error
    that is only visible on stderr would otherwise look like silence.
    """
    btcli = shutil.which("bluetoothctl")
    if not btcli:
        return ""
    try:
        proc = subprocess.run(
            [btcli] + list(args),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout, check=False
        )
        return proc.stdout.decode("utf-8", "ignore")
    except subprocess.TimeoutExpired as exc:
        out = exc.output or b""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "ignore")
        return out + "\n(command timed out)"
    except Exception:
        return ""


def _btctl_session(commands: List[str], timeout: float,
                   terminators: Optional[List[str]] = None) -> str:
    """Run several bluetoothctl commands in ONE session.

    bluetoothctl registers its pairing agent for the lifetime of the
    process, so "agent on" in one invocation and "pair" in another cannot
    work: the second process has no agent and the PIN exchange never
    happens. Everything therefore happens in a single session here, and the
    output is read until a terminal line appears - the verdict is
    asynchronous - or the timeout expires.
    """
    btcli = shutil.which("bluetoothctl")
    if not btcli:
        return ""
    try:
        proc = subprocess.Popen(
            [btcli], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1
        )
    except Exception:
        return ""

    collected: List[str] = []
    finished = threading.Event()

    def reader() -> None:
        try:
            for line in proc.stdout:
                collected.append(line)
                if terminators and any(t.lower() in line.lower() for t in terminators):
                    break
        except Exception:
            pass
        finally:
            finished.set()

    thread = threading.Thread(target=reader, daemon=True, name="gba-bluetoothctl")
    thread.start()
    try:
        for command in commands:
            proc.stdin.write(command + "\n")
            proc.stdin.flush()
        finished.wait(timeout)
    except Exception:
        pass
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.stdout.close()
        except Exception:
            pass
    return "".join(collected)


def _bt_connected_macs() -> List[str]:
    """MACs BlueZ currently reports as connected."""
    macs: List[str] = []
    for line in _btctl(["devices", "Connected"], timeout=5.0).splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].lower() == "device":
            macs.append(parts[1].upper())
    return macs


def _bt_device_info(mac: str) -> Dict[str, Any]:
    """Parse "bluetoothctl info <mac>" into paired/trusted/connected/name."""
    info: Dict[str, Any] = {"paired": False, "trusted": False,
                            "connected": False, "name": ""}
    for line in _btctl(["info", mac], timeout=5.0).splitlines():
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key == "paired":
            info["paired"] = value.strip().lower() == "yes"
        elif key == "trusted":
            info["trusted"] = value.strip().lower() == "yes"
        elif key == "connected":
            info["connected"] = value.strip().lower() == "yes"
        elif key == "name" and not info["name"]:
            info["name"] = value.strip()
    return info


def _bt_wait_connected(mac: str, want_connected: bool, timeout: float) -> bool:
    """Wait until the device is (or is no longer) connected.

    The device list is the only trustworthy answer: bluetoothctl's exit code
    is zero for a failed connection just as often as for a successful one.
    """
    mac = mac.upper()
    deadline = time.monotonic() + timeout
    while True:
        if (mac in _bt_connected_macs()) == want_connected:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(BT_POLL_INTERVAL)


def bluetooth_error_message(raw: str) -> str:
    """Translate raw bluetoothctl/BlueZ output into one actionable sentence.

    "org.bluez.Error.Failed br-connection-profile-unavailable" tells a user
    nothing; "Headset busy: disconnect it from your phone" tells them what to
    do next.
    """
    text = (raw or "").strip()
    low = text.lower()
    if not text:
        return "No answer from BlueZ - is bluetoothd running?"
    for needle in sorted(BT_ERROR_HINTS, key=len, reverse=True):
        if needle.lower() in low:
            return BT_ERROR_HINTS[needle]
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line or line.lower().startswith("attempting to connect"):
            continue
        if line.lower().startswith("failed to connect:"):
            line = line.split(":", 1)[1].strip()
        return line[:72]
    return "Connection failed"


def _process_running(*names: str) -> bool:
    """True if any of the given process names is running.

    Reads /proc directly: the shell has to work on a machine without
    procps, and pgrep is not part of the appliance's dependency list.
    """
    wanted = {name.lower() for name in names}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return False
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/comm" % entry, "r", encoding="utf-8") as handle:
                comm = handle.read().strip().lower()
        except OSError:
            continue
        if comm in wanted:
            return True
    return False


def get_audio_service() -> str:
    """Name of the running sound server ("pipewire", "pulseaudio" or "")."""
    if _process_running("pipewire"):
        return "pipewire"
    if _process_running("pulseaudio"):
        return "pulseaudio"
    return ""


def _bluez_object_paths() -> List[str]:
    """Object paths BlueZ currently exports (e.g. /org/bluez/hci0/A2DPSink)."""
    dbus_send = shutil.which("dbus-send")
    if dbus_send:
        try:
            proc = subprocess.run(
                [dbus_send, "--system", "--print-reply", "--dest=org.bluez",
                 "/org/bluez", "org.freedesktop.DBus.ObjectManager.GetManagedObjects"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=5.0, check=False
            )
            out = proc.stdout.decode("utf-8", "ignore")
            return re.findall(r'string "(/org/bluez/[^"]+)"', out)
        except Exception:
            return []
    busctl = shutil.which("busctl")
    if busctl:
        try:
            proc = subprocess.run(
                [busctl, "--system", "--no-pager", "tree", "org.bluez"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=5.0, check=False
            )
            out = proc.stdout.decode("utf-8", "ignore")
            return [line.strip() for line in out.splitlines()
                    if line.strip().startswith("/org/bluez")]
        except Exception:
            return []
    return []


def get_bluetooth_profiles() -> List[str]:
    """Profiles BlueZ currently exposes (e.g. ["a2dp-sink", "hsp-ag"]).

    A Bluetooth headset can only be connected when the profile it speaks is
    registered locally. Without an audio service there is no a2dp-sink, and
    every connect attempt fails with br-connection-profile-unavailable.
    """
    profiles: List[str] = []
    for path in _bluez_object_paths():
        segment = path.rstrip("/").rsplit("/", 1)[-1]
        if not segment or segment.startswith("hci"):
            continue
        name = BLUEZ_PROFILE_NAMES.get(segment.lower(), segment.lower())
        if name not in profiles:
            profiles.append(name)
    return profiles


def get_bluetooth_diagnostics() -> Dict[str, Any]:
    """Everything needed to explain a failed connection, in one dict.

      - available:   bluetoothctl is installed
      - adapter:     adapter name
      - powered:     the radio is on
      - profiles:    profiles BlueZ exposes right now
      - audio_profile: an a2dp-sink exists (Bluetooth sound can work)
      - audio_service: name of the running sound server
      - hint:        one line the UI can show the user
    """
    info: Dict[str, Any] = {
        "available": bool(shutil.which("bluetoothctl")),
        "adapter": "",
        "powered": False,
        "profiles": [],
        "audio_profile": False,
        "audio_service": "",
        "hint": "",
    }
    if not info["available"]:
        info["hint"] = "bluetoothctl is missing - install the bluez package"
        return info

    for line in _btctl(["show"], timeout=5.0).splitlines():
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key == "name" and not info["adapter"]:
            info["adapter"] = value.strip()
        elif key == "powered":
            info["powered"] = value.strip().lower() == "yes"

    info["profiles"] = get_bluetooth_profiles()
    info["audio_profile"] = "a2dp-sink" in info["profiles"]
    info["audio_service"] = get_audio_service()

    if not info["powered"]:
        info["hint"] = "Bluetooth is off"
    elif not info["audio_profile"]:
        if info["audio_service"]:
            info["hint"] = ("No A2DP sink profile - %s cannot drive Bluetooth sound"
                            % info["audio_service"])
        else:
            info["hint"] = ("No A2DP sink profile - start PipeWire/WirePlumber "
                            "for Bluetooth sound")
    return info


def get_bluetooth_status() -> Optional[Dict[str, Any]]:
    """Detect Bluetooth adapter and connection status.

    Returns None if no BT hardware found, otherwise:
      - powered: bool
      - connected: bool
      - device_mac: str (connected device MAC, empty if none)
      - device_name: str (connected device name, empty if none)
    """
    btcli = shutil.which("bluetoothctl")
    if not btcli:
        return None

    result: Dict[str, Any] = {"powered": False, "connected": False,
                               "device_mac": "", "device_name": ""}

    try:
        # Check adapter power
        out = subprocess.run(
            [btcli, "show"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=3.0, check=False
        ).stdout.decode("utf-8", "ignore")
        for line in out.splitlines():
            if "Powered:" in line:
                result["powered"] = "yes" in line.lower()

        if not result["powered"]:
            return result

        # Check connected devices
        out = subprocess.run(
            [btcli, "devices", "Connected"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=3.0, check=False
        ).stdout.decode("utf-8", "ignore").strip()
        if out:
            result["connected"] = True
            # The device line is "Device <MAC> <name>": the MAC is the second
            # field. It used to be read from the last field, which is the
            # *name* - so the connected device was never identified and the
            # UI could neither show it nor disconnect it.
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0].lower() == "device":
                    mac = parts[1]
                    result["device_mac"] = mac
                    name = " ".join(parts[2:]) if len(parts) > 2 else ""
                    if not name:
                        info = _bt_device_info(mac)
                        name = info.get("name", "")
                    result["device_name"] = name
                    break
    except Exception:
        pass

    return result


def get_bluetooth_devices() -> List[Dict[str, Any]]:
    """List known Bluetooth devices with their pairing state."""
    btcli = shutil.which("bluetoothctl")
    if not btcli:
        return []
    devices: List[Dict[str, Any]] = []
    try:
        out = subprocess.run(
            [btcli, "devices"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=3.0, check=False
        ).stdout.decode("utf-8", "ignore").strip()

        paired: set = set()
        pout = subprocess.run(
            [btcli, "devices", "Paired"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=3.0, check=False
        ).stdout.decode("utf-8", "ignore")
        for line in pout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].lower() == "device":
                paired.add(parts[1].upper())

        for line in out.splitlines():
            parts = line.split(None, 2)
            if len(parts) >= 2 and parts[0] == "Device":
                mac = parts[1]
                name = parts[2].strip() if len(parts) > 2 else ""
                devices.append({"mac": mac, "name": name or "Unknown device",
                                "paired": mac.upper() in paired})
        return devices
    except Exception:
        return []


def toggle_bluetooth(enabled: bool) -> bool:
    """Power on/off the Bluetooth adapter."""
    btcli = shutil.which("bluetoothctl")
    if not btcli:
        return False
    try:
        state = "on" if enabled else "off"
        subprocess.run(
            [btcli, "power", state],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=3.0, check=False
        )
        return True
    except Exception:
        return False


def pair_bluetooth(mac: str) -> Tuple[bool, str]:
    """Pair and trust a device.

    A headset that was never paired cannot be connected at all, and the
    pairing agent has to live in the same bluetoothctl process as the
    "pair" command - otherwise the PIN exchange never happens.
    """
    if not shutil.which("bluetoothctl"):
        return False, "bluetoothctl is not installed (package: bluez)"
    mac = (mac or "").strip().upper()
    if not mac:
        return False, "No device selected"

    out = _btctl_session(
        ["agent on", "default-agent", "scan on", "pair " + mac],
        timeout=BT_PAIR_TIMEOUT,
        terminators=["pairing successful", "successfully paired", "already exists",
                     "failed to pair", "authentication failed", "authentication rejected"]
    )
    low = out.lower()
    if "pairing successful" in low or "successfully paired" in low or "already exists" in low:
        _btctl(["trust", mac], timeout=5.0)
        return True, "Paired"
    return False, bluetooth_error_message(out or "Pairing failed")


def connect_bluetooth(mac: str) -> Tuple[bool, str]:
    """Connect to a Bluetooth device. Returns (success, message).

    The device is paired and trusted first, the verdict is read from the
    asynchronous output, and the connection is confirmed against the device
    list. The message is one actionable sentence - never a raw D-Bus error.
    """
    if not shutil.which("bluetoothctl"):
        return False, "bluetoothctl is not installed (package: bluez)"
    mac = (mac or "").strip().upper()
    if not mac:
        return False, "No device selected"

    info = _bt_device_info(mac)
    if not info.get("paired"):
        paired, message = pair_bluetooth(mac)
        if not paired:
            return False, message
    if not info.get("trusted"):
        _btctl(["trust", mac], timeout=5.0)

    out = _btctl_session(
        ["connect " + mac],
        timeout=BT_CONNECT_TIMEOUT,
        terminators=["connection successful", "failed to connect",
                     "already connected", "not available"]
    )
    low = out.lower()
    if "already connected" in low:
        return True, "Already connected"
    if ("failed to connect" in low or "not available" in low
            or "authentication" in low or "not ready" in low):
        return False, bluetooth_error_message(out)
    if _bt_wait_connected(mac, True, BT_VERIFY_TIMEOUT):
        return True, "Connected"
    # No error, no connection: the headset accepted the request but never
    # brought the profile up (usually a second device already holds it).
    low = out.lower()
    if "connection successful" in low or "already connected" in low:
        return False, "The headset did not connect (profile busy)"
    return False, bluetooth_error_message(out or "The headset did not connect")


def disconnect_bluetooth(mac: str) -> Tuple[bool, str]:
    """Disconnect a Bluetooth device."""
    if not shutil.which("bluetoothctl"):
        return False, "bluetoothctl is not installed (package: bluez)"
    mac = (mac or "").strip().upper()
    if not mac:
        return False, "No device selected"

    out = _btctl_session(
        ["disconnect " + mac],
        timeout=10.0,
        terminators=["disconnected", "failed to disconnect", "not connected"]
    )
    if _bt_wait_connected(mac, False, 6.0):
        return True, "Disconnected"
    return False, bluetooth_error_message(out or "Still connected")


def scan_bluetooth_devices() -> List[Dict[str, Any]]:
    """Scan for nearby Bluetooth devices (quick 5-second scan)."""
    btcli = shutil.which("bluetoothctl")
    if not btcli:
        return []
    try:
        # Start scan, wait, stop scan
        subprocess.run(
            [btcli, "scan", "on"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=1.0, check=False
        )
        time.sleep(5)
        subprocess.run(
            [btcli, "scan", "off"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=1.0, check=False
        )
        # Get devices found
        return get_bluetooth_devices()
    except Exception:
        return []
