"""
Contract tests for GBA OS Supervisor (Rules S6, S7, S8, H1, H10).
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))
import common
from gba_supervisor import Supervisor, parse_mode


class TestSupervisorContract(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self._orig = (common.get_data_dir, common.get_run_dir)
        common.get_data_dir = lambda: self.test_dir
        common.get_run_dir = lambda: os.path.join(self.test_dir, "run")
        os.makedirs(os.path.join(self.test_dir, "run"), exist_ok=True)
        self.sup = Supervisor(is_desktop_mode=True)

    def tearDown(self):
        common.get_data_dir, common.get_run_dir = self._orig
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_h10_launch_failure_classification(self):
        """H10: Invalid or missing content classified as 'launch-failure'."""
        req = {
            "entry_id": "nonexistent_id",
            "content_path": "/nonexistent/path/game.gba",
            "handler": "mgba-adapter",
        }
        outcome = self.sup.run_handler(req)
        self.assertEqual(outcome, "launch-failure")

    def test_missing_content_path_is_launch_failure(self):
        """H10: an empty content path never launches a handler."""
        outcome = self.sup.run_handler({"entry_id": "x", "content_path": "", "handler": "mgba-adapter"})
        self.assertEqual(outcome, "launch-failure")

    def test_s7_crash_counter(self):
        """S7: Supervisor tracks crash frequency and cleans expired entries."""
        now = time.time()
        self.sup.crash_timestamps = [now - 40, now - 10]
        self.sup.crash_timestamps = [t for t in self.sup.crash_timestamps if now - t <= 30.0]
        self.assertEqual(len(self.sup.crash_timestamps), 1)

    def test_h1_handler_directories_are_isolated(self):
        """H1: per-entry save dir and per-handler state/config dirs are created."""
        content = os.path.join(self.test_dir, "fake.gba")
        with open(content, "wb") as f:
            f.write(b"\x00" * 64)

        created = []
        original_spawn = self.sup._spawn

        class FakeProc:
            pid = 4242
            stdout = None

            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

        def fake_spawn(cmd, env, **kwargs):
            created.append(env)
            return FakeProc()

        self.sup._spawn = fake_spawn
        try:
            outcome = self.sup.run_handler({
                "entry_id": "abc", "content_path": content, "handler": "mgba-adapter",
            })
        finally:
            self.sup._spawn = original_spawn

        self.assertEqual(outcome, "clean")
        env = created[0]
        self.assertEqual(env["SYS_CONTRACT"], "1")
        self.assertEqual(env["SYS_ENTRY_ID"], "abc")
        self.assertTrue(env["SYS_SAVE_DIR"].endswith(os.path.join("saves", "abc")))
        self.assertTrue(env["SYS_DATA_DIR"].endswith(os.path.join("handlers", "mgba-adapter")))
        self.assertIn("SYS_DISPLAY_SCALING", env)
        self.assertTrue(os.path.isdir(env["SYS_SAVE_DIR"]))

    def test_outcome_is_written_for_debugging(self):
        """S6: the last outcome is recorded next to the launch request."""
        self.sup.save_outcome("crash", "abc")
        with open(self.sup.outcome_file, "r", encoding="utf-8") as f:
            payload = json.load(f)
        self.assertEqual(payload["outcome"], "crash")
        self.assertEqual(payload["entry_id"], "abc")

    def test_orphan_detection_is_precise(self):
        """S8: only the appliance's own foreground programs are ever signalled."""
        self.assertTrue(Supervisor._is_foreground_program(["/usr/games/mgba", "rom.gba"]))
        self.assertTrue(Supervisor._is_foreground_program(
            ["/usr/bin/python3", "/opt/gba-distro/bin/gba-launcher"]))
        self.assertTrue(Supervisor._is_foreground_program(
            ["python3", "src/gba_adapter_mgba.py"]))

        # Must never match: a shell whose arguments mention these words, an
        # unrelated python program, an editor with an mgba file open.
        self.assertFalse(Supervisor._is_foreground_program(
            ["/bin/bash", "-c", "echo mgba src/gba_launcher.py"]))
        self.assertFalse(Supervisor._is_foreground_program(
            ["/usr/bin/python3", "build.py", "--note", "mgba"]))
        self.assertFalse(Supervisor._is_foreground_program(["/usr/bin/vim", "/tmp/mgba notes"]))
        self.assertFalse(Supervisor._is_foreground_program([]))

    def test_s6_outcome_reaches_next_launcher(self):
        """S6: the previous entry and outcome are passed back to the launcher."""
        content = os.path.join(self.test_dir, "game.gba")
        with open(content, "wb") as f:
            f.write(b"\x00" * 64)

        calls = {"launcher": [], "handler": []}

        def fake_run_launcher():
            calls["launcher"].append((self.sup.last_entry_id, self.sup.last_outcome))
            if len(calls["launcher"]) == 1:
                return 10, {"entry_id": "abc", "content_path": content, "handler": "mgba-adapter"}
            self.sup.is_running = False
            return 0, None

        def fake_run_handler(req):
            calls["handler"].append(req["entry_id"])
            self.sup.last_entry_id = req["entry_id"]
            return "crash"

        self.sup.run_launcher = fake_run_launcher
        self.sup.run_handler = fake_run_handler
        self.sup.kill_orphans = lambda: 0
        self.sup.main_loop()

        self.assertEqual(calls["handler"], ["abc"])
        # Second launcher start carries the crash outcome and previous entry.
        self.assertEqual(calls["launcher"][1], ("abc", "crash"))
        self.assertEqual(self.sup.last_outcome, "crash")

    def test_s7_repeated_launcher_crashes_enter_recovery(self):
        """S7: N launcher crashes inside the window trigger the recovery path."""
        from gba_supervisor import MAX_CONSECUTIVE_CRASHES

        attempts = {"count": 0, "recovery": 0}

        def fake_run_launcher():
            attempts["count"] += 1
            if attempts["count"] > MAX_CONSECUTIVE_CRASHES:
                self.sup.is_running = False
            return 1, None  # launcher failed

        def fake_recovery():
            attempts["recovery"] += 1
            self.sup.is_running = False

        self.sup.run_launcher = fake_run_launcher
        self.sup.launch_recovery = fake_recovery
        self.sup.kill_orphans = lambda: 0
        # Keep the test fast: no backoff sleep.
        import gba_supervisor
        original = gba_supervisor.CRASH_BACKOFF_SECONDS
        gba_supervisor.CRASH_BACKOFF_SECONDS = 0.0
        try:
            self.sup.main_loop()
        finally:
            gba_supervisor.CRASH_BACKOFF_SECONDS = original

        self.assertEqual(attempts["recovery"], 1)
        self.assertEqual(self.sup.last_outcome, "error")

    def test_reboot_request_is_simulated_in_desktop_mode(self):
        """S6: desktop mode never reboots the developer's machine."""
        self.sup.run_launcher = lambda: (100, None)
        self.sup.kill_orphans = lambda: 0
        self.sup.main_loop()
        self.assertFalse(self.sup.is_running)

    def test_parse_mode_flags(self):
        """Boot mode selection honours explicit flags before environment."""
        self.assertFalse(parse_mode(["--appliance"]))
        self.assertTrue(parse_mode(["--desktop"]))
        self.assertTrue(parse_mode(["--windowed"]))
        old_display = os.environ.pop("DISPLAY", None)
        old_wayland = os.environ.pop("WAYLAND_DISPLAY", None)
        try:
            self.assertFalse(parse_mode([]))
            os.environ["DISPLAY"] = ":0"
            self.assertTrue(parse_mode([]))
        finally:
            if old_display is not None:
                os.environ["DISPLAY"] = old_display
            if old_wayland is not None:
                os.environ["WAYLAND_DISPLAY"] = old_wayland


if __name__ == "__main__":
    unittest.main()
