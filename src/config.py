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
}


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
                if key in DEFAULT_SETTINGS and isinstance(value, type(DEFAULT_SETTINGS[key])):
                    settings[key] = value
    except Exception:
        pass
    settings["scaling"] = normalise_scaling(settings.get("scaling"))
    if settings.get("theme") not in ("indigo", "violet", "amber", "mint"):
        settings["theme"] = "indigo"
    if settings.get("view_mode") not in ("grid", "list"):
        settings["view_mode"] = "grid"
    settings["volume"] = max(0, min(100, int(settings.get("volume", 85))))
    return settings


def save_settings(settings: Dict[str, Any]) -> None:
    """Atomically persists settings, keeping only known keys."""
    path = settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {k: settings.get(k, v) for k, v in DEFAULT_SETTINGS.items()}
    payload["version"] = SETTINGS_VERSION
    payload["scaling"] = normalise_scaling(payload.get("scaling"))
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
