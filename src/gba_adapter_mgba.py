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
import re
import threading
import time
from typing import Optional

# Ensure local src directory is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import (
    setup_logging, load_keybindings, load_display_config, DEFAULT_KEYBINDINGS,
    compute_content_id, extract_gba_title,
)
import config as settings_store
from config import (
    cards_without_output,
    display_mode_size,
    display_session,
    other_drm_devices,
    prepare_child_environment,
    ranked_drm_cards,
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


def sync_directory(dir_path: str) -> int:
    """Performs fsync on save files and their containing directory (Rule SV2).

    Returns how many files were fsynced, and says so in the log: the closing
    stage of a session must show that save data reached the disk rather than
    only that the emulator exited.
    """
    synced = 0
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
                        synced += 1
                    except Exception as exc:
                        logger.warning("Could not fsync %s: %s", f_path, exc)
            os.fsync(dir_fd)
            logger.info("Save fsync verified: %d file(s) plus the directory %s",
                        synced, dir_path)
        finally:
            os.close(dir_fd)
    except Exception as e:
        logger.warning("Directory fsync warning: %s", e)
    return synced


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
        pid = getattr(self.proc, "pid", -1)
        logger.info("Input: %s; ending the session (SIGTERM to emulator pid %s)",
                    reason, pid)
        if self.on_exit_requested is not None:
            try:
                self.on_exit_requested()
            except Exception:
                pass
        try:
            self.proc.terminate()
            logger.info("Input: SIGTERM delivered to emulator pid %s", pid)
        except Exception as exc:
            logger.warning("Input: SIGTERM to emulator pid %s failed: %s", pid, exc)

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
        watched = []
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
                        watched.append(f"{dev} ({name or 'unnamed'})")
                    else:
                        watched.append(f"{dev} (joystick)")
                    fds[fd] = dev.startswith("js")
                except Exception as exc:
                    # A permission error here is the usual reason an exit combo
                    # "does nothing", so it is recorded rather than swallowed.
                    logger.debug("Input: cannot open /dev/input/%s: %s", dev, exc)
            if not fds:
                logger.warning(
                    "Input: no readable /dev/input device; ESC and every in-game "
                    "combo are undetectable in this session (escape keys=%s)",
                    sorted(self.escape_keys or ()))
                return
            logger.info("Input: watching %d device(s): %s | escape-keys=%s | "
                        "combos=%s", len(fds), ", ".join(watched) or "(none named)",
                        sorted(self.escape_keys or ()),
                        sorted(self.shortcuts) if self.shortcuts else "(none)")

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
                                    self._trigger(f"Gamepad exit combo (button {number}, held={sorted(held)})")
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
                                    self._trigger(f"Exit key detected (evdev code {code})")
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


# --- verbose diagnostics ----------------------------------------------------
# Everything in this section exists to answer one question from the boot log
# alone: did the emulator open a window that actually reached the screen?
#
# mgba-sdl's default log mask is FATAL|ERROR|WARN, so an emulator that quietly
# fails to present prints nothing at all, and SDL2/Mesa only describe what they
# did at `verbose`. src/gba_session.sh exports SDL_LOG_PRIORITY=verbose,
# LIBGL_DEBUG=verbose and MESA_DEBUG=1 for the subsystem side; GBA_DEBUG
# controls this side. GBA_DEBUG=0 restores the quiet, pre-diagnostics
# behaviour, which is the escape hatch if a capture ever proves too chatty.

# mgba-sdl `-l`/`--log-level` bitmask (include/mgba/core/log.h):
#   FATAL 0x01, ERROR 0x02, WARN 0x04, INFO 0x08, DEBUG 0x10, STUB 0x20,
#   GAME_ERROR 0x40; mLOG_ALL is 0x7F. 31 = FATAL|ERROR|WARN|INFO|DEBUG, which
# adds the ROM/BIOS load, save routing, audio buffer negotiation and frame
# timing without mGBA's per-call "unimplemented" stub spam.
MGBA_LOG_MASK_DEFAULT = 31
MGBA_LOG_MASK_ALL = 0x7F
_LOG_MASK_BITS = ((0x01, "FATAL"), (0x02, "ERROR"), (0x04, "WARN"),
                  (0x08, "INFO"), (0x10, "DEBUG"), (0x20, "STUB"),
                  (0x40, "GAME_ERROR"))

# `xwininfo -root -tree` under a compositor can list hundreds of windows, and
# the boot log is size-capped (Rule ST5), so the evidence is truncated rather
# than free. Map state costs one extra `xwininfo -id` per window, so it is only
# ever queried for windows that look like the emulator.
DIAG_MAX_TREE_LINES = 120
DIAG_WINDOW_DETAIL_LIMIT = 8
DIAG_TOOL_TIMEOUT = 4.0
# mGBA's SDL2 window is titled with the project name ("mGBA") and carries SDL's
# WM_CLASS ("SDL_app"), so either identifies it even if a build renames one.
DIAG_WINDOW_HINTS = ("mgba", "sdl_app")
# SDL creates the window, then maps and resizes it; sampling immediately after
# Popen would report a healthy session as "no window".
DIAG_SETTLE_SECONDS = 1.5
# A window manager's frame offsets make a screen-sized window read a few pixels
# off. Only larger disagreements are worth calling "off-screen".
DIAG_EDGE_TOLERANCE = 8

_XWININFO_TREE_LINE = re.compile(
    r"^\s*(0x[0-9a-fA-F]+)\s+"
    r"(?:\"(?P<name>[^\"]*)\":\s*\(\"(?P<instance>[^\"]*)\"\s*\"(?P<wmclass>[^\"]*)\"\)"
    r"|\(has no name\):\s*\(\))"
    r"\s*(?P<geometry>\d+x\d+[+-]\d+[+-]\d+)"
)
_MODE_SIZE = re.compile(r"^\s*(\d{2,5})x(\d{2,5})\b")


def diagnostics_enabled() -> bool:
    """True unless the operator silenced diagnostics with GBA_DEBUG=0."""
    value = os.environ.get("GBA_DEBUG", "1").strip().lower()
    return value not in ("", "0", "false", "no", "off")


def describe_log_mask(mask: int) -> str:
    """31 -> 'FATAL|ERROR|WARN|INFO|DEBUG', for the log rather than for mGBA."""
    names = [name for bit, name in _LOG_MASK_BITS if int(mask) & bit]
    return "|".join(names) if names else "(silent)"


def mgba_log_level() -> Optional[int]:
    """The `-l` mask for this session, or None to leave mGBA's default.

    GBA_MGBA_LOG_LEVEL raises the mask for a deeper capture (127 is mLOG_ALL:
    it adds STUB and GAME_ERROR). An unusable value is reported and replaced
    rather than passed on, because getopt_long would reject the whole command
    line and turn a logging typo into a game that does not start.
    """
    if not diagnostics_enabled():
        return None
    raw = (os.environ.get("GBA_MGBA_LOG_LEVEL") or "").strip()
    mask = MGBA_LOG_MASK_DEFAULT
    if raw:
        try:
            mask = int(raw, 0)
        except ValueError:
            logger.warning("Ignoring GBA_MGBA_LOG_LEVEL=%r (not a number); using %d",
                           raw, MGBA_LOG_MASK_DEFAULT)
    return max(0, min(int(mask), MGBA_LOG_MASK_ALL))


def _read_sysfs(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def _tool(name: str, package: str) -> Optional[str]:
    """Absolute path of a read-only diagnostic tool, or None with a hint."""
    found = shutil.which(name)
    if found:
        return found
    logger.info("[diag] %s is not installed (package: %s); that evidence is unavailable",
                name, package)
    return None


def _capture_output(cmd: list, env: Optional[dict] = None) -> Optional[str]:
    """Runs a read-only tool and returns its output; never raises.

    Returns None when the tool could not be run *or* exited non-zero, because
    "xwininfo failed" and "there is no emulator window" are opposite diagnoses
    and must not be reported as the same thing.
    """
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=DIAG_TOOL_TIMEOUT, check=False, env=env)
    except Exception as exc:
        logger.info("[diag] %s did not answer: %s", os.path.basename(cmd[0]), exc)
        return None
    out = proc.stdout.decode("utf-8", "ignore")
    if proc.returncode != 0:
        reason = next((line.strip() for line in out.splitlines() if line.strip()), "")
        logger.info("[diag] %s exited with status %d%s", os.path.basename(cmd[0]),
                    proc.returncode, f": {reason}" if reason else "")
        return None
    return out


def _parse_mode_size(text: str) -> Optional[tuple]:
    """'1920x1080  60.00*+' -> (1920, 1080)."""
    match = _MODE_SIZE.match(text or "")
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)))


def _matches_emulator(window: dict) -> bool:
    fields = (window.get("name", ""), window.get("instance", ""),
              window.get("wmclass", ""))
    return any(hint in field.lower() for field in fields for hint in DIAG_WINDOW_HINTS)


def _log_xrandr(env: dict) -> Optional[tuple]:
    """Logs `xrandr --current`; returns the active ('*') mode as (w, h)."""
    xrandr = _tool("xrandr", "x11-server-utils")
    if not xrandr:
        return None
    out = _capture_output([xrandr, "--current"], env)
    if not out:
        logger.info("[x11] xrandr --current returned nothing; is DISPLAY=%s alive?",
                    env.get("DISPLAY"))
        return None
    active = None
    logged = 0
    for line in out.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Output heads are unindented; their mode lines are indented and the
        # active one carries '*'. Together they are the resolution and refresh
        # rate mGBA is being asked to drive.
        if not line.startswith((" ", "\t")) and (
                " connected" in line or " disconnected" in line):
            logger.info("[x11] output: %s", stripped)
            logged += 1
        elif "*" in stripped:
            logger.info("[x11] active mode: %s", stripped)
            logged += 1
            if active is None:
                active = _parse_mode_size(stripped)
        if logged >= DIAG_MAX_TREE_LINES:
            logger.info("[x11] xrandr output truncated at %d lines", logged)
            break
    if active is None:
        logger.info("[x11] xrandr reports no active mode")
    return active


def _parse_window_tree(env: dict) -> Optional[list]:
    """Parses `xwininfo -root -tree` into window dicts (id, name, geometry)."""
    xwininfo = _tool("xwininfo", "x11-utils")
    if not xwininfo:
        return None
    out = _capture_output([xwininfo, "-root", "-tree"], env)
    if out is None:
        return None
    windows = []
    for line in out.splitlines():
        match = _XWININFO_TREE_LINE.match(line)
        if not match:
            continue
        windows.append({
            "id": match.group(1),
            "name": match.group("name") or "",
            "instance": match.group("instance") or "",
            "wmclass": match.group("wmclass") or "",
            "geometry": match.group("geometry") or "",
        })
    if not windows and out.strip():
        logger.info("[x11] xwininfo -root -tree returned %d unparseable line(s)",
                    len(out.splitlines()))
    return windows


def _window_detail(window_id: str, env: dict) -> dict:
    """`xwininfo -id` for one window: Map State and absolute geometry.

    Keys are lower-cased because xwininfo's own spelling differs between
    fields ("Map State" but "Override redirection state") and across versions;
    callers must not have to guess the capitalisation.
    """
    xwininfo = _tool("xwininfo", "x11-utils")
    if not xwininfo:
        return {}
    out = _capture_output([xwininfo, "-id", window_id], env)
    detail = {}
    for line in (out or "").splitlines():
        key, sep, value = line.strip().partition(":")
        if sep and key:
            detail[key.lower()] = value.strip()
    return detail


def _detail_value(detail: dict, *names: str) -> Optional[str]:
    """The first present of `names` in an `xwininfo -id` field map.

    xwininfo is not consistent with itself, between fields ("Map State" but
    "Override redirection state") or between builds, so lookups accept the
    variants rather than reporting "?" for evidence that is on the screen.
    """
    for name in names:
        value = detail.get(name.lower())
        if value not in (None, ""):
            return value
    return None


def _log_drm_connectors(env: dict, drm_root: str = "/sys/class/drm") -> None:
    """Connector evidence for KMS/DRM, where there is no X11 window tree.

    A card with no connected output is the console equivalent of an unmapped
    window, and it is exactly what `retry_strategy` tries to work around by
    moving to another /dev/dri/cardN.
    """
    pinned = os.path.basename(env.get("SDL_VIDEO_KMSDRM_DEVICE", "") or "")
    try:
        names = sorted(os.listdir(drm_root))
    except OSError as exc:
        logger.info("[drm] %s is unreadable (%s); no connector evidence", drm_root, exc)
        return
    connected = 0
    for name in names:
        card, _, connector = name.partition("-")
        if not connector or (pinned and card != pinned):
            continue
        base = os.path.join(drm_root, name)
        if (_read_sysfs(os.path.join(base, "status")) or "") != "connected":
            logger.info("[drm] %s: not connected", name)
            continue
        connected += 1
        modes = (_read_sysfs(os.path.join(base, "modes")) or "").splitlines()
        preferred = modes[0].strip() if modes else "none"
        logger.info("[drm] %s: connected, enabled=%s dpms=%s preferred=%s (%d mode(s))",
                    name, _read_sysfs(os.path.join(base, "enabled")) or "?",
                    _read_sysfs(os.path.join(base, "dpms")) or "?",
                    preferred, len(modes))
    logger.info("[drm] SDL_VIDEODRIVER=%s device=%s connected-connector(s)=%d",
                env.get("SDL_VIDEODRIVER", "auto"),
                env.get("SDL_VIDEO_KMSDRM_DEVICE") or "(unpinned)", connected)
    if not connected:
        logger.warning("[drm] no connected connector on %s: this card drives no "
                       "output, which presents as a black screen",
                       pinned or drm_root)


def log_display_diagnostics(env: Optional[dict] = None,
                            settle: float = DIAG_SETTLE_SECONDS,
                            alive_check=None) -> None:
    """Records whether the emulator's window mapped, and where it landed.

    A black screen has two very different causes, and only the second one is
    visible to the adapter: mGBA never created a window, or it created one that
    is unmapped, off-screen, or smaller than the screen. Sampling the X11 tree
    (or the DRM connectors when there is no X) once, shortly after the child
    starts, turns that into a line in the log instead of a guess.

    Read-only, time-bounded and never fatal: a missing `xwininfo` or a dead
    display must not be able to stop a game from starting.
    """
    if not diagnostics_enabled():
        return
    env = dict(os.environ if env is None else env)
    if settle > 0:
        time.sleep(settle)

    child_alive = "unknown"
    if alive_check is not None:
        try:
            child_alive = "yes" if alive_check() else "no"
        except Exception:
            child_alive = "unknown"
    logger.info("[diag] sampling the display: child-alive=%s DISPLAY=%s "
                "SDL_VIDEODRIVER=%s SDL_VIDEO_WINDOW_POS=%s",
                child_alive, env.get("DISPLAY") or "none",
                env.get("SDL_VIDEODRIVER", "auto"),
                env.get("SDL_VIDEO_WINDOW_POS", "(unset)"))

    if not env.get("DISPLAY"):
        _log_drm_connectors(env)
        return

    active = _log_xrandr(env)
    windows = _parse_window_tree(env)
    if windows is None:
        # Say so rather than staying quiet: a missing window tree is not the
        # same evidence as an empty one, and the gap must be visible.
        logger.info("[x11] window tree unavailable, so whether the emulator "
                    "window mapped is unconfirmed (reason logged above)")
        return

    matched = [w for w in windows if _matches_emulator(w)]
    others = [w for w in windows if not _matches_emulator(w)]
    logger.info("[x11] window tree: %d window(s), %d look like the emulator",
                len(windows), len(matched))

    # Log the raw tree lines (resolution + position for every window), matched
    # windows first so a truncated capture keeps the useful part.
    budget = DIAG_MAX_TREE_LINES
    for window in matched[:budget]:
        logger.info("[x11] tree (match): %s \"%s\" class=%s/%s geometry=%s",
                    window["id"], window["name"], window["instance"],
                    window["wmclass"], window["geometry"])
    for window in others[:max(0, budget - len(matched[:budget]))]:
        logger.info("[x11] tree: %s \"%s\" class=%s/%s geometry=%s",
                    window["id"], window["name"], window["instance"],
                    window["wmclass"], window["geometry"])
    if len(windows) > budget:
        logger.info("[x11] tree truncated at %d of %d window(s)", budget, len(windows))

    if not matched:
        logger.warning("[x11] NO emulator window in the X11 tree: mGBA never "
                       "created one, or died before mapping it. The [mgba] lines "
                       "above carry the SDL/Mesa reason.")
        return

    for window in matched[:DIAG_WINDOW_DETAIL_LIMIT]:
        detail = _window_detail(window["id"], env)
        state = _detail_value(detail, "map state") or "unknown"
        width = _detail_value(detail, "width")
        height = _detail_value(detail, "height")
        pos_x = _detail_value(detail, "absolute upper-left x", "absolute upper left x")
        pos_y = _detail_value(detail, "absolute upper-left y", "absolute upper left y")
        override = _detail_value(detail, "override redirection state",
                                 "override redirect state")
        logger.info("[x11] emulator window %s \"%s\": map-state=%s size=%sx%s "
                    "position=%s,%s override-redirect=%s",
                    window["id"], window["name"], state, width or "?", height or "?",
                    pos_x or "?", pos_y or "?", override or "?")
        if state != "IsViewable":
            logger.warning("[x11] window %s is %s: SDL created it but it is not "
                           "on screen (never mapped, or covered by another surface)",
                           window["id"], state)
        try:
            size = (int(width), int(height))
            origin = (int(pos_x), int(pos_y))
        except (TypeError, ValueError):
            continue
        if not active:
            continue
        screen_w, screen_h = active
        win_w, win_h = size
        win_x, win_y = origin
        if (win_x >= screen_w or win_y >= screen_h
                or win_x + win_w <= 0 or win_y + win_h <= 0):
            logger.warning("[x11] window %s at %dx%d+%d+%d lies entirely outside "
                           "the active %dx%d mode: it is off-screen",
                           window["id"], win_w, win_h, win_x, win_y, screen_w, screen_h)
        elif (win_x < -DIAG_EDGE_TOLERANCE or win_y < -DIAG_EDGE_TOLERANCE
              or win_x + win_w > screen_w + DIAG_EDGE_TOLERANCE
              or win_y + win_h > screen_h + DIAG_EDGE_TOLERANCE):
            logger.info("[x11] window %s at %dx%d+%d+%d overflows the %dx%d mode "
                        "(clipped by the screen edge)",
                        window["id"], win_w, win_h, win_x, win_y, screen_w, screen_h)
        elif (win_w, win_h) != (screen_w, screen_h):
            logger.info("[x11] window %s is %dx%d on a %dx%d screen: it does not "
                        "fill the display", window["id"], win_w, win_h,
                        screen_w, screen_h)
        else:
            logger.info("[x11] window %s fills the active %dx%d mode",
                        window["id"], screen_w, screen_h)
    if len(matched) > DIAG_WINDOW_DETAIL_LIMIT:
        logger.info("[x11] %d further emulator-looking window(s) not detailed",
                    len(matched) - DIAG_WINDOW_DETAIL_LIMIT)


def log_load_diagnostics(content_path: str, save_dir: str, config_dir: Optional[str] = None) -> None:
    """Records the 'Load' stage: ROM header, save routing and BIOS resolution.

    mGBA derives the save filename from the ROM's basename inside
    `savegamePath`, so the expected path is checked here rather than after a
    session ends and a missing save looks like data loss.
    """
    if not diagnostics_enabled():
        return
    try:
        size = os.path.getsize(content_path)
    except OSError as exc:
        logger.warning("[load] cannot stat %s: %s", content_path, exc)
        size = -1
    title = ""
    try:
        title = extract_gba_title(content_path)
    except Exception as exc:
        logger.info("[load] cartridge header unreadable: %s", exc)
    logger.info("[load] ROM: %s (%s bytes, title=%r)", content_path,
                f"{size:,}" if size >= 0 else "?", title or "(unparsed)")

    stem = os.path.splitext(os.path.basename(content_path))[0]
    for extension in (".sav", ".srm"):
        candidate = os.path.join(save_dir, stem + extension)
        try:
            if os.path.exists(candidate):
                logger.info("[load] save file: %s exists (%s bytes)", candidate,
                            f"{os.path.getsize(candidate):,}")
            else:
                logger.info("[load] save file: %s does not exist yet (mGBA creates "
                            "it on the first in-game save)", candidate)
        except OSError as exc:
            logger.warning("[load] save file %s is unreadable: %s", candidate, exc)

    bios = (os.environ.get("GBA_BIOS_PATH") or os.environ.get("SYS_BIOS_PATH") or "").strip()
    if bios and os.path.isfile(bios):
        try:
            logger.info("[load] BIOS: %s (%s bytes)", bios,
                        f"{os.path.getsize(bios):,}")
        except OSError:
            logger.info("[load] BIOS: %s (size unknown)", bios)
    elif bios:
        logger.warning("[load] BIOS: configured path %s does not exist; mGBA will "
                       "fall back to its built-in BIOS", bios)
    else:
        logger.info("[load] BIOS: none configured; mGBA uses its built-in HLE BIOS "
                    "(no proprietary file is required)")
    if config_dir:
        logger.info("[load] mGBA config: XDG_CONFIG_HOME=%s -> %s", config_dir,
                    mgba_config_path(config_dir))


def build_mgba_command(mgba_bin: str, content_path: str, save_dir: str,
                       windowed: bool, display_scaling: str,
                       display_size: Optional[tuple] = None,
                       cheat_file: Optional[str] = None,
                       cheats_dir: Optional[str] = None,
                       savestate: Optional[str] = None,
                       fps_target: Optional[float] = None,
                       runtime_dir: Optional[str] = None,
                       bare_x11: bool = False,
                       log_level: Optional[int] = None) -> list:
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

    Bare X11 uses SDL's fullscreen (`-f` flag) directly. With software rendering
    (LIBGL_ALWAYS_SOFTWARE), fullscreen is just an X11 blit — no WM needed, no
    DRI3 page-flipping. The original screen-sized window workaround
    (fullscreen=0 with width/height) doesn't work because mGBA's SDL2 renderer
    waits for WM events that never come in bare Matchbox.

    `log_level` is mgba-sdl's `-l` bitmask. With None (the default) the command
    line is untouched and mGBA logs only FATAL|ERROR|WARN; a diagnostic session
    passes 31 so the boot log also shows the ROM/BIOS load, save routing, audio
    buffer negotiation and frame timing.
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
    if log_level is not None:
        cmd.extend(["-l", str(int(log_level))])
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

    * bare X11: software rendering is the default (LIBGL_ALWAYS_SOFTWARE),
      so a crash here usually means something other than GL; retry once
      windowed with no driver pinning, so SDL negotiates for itself;
    * desktop (x11/wayland): retry once windowed with no driver pinning;
    * bare console: the first DRM card may drive no output, so try the others.

    When the environment was explicitly set to hardware GL (GBA_HARDWARE_GL=1),
    a windowed failure escalates to software rendering as a last resort.

    Returns ("windowed", None), ("software", None) or ("card", "/dev/dri/cardN").
    """
    if ret in (0, -signal.SIGTERM, -signal.SIGINT, -signal.SIGHUP, -signal.SIGKILL) \
            or elapsed >= 5.0:
        return None
    if session == "x11":
        # Bare X11 without a compositor: DRI3 page-flipping on AMD/Intel
        # sometimes fails silently.  If the first attempt used DRI2 blit
        # (LIBGL_DRI3_DISABLE) and still died, escalate to software rendering.
        if env.get("LIBGL_DRI3_DISABLE") and not windowed:
            return ("windowed", None)
        if not windowed:
            return ("windowed", None)
        # Already windowed and still failed: try full software rendering.
        if env.get("LIBGL_ALWAYS_SOFTWARE") != "1":
            return ("software", None)
        return None
    if session in ("wayland",):
        if not windowed:
            return ("windowed", None)
        return None
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
                 stop_timeout: float = 2.0):
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
        pid = getattr(child, "pid", -1)
        logger.info("Stopping mGBA (pid %s) for a display handoff", pid)
        # Every wait is bounded, including the final reap. SIGCONT lets a
        # suspended child handle termination; SIGKILL also works if SDL is hung.
        # Each step is logged so the closing stage of the log shows exactly
        # which signal ended the session and how long escalation took.
        stop_started = time.monotonic()
        for sig, grace in ((signal.SIGCONT, 0), (signal.SIGTERM, timeout),
                           (signal.SIGHUP, 0.5), (signal.SIGKILL, 2.0)):
            name = getattr(sig, "name", str(sig))
            signal_at = time.monotonic()
            try:
                if sig == signal.SIGTERM:
                    child.terminate()
                elif sig == signal.SIGKILL:
                    child.kill()
                else:
                    child.send_signal(sig)
                logger.info("Stop sequence: %s -> pid %s (%.2fs into the stop, "
                            "grace %.1fs)", name, pid, signal_at - stop_started, grace)
            except ProcessLookupError:
                logger.info("Stop sequence: %s not delivered; pid %s already exited",
                            name, pid)
            except OSError as exc:
                logger.warning("Could not signal mGBA (%s): %s", name, exc)
            if not grace:
                continue
            try:
                child.wait(timeout=grace)
                logger.info("Stop sequence: pid %s exited after %s (%.2fs)",
                            pid, name, time.monotonic() - signal_at)
                break
            except subprocess.TimeoutExpired:
                logger.warning("mGBA did not exit after %s in %.1fs; escalating",
                               name, grace)
            except OSError as exc:
                logger.warning("Could not reap mGBA: %s", exc)
        # The input watcher deliberately keeps running: it is what delivers
        # the combo that closes the overlay, and it lives as long as the
        # session (`_session_alive`), not as long as this child.
        stopped = child.poll() is not None
        if stopped:
            logger.info("Stop sequence: pid %s reaped with status %s after %.2fs",
                        pid, child.returncode, time.monotonic() - stop_started)
        else:
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
        # stderr is merged into stdout (never DEVNULL, never a second pipe):
        # mGBA logs to stdout, while SDL2 and Mesa write their verbose
        # diagnostics to stderr, and both must land in /data/logs/latest.log.
        self.child = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True,
                                      bufsize=1, env=self.env)
        self.spawn_count += 1
        # The first spawn's command line is already in the log as `Executing
        # mGBA:` (or `Retrying:`) from main(); a restart after an overlay
        # handoff or a speed change is not, so argv is recorded again only when
        # it is new information rather than twice per session.
        logger.info("Spawned mGBA (pid %s, spawn #%d)%s",
                    getattr(self.child, "pid", -1), self.spawn_count,
                    "" if self.spawn_count == 1 else f": {' '.join(cmd)}")
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
        if diagnostics_enabled():
            # Off the main thread: sampling the window tree must never delay the
            # emulator's own start, and it re-runs after every handoff restart so
            # an overlay can also be caught leaving a stale or misplaced window.
            threading.Thread(target=self._log_display_evidence, args=(self.child,),
                             daemon=True, name="gba-display-diag").start()

    def _log_display_evidence(self, child) -> None:
        """Samples the display once mGBA has had a moment to map its window."""
        try:
            log_display_diagnostics(
                self.env,
                alive_check=(lambda: child.poll() is None) if child is not None else None)
        except Exception as exc:
            logger.debug("Display diagnostics failed: %s", exc)

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
        """Forwards mGBA's merged stdout+stderr into the session log.

        With `-l 31` this is where mGBA's own ROM/BIOS load, save routing,
        audio buffer negotiation and frame timing lines arrive; SDL2's and
        Mesa's verbose diagnostics come in on the same stream because stderr
        is merged at spawn time. A pump that dies silently would look exactly
        like an emulator that logged nothing, so both ends are recorded.
        """
        lines = 0
        try:
            if child.stdout:
                for line in iter(child.stdout.readline, ""):
                    cleaned = line.strip()
                    if cleaned:
                        lines += 1
                        logger.info("[mgba] %s", cleaned)
                child.stdout.close()
            logger.info("[mgba] output stream closed after %d line(s)", lines)
        except Exception as exc:
            logger.warning("[mgba] log pump stopped early after %d line(s): %s",
                           lines, exc)

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
        if self.exit_requested:
            if self.session is not None:
                self.session.close_overlay()
            return
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
            self.shutting_down = True
            if self.monitor:
                self.monitor.stop()
            self._terminate_child()
            if self.child is not None and self.child.stdout is not None:
                self.child.stdout.close()
            return 3

        def handle_signal(sig, frame):
            self._request_exit()
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
            if child is not None:
                self.stop_emulator()
                # poll reaps an exited child; never wait indefinitely even if
                # the kernel cannot complete SIGKILL (e.g. uninterruptible I/O).
                ret = child.poll()
                if ret is None:
                    logger.error("mGBA could not be reaped after SIGKILL")
                    ret = -signal.SIGKILL
            else:
                ret = 0
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
    # Stage 1 of the log: what we are about to hand mGBA (ROM header, save
    # routing, BIOS). Recorded before the emulator starts so a session that
    # never gets that far still leaves its input behind.
    log_load_diagnostics(content_path, save_dir, config_dir)
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
    bare_x11 = (env.get("GBA_BARE_X11") == "1"
                and display_session() == "x11" and not is_windowed)
    if bare_x11:
        # Use SDL's fullscreen directly. With software rendering (LIBGL_ALWAYS_SOFTWARE),
        # fullscreen is just an X11 blit — no WM needed, no DRI3 page-flipping.
        # This avoids the "fullscreen=0 + screen-sized window" workaround which
        # doesn't present frames in bare Matchbox.
        if screen:
            logger.info("Bare X11: SDL fullscreen with %sx%s display", *screen)
        else:
            logger.info("Bare X11: SDL fullscreen (display size unknown)")

    # Bare-X11 workaround: without a compositor, mGBA's OpenGL via GLX
    # can't present frames to bare X11. The solution requires TWO things:
    # 1. Software OpenGL (llvmpipe) to avoid AMD DRI3 page-flip failures
    # 2. A compositor (picom/compton) to present the software-rendered frames
    #
    # LIBGL_ALWAYS_SOFTWARE=1 forces Mesa to use llvmpipe (software OpenGL)
    # instead of hardware radeonsi. This is slower but avoids DRI3 issues.
    # The compositor (started in gba_session.sh) handles X11 presentation.
    #
    # Set GBA_HARDWARE_GL=1 to skip software fallback (will likely black-screen
    # on bare X11 without a compositor).
    if bare_x11:
        force_hw = env.get("GBA_HARDWARE_GL") == "1"
        if not force_hw:
            # Force software OpenGL rendering (llvmpipe) to avoid DRI3 issues
            env["LIBGL_ALWAYS_SOFTWARE"] = "1"
            logger.info("Bare X11: LIBGL_ALWAYS_SOFTWARE=1 set to use software OpenGL "
                        "(llvmpipe). A compositor (picom/compton) is required to present frames.")
    logger.info("Display session=%s driver=%s windowed=%s scaling=%s screen=%s",
                display_session(), env.get("SDL_VIDEODRIVER", "auto"),
                is_windowed, display_scaling,
                f"{screen[0]}x{screen[1]}" if screen else "unknown")
    if display_session() == "console":
        # A card with nothing wired to it emulates perfectly and shows nothing,
        # so name the pin, the ranking behind it and every card that sysfs says
        # drives no output (T-DISPLAY-2 / D8).
        blind = cards_without_output()
        logger.info("KMS/DRM pin: %s | ranked: %s%s",
                    env.get("SDL_VIDEO_KMSDRM_DEVICE") or "(unset)",
                    ", ".join(ranked_drm_cards()) or "no /dev/dri card found",
                    f" | output-less: {', '.join(blind)}" if blind else "")

    # Stage 2 of the log: the subsystem verbosity actually in force. mGBA's own
    # mask is ours to set; SDL2/Mesa are configured by src/gba_session.sh, so
    # recording what arrived makes a silent capture self-explanatory.
    log_level = mgba_log_level()
    if log_level is not None:
        logger.info("Subsystem diagnostics: mGBA log mask -l %d (%s); "
                    "SDL_LOG_PRIORITY=%s LIBGL_DEBUG=%s MESA_DEBUG=%s "
                    "LIBGL_DRI3_DISABLE=%s LIBGL_ALWAYS_SOFTWARE=%s",
                    log_level, describe_log_mask(log_level),
                    env.get("SDL_LOG_PRIORITY", "(default)"),
                    env.get("LIBGL_DEBUG", "(default)"),
                    env.get("MESA_DEBUG", "(default)"),
                    env.get("LIBGL_DRI3_DISABLE", "(default)"),
                    env.get("LIBGL_ALWAYS_SOFTWARE", "(default)"))
    else:
        logger.info("Subsystem diagnostics: off (GBA_DEBUG=0); mGBA logs "
                    "FATAL|ERROR|WARN only; "
                    "LIBGL_DRI3_DISABLE=%s LIBGL_ALWAYS_SOFTWARE=%s",
                    env.get("LIBGL_DRI3_DISABLE", "(default)"),
                    env.get("LIBGL_ALWAYS_SOFTWARE", "(default)"))

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
            runtime_dir=runtime_dir, bare_x11=bare_x11,
            log_level=log_level)

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
    # On bare X11 the DRI3 page-flipping can also fail silently, so if windowed
    # also fails, escalate to full software rendering.
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
        # If windowed also failed, try software rendering as a last resort.
        if ret != 0 and ret not in (-signal.SIGTERM, -signal.SIGINT, -signal.SIGHUP):
            strategy2 = retry_strategy("x11", ret, time.monotonic() - started, True, retry_env)
            if strategy2 and strategy2[0] == "software":
                logger.warning("Windowed retry also failed; escalating to software rendering")
                sw_env = dict(retry_env)
                sw_env["LIBGL_ALWAYS_SOFTWARE"] = "1"
                sw_env.pop("LIBGL_DRI3_DISABLE", None)
                sw_cmd = make_command(windowed=True, size=None)
                logger.info("Retrying (software): %s", " ".join(sw_cmd))
                ret = _run_mgba(sw_cmd, sw_env, **retry_kwargs)
    elif strategy and strategy[0] == "software":
        logger.warning("mGBA failed early; retrying with full software rendering")
        sw_env = dict(env)
        sw_env["LIBGL_ALWAYS_SOFTWARE"] = "1"
        sw_env.pop("LIBGL_DRI3_DISABLE", None)
        sw_cmd = make_command()
        logger.info("Retrying (software): %s", " ".join(sw_cmd))
        ret = _run_mgba(sw_cmd, sw_env, **run_kwargs)
    elif strategy and strategy[0] == "card":
        retry_env = dict(env)
        retry_env["SDL_VIDEO_KMSDRM_DEVICE"] = strategy[1]
        logger.warning("mGBA failed early on %s; retrying on %s",
                       env.get("SDL_VIDEO_KMSDRM_DEVICE") or "default", strategy[1])
        ret = _run_mgba(cmd, retry_env, **run_kwargs)

    # Stage 4 of the log: closing. mGBA's own audio-shutdown and surface
    # cleanup lines arrive through the log pump above; what the adapter owns is
    # durability, so that is what gets recorded and verified here.
    logger.info("Teardown: exit status %d after %.1fs total; flushing saves",
                ret, time.monotonic() - started)
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
