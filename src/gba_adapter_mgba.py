#!/usr/bin/env python3
"""
mGBA Handler Adapter for GBA OS.
Implements Rules H1-H9, SV1-SV5 from CONTRACT.md.
Wraps mgba-sdl as an isolated handler process with input monitoring and auto-config.
"""

import os
import sys
import signal
import subprocess
import time
import struct
import select
import threading
import logging

# Ensure local src directory is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import setup_logging
logger = setup_logging("gba.adapter")

def find_mgba_binary() -> str:
    candidates = [
        "/usr/games/mgba",
        "/usr/local/bin/mgba",
        "/usr/bin/mgba"
    ]
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    raise FileNotFoundError("mGBA binary not found in standard paths.")

def ensure_mgba_config() -> None:
    """Ensures mGBA config.ini exists with optimal appliance controls and scaling."""
    home_dir = os.path.expanduser("~")
    config_dir = os.path.join(home_dir, ".config", "mgba")
    config_file = os.path.join(config_dir, "config.ini")

    try:
        os.makedirs(config_dir, exist_ok=True)
        if not os.path.exists(config_file):
            content = """[ports.sdl]
fullscreen=1
lockAspectRatio=1
lockIntegerScaling=0

[input.SDLK]
keyUp=1073741906
keyDown=1073741905
keyLeft=1073741904
keyRight=1073741903
keyA=120
keyB=122
keyL=97
keyR=115
keyStart=13
keySelect=8
"""
            with open(config_file, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info("Initialized default mGBA config at %s", config_file)
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

class InputMonitor(threading.Thread):
    """
    Background input event watcher to ensure Escape key and Gamepad combos
    immediately trigger graceful emulator exit without getting stuck.
    """
    def __init__(self, target_proc: subprocess.Popen):
        super().__init__(daemon=True)
        self.proc = target_proc
        self.running = True

    def stop(self):
        self.running = False

    def run(self):
        fds = {}
        if not os.path.exists("/dev/input"):
            return

        try:
            for dev in os.listdir("/dev/input"):
                if dev.startswith("event") or dev.startswith("js"):
                    path = os.path.join("/dev/input", dev)
                    try:
                        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                        fds[fd] = (dev.startswith("js"), path)
                    except Exception:
                        pass

            if not fds:
                return

            held_buttons = set()
            while self.running and self.proc.poll() is None:
                r, _, _ = select.select(list(fds.keys()), [], [], 0.2)
                for fd in r:
                    is_js, path = fds[fd]
                    try:
                        if is_js:
                            raw = os.read(fd, 8)
                            if len(raw) == 8:
                                t_ms, val, ev_type, num = struct.unpack("IhBB", raw)
                                if ev_type & 0x01: # Joystick button
                                    if val:
                                        held_buttons.add(num)
                                        # Guide/Home (8, 10, 12, 16) or Start+Select (6+7 or 8+9)
                                        if num in (8, 10, 12, 16) or (6 in held_buttons and 7 in held_buttons) or (8 in held_buttons and 9 in held_buttons):
                                            logger.info("Gamepad exit combo triggered! Sending SIGTERM to emulator...")
                                            self.proc.terminate()
                                            return
                                    else:
                                        held_buttons.discard(num)
                        else:
                            raw = os.read(fd, 24)
                            if len(raw) == 24:
                                s, us, ev_t, code, val = struct.unpack("qqHHi", raw)
                                # EV_KEY = 1, KEY_ESC = 1, val = 1 (pressed)
                                if ev_t == 1 and code == 1 and val == 1:
                                    logger.info("Escape key press detected via raw input! Sending SIGTERM to emulator...")
                                    self.proc.terminate()
                                    return
                    except Exception:
                        pass
        finally:
            for fd in list(fds.keys()):
                try:
                    os.close(fd)
                except Exception:
                    pass

def main():
    contract = os.environ.get("SYS_CONTRACT", "1")
    entry_id = os.environ.get("SYS_ENTRY_ID")
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

    os.makedirs(save_dir, exist_ok=True)
    if data_dir:
        os.makedirs(data_dir, exist_ok=True)
    if config_dir:
        os.makedirs(config_dir, exist_ok=True)

    ensure_mgba_config()
    mgba_bin = find_mgba_binary()

    # If running inside an X11 appliance session, ensure SDL_VIDEODRIVER does not force KMSDRM
    if "DISPLAY" in os.environ and os.environ.get("SDL_VIDEODRIVER") == "KMSDRM":
        logger.info("Unsetting SDL_VIDEODRIVER=KMSDRM in X11 environment for mGBA compatibility")
        del os.environ["SDL_VIDEODRIVER"]

    # Determine display mode: fullscreen unless running in desktop test window
    is_windowed = os.environ.get("GBA_WINDOWED", "0") == "1"

    cmd = [
        mgba_bin,
        "-C", f"savegamePath={save_dir}",
        "-C", f"savestatePath={save_dir}",
        "-C", f"screenshotPath={save_dir}",
    ]
    if not is_windowed:
        cmd.append("-f")
    else:
        cmd.extend(["-3"]) # 3x scale in windowed mode

    cmd.append(content_path)
    logger.info("Executing mGBA: %s", " ".join(cmd))

    # Child process management with signal forwarding (Rule H7, H9)
    child = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

    # Spawn hardware input monitor
    monitor = InputMonitor(child)
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

    # Durability: sync save directory
    sync_directory(save_dir)

    # If child died due to a signal, re-raise it per Rule H9
    if ret < 0:
        sig = -ret
        logger.info("Child was terminated by signal %d, re-raising", sig)
        signal.signal(sig, signal.SIG_DFL)
        os.kill(os.getpid(), sig)

    sys.exit(ret)

if __name__ == "__main__":
    main()
