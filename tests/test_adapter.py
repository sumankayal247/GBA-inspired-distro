"""
Contract Tests for GBA OS mGBA Adapter (Rules H1, H5, SV1-SV4).
"""

import os
import sys
import unittest
import tempfile
import shutil

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))
import common
from gba_adapter_mgba import (
    sync_directory,
    backup_saves,
    restore_backup_if_needed,
    build_mgba_command,
    find_mgba_binary,
    InputMonitor,
)


class TestAdapterContract(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.save_dir = os.path.join(self.test_dir, "saves", "entry1")
        os.makedirs(self.save_dir, exist_ok=True)

        self.save_file = os.path.join(self.save_dir, "game.sav")
        with open(self.save_file, "wb") as f:
            f.write(b"SAMPLE_SAVE_STATE_DATA")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_sv2_sync_directory(self):
        """SV2: Adapter must fsync save files and save directory without error."""
        try:
            sync_directory(self.save_dir)
        except Exception as e:
            self.fail(f"sync_directory raised exception: {e}")

    def test_sync_directory_tolerates_missing_dir(self):
        sync_directory(os.path.join(self.test_dir, "does-not-exist"))

    def test_sv3_backup_created_before_play(self):
        """SV3/SV4: a last-known-good copy exists before the session starts."""
        copied = backup_saves(self.save_dir)
        self.assertEqual(copied, 1)
        backup = self.save_file + ".bak"
        self.assertTrue(os.path.exists(backup))
        with open(backup, "rb") as f:
            self.assertEqual(f.read(), b"SAMPLE_SAVE_STATE_DATA")

        # A second run with an unchanged save does not churn the disk.
        self.assertEqual(backup_saves(self.save_dir), 0)

    def test_sv4_torn_write_is_recovered_from_backup(self):
        """SV4: a truncated/zeroed save is restored from the backup."""
        backup_saves(self.save_dir)
        with open(self.save_file, "wb") as f:
            f.truncate(0)

        self.assertTrue(restore_backup_if_needed(self.save_dir))
        with open(self.save_file, "rb") as f:
            self.assertEqual(f.read(), b"SAMPLE_SAVE_STATE_DATA")

    def test_restore_is_noop_when_save_is_healthy(self):
        backup_saves(self.save_dir)
        self.assertFalse(restore_backup_if_needed(self.save_dir))

    def test_sv1_save_routing_flags(self):
        """SV1: mGBA is told to route saves/states/screenshots to SYS_SAVE_DIR."""
        cmd = build_mgba_command("/usr/bin/mgba", "/games/x.gba", self.save_dir,
                                 windowed=False, display_scaling="integer")
        joined = " ".join(cmd)
        self.assertIn(f"savegamePath={self.save_dir}", joined)
        self.assertIn(f"savestatePath={self.save_dir}", joined)
        self.assertIn(f"screenshotPath={self.save_dir}", joined)
        self.assertIn("-f", cmd)
        self.assertIn("lockIntegerScaling=1", joined)
        self.assertEqual(cmd[-1], "/games/x.gba")

    def test_d2_scaling_preference_reaches_emulator(self):
        cmd = build_mgba_command("/usr/bin/mgba", "/games/x.gba", self.save_dir,
                                 windowed=False, display_scaling="stretch")
        self.assertIn("lockIntegerScaling=0", " ".join(cmd))

        windowed = build_mgba_command("/usr/bin/mgba", "/games/x.gba", self.save_dir,
                                      windowed=True, display_scaling="integer")
        self.assertNotIn("-f", windowed)
        self.assertIn("-3", windowed)

    def test_h1_shared_input_profile_written_into_config_dir(self):
        """I3/H5: the emulator config is derived from the shared profile."""
        config_dir = os.path.join(self.test_dir, "config", "handlers", "mgba-adapter")
        home = tempfile.mkdtemp()
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = home
        try:
            bindings = dict(common.DEFAULT_KEYBINDINGS)
            bindings["keyA"] = 113  # F2
            common.save_keybindings(bindings)

            from gba_adapter_mgba import ensure_mgba_config
            ensure_mgba_config(config_dir)

            with open(os.path.join(config_dir, "config.ini"), "r") as f:
                content = f.read()
            self.assertIn("keyA=113", content)
            self.assertIn("fullscreen=1", content)
            # Display scaling is honoured too.
            common.save_display_config({"scaling": "stretch"})
            ensure_mgba_config(config_dir)
            with open(os.path.join(config_dir, "config.ini"), "r") as f:
                self.assertIn("lockIntegerScaling=0", f.read())
        finally:
            if old_home is not None:
                os.environ["HOME"] = old_home
            shutil.rmtree(home, ignore_errors=True)

    def test_find_mgba_binary_or_clear_error(self):
        try:
            path = find_mgba_binary()
            self.assertTrue(os.access(path, os.X_OK))
        except FileNotFoundError as exc:
            self.assertIn("mGBA", str(exc))

    def test_input_monitor_only_watches_exit_gestures(self):
        """I1/S4: the monitor is harmless without devices and stoppable."""
        class DummyProc:
            def poll(self):
                return None

            def terminate(self):
                raise AssertionError("must not terminate without an exit gesture")

        monitor = InputMonitor(DummyProc(), escape_keys={1})
        monitor.start()
        monitor.stop()
        monitor.join(timeout=2)
        self.assertFalse(monitor.is_alive())


if __name__ == "__main__":
    unittest.main()
