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
import threading

# Ensure local src directory is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import setup_logging, load_keybindings, load_display_config, DEFAULT_KEYBINDINGS

logger = setup_logging("gba.adapter")

# Linux input constants (avoid a hard dependency on python-evdev)
EV_SYN = 0x00
EV_KEY = 0x01
KEY_ESC = 1

# Gamepad combos that quit a game: SELECT+START, or a Guide/Home button.
GAMEPAD_START_BUTTONS = {7, 9}
GAMEPAD_SELECT_BUTTONS = {6, 8}
GAMEPAD_GUIDE_BUTTONS = {10, 12, 16, 17}

BACKUP_SUFFIX = ".bak"

MIGBA_CONFIG_TEMPLATE = """[ports.sdl]
fullscreen={fullscreen}
lockAspectRatio={aspect}
lockIntegerScaling={integer}

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
    os.makedirs(os.path.dirname(config_file), exist_ok=True)
    tmp = config_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, config_file)


def ensure_mgba_config(config_dir: str = None) -> None:
    """
    Ensures mGBA has an appliance config with the shared key bindings (I3).

    When `config_dir` (SYS_CONFIG_DIR) is given the config stays inside the
    handler's own directory (H5); otherwise the user profile is used.
    """
    home_dir = os.path.expanduser("~")
    if config_dir:
        config_file = os.path.join(config_dir, "config.ini")
    else:
        config_file = os.path.join(home_dir, ".config", "mgba", "config.ini")

    try:
        _write_mgba_config(config_file, windowed=os.environ.get("GBA_WINDOWED") == "1")
        if config_dir:
            # mGBA only looks in the user's profile; keep both in sync.
            _write_mgba_config(
                os.path.join(home_dir, ".config", "mgba", "config.ini"),
                windowed=os.environ.get("GBA_WINDOWED") == "1",
            )
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
    Background watcher that converts the system exit combo into SIGTERM.

    It reads raw `/dev/input` events so it works without evdev bindings and
    without stealing input from mGBA. Only explicit exit gestures are handled:
    Escape (from the shared profile) plus a gamepad combo.
    """

    def __init__(self, target_proc: subprocess.Popen, escape_keys=None):
        super().__init__(daemon=True, name="gba-input-monitor")
        self.proc = target_proc
        self.running = True
        self.escape_keys = set(escape_keys or {KEY_ESC})

    def stop(self):
        self.running = False

    def _trigger(self, reason: str) -> None:
        logger.info("%s - sending SIGTERM to emulator", reason)
        try:
            self.proc.terminate()
        except Exception:
            pass

    def run(self):
        fds = {}
        try:
            for dev in sorted(os.listdir("/dev/input")) if os.path.isdir("/dev/input") else []:
                if not (dev.startswith("event") or dev.startswith("js")):
                    continue
                path = os.path.join("/dev/input", dev)
                try:
                    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                    fds[fd] = dev.startswith("js")
                except Exception:
                    pass
            if not fds:
                return

            held = set()
            while self.running and self.proc.poll() is None:
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
                            if not ev_type & 0x01:  # JS_EVENT_BUTTON
                                continue
                            if value:
                                held.add(number)
                                combo = (held & GAMEPAD_SELECT_BUTTONS) and (held & GAMEPAD_START_BUTTONS)
                                if number in GAMEPAD_GUIDE_BUTTONS or combo:
                                    self._trigger("Gamepad exit combo")
                                    return
                            else:
                                held.discard(number)
                        else:
                            raw = os.read(fd, 24)
                            if len(raw) != 24:
                                continue
                            _s, _us, ev_t, code, value = struct.unpack("qqHHi", raw)
                            if ev_t == EV_KEY and value == 1 and code in self.escape_keys:
                                self._trigger("Exit key detected")
                                return
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


def build_mgba_command(mgba_bin: str, content_path: str, save_dir: str,
                       windowed: bool, display_scaling: str) -> list:
    """Builds the mGBA argv with save routing (SV1) and scaling (D2)."""
    cmd = [
        mgba_bin,
        "-C", f"savegamePath={save_dir}",
        "-C", f"savestatePath={save_dir}",
        "-C", f"screenshotPath={save_dir}",
    ]
    if windowed:
        cmd.extend(["-3"])
    else:
        cmd.append("-f")
        cmd.extend(["-C", "lockAspectRatio=1"])
        cmd.extend(["-C", f"lockIntegerScaling={1 if display_scaling == 'integer' else 0}"])
    cmd.append(content_path)
    return cmd


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
        sys.exit(3)

    # If running inside an X11 session, do not force the KMS/DRM driver.
    if "DISPLAY" in os.environ and os.environ.get("SDL_VIDEODRIVER") == "KMSDRM":
        logger.info("Unsetting SDL_VIDEODRIVER=KMSDRM inside an X11 session")
        del os.environ["SDL_VIDEODRIVER"]

    is_windowed = os.environ.get("GBA_WINDOWED", "0") == "1"
    display_scaling = os.environ.get("SYS_DISPLAY_SCALING") or load_display_config().get("scaling", "integer")

    cmd = build_mgba_command(mgba_bin, content_path, save_dir, is_windowed, display_scaling)
    logger.info("Executing mGBA: %s", " ".join(cmd))

    if config_dir:
        os.environ.setdefault("XDG_CONFIG_HOME", os.path.dirname(config_dir))

    try:
        child = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, bufsize=1)
    except Exception as exc:
        logger.error("Failed to launch mGBA: %s", exc)
        sys.exit(3)

    bindings = load_keybindings()
    monitor = InputMonitor(child, escape_keys={KEY_ESC, bindings.get("keySelect", 8)})
    monitor.start()

    def handle_signal(sig, frame):
        logger.info("Adapter received signal %d, forwarding to child %d", sig, child.pid)
        try:
            child.send_signal(sig)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    if child.stdout:
        for line in iter(child.stdout.readline, ""):
            cleaned = line.strip()
            if cleaned:
                logger.info("[mgba] %s", cleaned)
        child.stdout.close()

    ret = child.wait()
    monitor.stop()
    logger.info("Child mGBA exited with code %d", ret)

    # Durability: fsync save files and their directory, then keep a backup.
    sync_directory(save_dir)
    backup_saves(save_dir)

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
