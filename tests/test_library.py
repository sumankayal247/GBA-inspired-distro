"""
Contract Tests for GBA OS Library (Rules L1-L6, ST2, ST4).
"""

import os
import shutil
import tempfile
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

        # Delete cache directory
        shutil.rmtree(lib.cache_dir)
        self.assertFalse(os.path.exists(lib.cache_file))

        # Re-scan must rebuild cache without errors
        entries_rebuilt = lib.scan_games()
        self.assertEqual(len(entries_rebuilt), 1)
        self.assertEqual(entries_rebuilt[0]["id"], entries[0]["id"])
        self.assertTrue(os.path.exists(lib.cache_file))

if __name__ == "__main__":
    unittest.main()
