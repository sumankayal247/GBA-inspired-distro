#!/usr/bin/env python3
"""
mGBA Handler Adapter for GBA OS.
Implements Rules H1-H9, SV1-SV5 from CONTRACT.md.
Wraps mgba-sdl as an isolated handler process.
"""

import os
import sys
import signal
import subprocess
import time
import logging

logging.basicConfig(level=logging.INFO, format="[adapter] %(message)s")
logger = logging.getLogger("gba.adapter")

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

    mgba_bin = find_mgba_binary()

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
    child = subprocess.Popen(cmd)

    def handle_signal(sig, frame):
        logger.info("Adapter received signal %d, forwarding to child %d", sig, child.pid)
        try:
            child.send_signal(sig)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    ret = child.wait()
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
