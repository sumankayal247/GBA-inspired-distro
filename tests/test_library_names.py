"""Library names and renaming.

Two behaviours are pinned here:

* the shelf shows the **file name**, not the cartridge header. Hundreds of
  ROM hacks ship the same header title ("POKEMON EMER" is a popular base),
  so a shelf built from header titles shows the same name over and over and
  the games become impossible to tell apart.
* renaming moves the file and nothing else: the content id - and with it
  every save, cheat, save state and the favourite flag - is derived from the
  content, not from the name, so it survives the rename untouched.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import common  # noqa: E402
import game_data  # noqa: E402
from game_data import Favorites, CheatStore, SaveStateStore  # noqa: E402
from library import Library  # noqa: E402


def write_rom(path: str, header_title: str = "POKEMON EMER") -> str:
    """A ROM whose cartridge header carries `header_title`."""
    with open(path, "wb") as handle:
        handle.write(b"\x00" * 0xA0)
        handle.write(header_title.encode("ascii").ljust(12, b"\x00")[:12])
        handle.write(b"\x00" * 1024)
    return path


class NameTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.games_dir = os.path.join(self.root, "games")
        self.data_dir = os.path.join(self.root, "data")
        os.makedirs(self.games_dir, exist_ok=True)
        os.makedirs(self.data_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def library(self):
        return Library(games_dir=self.games_dir, data_dir=self.data_dir)

    def entry(self, name="Pokemon Emerald (USA).gba"):
        path = write_rom(os.path.join(self.games_dir, name))
        entries = self.library().scan_games()
        self.assertEqual(len(entries), 1)
        return entries[0]


class TestLibraryTitles(NameTestCase):
    def test_the_title_is_the_file_name_not_the_cartridge_header(self):
        entry = self.entry()
        self.assertEqual(entry["title"], "Pokemon Emerald (USA)")

    def test_rom_hacks_with_one_header_title_get_different_names(self):
        write_rom(os.path.join(self.games_dir, "Pokemon Emerald (USA).gba"))
        write_rom(os.path.join(self.games_dir, "Pokemon Emerald RSE.gba"))
        write_rom(os.path.join(self.games_dir, "Pokemon Emerald Hard.gba"))
        titles = sorted(e["title"] for e in self.library().scan_games())
        self.assertEqual(titles, ["Pokemon Emerald (USA)",
                                  "Pokemon Emerald Hard",
                                  "Pokemon Emerald RSE"])

    def test_decorations_are_kept_because_they_tell_dumps_apart(self):
        entry = self.entry("Pokemon Emerald (USA) (Rev 1) [b].gba")
        self.assertEqual(entry["title"], "Pokemon Emerald (USA) (Rev 1) [b]")

    def test_the_file_name_survives_a_rescan(self):
        self.entry()
        titles = [e["title"] for e in self.library().scan_games()]
        self.assertEqual(titles, ["Pokemon Emerald (USA)"])

    def test_a_cache_written_before_the_change_is_refreshed(self):
        """An old cache carries the header title; it is fixed without re-hashing."""
        entry = self.entry()
        lib = self.library()
        with open(lib.cache_file, "r", encoding="utf-8") as handle:
            cache = json.load(handle)
        for record in cache.values():
            record["entry"]["title"] = "POKEMON EMER"
            record.pop("title_source", None)
        with open(lib.cache_file, "w", encoding="utf-8") as handle:
            json.dump(cache, handle)

        refreshed = self.library().scan_games()
        self.assertEqual(refreshed[0]["title"], "Pokemon Emerald (USA)")
        # The content id is untouched: the ROM was not re-hashed.
        self.assertEqual(refreshed[0]["id"], entry["id"])

    def test_filename_title_helper(self):
        self.assertEqual(common.filename_title("/games/Pokemon Emerald.gba"),
                         "Pokemon Emerald")
        self.assertEqual(common.filename_title("/games/empty.gba"), "empty")


class TestRename(NameTestCase):
    def test_the_file_is_renamed_and_the_content_id_survives(self):
        entry = self.entry()
        cheats = CheatStore(entry["id"], entry["content_path"],
                             data_dir=self.data_dir)
        cheats.add_cheat("Infinite money", "01:02", enabled=True)
        SaveStateStore(entry["id"], data_dir=self.data_dir).save(1, b"STATE")
        Favorites(self.data_dir).set_favorite(entry["id"], True)

        result = game_data.rename_game(entry, "Emerald Randomizer",
                                       games_dir=self.games_dir)
        self.assertTrue(result["ok"], result["errors"])
        self.assertFalse(os.path.exists(entry["content_path"]))
        new_path = os.path.join(self.games_dir, "Emerald Randomizer.gba")
        self.assertTrue(os.path.isfile(new_path))
        self.assertEqual(result["title"], "Emerald Randomizer")

        # Everything keyed by the content id is still there.
        entries = self.library().scan_games()
        self.assertEqual(entries[0]["id"], entry["id"])
        self.assertEqual(entries[0]["title"], "Emerald Randomizer")
        self.assertTrue(Favorites(self.data_dir).is_favorite(entry["id"]))
        self.assertTrue(os.path.isdir(os.path.join(self.data_dir, "saves",
                                                   entry["id"])))

    def test_an_existing_name_is_never_overwritten(self):
        first = self.entry("Alpha.gba")
        second = write_rom(os.path.join(self.games_dir, "Bravo.gba"))
        result = game_data.rename_game(first, "Bravo", games_dir=self.games_dir)
        self.assertFalse(result["ok"])
        self.assertIn("already exists", result["errors"][0])
        self.assertTrue(os.path.isfile(first["content_path"]))
        self.assertTrue(os.path.isfile(second))

    def test_a_path_is_not_a_name(self):
        entry = self.entry()
        result = game_data.rename_game(entry, "../../etc/passwd",
                                       games_dir=self.games_dir)
        self.assertFalse(result["ok"])
        self.assertTrue(os.path.isfile(entry["content_path"]))
        # The name is refused, not silently rewritten into something else.
        self.assertIn("cannot contain", result["errors"][0])

    def test_an_empty_name_is_refused(self):
        entry = self.entry()
        for bad in ("", "   ", "..", "."):
            result = game_data.rename_game(entry, bad, games_dir=self.games_dir)
            self.assertFalse(result["ok"], bad)
            self.assertTrue(os.path.isfile(entry["content_path"]))

    def test_a_file_outside_the_games_folder_is_refused(self):
        outside = write_rom(os.path.join(self.root, "outside.gba"))
        entry = {"id": "0" * 16, "title": "Outside",
                 "content_path": outside, "filename": "outside.gba"}
        result = game_data.rename_game(entry, "Renamed",
                                       games_dir=self.games_dir)
        self.assertFalse(result["ok"])
        self.assertIn("outside", result["errors"][0])
        self.assertTrue(os.path.isfile(outside))

    def test_a_missing_file_is_reported_not_raised(self):
        entry = self.entry()
        os.remove(entry["content_path"])
        result = game_data.rename_game(entry, "Whatever",
                                       games_dir=self.games_dir)
        self.assertFalse(result["ok"])
        self.assertIn("gone", result["errors"][0])

    def test_the_library_cache_follows_the_rename(self):
        entry = self.entry()
        lib = self.library()
        lib.scan_games()
        self.assertIn(entry["content_path"], lib._cache)
        result = game_data.rename_game(entry, "Renamed",
                                       games_dir=self.games_dir, library=lib)
        self.assertTrue(result["ok"])
        self.assertNotIn(entry["content_path"], lib._cache)
        self.assertIn(result["path"], lib._cache)
        cached = lib._cache[result["path"]]["entry"]
        self.assertEqual(cached["title"], "Renamed")
        self.assertEqual(cached["id"], entry["id"])

    def test_sanitize_game_name(self):
        self.assertEqual(game_data.sanitize_game_name("  Pokemon Emerald  "),
                         "Pokemon Emerald")
        self.assertEqual(game_data.sanitize_game_name(".hidden"), "hidden")
        self.assertEqual(game_data.sanitize_game_name(""), "")
        self.assertEqual(game_data.sanitize_game_name(None), "")
        self.assertLessEqual(len(game_data.sanitize_game_name("x" * 500)), 120)


if __name__ == "__main__":
    unittest.main()
