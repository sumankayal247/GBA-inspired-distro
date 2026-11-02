#!/usr/bin/env python3
"""
GBA OS System Supervisor.

Implements Rules S1-S9, H1-H10 from CONTRACT.md.
Supervises foreground sessions with strict single-owner semantics and crash-loop
recovery.

Run modes
---------
* `--appliance` (default when no DISPLAY): fullscreen shell, real reboot/poweroff.
* `--desktop` / `--windowed`: windowed shell for testing inside a desktop session;
  reboot and poweroff are simulated and quitting the shell stops the supervisor.
"""

import os
import sys
import time
import json
import signal
import subprocess
import threading

# Ensure local src directory is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from typing import Optional, Dict, Any, Tuple
from common import (
    get_base_dir,
    get_data_dir,
    get_games_dir,
    get_run_dir,
    get_current_log_file,
    setup_logging,
    prune_old_logs,
    load_display_config,
    SYS_CONTRACT_VERSION,
    SYS_SAVE_DIR_NAME,
    SYS_HANDLERS_DIR_NAME,
    EXIT_CODE_LAUNCH_REQUEST,
    EXIT_CODE_REBOOT,
    EXIT_CODE_POWEROFF,
    EXIT_CODE_MISSING_DEPS,
    EXIT_CODE_RECOVERY,
)

logger = setup_logging("gba.supervisor")

# Contract constants (S7 parameters: N crashes within M seconds -> recovery)
GRACE_PERIOD_SECONDS = 3.0
CRASH_WINDOW_SECONDS = 30.0
MAX_CONSECUTIVE_CRASHES = 3

# Backoff applied before respawning a crashing launcher, so a broken shell does
# not spin the CPU or flood the log.
CRASH_BACKOFF_SECONDS = 0.5

# Foreground programs that must not survive a supervisor restart (Rule S8).
# Matching is by executable/script *basename* against this exact list, never by
# substring, so unrelated processes whose arguments merely mention a ROM or a
# path containing these words are never touched.
ORPHAN_EXECUTABLES = {
    "mgba", "mgba-sdl", "mgba-qt",
    "gba-launcher", "gba_launcher.py", "gba-launcher.py",
    "gba_adapter_mgba.py", "mgba-adapter",
}
PYTHON_EXECUTABLES = {"python", "python3", "python3.8", "python3.9", "python3.10",
                      "python3.11", "python3.12", "python3.13"}

# Outcomes the launcher can present to the user (Rule S6, H10).
OUTCOME_CLEAN = "clean"
OUTCOME_ERROR = "error"
OUTCOME_CRASH = "crash"
OUTCOME_HUNG = "hung"
OUTCOME_TERMINATED = "terminated"
OUTCOME_USER_EXIT = "user-exit"
OUTCOME_LAUNCH_FAILURE = "launch-failure"


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
        self.last_outcome: str = OUTCOME_CLEAN
        
        # Crash tracking for launcher
        self.crash_timestamps = []
        self.is_running = True
        self.active_child: Optional[subprocess.Popen] = None

        # Signal handlers for supervisor itself
        signal.signal(signal.SIGINT, self._handle_supervisor_signal)
        signal.signal(signal.SIGTERM, self._handle_supervisor_signal)

    def _handle_supervisor_signal(self, signum, frame):
        """
        Requests shutdown and stops the foreground child.

        The handler must not block: the main thread may already be inside
        `Popen.wait()` for that very child, and re-entering it here would
        deadlock. Escalation to SIGKILL happens on a helper thread.
        """
        logger.info("Supervisor received shutdown signal %d", signum)
        self.is_running = False
        child = self.active_child
        if child is None or child.poll() is not None:
            return
        logger.info("Terminating active foreground session %d", child.pid)
        try:
            child.terminate()
        except Exception:
            return
        threading.Thread(target=self._escalate_termination, args=(child,),
                         daemon=True, name="gba-terminate").start()

    @staticmethod
    def _escalate_termination(child) -> None:
        try:
            child.wait(timeout=GRACE_PERIOD_SECONDS)
        except Exception:
            logger.warning("Foreground session ignored SIGTERM for %ss; killing it.",
                           GRACE_PERIOD_SECONDS)
            try:
                child.kill()
            except Exception:
                pass

    # ------------------------------------------------------------- helpers
    def _spawn(self, cmd, env, **kwargs) -> subprocess.Popen:
        """
        Popen wrapper so a Ctrl-C in a terminal or a PID-1 restart signal hits
        the supervisor, never the foreground child (they share the process
        group; S2/S3/S8).
        """
        # A new session detaches the child from this terminal's process group,
        # so Ctrl-C at the console stops the supervisor, never the foreground
        # session directly (S2/S3). The supervisor then shuts the child down.
        preexec = os.setsid if hasattr(os, "setsid") else None
        if preexec is not None:
            try:
                proc = subprocess.Popen(cmd, env=env, preexec_fn=preexec, **kwargs)
            except Exception:
                proc = subprocess.Popen(cmd, env=env, **kwargs)
        else:
            proc = subprocess.Popen(cmd, env=env, **kwargs)
        self.active_child = proc
        return proc

    def _release_child(self) -> None:
        self.active_child = None

    def _script_path(self, *candidates) -> Optional[str]:
        for candidate in candidates:
            if candidate and os.path.exists(candidate):
                return candidate
        return None

    @staticmethod
    def _is_foreground_program(argv) -> bool:
        """
        True only for the appliance's own foreground programs.

        `argv` comes from /proc/<pid>/cmdline. A python interpreter counts only
        when the script it runs is one of ours, so `python3 build.py --note
        mgba` is never matched.
        """
        if not argv:
            return False
        exe = os.path.basename(argv[0])
        if exe in ORPHAN_EXECUTABLES:
            return True
        if exe in PYTHON_EXECUTABLES or exe.startswith("python"):
            for argument in argv[1:]:
                if argument.startswith("-"):
                    continue
                return os.path.basename(argument) in ORPHAN_EXECUTABLES
        return False

    def kill_orphans(self) -> int:
        """
        Terminates leftover foreground programs from a previous supervisor
        instance (Rule S8). Returns the number of processes signalled.
        """
        killed = 0
        me = os.getpid()
        my_uid = os.getuid()
        proc_root = "/proc"
        if not os.path.isdir(proc_root):
            return 0
        for entry in os.listdir(proc_root):
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid == me:
                continue
            base = os.path.join(proc_root, entry)
            try:
                if os.stat(base).st_uid != my_uid:
                    continue
                with open(os.path.join(base, "cmdline"), "rb") as f:
                    argv = [part.decode("utf-8", "ignore")
                            for part in f.read().split(b"\x00") if part]
            except Exception:
                continue
            if not self._is_foreground_program(argv):
                continue
            # Never kill our own launcher child; only pre-existing strays.
            if self.active_child is not None and self.active_child.pid == pid:
                continue
            try:
                os.kill(pid, signal.SIGTERM)
                killed += 1
                logger.warning("Terminated orphaned foreground process %d (%s)",
                               pid, " ".join(argv)[:120])
            except Exception:
                pass
        return killed

    def save_outcome(self, outcome: str, entry_id: Optional[str] = None) -> None:
        """Mirrors the last outcome to the runtime dir (debugging aid, not norm)."""
        try:
            payload = {
                "outcome": outcome,
                "entry_id": entry_id,
                "timestamp": time.time(),
            }
            tmp = self.outcome_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, self.outcome_file)
        except Exception:
            pass

    # -------------------------------------------------------------- launcher
    def run_launcher(self) -> Tuple[int, Optional[Dict[str, Any]]]:
        """Runs the foreground launcher and waits for its exit (D-004, S2)."""
        launcher_script = self._script_path(
            os.path.join(self.base_dir, "bin", "gba-launcher"),
            os.path.join(self.base_dir, "src", "gba_launcher.py"),
            os.path.join(os.path.dirname(__file__), "gba_launcher.py"),
            os.path.join(os.path.dirname(__file__), "gba-launcher"),
        )
        if not launcher_script:
            logger.critical("Launcher not found in the installation directory.")
            return 127, None

        cmd = [sys.executable, launcher_script]
        if self.last_entry_id:
            cmd.extend(["--focus", self.last_entry_id])
        if self.last_outcome:
            cmd.extend(["--outcome", self.last_outcome])
        cmd.append("--windowed" if self.is_desktop_mode else "--appliance")

        # Clean out any stale launch request so a crash cannot replay a launch.
        if os.path.exists(self.launch_request_file):
            try:
                os.remove(self.launch_request_file)
            except Exception:
                pass

        env = os.environ.copy()
        env["GBA_LOG_FILE"] = get_current_log_file()
        env["SYS_CONTRACT"] = str(SYS_CONTRACT_VERSION)
        if self.is_desktop_mode:
            env["GBA_APPLIANCE"] = "0"
        else:
            env["GBA_APPLIANCE"] = "1"
        logger.info("Starting launcher foreground session (cmd: %s)...", " ".join(cmd))

        try:
            proc = self._spawn(cmd, env)
        except Exception as exc:
            logger.error("Failed to start launcher: %s", exc)
            return 127, None

        try:
            ret = proc.wait()
        finally:
            self._release_child()
        logger.info("Launcher exited with status code: %d", ret)

        launch_data = None
        if ret == EXIT_CODE_LAUNCH_REQUEST and os.path.exists(self.launch_request_file):
            try:
                with open(self.launch_request_file, "r", encoding="utf-8") as f:
                    launch_data = json.load(f)
                if not isinstance(launch_data, dict) or not launch_data.get("content_path"):
                    logger.error("Malformed launch request; ignoring.")
                    launch_data = None
            except Exception as e:
                logger.error("Failed to read launch request: %s", e)
                launch_data = None

        return ret, launch_data

    # --------------------------------------------------------------- handler
    def run_handler(self, req: Dict[str, Any]) -> str:
        """
        Runs the handler foreground session with strict environment and
        lifecycle classification (Rules H1, H2, H7, H8, H10, S2, S3, S6).
        """
        entry_id = str(req.get("entry_id") or "unknown")
        content_path = req.get("content_path", "")
        handler_name = req.get("handler") or "mgba-adapter"
        
        self.last_entry_id = entry_id

        # Standard handler paths (Rule H1)
        save_dir = os.path.join(self.data_dir, SYS_SAVE_DIR_NAME, entry_id)
        handler_data_dir = os.path.join(self.data_dir, SYS_HANDLERS_DIR_NAME, handler_name)
        handler_config_dir = os.path.join(self.data_dir, "config", "handlers", handler_name)

        for path in (save_dir, handler_data_dir, handler_config_dir):
            try:
                os.makedirs(path, exist_ok=True)
            except Exception as exc:
                logger.error("Cannot create handler directory %s: %s", path, exc)
                return OUTCOME_LAUNCH_FAILURE

        handler_bin = self._script_path(
            os.path.join(self.base_dir, "handlers", "mgba-adapter"),
            os.path.join(self.base_dir, "src", "gba_adapter_mgba.py"),
            os.path.join(os.path.dirname(__file__), "gba_adapter_mgba.py"),
        )
        if not handler_bin:
            logger.error("No handler executable found for %s", handler_name)
            return OUTCOME_LAUNCH_FAILURE

        if not content_path or not os.path.exists(content_path):
            logger.error("Content not found: %s", content_path)
            return OUTCOME_LAUNCH_FAILURE

        display = load_display_config()
        env = os.environ.copy()
        env.update({
            "SYS_CONTRACT": str(SYS_CONTRACT_VERSION),
            "SYS_ENTRY_ID": entry_id,
            "SYS_CONTENT_PATH": content_path,
            "SYS_SAVE_DIR": save_dir,
            "SYS_DATA_DIR": handler_data_dir,
            "SYS_CONFIG_DIR": handler_config_dir,
            "SYS_DISPLAY_SCALING": display.get("scaling", "integer"),
            "GBA_WINDOWED": "1" if self.is_desktop_mode else "0",
            "GBA_LOG_FILE": get_current_log_file(),
        })

        cmd = [sys.executable, handler_bin]
        logger.info("Starting handler: %s for entry %s (%s)", handler_name, entry_id, content_path)

        try:
            proc = self._spawn(cmd, env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, bufsize=1)
        except Exception as e:
            self._release_child()
            logger.error("Failed to start handler process: %s", e)
            return OUTCOME_LAUNCH_FAILURE

        outcome = "unknown"
        try:
            # Stream handler output into the supervisor log so crashes are
            # diagnosable from recovery (Rule F2 / T-STORE-3).
            if proc.stdout:
                for line in iter(proc.stdout.readline, ""):
                    cleaned = line.strip()
                    if cleaned:
                        logger.info("[handler] %s", cleaned)
                proc.stdout.close()
            ret = proc.wait()
            if ret == 0:
                outcome = OUTCOME_CLEAN
            elif ret > 0:
                outcome = OUTCOME_ERROR
            else:
                outcome = OUTCOME_CRASH
        except KeyboardInterrupt:
            logger.info("Supervisor requested termination of handler (SIGTERM)...")
            try:
                proc.terminate()
                ret = proc.wait(timeout=GRACE_PERIOD_SECONDS)
                outcome = OUTCOME_USER_EXIT
            except subprocess.TimeoutExpired:
                logger.warning("Handler hung after GRACE (%s s). Escalating to SIGKILL.",
                               GRACE_PERIOD_SECONDS)
                proc.kill()
                proc.wait()
                outcome = OUTCOME_HUNG
        finally:
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass
            self._release_child()

        # S6: final safeguard sync (not the durability mechanism, SV7)
        try:
            os.sync()
        except Exception:
            pass

        logger.info("Handler session completed. Classified outcome: %s", outcome)
        self.save_outcome(outcome, entry_id)
        return outcome

    # -------------------------------------------------------------- recovery
    def launch_recovery(self) -> None:
        """Invokes text-based recovery shell (F1, F2)."""
        logger.warning("Entering GBA OS Emergency Recovery Environment...")
        recovery_script = self._script_path(
            os.path.join(self.base_dir, "bin", "gba-recovery.py"),
            os.path.join(self.base_dir, "src", "gba_recovery.py"),
            os.path.join(os.path.dirname(__file__), "gba_recovery.py"),
        )
        if recovery_script:
            subprocess.run([sys.executable, recovery_script])
        else:
            subprocess.run(["/bin/sh"])

    # ------------------------------------------------------------------ loop
    def _system_action(self, action: str) -> None:
        if self.is_desktop_mode:
            logger.info("%s requested (simulated in desktop mode).", action.capitalize())
            self.is_running = False
            return
        logger.info("Executing system %s...", action)
        try:
            os.sync()
        except Exception:
            pass
        os.system(action)

    def main_loop(self) -> None:
        """Main supervisor loop adhering to Rule S7."""
        logger.info("GBA OS Supervisor starting. Contract Version: %d", SYS_CONTRACT_VERSION)
        logger.info("Mode: %s | BASE=%s DATA=%s GAMES=%s",
                    "desktop" if self.is_desktop_mode else "appliance",
                    self.base_dir, self.data_dir, self.games_dir)

        self.kill_orphans()
        prune_old_logs()
        self.save_outcome(OUTCOME_CLEAN)

        while self.is_running:
            ret, launch_req = self.run_launcher()
            if not self.is_running:
                break

            if ret == EXIT_CODE_LAUNCH_REQUEST and launch_req:
                # Reset crash counter on successful user interaction
                self.crash_timestamps.clear()
                self.last_outcome = self.run_handler(launch_req)

            elif ret == EXIT_CODE_REBOOT:
                self._system_action("reboot")
                break

            elif ret == EXIT_CODE_POWEROFF:
                self._system_action("poweroff")
                break

            elif ret == EXIT_CODE_RECOVERY:
                self.launch_recovery()

            elif ret == EXIT_CODE_MISSING_DEPS:
                # A required piece is missing on this machine. Retrying cannot
                # help, and a crash loop would hide the message: recovery tells
                # the user what to install, then returns to the menu.
                logger.critical("Launcher reported missing runtime dependencies; "
                                "entering recovery (Rule F3).")
                self.last_outcome = "missing-deps"
                self.save_outcome("missing-deps")
                self.launch_recovery()

            elif ret == 0:
                logger.info("Launcher exited cleanly.")
                self.last_outcome = OUTCOME_CLEAN
                if self.is_desktop_mode:
                    break
                # In appliance mode a clean exit still returns to the library.

            else:
                now = time.time()
                self.crash_timestamps.append(now)
                self.crash_timestamps = [t for t in self.crash_timestamps
                                         if now - t <= CRASH_WINDOW_SECONDS]
                logger.warning("Launcher exited with error code %d (%d crashes in window)",
                               ret, len(self.crash_timestamps))
                self.last_outcome = "error"
                self.save_outcome("launcher-error")

                if len(self.crash_timestamps) >= MAX_CONSECUTIVE_CRASHES:
                    logger.critical("Crash threshold exceeded! Activating recovery path (Rule S7).")
                    self.launch_recovery()
                    self.crash_timestamps.clear()
                else:
                    time.sleep(CRASH_BACKOFF_SECONDS)

        logger.info("Supervisor loop finished.")


def parse_mode(argv) -> bool:
    """Returns True when the supervisor should run in desktop (windowed) mode."""
    if "--appliance" in argv:
        return False
    if "--desktop" in argv or "--windowed" in argv:
        return True
    # No explicit flag: only use windowed mode when a graphical session exists.
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def main():
    is_desktop = parse_mode(sys.argv[1:])
    sup = Supervisor(is_desktop_mode=is_desktop)
    try:
        sup.main_loop()
    except Exception:
        logger.exception("Supervisor crashed with an unhandled exception")
        raise


if __name__ == "__main__":
    main()
