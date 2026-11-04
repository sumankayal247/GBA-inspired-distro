#!/usr/bin/env python3
"""
mGBA Handler Adapter for GBA OS.

Implements Rules H1-H9, SV1-SV5 from CONTRACT.md.
Wraps mgba-sdl as an isolated handler process with input monitoring, save
routing and auto-configuration.

Design notes
------------
* We never write outside SYS_SAVE_DIR / SYS_DATA_DIR / SYS_CONFIG_DIR (H5).
* Saves are mirrored before a session starts, so a hard power loss during play
  leaves a last-known-good copy behind (SV3, SV4).
* The escape hotkey is read from the shared input profile, not hard-coded,
  and reserved exclusivity is left to mGBA where possible (I3).
"""

import os
import sys
import signal
import shutil
import subprocess
import struct
import select
import glob
import json
import queue
import threading
import time
from typing import Optional

# Ensure local src directory is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import (
    setup_logging, load_keybindings, load_display_config, DEFAULT_KEYBINDINGS,
    compute_content_id,
)
import config as settings_store
from config import (
    display_mode_size,
    display_session,
    other_drm_devices,
    prepare_child_environment,
)
from game_data import CheatStore, SaveStateStore, valid_game_id
from gba_ingame import (
    InGameBackend, InGameSession, ComboRouter,
    button_from_joy_button, button_from_axis, buttons_from_hat, BASE_FPS,
)

logger = setup_logging("gba.adapter")

# Linux input constants (avoid a hard dependency on python-evdev)
EV_SYN = 0x00
EV_KEY = 0x01
EV_ABS = 0x03
EVIOCGNAME_256 = 0x81004506       # read a /dev/input device's name
VIRTUAL_INPUT_NAME = "gba-os-hotkeys"
KEY_ESC = 1
ABS_HAT0X, ABS_HAT0Y = 16, 17

# Gamepad combos that quit a game: SELECT+START, or a Guide/Home button.
GAMEPAD_START_BUTTONS = {7, 9}
GAMEPAD_SELECT_BUTTONS = {6, 8}
GAMEPAD_GUIDE_BUTTONS = {10, 12, 16, 17}

BACKUP_SUFFIX = ".bak"

# --- input translation ------------------------------------------------------
# SDL key codes (the shared keybinding profile, I3) -> Linux evdev key codes.
# The input monitor reads raw /dev/input events, so shortcut detection has to
# translate the profile instead of hard-coding "Up + Left" or "L + R".
_SDL_TO_EVDEV = {
    1073741906: 103,  # Up
    1073741905: 108,  # Down
    1073741904: 105,  # Left
    1073741903: 106,  # Right
    8: 14,            # Backspace (SELECT)
    13: 28,           # Return (START)
    9: 15,            # Tab
    32: 57,           # Space
    27: 1,            # Escape
}
_EVDEV_LETTERS = {
    "q": 16, "w": 17, "e": 18, "r": 19, "t": 20, "y": 21, "u": 22,
    "i": 23, "o": 24, "p": 25, "a": 30, "s": 31, "d": 32, "f": 33,
    "g": 34, "h": 35, "j": 36, "k": 37, "l": 38, "z": 44, "x": 45,
    "c": 46, "v": 47, "b": 48, "n": 49, "m": 50,
}

# evdev gamepad button codes -> SDL-style joystick button numbers.
_EVDEV_GAMEPAD_BUTTONS = {
    304: 0,  # BTN_A
    305: 1,  # BTN_B
    307: 2,  # BTN_X
    306: 3,  # BTN_Y
    310: 4,  # BTN_TL (L)
    311: 5,  # BTN_TR (R)
    314: 6,  # BTN_SELECT
    315: 7,  # BTN_START
}

# mGBA SDL hotkeys (see src/platform/sdl/sdl-events.c): F1..F9 load a native
# state slot, Shift+F1..F9 save it, F12 takes a native screenshot.
KEY_LEFTSHIFT = 42
KEY_F1 = 59
KEY_F12 = 88
INJECTABLE_KEYS = tuple(range(KEY_F1, KEY_F1 + 9)) + (KEY_F12, KEY_LEFTSHIFT)

# X11 key names for "release the buttons we just consumed".  Under X11 a
# key-up from a *different* uinput device does not clear the physical key's
# state, so xdotool is used in addition to uinput when a display is present.
_EVDEV_TO_X11 = {
    103: "Up", 108: "Down", 105: "Left", 106: "Right",
    14: "BackSpace", 28: "Return", 15: "Tab", 57: "space", 1: "Escape",
}
for _letter, _code in _EVDEV_LETTERS.items():
    _EVDEV_TO_X11[_code] = _letter


def evdev_button_map(bindings: dict):
    """Returns {evdev code: GBA button id} for every bound keyboard key."""
    result = {}
    for button_id, sdl_code in (bindings or {}).items():
        code = _SDL_TO_EVDEV.get(sdl_code)
        if code is None and isinstance(sdl_code, int):
            # SDL uses ASCII for plain letters (a=97 ... z=122).
            letter = chr(sdl_code).lower() if 0 < sdl_code < 128 else ""
            code = _EVDEV_LETTERS.get(letter)
        if code is not None:
            result[code] = button_id
    return result

MIGBA_CONFIG_TEMPLATE = """[ports.sdl]
fullscreen={fullscreen}
lockAspectRatio={aspect}
lockIntegerScaling={integer}
rewindEnable=0

[input.SDLK]
keyUp={keyUp}
keyDown={keyDown}
keyLeft={keyLeft}
keyRight={keyRight}
keyA={keyA}
keyB={keyB}
keyL={keyL}
keyR={keyR}
keyStart={keyStart}
keySelect={keySelect}
"""


def find_mgba_binary() -> str:
    candidates = [
        "/usr/games/mgba",
        "/usr/local/bin/mgba",
        "/usr/bin/mgba",
    ]
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    # Last resort: whatever is on PATH (helpful on non-Debian layouts).
    found = shutil.which("mgba") or shutil.which("mgba-sdl")
    if found:
        return found
    raise FileNotFoundError("mGBA binary not found in standard paths.")


def _write_mgba_config(config_file: str, windowed: bool = False) -> None:
    """Writes the appliance mGBA configuration from the shared input profile."""
    bindings = load_keybindings()
    display = load_display_config()
    integer = display.get("scaling", "integer") == "integer"
    content = MIGBA_CONFIG_TEMPLATE.format(
        fullscreen="0" if windowed else "1",
        aspect="1",
        integer="1" if integer else "0",
        keyUp=bindings.get("keyUp", DEFAULT_KEYBINDINGS["keyUp"]),
        keyDown=bindings.get("keyDown", DEFAULT_KEYBINDINGS["keyDown"]),
        keyLeft=bindings.get("keyLeft", DEFAULT_KEYBINDINGS["keyLeft"]),
        keyRight=bindings.get("keyRight", DEFAULT_KEYBINDINGS["keyRight"]),
        keyA=bindings.get("keyA", DEFAULT_KEYBINDINGS["keyA"]),
        keyB=bindings.get("keyB", DEFAULT_KEYBINDINGS["keyB"]),
        keyL=bindings.get("keyL", DEFAULT_KEYBINDINGS["keyL"]),
        keyR=bindings.get("keyR", DEFAULT_KEYBINDINGS["keyR"]),
        keyStart=bindings.get("keyStart", DEFAULT_KEYBINDINGS["keyStart"]),
        keySelect=bindings.get("keySelect", DEFAULT_KEYBINDINGS["keySelect"]),
    )
    os.makedirs(os.path.dirname(config_file) or ".", exist_ok=True)
    tmp = config_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, config_file)


def mgba_config_path(config_dir: str = None) -> str:
    """
    Where mGBA will actually look for its config.

    mGBA resolves `$XDG_CONFIG_HOME/mgba/config.ini`. We point XDG_CONFIG_HOME
    at SYS_CONFIG_DIR so the handler stays inside its sandbox (H5) and can be
    certain the file it writes is the file mGBA reads.
    """
    if config_dir:
        return os.path.join(config_dir, "mgba", "config.ini")
    return os.path.join(os.path.expanduser("~"), ".config", "mgba", "config.ini")


def ensure_mgba_config(config_dir: str = None) -> None:
    """
    Ensures mGBA has an appliance config with the shared key bindings (I3).

    When `config_dir` (SYS_CONFIG_DIR) is given the config stays inside the
    handler's own directory (H5); otherwise the user profile is used.
    """
    if config_dir:
        os.environ["XDG_CONFIG_HOME"] = config_dir
    config_file = mgba_config_path(config_dir)

    try:
        _write_mgba_config(config_file, windowed=os.environ.get("GBA_WINDOWED") == "1")
        # Keep a copy at the historically documented location so existing
        # installs, docs and tests that read SYS_CONFIG_DIR/config.ini agree
        # with what mGBA actually loads.
        if config_dir:
            legacy = os.path.join(config_dir, "config.ini")
            if os.path.abspath(legacy) != os.path.abspath(config_file):
                _write_mgba_config(legacy, windowed=os.environ.get("GBA_WINDOWED") == "1")
        logger.info("mGBA config prepared at %s", config_file)
    except Exception as e:
        logger.warning("Could not initialize mGBA config: %s", e)


def sync_directory(dir_path: str) -> None:
    """Performs fsync on save files and their containing directory (Rule SV2)."""
    try:
        dir_fd = os.open(dir_path, os.O_RDONLY)
        try:
            for item in os.listdir(dir_path):
                f_path = os.path.join(dir_path, item)
                if os.path.isfile(f_path):
                    try:
                        fd = os.open(f_path, os.O_RDONLY)
                        os.fsync(fd)
                        os.close(fd)
                    except Exception:
                        pass
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception as e:
        logger.warning("Directory fsync warning: %s", e)


def backup_saves(save_dir: str) -> int:
    """
    Mirrors current save files to `<name>.bak` before play starts (SV3, SV4).

    The copy is skipped when the existing backup is already fresh, so repeated
    short sessions do not churn the disk.
    """
    copied = 0
    try:
        for name in os.listdir(save_dir):
            if not name.endswith((".sav", ".srm")) or name.endswith(BACKUP_SUFFIX):
                continue
            src = os.path.join(save_dir, name)
            dst = src + BACKUP_SUFFIX
            try:
                if os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(src):
                    continue
                tmp = dst + ".tmp"
                shutil.copyfile(src, tmp)
                with open(tmp, "rb") as f:
                    os.fsync(f.fileno())
                os.replace(tmp, dst)
                copied += 1
            except Exception as exc:
                logger.warning("Could not back up %s: %s", src, exc)
    except Exception:
        pass
    if copied:
        logger.info("Backed up %d save file(s) for crash recovery", copied)
    return copied


def restore_backup_if_needed(save_dir: str) -> bool:
    """
    Restores `<name>.bak` when the live save is missing or empty (torn write).

    Returns True when a restore happened.
    """
    restored = False
    try:
        for name in os.listdir(save_dir):
            if not name.endswith(BACKUP_SUFFIX):
                continue
            backup = os.path.join(save_dir, name)
            live = backup[: -len(BACKUP_SUFFIX)]
            try:
                backup_ok = os.path.getsize(backup) > 0
                live_ok = os.path.exists(live) and os.path.getsize(live) > 0
                if backup_ok and not live_ok:
                    shutil.copyfile(backup, live)
                    logger.warning("Restored save %s from backup", live)
                    restored = True
            except Exception as exc:
                logger.warning("Backup restore failed for %s: %s", live, exc)
    except Exception:
        pass
    return restored


class InputMonitor(threading.Thread):
    """
    Background watcher for the system exit combo and in-game action combos.

    It reads raw `/dev/input` events so it works without evdev bindings and
    without stealing input from mGBA.  Combo detection is edge-triggered by
    `ComboRouter`: a shortcut is queued once when its last button is pressed,
    never again while the buttons stay held.  The buttons of a consumed combo
    are reported through `on_consumed` so the handler can neutralise them in
    the emulator instead of letting them leak into gameplay.
    """

    def __init__(self, target_proc: subprocess.Popen, escape_keys=None,
                 shortcuts=None, action_queue=None, keybindings=None,
                 on_consumed=None, alive_check=None, on_exit_requested=None):
        super().__init__(daemon=True, name="gba-input-monitor")
        self.proc = target_proc
        self.running = True
        self.escape_keys = set(escape_keys or {KEY_ESC})
        self.shortcuts = shortcuts
        self.action_queue = action_queue
        self.on_consumed = on_consumed
        # During a display handoff the emulator process is stopped on purpose
        # while the overlay is open. The watcher must keep running through
        # that window (otherwise the combo that closes the overlay is never
        # seen), so liveness comes from the session, not from the child.
        self.alive_check = alive_check
        self.on_exit_requested = on_exit_requested
        self._evdev_map = evdev_button_map(keybindings or load_keybindings())

    def stop(self):
        self.running = False
        # Before a relaunch, make sure the old watcher has stopped reading
        # /dev/input and cannot terminate the freshly spawned child.
        try:
            if self.is_alive() and threading.current_thread() is not self:
                self.join(timeout=0.6)
        except Exception:
            pass

    def alive(self) -> bool:
        """True while the game session still owns input (overlay included)."""
        if not self.running:
            return False
        if self.alive_check is not None:
            try:
                return bool(self.alive_check())
            except Exception:
                return False
        try:
            return self.proc.poll() is None
        except Exception:
            return False

    def _trigger(self, reason: str) -> None:
        logger.info("%s - sending SIGTERM to emulator", reason)
        if self.on_exit_requested is not None:
            try:
                self.on_exit_requested()
            except Exception:
                pass
        try:
            self.proc.terminate()
        except Exception:
            pass

    # -- combo helpers -------------------------------------------------
    def _press(self, router, button) -> None:
        if router is None or not button:
            return
        for action in router.press(button):
            if self.action_queue is not None:
                self.action_queue.put(action)
        if self.on_consumed is not None:
            self.on_consumed(router.consumed_buttons())

    def _release(self, router, button) -> None:
        if router is None or not button:
            return
        router.release(button)
        if self.on_consumed is not None:
            self.on_consumed(router.consumed_buttons())

    @staticmethod
    def _hat_button(code, value):
        if code == ABS_HAT0X:
            return "keyLeft" if value < 0 else ("keyRight" if value > 0 else None)
        if code == ABS_HAT0Y:
            return "keyUp" if value < 0 else ("keyDown" if value > 0 else None)
        return None

    def run(self):
        fds = {}
        try:
            for dev in sorted(os.listdir("/dev/input")) if os.path.isdir("/dev/input") else []:
                if not (dev.startswith("event") or dev.startswith("js")):
                    continue
                path = os.path.join("/dev/input", dev)
                try:
                    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                    # Never watch our own uinput device: the synthetic key-ups
                    # used to neutralise a consumed combo would otherwise look
                    # like a real release and could re-arm a still-held combo.
                    if dev.startswith("event"):
                        import fcntl
                        name_buf = bytearray(256)
                        try:
                            fcntl.ioctl(fd, EVIOCGNAME_256, name_buf, True)
                            name = bytes(name_buf).split(b"\x00", 1)[0].decode(
                                "utf-8", "ignore")
                        except Exception:
                            name = ""
                        if VIRTUAL_INPUT_NAME in name:
                            os.close(fd)
                            continue
                    fds[fd] = dev.startswith("js")
                except Exception:
                    pass
            if not fds:
                return

            router = ComboRouter(self.shortcuts) if self.shortcuts else None
            held = set()                 # legacy gamepad exit combo
            axis_buttons = {}            # (fd, axis) -> currently held button

            def axis_update(fd, axis, value, normalise=False):
                key = (fd, axis)
                old = axis_buttons.get(key)
                new = None
                if normalise:
                    new = button_from_axis(axis, value / 32767.0)
                else:
                    new = self._hat_button(axis, value)
                if new == old:
                    return
                if old:
                    self._release(router, old)
                    held.discard(old)
                if new:
                    self._press(router, new)
                    held.add(new)
                axis_buttons[key] = new

            while self.alive():
                try:
                    readable, _, _ = select.select(list(fds.keys()), [], [], 0.2)
                except Exception:
                    break
                for fd in readable:
                    is_js = fds[fd]
                    try:
                        if is_js:
                            raw = os.read(fd, 8)
                            if len(raw) != 8:
                                continue
                            _t_ms, value, ev_type, number = struct.unpack("IhBB", raw)
                            if ev_type & 0x02:       # JS_EVENT_AXIS
                                axis_update(fd, number, value, normalise=True)
                                continue
                            if not ev_type & 0x01:   # JS_EVENT_BUTTON
                                continue
                            button = button_from_joy_button(number)
                            if value:
                                held.add(number)
                                combo = ((held & GAMEPAD_SELECT_BUTTONS) and
                                         (held & GAMEPAD_START_BUTTONS))
                                if number in GAMEPAD_GUIDE_BUTTONS or combo:
                                    self._trigger("Gamepad exit combo")
                                    return
                                self._press(router, button)
                            else:
                                held.discard(number)
                                self._release(router, button)
                        else:
                            raw = os.read(fd, 24)
                            if len(raw) != 24:
                                continue
                            _s, _us, ev_t, code, value = struct.unpack("qqHHi", raw)
                            if ev_t == EV_KEY:
                                if value == 1 and code in self.escape_keys:
                                    self._trigger("Exit key detected")
                                    return
                                button = self._evdev_map.get(code)
                                if button is None:
                                    button = button_from_joy_button(
                                        _EVDEV_GAMEPAD_BUTTONS.get(code, -1))
                                if button:
                                    if value == 1:      # ignore auto-repeat (2)
                                        self._press(router, button)
                                    elif value == 0:
                                        self._release(router, button)
                            elif ev_t == EV_ABS and code in (ABS_HAT0X, ABS_HAT0Y):
                                axis_update(fd, code, value)
                    except BlockingIOError:
                        continue
                    except Exception:
                        continue
        finally:
            for fd in list(fds.keys()):
                try:
                    os.close(fd)
                except Exception:
                    pass


class HotkeyInjector:
    """Sends mGBA's *native* hotkeys (F1-F9, Shift+F1-F9, F12) to the emulator.

    `/dev/uinput` is the primary transport: it works under X11 as well as
    KMS/DRM without a window manager.  On X11-only setups `xdotool` is a
    fallback.  Both are optional: if neither exists the emulator still runs
    and every in-game operation fails gracefully instead of crashing.
    """

    UI_SET_EVBIT = 0x40045564
    UI_SET_KEYBIT = 0x40045565
    UI_DEV_CREATE = 0x5501
    UI_DEV_DESTROY = 0x5502

    def __init__(self, device: str = "/dev/uinput"):
        self.fd = None
        self._xdotool = None
        fd = None
        try:
            import fcntl
            fd = os.open(device, os.O_WRONLY | os.O_NONBLOCK)
            fcntl.ioctl(fd, self.UI_SET_EVBIT, EV_KEY)
            for code in INJECTABLE_KEYS:
                fcntl.ioctl(fd, self.UI_SET_KEYBIT, code)
            setup = bytearray(1116)      # struct uinput_user_dev
            name = VIRTUAL_INPUT_NAME.encode()
            setup[:len(name)] = name
            os.write(fd, bytes(setup))
            fcntl.ioctl(fd, self.UI_DEV_CREATE)
            time.sleep(0.05)            # let udev create /dev/input/eventN
            self.fd = fd
        except Exception:
            if fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    pass
            self.fd = None
        # xdotool is useful even when uinput works: X11 tracks key state per
        # input device, so a uinput key-up alone cannot neutralise a physical
        # key that mGBA saw through X.
        if os.environ.get("DISPLAY"):
            self._xdotool = shutil.which("xdotool")

    def available(self) -> bool:
        return self.fd is not None or self._xdotool is not None

    def key(self, code: int, value: int) -> bool:
        """Sends one key press/release (used to neutralise consumed combos)."""
        if self.fd is None:
            return False
        try:
            os.write(self.fd, struct.pack("qqHHi", 0, 0, EV_KEY, int(code), int(value)))
            os.write(self.fd, struct.pack("qqHHi", 0, 0, EV_SYN, 0, 0))
            return True
        except Exception:
            return False

    @staticmethod
    def _x11_names(codes):
        names = []
        for code in codes:
            if KEY_F1 <= code < KEY_F1 + 9:
                names.append(f"F{code - KEY_F1 + 1}")
            elif code == KEY_F12:
                names.append("F12")
            elif code == KEY_LEFTSHIFT:
                names.append("shift")
            else:
                name = _EVDEV_TO_X11.get(code)
                if name:
                    names.append(name)
        return names

    def tap(self, codes, hold: float = 0.04) -> bool:
        codes = list(codes)
        if self.fd is not None:
            ok = True
            for code in codes:
                ok = self.key(code, 1) and ok
            time.sleep(hold)
            for code in reversed(codes):
                ok = self.key(code, 0) and ok
            if ok:
                return True
        names = self._x11_names(codes)
        if self._xdotool and names:
            try:
                subprocess.run([self._xdotool, "key", "--clearmodifiers",
                                "+".join(names)], timeout=3.0, check=False,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
                return True
            except Exception:
                return False
        return False

    def release(self, codes) -> bool:
        """Neutralise a consumed shortcut so it cannot also reach gameplay."""
        codes = list(codes)
        ok = False
        for code in codes:
            ok = self.key(code, 0) or ok
        names = self._x11_names(codes)
        if self._xdotool and names:
            try:
                subprocess.run([self._xdotool, "keyup"] + names, timeout=2.0,
                               check=False, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
                ok = True
            except Exception:
                pass
        return ok

    def close(self) -> None:
        if self.fd is None:
            return
        try:
            import fcntl
            fcntl.ioctl(self.fd, self.UI_DEV_DESTROY)
            os.close(self.fd)
        except Exception:
            pass
        self.fd = None


class MgbaNativeController(InGameBackend):
    """mGBA-native operations for cheats, nine states and fast-forward.

    The real emulator state is always an mGBA `.ssN` file produced by mGBA's
    own Shift+F<N> hotkey.  Screenshots are separate PNGs produced by mGBA's
    F12 hotkey; they are only ever visual thumbnails.  Speed changes use
    mGBA's `fpsTarget` config (60/120/180), applied with a state-preserving
    in-session reload.  Nothing here writes outside the handler's save dir.

    Display handoff
    ---------------
    A Pygame overlay cannot take over a KMS/DRM or SDL display that a
    suspended (SIGSTOP) mGBA still owns, which is why overlays used to show a
    black screen with a frozen game.  ``suspend_for_overlay`` therefore:

    1. captures a gameplay screenshot (F12) and a temporary native save state
       (Shift+F9) **while mGBA is still running**,
    2. asks the handler's ``handoff`` (the session runner) to stop mGBA
       cleanly so the display is released,
    3. lets the overlay renderer open its own window.

    ``resume_from_handoff`` restarts mGBA with ``-t <captured state>`` and the
    current ``fpsTarget``, so gameplay continues on the exact frame it left —
    optionally at a new speed, and with the freshly written cheat file.

    Hotkeys are never sent to a stopped process: every capture above happens
    before the stop, and everything afterwards uses files plus a relaunch.
    """

    supports_runtime_speed = True
    supports_display_handoff = True

    #: Native slot used only for the private handoff/speed captures. It lives
    #: in the runtime folder, never in the user's nine archived slots.
    TRANSIENT_SLOT = 9

    def __init__(self, proc, content_path: str, save_dir: str,
                 injector=None, fps_target: float = BASE_FPS,
                 handoff=None, capture_timeout: float = 1.6):
        self.proc = proc
        self.content_path = content_path
        self.save_dir = save_dir
        self.runtime_dir = os.path.join(save_dir, "runtime")
        self.capture_dir = os.path.join(self.runtime_dir, "captures")
        os.makedirs(self.capture_dir, exist_ok=True)
        self.base = os.path.splitext(os.path.basename(content_path))[0] or "game"
        self.injector = injector if injector is not None else HotkeyInjector()
        self.fps_target = float(fps_target)
        self._paused = False
        self.last_screenshot = None
        self._button_to_evdev = {button: code for code, button in
                                 evdev_button_map(load_keybindings()).items()}
        # Set by the session runner (the only owner of the child process).
        self.handoff = handoff
        self.capture_timeout = float(capture_timeout)
        self._suspended = False
        #: Native state that gameplay will be restarted from.
        self.resume_state: Optional[str] = None
        self.last_error = ""

    # -- process control -----------------------------------------------
    def _signal(self, sig) -> bool:
        try:
            if self.proc is not None and self.proc.poll() is None:
                self.proc.send_signal(sig)
                return True
        except Exception:
            pass
        return False

    def pause(self) -> bool:
        """Legacy SIGSTOP pause (only used when no process handoff exists)."""
        self.last_screenshot = self.capture_screenshot(0, timeout=0.6)
        self._paused = self._signal(signal.SIGSTOP)
        return self._paused

    def resume(self) -> bool:
        if not self._paused:
            return False
        self._paused = False
        return self._signal(signal.SIGCONT)

    # -- display handoff ------------------------------------------------
    @property
    def suspended(self) -> bool:
        return self._suspended

    def suspend_for_overlay(self, kind: str = "") -> bool:
        """Captures the game, then releases the display for an overlay."""
        if self._suspended:
            return True
        if self.handoff is None:
            # No process owner to restart us: the only safe thing left is the
            # old pause, and the renderer resumes on failure.
            logger.info("No display handoff available; falling back to pause")
            return self.pause()
        logger.info("Display handoff for overlay '%s': capturing frame + state", kind)
        # Step 1: both captures happen while mGBA is still executing.
        self.last_screenshot = self.capture_screenshot(0, timeout=self.capture_timeout)
        state = self.capture_runtime_state("handoff")
        if not state:
            self.last_error = "could not capture a native save state"
            logger.warning("Display handoff aborted: %s (game keeps running)",
                           self.last_error)
            return False
        # Step 2: stop mGBA so it releases SDL/KMS/DRM, keeping the state.
        self.resume_state = state
        if not self.handoff.stop_emulator():
            self.last_error = "mGBA did not stop"
            logger.error("Display handoff failed: %s", self.last_error)
            self.resume_state = None
            self._resume_if_needed()
            return False
        self._paused = False
        self._suspended = True
        logger.info("Gameplay suspended at %s; overlay owns the display now",
                    os.path.basename(state))
        return True

    def resume_from_handoff(self) -> bool:
        """Restarts mGBA from the captured state (new speed/cheats applied)."""
        if self.handoff is None:
            return self.resume()
        if not self._suspended:
            return False
        self._suspended = False
        state = self.resume_state
        ok = self.handoff.start_emulator(state, self.fps_target)
        if not ok:
            self.last_error = "mGBA could not be restarted"
            logger.error("Resume failed (%s); retrying without the state",
                         self.last_error)
            # A failed resume must never leave the user without a game.
            ok = self.handoff.start_emulator(None, self.fps_target)
        else:
            logger.info("Gameplay resumed from %s at fpsTarget=%g",
                        os.path.basename(state or ""), self.fps_target)
        self.resume_state = None
        return ok

    def _resume_if_needed(self) -> None:
        """Safety net for a half-applied handoff."""
        if not self._suspended:
            return
        try:
            self.resume_from_handoff()
        except Exception:
            pass

    def overlay_state(self):
        """The state captured before the handoff (None when not suspended)."""
        return self.resume_state

    def overlay_screenshot(self):
        return self.last_screenshot

    def set_speed(self, multiplier: int, fps_target: float) -> bool:
        self.fps_target = float(fps_target)
        # With a handoff the overlay-driven restart picks the new fpsTarget up
        # (mGBA SDL reads it at core start), so no separate relaunch is
        # requested. Without one, ask the runner for the legacy reload.
        if self.handoff is not None:
            return False
        return self.injector.available()

    def reload_cheats(self) -> bool:
        """Cheat toggles are applied by the handoff restart reading `-c`."""
        return self.handoff is not None

    def suppress_combo(self, buttons) -> None:
        """Best effort: release consumed keys so they do not reach gameplay."""
        codes = [self._button_to_evdev[button] for button in buttons or ()
                 if button in self._button_to_evdev]
        if not codes:
            return
        release = getattr(self.injector, "release", None)
        if callable(release):
            release(codes)
        else:                              # duck-typed test injectors
            for code in codes:
                self.injector.key(code, 0)

    # -- native files ---------------------------------------------------
    def native_state_path(self, slot: int) -> str:
        return os.path.join(self.runtime_dir, f"{self.base}.ss{int(slot)}")

    @staticmethod
    def _wait_changed(path: str, before: float, timeout: float = 1.6) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if os.path.isfile(path) and os.path.getsize(path) > 0 and \
                        os.path.getmtime(path) > before:
                    return True
            except OSError:
                pass
            time.sleep(0.05)
        return False

    @staticmethod
    def _restore(path: str, backup: str, had_previous: bool) -> None:
        try:
            if had_previous and os.path.isfile(backup):
                shutil.copyfile(backup, path)
            elif not had_previous and os.path.isfile(path):
                os.remove(path)
        except Exception:
            pass
        try:
            if os.path.isfile(backup):
                os.remove(backup)
        except OSError:
            pass

    def save_state(self, slot: int):
        """Asks mGBA for a native state; returns a staged copy or None.

        Requires a *running* emulator: mGBA only produces a state when its own
        Shift+F<N> action is processed. A stopped (or handed-off) emulator
        never sees the hotkey, so this refuses instead of hanging on a timeout.
        """
        try:
            slot = int(slot)
        except (TypeError, ValueError):
            return None
        if not 1 <= slot <= 9:
            return None
        if self._suspended or self._paused:
            logger.warning("Not capturing a native state while the emulator is stopped")
            return None
        path = self.native_state_path(slot)
        backup = path + ".previous"
        had_previous = os.path.isfile(path)
        try:
            if had_previous:
                shutil.copyfile(path, backup)
            before = os.path.getmtime(path) if os.path.isfile(path) else 0.0
        except OSError:
            return None
        ok = self.injector.tap([KEY_LEFTSHIFT, KEY_F1 + slot - 1])
        if not ok or not self._wait_changed(path, before):
            self._restore(path, backup, had_previous)
            return None
        staged = os.path.join(self.capture_dir, f"slot_{slot}.ss")
        try:
            shutil.copyfile(path, staged)
        except Exception:
            self._restore(path, backup, had_previous)
            return None
        self._restore(path, backup, had_previous)
        return staged

    def capture_screenshot(self, slot: int = 0, timeout: float = 1.6):
        """Asks mGBA for a native PNG frame; a visual thumbnail, never state."""
        if self._suspended or self._paused:
            # F12 to a stopped process would only burn the timeout.
            return self.last_screenshot
        pattern = os.path.join(self.runtime_dir, "*.png")
        before = set(glob.glob(pattern))
        if not self.injector.tap([KEY_F12]):
            return None
        deadline = time.monotonic() + max(0.1, float(timeout))
        while time.monotonic() < deadline:
            fresh = [p for p in set(glob.glob(pattern)) - before
                     if os.path.isfile(p) and os.path.getsize(p) > 0]
            if fresh:
                shot = max(fresh, key=os.path.getmtime)
                self._prune_screenshots(keep=shot)
                return shot
            time.sleep(0.05)
        return None

    def _prune_screenshots(self, keep: Optional[str] = None) -> None:
        """mGBA creates a new PNG per F12; keep only a few runtime frames."""
        files = sorted(glob.glob(os.path.join(self.runtime_dir, "*.png")),
                       key=os.path.getmtime, reverse=True)
        for stale in files[3:]:
            if stale == keep:
                continue
            try:
                os.remove(stale)
            except OSError:
                pass

    def prepare_resume_state(self, state_path: str) -> Optional[str]:
        """Copies a slot's state to a scratch file mGBA can be restarted from.

        The archived slot itself is never handed to mGBA, so a failed load can
        neither corrupt nor rewrite it; the copy is atomic.
        """
        if not state_path or not os.path.isfile(state_path):
            return None
        target = os.path.join(self.runtime_dir, "resume_state.ss")
        tmp = target + ".tmp"
        try:
            if os.path.getsize(state_path) <= 0:
                return None
            shutil.copyfile(state_path, tmp)
            os.replace(tmp, target)
            return target
        except Exception as exc:
            logger.warning("Could not stage %s for resume: %s", state_path, exc)
            for path in (tmp, target):
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass
            return None

    def load_state(self, slot: int, state_path: str) -> bool:
        """Restores the actual mGBA state.

        During a handoff the emulator is *not running*, so no F-key is sent:
        the state is staged and the restart at overlay close loads it with
        ``-t``. Only a live emulator without a handoff uses the native F<N>
        hotkey.
        """
        try:
            slot = int(slot)
        except (TypeError, ValueError):
            return False
        if not 1 <= slot <= 9 or not state_path or not os.path.isfile(state_path):
            return False
        if self._suspended or (self.handoff is not None and self.resume_state):
            staged = self.prepare_resume_state(state_path)
            if not staged:
                return False
            self.resume_state = staged
            logger.info("Slot %d staged as the resume state for the restart", slot)
            return True
        try:
            shutil.copyfile(state_path, self.native_state_path(slot))
        except Exception:
            return False
        return self.injector.tap([KEY_F1 + slot - 1])

    def capture_runtime_state(self, name: str = "handoff") -> Optional[str]:
        """Native state used only to resume at the exact frame after a reload.

        The capture must happen while mGBA is running (it is mGBA's own
        Shift+F<N> action); the returned file is a private runtime copy.
        """
        staged = self.save_state(self.TRANSIENT_SLOT)
        if not staged:
            return None
        target = os.path.join(self.runtime_dir, f"{name}.ss")
        try:
            shutil.copyfile(staged, target)
            return target
        except Exception:
            return None

    def capture_ephemeral_state(self):
        """Backwards-compatible alias for the fast-forward capture."""
        return self.capture_runtime_state("speed_state")


def build_mgba_command(mgba_bin: str, content_path: str, save_dir: str,
                       windowed: bool, display_scaling: str,
                       display_size: Optional[tuple] = None,
                       cheat_file: Optional[str] = None,
                       cheats_dir: Optional[str] = None,
                       savestate: Optional[str] = None,
                       fps_target: Optional[float] = None,
                       runtime_dir: Optional[str] = None) -> list:
    """
    Builds the mGBA argv with save routing (SV1), scaling (D2) and explicit
    video settings.

    The optional arguments are the mGBA-native hooks for the in-game
    features: `-c` loads this game's cheat file, `-t` resumes a native state
    after a speed reload, `fpsTarget` is mGBA's real speed setting, and the
    native state/screenshot slots are routed into a per-game runtime folder
    separate from the nine archived slots.  With no optional arguments the
    command is byte-for-byte the original one (SV1/D2 tests included).

    The video settings are passed as `-C` overrides rather than only through
    config.ini: mGBA reads its config from `$XDG_CONFIG_HOME/mgba/config.ini`,
    and if that file is not where we think it is, defaulting to a window is
    harmless while defaulting to the *wrong* fullscreen backend is what leaves
    the user staring at a black screen.

    For fullscreen the window is explicitly sized to the physical display
    (`-C width= -C height=`) when its mode is known. SDL2's X11 backend
    implements FULLSCREEN_DESKTOP purely as a window-manager request, and the
    appliance X session runs without a WM, so mGBA's default geometry (native
    240x160 / 160x144 scaled once) would remain a tiny centred window on a
    much larger screen. Screen-sized geometry fills the display with or
    without a WM, and mGBA's lockAspectRatio/lockIntegerScaling viewport then
    centres the largest correctly-proportioned image inside it (no crop, no
    stretch). When the mode cannot be determined, `-4` requests 4x native
    (960x640) instead of the useless 1x window.
    """
    native_dir = runtime_dir or save_dir
    cmd = [
        mgba_bin,
        "-C", f"savegamePath={save_dir}",
        "-C", f"savestatePath={native_dir}",
        "-C", f"screenshotPath={native_dir}",
        "-C", f"fullscreen={0 if windowed else 1}",
        # mgba-sdl enables rewind recording by default: a full core snapshot
        # every emulated frame plus a 600-second buffer, serialized on the
        # emulation thread. The appliance exposes no rewind control, so this
        # per-frame cost is pure overhead on modest hardware and a prime
        # suspect for stutter (most visible in the GB/GBC frame loop).
        "-C", "rewindEnable=0",
    ]
    if fps_target:
        cmd.extend(["-C", f"fpsTarget={float(fps_target):g}"])
    if cheats_dir:
        cmd.extend(["-C", f"cheatsPath={cheats_dir}"])
        if not cheat_file:
            cmd.extend(["-C", "cheatAutoload=0"])
    if cheat_file:
        cmd.extend(["-c", cheat_file])
    if savestate:
        cmd.extend(["-t", savestate])
    if windowed:
        cmd.extend(["-3"])
    else:
        cmd.append("-f")
        cmd.extend(["-C", "lockAspectRatio=1"])
        cmd.extend(["-C", f"lockIntegerScaling={1 if display_scaling == 'integer' else 0}"])
        if display_size and int(display_size[0]) > 0 and int(display_size[1]) > 0:
            cmd.extend(["-C", f"width={int(display_size[0])}"])
            cmd.extend(["-C", f"height={int(display_size[1])}"])
        else:
            cmd.append("-4")
    cmd.append(content_path)
    return cmd


def retry_strategy(session: str, ret: int, elapsed: float, windowed: bool,
                   env: dict) -> Optional[tuple]:
    """
    What to try when mGBA died immediately, or None to accept the result.

    A "black screen then back to the library" is almost always a video backend
    that could not present. The fallback depends on where we are running:

    * desktop (x11/wayland): retry once windowed with no driver pinning, so SDL
      negotiates for itself;
    * bare console: the first DRM card may drive no output, so try the others.

    Returns ("windowed", None) or ("card", "/dev/dri/cardN").
    """
    if ret == 0 or windowed or elapsed >= 5.0:
        return None
    if session in ("x11", "wayland"):
        return ("windowed", None)
    if session == "console":
        cards = other_drm_devices(env.get("SDL_VIDEO_KMSDRM_DEVICE", ""))
        if cards:
            return ("card", cards[0])
    return None


class MgbaSessionRunner:
    """Runs mGBA, streams its log, watches input and serves in-game actions.

    With no session this is exactly the old behaviour (one child, one input
    watcher).  With a session this runner is also the *process owner* for the
    display handoff: overlays ask it to stop mGBA (``stop_emulator``) so the
    overlay can own the screen, and to restart it from the captured native
    state (``start_emulator``) when the overlay closes.  A fast-forward speed
    change uses the same relaunch, because mGBA SDL reads `fpsTarget` at core
    start.

    The runner is the only place that spawns or terminates mGBA, so no code
    path can leave a stopped or orphaned emulator behind: every stop is paired
    with either a restart or the end of the session, and ``run()`` clears any
    remaining suspension in its ``finally`` block.
    """

    def __init__(self, cmd: list, env: dict, session=None, action_queue=None,
                 native=None, build_command=None, overlay_size=None,
                 theme_name: str = "indigo", windowed: bool = False,
                 stop_timeout: float = 4.0):
        self.cmd = cmd
        self.env = env
        self.session = session
        self.action_queue = action_queue
        self.native = native
        self.build_command = build_command
        self.overlay_size = overlay_size
        self.theme_name = theme_name
        self.windowed = windowed
        self.stop_timeout = float(stop_timeout)
        self.child = None
        self.monitor = None
        self.bindings = load_keybindings()
        self.exit_requested = False
        self.shutting_down = False
        self.spawn_count = 0
        if self.native is not None:
            # The controller uses this to stop/start the emulator around an
            # overlay without ever signalling a stopped process.
            self.native.handoff = self

    # -- handoff interface (used by MgbaNativeController) -----------------
    def stop_emulator(self, timeout: Optional[float] = None) -> bool:
        """Stops mGBA cleanly so its SDL/KMS/DRM display is released."""
        child = self.child
        if child is None:
            return True
        if child.poll() is not None:
            return True
        timeout = self.stop_timeout if timeout is None else float(timeout)
        logger.info("Stopping mGBA (pid %s) for a display handoff", getattr(child, "pid", -1))
        try:
            child.terminate()
        except Exception as exc:
            logger.warning("Could not send SIGTERM to mGBA: %s", exc)
        try:
            child.wait(timeout=timeout)
        except Exception:
            logger.warning("mGBA did not exit in %.1fs; killing it", timeout)
            try:
                child.kill()
                child.wait(timeout=2.0)
            except Exception:
                pass
        # The input watcher deliberately keeps running: it is what delivers
        # the combo that closes the overlay, and it lives as long as the
        # session (`_session_alive`), not as long as this child.
        stopped = child.poll() is not None
        if not stopped:
            logger.error("mGBA is still running after the handoff stop")
        return stopped

    def start_emulator(self, state_path: Optional[str] = None,
                       fps_target: Optional[float] = None) -> bool:
        """Restarts mGBA, optionally resuming a native state and speed."""
        if self.shutting_down or self.exit_requested or not self.build_command:
            logger.info("Not restarting mGBA (session ending)")
            return False
        try:
            cmd = self.build_command(fps_target=fps_target, savestate=state_path)
            logger.info("Restarting mGBA%s at fpsTarget=%s",
                        " from saved state" if state_path else "",
                        f"{float(fps_target):g}" if fps_target else "default")
            self._spawn(cmd)
        except Exception as exc:
            logger.error("Restarting mGBA failed: %s", exc)
            return False
        return self.child is not None and self.child.poll() is None

    # -- process plumbing -------------------------------------------------
    def _spawn(self, cmd: list) -> None:
        if self.monitor is not None:
            self.monitor.stop()
            self.monitor = None
        self.child = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True,
                                      bufsize=1, env=self.env)
        self.spawn_count += 1
        if self.native is not None:
            self.native.proc = self.child
        self.monitor = InputMonitor(
            self.child,
            escape_keys={KEY_ESC, self.bindings.get("keySelect", 8)},
            shortcuts=self.session.shortcuts if self.session else None,
            action_queue=self.action_queue,
            keybindings=self.bindings,
            on_consumed=(self.native.suppress_combo if self.native else None),
            alive_check=self._session_alive,
            on_exit_requested=self._request_exit,
        )
        self.monitor.start()
        threading.Thread(target=self._pump_stdout, args=(self.child,),
                         daemon=True, name="gba-mgba-log").start()

    def _session_alive(self) -> bool:
        """Liveness for the input watcher: the session, not the child.

        During a handoff the child is deliberately stopped, but the watcher
        must keep delivering the shortcut that closes the overlay.
        """
        return not (self.shutting_down or self.exit_requested)

    def _request_exit(self) -> None:
        self.exit_requested = True
        logger.info("Exit gesture requested; ending the session after the overlay")

    @staticmethod
    def _pump_stdout(child) -> None:
        try:
            if child.stdout:
                for line in iter(child.stdout.readline, ""):
                    cleaned = line.strip()
                    if cleaned:
                        logger.info("[mgba] %s", cleaned)
                child.stdout.close()
        except Exception:
            pass

    def _terminate_child(self) -> None:
        self.stop_emulator()

    def _restart_for_speed(self, fps_target: float) -> bool:
        """Applies mGBA's fpsTarget natively, resuming at the exact frame.

        Only used when the controller has no handoff; the normal path applies
        the speed while resuming from the overlay.
        """
        if self.native is None or not self.build_command:
            return False
        state = None
        try:
            state = self.native.capture_runtime_state("speed_state")
        except Exception as exc:
            logger.warning("Could not capture a state for speed change: %s", exc)
        if not state:
            logger.warning("Speed change needs a native state; keeping current speed")
            return False
        if not self.stop_emulator():
            return False
        return self.start_emulator(state, fps_target)

    def _drain_actions(self) -> None:
        if self.action_queue is None or self.session is None:
            return
        while True:
            try:
                action = self.action_queue.get_nowait()
            except queue.Empty:
                return
            self.session.handle_action(action)

    def _serve_overlay(self) -> None:
        if self.session is None or self.session.overlay is None:
            return
        overlay_kind = getattr(self.session.overlay, "kind", "")
        try:
            from gba_ingame_ui import InGameOverlayRenderer
            renderer = InGameOverlayRenderer(
                self.session,
                self.overlay_size or (960, 640),
                theme_name=self.theme_name,
                screenshot_path=getattr(self.native, "last_screenshot", None),
                windowed=self.windowed,
                logger=logger,
                poll_actions=self._drain_actions,
            )
            renderer.run()
        except Exception as exc:
            logger.warning("In-game overlay unavailable: %s", exc)
        finally:
            # Always close the model: closing resumes gameplay from the
            # captured state, so a failed display cannot freeze the game.
            self.session.close_overlay()
            logger.info("Overlay '%s' closed; gameplay %s", overlay_kind,
                        "resumed" if not self.exit_requested else "ending")

    def run(self) -> int:
        try:
            self._spawn(self.cmd)
        except Exception as exc:
            logger.error("Failed to launch mGBA: %s", exc)
            return 3

        def handle_signal(sig, frame):
            child = self.child
            logger.info("Adapter received signal %d, forwarding to child %d",
                        sig, getattr(child, "pid", -1))
            try:
                if child is not None:
                    child.send_signal(sig)
            except (ProcessLookupError, Exception):
                pass

        previous = {}
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, handle_signal)

        try:
            while True:
                if self.session is not None and self.action_queue is not None:
                    try:
                        action = self.action_queue.get(timeout=0.1)
                    except queue.Empty:
                        action = None
                    if action:
                        self.session.handle_action(action)
                    # The overlay loop can also drain actions (e.g. a speed
                    # change queued while a badge was showing), so serve and
                    # check for a pending reload on every tick, not only when
                    # this thread dequeued the action itself.
                    if self.session.overlay is not None:
                        self._serve_overlay()
                    if self.session.take_relaunch():
                        if not self._restart_for_speed(
                                self.session.fast_forward.fps_target):
                            self.session.note_speed_reload_failed()
                else:
                    time.sleep(0.1)
                if self.exit_requested:
                    break
                child = self.child
                if child is None or child.poll() is not None:
                    break
            child = self.child
            ret = child.wait() if child is not None else 0
        finally:
            self.shutting_down = True
            # Rule: never leave mGBA stopped, running, or half-relaunched.
            if self.session is not None:
                try:
                    self.session.release_display()
                except Exception:
                    pass
            if self.monitor:
                self.monitor.stop()
            self._terminate_child()
            for sig, handler in previous.items():
                try:
                    signal.signal(sig, handler)
                except Exception:
                    pass
            if self.native is not None:
                try:
                    self.native.resume()
                except Exception:
                    pass
        return ret


def _run_mgba(cmd: list, env: dict, session=None, action_queue=None,
              native=None, build_command=None, overlay_size=None,
              theme_name: str = "indigo", windowed: bool = False) -> int:
    """Runs mGBA, streaming its log, and returns the exit status."""
    return MgbaSessionRunner(
        cmd, env, session=session, action_queue=action_queue, native=native,
        build_command=build_command, overlay_size=overlay_size,
        theme_name=theme_name, windowed=windowed,
    ).run()


def main():
    content_path = os.environ.get("SYS_CONTENT_PATH")
    save_dir = os.environ.get("SYS_SAVE_DIR")
    data_dir = os.environ.get("SYS_DATA_DIR")
    config_dir = os.environ.get("SYS_CONFIG_DIR")

    if not content_path or not os.path.isfile(content_path):
        logger.error("Content path missing or invalid: %s", content_path)
        sys.exit(2)

    if not save_dir:
        logger.error("SYS_SAVE_DIR is required.")
        sys.exit(2)

    for path in (save_dir, data_dir, config_dir):
        if path:
            try:
                os.makedirs(path, exist_ok=True)
            except Exception as exc:
                logger.error("Cannot create %s: %s", path, exc)
                sys.exit(2)

    restore_backup_if_needed(save_dir)
    backup_saves(save_dir)

    ensure_mgba_config(config_dir)
    try:
        mgba_bin = find_mgba_binary()
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        # Also on the console: whoever is holding the handheld should not have to
        # read a log file to find out why nothing happened.
        sys.stderr.write(
            "\n  mGBA is not installed, so this game cannot start.\n\n"
            "  Nothing is broken and your saves are untouched.\n"
            "  Install it with:  sudo apt install mgba-sdl\n"
            "  (or press Ctrl+Alt+T and run 'gba-selftest' for a full report)\n\n")
        sys.stderr.flush()
        sys.exit(3)

    is_windowed = os.environ.get("GBA_WINDOWED", "0") == "1"
    display_scaling = os.environ.get("SYS_DISPLAY_SCALING") or \
        load_display_config().get("scaling", "integer")

    # A child that inherits a dead $DISPLAY or a KMS/DRM driver inside a live
    # desktop renders into nowhere: that is the "game starts to a black screen"
    # bug. Resolve the session and hand mGBA a coherent environment (D4).
    env = prepare_child_environment()
    screen = display_mode_size(env)
    logger.info("Display session=%s driver=%s windowed=%s scaling=%s screen=%s",
                display_session(), env.get("SDL_VIDEODRIVER", "auto"),
                is_windowed, display_scaling,
                f"{screen[0]}x{screen[1]}" if screen else "unknown")

    # --- per-game in-game features (keyed by stable content identity) ------
    settings = settings_store.load_settings()
    shortcuts = settings_store.load_shortcuts(settings)
    game_id = os.environ.get("SYS_ENTRY_ID") or ""
    if not valid_game_id(game_id):
        # Fall back to hashing the ROM itself: identity must never be a name.
        try:
            game_id = compute_content_id(content_path)
        except Exception:
            game_id = ""
    session = None
    actions = None
    native = None
    cheat_store = None
    cheats_dir = None
    runtime_dir = None

    def current_cheat_file():
        """Re-renders mGBA's native cheat file for every emulator start.

        The Cheats overlay only edits the per-game list; the file mGBA reads
        with `-c` is rewritten here, so a cheat enabled in the overlay is
        active in the restarted session without a launcher restart. A game
        with no cheats at all must not get an empty `-c` argument.
        """
        if cheat_store is None:
            return None
        try:
            return cheat_store.write_native()
        except Exception as exc:
            logger.warning("Could not write the native cheat file: %s", exc)
            return None

    if valid_game_id(game_id):
        try:
            cheat_store = CheatStore(game_id, content_path)
            state_store = SaveStateStore(game_id)
            runtime_dir = os.path.join(save_dir, "runtime")
            os.makedirs(runtime_dir, exist_ok=True)
            native = MgbaNativeController(None, content_path, save_dir)
            session = InGameSession(game_id, shortcuts, cheat_store,
                                    state_store, native)
            actions = queue.Queue()
            cheats_dir = cheat_store.cheats_dir
            current_cheat_file()
            logger.info("In-game features ready for %s (cheats=%d, shortcuts=%s)",
                        game_id, len(cheat_store.cheats()), shortcuts)
        except Exception as exc:
            # A broken optional feature must never stop a game from starting.
            logger.warning("In-game features unavailable: %s", exc)
            session, actions, native = None, None, None
            cheat_store, cheats_dir, runtime_dir = None, None, None
    else:
        logger.warning("Invalid game identity; in-game features disabled")

    def make_command(windowed=None, size=None, fps_target=None, savestate=None):
        return build_mgba_command(
            mgba_bin, content_path, save_dir,
            is_windowed if windowed is None else windowed,
            display_scaling, screen if size is None else size,
            cheat_file=current_cheat_file(), cheats_dir=cheats_dir,
            savestate=savestate, fps_target=fps_target,
            runtime_dir=runtime_dir)

    def reload_command(**kwargs):
        return make_command(fps_target=kwargs.get("fps_target"),
                            savestate=kwargs.get("savestate"))

    cmd = make_command()
    logger.info("Executing mGBA: %s", " ".join(cmd))

    run_kwargs = dict(
        session=session, action_queue=actions, native=native,
        build_command=reload_command if session else None,
        overlay_size=screen or (960, 640),
        theme_name=settings.get("theme", "indigo"),
        windowed=is_windowed,
    )

    started = time.monotonic()
    ret = _run_mgba(cmd, env, **run_kwargs)
    elapsed = time.monotonic() - started
    logger.info("Child mGBA exited with code %d after %.1fs", ret, elapsed)

    # Self-heal: a fast, non-zero exit inside a desktop session usually means
    # the requested SDL backend could not create a window. Retry once windowed
    # without our driver pin instead of bouncing the user to the library.
    strategy = retry_strategy(display_session(), ret, elapsed, is_windowed, env)
    if strategy and strategy[0] == "windowed":
        logger.warning("mGBA failed early; retrying once in a window without SDL video pinning")
        retry_env = dict(os.environ)
        retry_env.pop("SDL_VIDEODRIVER", None)
        retry_cmd = make_command(windowed=True, size=None)
        logger.info("Retrying: %s", " ".join(retry_cmd))
        retry_kwargs = dict(run_kwargs, windowed=True)

        def retry_reload(**kwargs):
            return make_command(windowed=True, size=None,
                                fps_target=kwargs.get("fps_target"),
                                savestate=kwargs.get("savestate"))
        retry_kwargs["build_command"] = retry_reload if session else None
        ret = _run_mgba(retry_cmd, retry_env, **retry_kwargs)
    elif strategy and strategy[0] == "card":
        retry_env = dict(env)
        retry_env["SDL_VIDEO_KMSDRM_DEVICE"] = strategy[1]
        logger.warning("mGBA failed early on %s; retrying on %s",
                       env.get("SDL_VIDEO_KMSDRM_DEVICE") or "default", strategy[1])
        ret = _run_mgba(cmd, retry_env, **run_kwargs)

    # Durability: fsync save files and their directory, then keep a backup.
    sync_directory(save_dir)
    backup_saves(save_dir)
    if native is not None:
        try:
            native.injector.close()
        except Exception:
            pass

    # If the child died from a signal, propagate it so the supervisor can
    # classify the session faithfully (H9).
    if ret < 0:
        sig = -ret
        logger.info("Child was terminated by signal %d, re-raising", sig)
        try:
            signal.signal(sig, signal.SIG_DFL)
            os.kill(os.getpid(), sig)
        except Exception:
            sys.exit(128 + sig)

    sys.exit(ret)


if __name__ == "__main__":
    main()
