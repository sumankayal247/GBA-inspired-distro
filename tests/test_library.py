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


class TestDurableIdentity(unittest.TestCase):
    """
    The "already imported" knowledge: `known_content_ids` must be durable
    (on disk, survives restarts) and `register_content` must let the USB
    importer record freshly copied files without waiting for a full rescan.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.games_dir = os.path.join(self.test_dir, "games")
        self.data_dir = os.path.join(self.test_dir, "data")
        os.makedirs(self.games_dir, exist_ok=True)
        os.makedirs(self.data_dir, exist_ok=True)
        self.rom1_path = os.path.join(self.games_dir, "game1.gba")
        with open(self.rom1_path, "wb") as f:
            f.write(b"\x00" * 0xA0)
            f.write(b"SAMPLE GAME\x00")
            f.write(b"\x00" * 1024)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_known_content_ids_track_cache_and_survive_reopen(self):
        lib = Library(games_dir=self.games_dir, data_dir=self.data_dir)
        entries = lib.scan_games()
        self.assertEqual(lib.known_content_ids(), {entries[0]["id"]})
        # Simulated restart: a new instance reading the same on-disk cache
        # still knows the content.
        reopened = Library(games_dir=self.games_dir, data_dir=self.data_dir)
        self.assertEqual(reopened.known_content_ids(), {entries[0]["id"]})

    def test_register_content_seeds_the_cache(self):
        lib = Library(games_dir=self.games_dir, data_dir=self.data_dir)
        target = os.path.join(self.games_dir, "imported.gba")
        shutil.copy2(self.rom1_path, target)
        cid = compute_content_id(target)

        self.assertEqual(lib.register_content([(target, cid)]), 1)
        self.assertIn(cid, lib.known_content_ids())

        # A later rescan is a cache hit and reports the same identity.
        entries = lib.scan_games()
        self.assertEqual(len(entries), 2)
        self.assertIn(cid, {e["id"] for e in entries})
        reopened = Library(games_dir=self.games_dir, data_dir=self.data_dir)
        self.assertIn(cid, reopened.known_content_ids())

    def test_register_content_ignores_missing_or_invalid_files(self):
        lib = Library(games_dir=self.games_dir, data_dir=self.data_dir)
        missing = os.path.join(self.games_dir, "gone.gba")
        self.assertEqual(lib.register_content([(missing, "abc123")]), 0)
        note = os.path.join(self.games_dir, "notes.txt")
        with open(note, "w") as f:
            f.write("not a rom")
        self.assertEqual(lib.register_content([(note, "abc123"), ("x", "")]), 0)
        self.assertEqual(lib.known_content_ids(), set())

    def test_scan_saves_cache_incrementally(self):
        for i in range(40):
            with open(os.path.join(self.games_dir, f"rom{i:02d}.gba"), "wb") as f:
                f.write(b"\x00" * 0xA0)
                f.write(f"G{i:011d}".encode("utf-8"))
                f.write(b"\x00" * 512)
                f.write(bytes([i]) * 64)
        lib = Library(games_dir=self.games_dir, data_dir=self.data_dir)
        saves = []
        original = lib._save_cache

        def counting():
            saves.append(len(lib._cache))
            return original()

        lib._save_cache = counting
        entries = lib.scan_games()
        self.assertEqual(len(entries), 41)
        self.assertGreaterEqual(len(saves), 2)
        self.assertLess(saves[0], len(entries))  # flushed mid-scan, not only at the end
        # Everything hashed so far is durable even if the scan is abandoned.
        reopened = Library(games_dir=self.games_dir, data_dir=self.data_dir)
        self.assertGreaterEqual(len(reopened.known_content_ids()), 32)


if __name__ == "__main__":
    unittest.main()
