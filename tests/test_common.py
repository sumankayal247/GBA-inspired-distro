"""
Contract tests for shared helpers in common.py.

Covers Rules ST5 (bounded logs), I3 (one valid input profile), D2 (display
scaling preference) and the launch-history bookkeeping the shell renders.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))
import common


class TestCommonHelpers(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self._orig_data_dir = common.get_data_dir
        common.get_data_dir = lambda: self.test_dir

    def tearDown(self):
        common.get_data_dir = self._orig_data_dir
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_sanitize_rejects_reserved_and_duplicate_keys(self):
        """I3: a hand-edited config can never leave the shell without controls."""
        bindings = dict(common.DEFAULT_KEYBINDINGS)
        bindings["keyA"] = 27            # reserved (Escape)
        bindings["keyB"] = bindings["keyL"]  # duplicate of L
        result = common.sanitize_keybindings(bindings)

        self.assertEqual(result["keyA"], common.DEFAULT_KEYBINDINGS["keyA"])
        self.assertEqual(result["keyB"], common.DEFAULT_KEYBINDINGS["keyB"])
        self.assertEqual(len(set(result.values())), len(result))

    def test_conflict_reports_reason(self):
        self.assertIn("reserved", common.keybindings_conflict_error(
            common.DEFAULT_KEYBINDINGS, "keyA", 27))
        self.assertIn("Already assigned", common.keybindings_conflict_error(
            common.DEFAULT_KEYBINDINGS, "keyA", common.DEFAULT_KEYBINDINGS["keyB"]))
        self.assertIsNone(common.keybindings_conflict_error(
            common.DEFAULT_KEYBINDINGS, "keyA", 113))  # F2, unused

    def test_save_and_load_keybindings_roundtrip(self):
        bindings = dict(common.DEFAULT_KEYBINDINGS)
        bindings["keyR"] = 113
        common.save_keybindings(bindings)

        with open(os.path.join(self.test_dir, "config", "keybindings.json")) as f:
            self.assertEqual(json.load(f)["keyR"], 113)
        self.assertEqual(common.load_keybindings()["keyR"], 113)

    def test_display_config_roundtrip_and_alias(self):
        """D2: scaling choice persists; legacy labels still resolve."""
        common.save_display_config({"scaling": "stretch"})
        self.assertEqual(common.load_display_config()["scaling"], "stretch")

        path = os.path.join(self.test_dir, "config", "display.json")
        with open(path, "w") as f:
            json.dump({"scaling": "Integer Aspect (Crisp)"}, f)
        self.assertEqual(common.load_display_config()["scaling"], "integer")

        common.save_display_config({"scaling": "nonsense"})
        self.assertEqual(common.load_display_config()["scaling"], "integer")

    def test_log_rotation_bounds_file_size(self):
        """ST5: a runaway log is rotated and capped instead of filling DATA."""
        log_path = os.path.join(self.test_dir, "boot_test.log")
        with open(log_path, "wb") as f:
            f.write(b"x" * (common.LOG_MAX_BYTES + 16))

        common._rotate_log_file(log_path)

        self.assertFalse(os.path.exists(log_path))
        self.assertTrue(os.path.exists(log_path + ".1"))
        self.assertLessEqual(os.path.getsize(log_path + ".1"), common.LOG_MAX_BYTES + 16)

        # Small files are left alone.
        with open(log_path, "wb") as f:
            f.write(b"small")
        common._rotate_log_file(log_path)
        self.assertTrue(os.path.exists(log_path))

    def test_prune_old_logs_keeps_newest(self):
        log_dir = os.path.join(self.test_dir, "logs")
        os.makedirs(log_dir)
        for index in range(6):
            path = os.path.join(log_dir, f"boot_{index}.log")
            with open(path, "w") as f:
                f.write("line")
            os.utime(path, (1000 + index, 1000 + index))

        common.prune_old_logs(keep=2)
        remaining = sorted(os.listdir(log_dir))
        self.assertEqual(remaining, ["boot_4.log", "boot_5.log"])

    def test_play_history_accumulates(self):
        common.record_play_session("abc123", 40)
        common.record_play_session("abc123", 20)
        record = common.load_play_history()["abc123"]
        self.assertEqual(record["sessions"], 2)
        self.assertEqual(record["seconds"], 60)
        self.assertGreater(record["last_played"], 0)

    def test_format_duration(self):
        self.assertEqual(common.format_duration(9), "9s")
        self.assertEqual(common.format_duration(120), "2m")
        self.assertEqual(common.format_duration(3600), "1h")
        self.assertEqual(common.format_duration(3660), "1h 1m")

    def test_title_extraction_prefers_header_and_cleans_filenames(self):
        gba = os.path.join(self.test_dir, "some_game.gba")
        with open(gba, "wb") as f:
            f.write(b"\x00" * 0xA0)
            f.write(b"METROID FUSI")
            f.write(b"\x00" * 64)
        self.assertEqual(common.extract_gba_title(gba), "Metroid Fusi")

        gb = os.path.join(self.test_dir, "Tetris (World) (Rev A) [b].gb")
        with open(gb, "wb") as f:
            f.write(b"\x00" * 0x134)
            f.write(b"TETRIS")
            f.write(b"\x00" * 64)
        self.assertEqual(common.extract_gba_title(gb), "Tetris")

        # Filler header bytes are rejected in favour of the filename.
        bad = os.path.join(self.test_dir, "My Homebrew (v2).gba")
        with open(bad, "wb") as f:
            f.write(b"\x00" * 0xA0)
            f.write(b"xxxxxxxxxxxx")
            f.write(b"\x00" * 64)
        self.assertEqual(common.extract_gba_title(bad), "My Homebrew")

    def test_memory_usage_shape(self):
        memory = common.get_memory_usage()
        self.assertIn("total_mb", memory)
        self.assertGreaterEqual(memory["percent"], 0)
        self.assertLessEqual(memory["percent"], 100)


if __name__ == "__main__":
    unittest.main()
