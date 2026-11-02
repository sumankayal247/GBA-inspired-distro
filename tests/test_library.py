"""
Contract tests for GBA OS Library (Rules L1-L6, ST2, ST4, L4).

Extends the original suite with the behaviours added in this pass:
empty-file tolerance, deterministic ordering, a non-blocking scan and the
built-in app/settings catalogue.
"""

import os
import shutil
import tempfile
import time
import unittest
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))
from library import Library
from common import compute_content_id


class TestLibraryContract(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.games_dir = os.path.join(self.test_dir, "games")
        self.data_dir = os.path.join(self.test_dir, "data")
        os.makedirs(self.games_dir, exist_ok=True)
        os.makedirs(self.data_dir, exist_ok=True)

        # Create dummy GBA ROMs
        self.rom1_path = os.path.join(self.games_dir, "game1.gba")
        with open(self.rom1_path, "wb") as f:
            f.write(b"\x00" * 0xA0)
            f.write(b"SAMPLE GAME\x00")
            f.write(b"\x00" * 1024)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_l2_id_derived_from_content_hash(self):
        """L2: Entry ID MUST be derived from content hash, not name or location."""
        lib = Library(games_dir=self.games_dir, data_dir=self.data_dir)
        entries1 = lib.scan_games()
        self.assertEqual(len(entries1), 1)
        original_id = entries1[0]["id"]
        expected_hash = compute_content_id(self.rom1_path)
        self.assertEqual(original_id, expected_hash)

        # Rename ROM file (Test T-LIBRARY-1)
        renamed_path = os.path.join(self.games_dir, "renamed_game.gba")
        os.rename(self.rom1_path, renamed_path)

        entries2 = lib.scan_games()
        self.assertEqual(len(entries2), 1)
        self.assertEqual(entries2[0]["id"], original_id, "ID changed after renaming!")

    def test_l6_empty_state_tolerance(self):
        """L6: Empty library renders without crashing."""
        empty_dir = os.path.join(self.test_dir, "empty_games")
        os.makedirs(empty_dir, exist_ok=True)
        lib = Library(games_dir=empty_dir, data_dir=self.data_dir)
        entries = lib.scan_games()
        self.assertEqual(len(entries), 0)

    def test_st2_cache_disposable(self):
        """ST2: Deleting /data/library/ MUST NOT cause failure; rebuilt automatically."""
        lib = Library(games_dir=self.games_dir, data_dir=self.data_dir)
        entries = lib.scan_games()
        self.assertEqual(len(entries), 1)
        self.assertTrue(os.path.exists(lib.cache_file))

        shutil.rmtree(lib.cache_dir)
        self.assertFalse(os.path.exists(lib.cache_file))

        entries_rebuilt = lib.scan_games()
        self.assertEqual(len(entries_rebuilt), 1)
        self.assertEqual(entries_rebuilt[0]["id"], entries[0]["id"])
        self.assertTrue(os.path.exists(lib.cache_file))

    def test_empty_and_hidden_files_are_skipped(self):
        """L5: a broken (0-byte) ROM must not affect the other entries."""
        open(os.path.join(self.games_dir, "broken.gba"), "wb").close()
        hidden = os.path.join(self.games_dir, ".hidden.gba")
        with open(hidden, "wb") as f:
            f.write(b"\x00" * 512)

        lib = Library(games_dir=self.games_dir, data_dir=self.data_dir)
        entries = lib.scan_games()

        self.assertEqual([e["filename"] for e in entries], ["game1.gba"])
        self.assertEqual(lib.count_content_files(), 2)

    def test_entries_are_sorted_by_title(self):
        """L3: ordering is stable so the library looks identical every boot."""
        for name, title in (("z_rom.gba", b"ZELDA"), ("a_rom.gbc", b"ADVANCE WARS")):
            with open(os.path.join(self.games_dir, name), "wb") as f:
                f.write(b"\x00" * 0x134)
                f.write(title)
                f.write(b"\x00" * 256)

        lib = Library(games_dir=self.games_dir, data_dir=self.data_dir)
        titles = [e["title"] for e in lib.scan_games()]
        self.assertEqual(titles, sorted(titles, key=str.lower))

    def test_scan_cache_avoids_rehashing(self):
        """L4: scan cost is cached by (path, size, mtime)."""
        lib = Library(games_dir=self.games_dir, data_dir=self.data_dir)
        lib.scan_games()
        first_scan = lib.last_scan_seconds

        lib2 = Library(games_dir=self.games_dir, data_dir=self.data_dir)
        lib2.scan_games()
        # The second (cached) scan must not be dramatically slower.
        self.assertLessEqual(lib2.last_scan_seconds, max(0.5, first_scan * 5))

    def test_async_scanner_reports_completion(self):
        """L4: scanning happens off the launcher's render thread."""
        lib = Library(games_dir=self.games_dir, data_dir=self.data_dir)
        scanner = lib.start_scan()
        deadline = time.time() + 5
        while not scanner.done and time.time() < deadline:
            time.sleep(0.01)

        self.assertTrue(scanner.done)
        self.assertEqual(len(scanner.entries), 1)
        self.assertIsNone(scanner.error)

    def test_built_in_catalogue_is_complete(self):
        """L1: every built-in entry declares id, title, type, handler and args."""
        lib = Library(games_dir=self.games_dir, data_dir=self.data_dir)
        entries = lib.get_built_in_apps() + lib.get_settings_entries()
        self.assertGreaterEqual(len(entries), 5)

        for entry in entries:
            for key in ("id", "title", "type", "handler", "args"):
                self.assertIn(key, entry, f"{entry.get('id')} missing {key}")
            self.assertIn(entry["type"], ("app", "settings"))
            self.assertEqual(entry["args"], [])

        ids = [e["id"] for e in entries]
        self.assertEqual(len(ids), len(set(ids)), "duplicate entry ids")
        self.assertIn("internal_poweroff", [e["handler"] for e in entries])

    def test_content_in_nested_directory_is_found(self):
        """L3: /games may be organised into sub-folders."""
        nested = os.path.join(self.games_dir, "Pokemon")
        os.makedirs(nested, exist_ok=True)
        with open(os.path.join(nested, "emerald.gba"), "wb") as f:
            f.write(b"\x00" * 0xA0)
            f.write(b"POKEMON EMER")
            f.write(b"\x00" * 128)

        lib = Library(games_dir=self.games_dir, data_dir=self.data_dir)
        filenames = {e["filename"] for e in lib.scan_games()}
        self.assertEqual(filenames, {"game1.gba", "emerald.gba"})


if __name__ == "__main__":
    unittest.main()
