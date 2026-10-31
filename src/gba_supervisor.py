#!/usr/bin/env python3
"""
GBA OS System Supervisor.
Implements Rules S1-S9, H1-H10 from CONTRACT.md.
Supervises foreground sessions with strict single-owner semantics and crash-loop recovery.
"""

import os
import sys
import time
import json
import signal
import subprocess
import logging

# Ensure local src directory is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from typing import Optional, Dict, Any, Tuple
from common import (
    get_base_dir,
    get_data_dir,
    get_games_dir,
    get_run_dir,
    SYS_CONTRACT_VERSION,
)

logging.basicConfig(
    level=logging.INFO,
    format="[supervisor %(asctime)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("gba.supervisor")

# Contract constants
GRACE_PERIOD_SECONDS = 3.0
CRASH_WINDOW_SECONDS = 30.0
MAX_CONSECUTIVE_CRASHES = 3

EXIT_CODE_LAUNCH_REQUEST = 10
EXIT_CODE_REBOOT = 100
EXIT_CODE_POWEROFF = 101
EXIT_CODE_RECOVERY = 102

class Supervisor:
    def __init__(self, is_desktop_mode: bool = False):
        self.is_desktop_mode = is_desktop_mode
        self.base_dir = get_base_dir()
        self.data_dir = get_data_dir()
        self.games_dir = get_games_dir()
        self.run_dir = get_run_dir()
        
        self.launch_request_file = os.path.join(self.run_dir, "launch_request.json")
        self.outcome_file = os.path.join(self.run_dir, "last_outcome.json")
        
        self.last_entry_id: Optional[str] = None
        self.last_outcome: str = "clean"
        
        # Crash tracking for launcher
        self.crash_timestamps = []
        self.is_running = True

        # Signal handlers for supervisor itself
        signal.signal(signal.SIGINT, self._handle_supervisor_signal)
        signal.signal(signal.SIGTERM, self._handle_supervisor_signal)

    def _handle_supervisor_signal(self, signum, frame):
        logger.info("Supervisor received shutdown signal %d", signum)
        self.is_running = False

    def run_launcher(self) -> Tuple[int, Optional[Dict[str, Any]]]:
        """Runs the foreground launcher and waits for its exit (D-004, S2)."""
        launcher_script = os.path.join(self.base_dir, "src", "gba_launcher.py")
        if not os.path.exists(launcher_script):
            launcher_script = os.path.join(os.path.dirname(__file__), "gba_launcher.py")
        if not os.path.exists(launcher_script):
            launcher_script = os.path.join(os.path.dirname(__file__), "gba-launcher")
        if not os.path.exists(launcher_script):
            launcher_script = os.path.join(self.base_dir, "bin", "gba-launcher")

        cmd = [sys.executable, launcher_script]
        if self.last_entry_id:
            cmd.extend(["--focus", self.last_entry_id])
        if self.last_outcome:
            cmd.extend(["--outcome", self.last_outcome])
        if self.is_desktop_mode:
            cmd.append("--windowed")

        # Clean out any old launch request
        if os.path.exists(self.launch_request_file):
            try:
                os.remove(self.launch_request_file)
            except Exception:
                pass

        logger.info("Starting launcher foreground session...")
        proc = subprocess.Popen(cmd)
        ret = proc.wait()
        logger.info("Launcher exited with status code: %d", ret)

        launch_data = None
        if ret == EXIT_CODE_LAUNCH_REQUEST:
            if os.path.exists(self.launch_request_file):
                try:
                    with open(self.launch_request_file, "r", encoding="utf-8") as f:
                        launch_data = json.load(f)
                except Exception as e:
                    logger.error("Failed to read launch request: %s", e)

        return ret, launch_data

    def run_handler(self, req: Dict[str, Any]) -> str:
        """
        Runs the handler foreground session with strict environment and lifecycle classification.
        Implements Rules H1, H2, H7, H8, H10, S2, S3, S6.
        """
        entry_id = req.get("entry_id", "unknown")
        content_path = req.get("content_path", "")
        handler_name = req.get("handler", "mgba-adapter")
        
        self.last_entry_id = entry_id

        # Setup standard handler paths (Rule H1)
        save_dir = os.path.join(self.data_dir, "saves", entry_id)
        handler_data_dir = os.path.join(self.data_dir, "handlers", handler_name)
        handler_config_dir = os.path.join(self.data_dir, "config", "handlers", handler_name)

        os.makedirs(save_dir, exist_ok=True)
        os.makedirs(handler_data_dir, exist_ok=True)
        os.makedirs(handler_config_dir, exist_ok=True)

        # Locate handler binary or script
        handler_bin = os.path.join(self.base_dir, "src", "gba_adapter_mgba.py")
        if not os.path.exists(handler_bin):
            handler_bin = os.path.join(os.path.dirname(__file__), "gba_adapter_mgba.py")

        env = os.environ.copy()
        env.update({
            "SYS_CONTRACT": str(SYS_CONTRACT_VERSION),
            "SYS_ENTRY_ID": entry_id,
            "SYS_CONTENT_PATH": content_path,
            "SYS_SAVE_DIR": save_dir,
            "SYS_DATA_DIR": handler_data_dir,
            "SYS_CONFIG_DIR": handler_config_dir,
            "GBA_WINDOWED": "1" if self.is_desktop_mode else "0"
        })

        if not os.path.exists(content_path):
            logger.error("Content not found: %s", content_path)
            return "launch-failure"

        cmd = [sys.executable, handler_bin]
        logger.info("Starting handler: %s for entry %s", handler_name, entry_id)

        try:
            proc = subprocess.Popen(cmd, env=env)
        except Exception as e:
            logger.error("Failed to start handler process: %s", e)
            return "launch-failure"

        termination_requested = False
        outcome = "unknown"

        # Wait for handler with graceful termination watchdog
        try:
            ret = proc.wait()
            if ret == 0:
                outcome = "clean"
            elif ret > 0:
                outcome = "error"
            else:
                # Killed by signal without supervisor request
                outcome = "crash"
        except KeyboardInterrupt:
            logger.info("Supervisor requested termination of handler (SIGTERM)...")
            termination_requested = True
            try:
                proc.terminate()
                ret = proc.wait(timeout=GRACE_PERIOD_SECONDS)
                if ret == 0:
                    outcome = "user-exit"
                elif ret > 0:
                    outcome = "error"
                else:
                    outcome = "terminated"
            except subprocess.TimeoutExpired:
                logger.warning("Handler hung after GRACE (%s s). Escalating to SIGKILL.", GRACE_PERIOD_SECONDS)
                proc.kill()
                proc.wait()
                outcome = "hung"

        # S6: Safeguard filesystem sync
        try:
            os.sync()
        except Exception:
            pass

        logger.info("Handler session completed. Classified outcome: %s", outcome)
        return outcome

    def launch_recovery(self) -> None:
        """Invokes text-based recovery shell (F1, F2)."""
        logger.warning("Entering GBA OS Emergency Recovery Environment...")
        recovery_script = os.path.join(self.base_dir, "src", "gba_recovery.py")
        if not os.path.exists(recovery_script):
            recovery_script = os.path.join(os.path.dirname(__file__), "gba_recovery.py")
        if os.path.exists(recovery_script):
            subprocess.run([sys.executable, recovery_script])
        else:
            subprocess.run(["/bin/bash"])

    def main_loop(self) -> None:
        """Main supervisor loop adhering to Rule S7."""
        logger.info("GBA OS Supervisor starting. Contract Version: %d", SYS_CONTRACT_VERSION)

        while self.is_running:
            ret, launch_req = self.run_launcher()

            if ret == EXIT_CODE_LAUNCH_REQUEST and launch_req:
                # Reset crash counter on successful user interaction
                self.crash_timestamps.clear()
                self.last_outcome = self.run_handler(launch_req)

            elif ret == EXIT_CODE_REBOOT:
                logger.info("User requested system reboot.")
                if self.is_desktop_mode:
                    print("Reboot requested (simulated in desktop mode).")
                    break
                else:
                    os.system("reboot")
                    break

            elif ret == EXIT_CODE_POWEROFF:
                logger.info("User requested poweroff.")
                if self.is_desktop_mode:
                    print("Poweroff requested (simulated in desktop mode).")
                    break
                else:
                    os.system("poweroff")
                    break

            elif ret == EXIT_CODE_RECOVERY:
                self.launch_recovery()

            elif ret == 0:
                # Clean exit (e.g. user quit in desktop mode)
                logger.info("Launcher exited cleanly.")
                if self.is_desktop_mode:
                    break
                # In appliance mode, restart launcher
                self.last_outcome = "clean"

            else:
                # Crash or non-standard exit
                now = time.time()
                self.crash_timestamps.append(now)
                self.crash_timestamps = [t for t in self.crash_timestamps if now - t <= CRASH_WINDOW_SECONDS]
                
                logger.warning(
                    "Launcher exited with error code %d (%d crashes in window)",
                    ret, len(self.crash_timestamps)
                )
                self.last_outcome = "error"

                if len(self.crash_timestamps) >= MAX_CONSECUTIVE_CRASHES:
                    logger.critical("Crash threshold exceeded! Activating recovery path (Rule S7).")
                    self.launch_recovery()
                    self.crash_timestamps.clear()

def main():
    is_desktop = "--desktop" in sys.argv or "--windowed" in sys.argv or "DISPLAY" in os.environ and "--appliance" not in sys.argv
    sup = Supervisor(is_desktop_mode=is_desktop)
    sup.main_loop()

if __name__ == "__main__":
    main()
