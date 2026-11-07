"""Per-game cheats, nine save-state slots, favorites and game deletion."""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import game_data
from game_data import CheatStore, Favorites, SaveStateStore, order_games
from library import Library

GAME_A = "44636205c3e1a1b2"
GAME_B = "80a88361e29f4a77"


class StorageTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.data_dir = os.path.join(self.root, "data")
        self.games_dir = os.path.join(self.root, "games")
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.games_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def store(self, game_id=GAME_A):
        return SaveStateStore(game_id, data_dir=self.data_dir)

    def cheats(self, game_id=GAME_A, rom="game.gba"):
        return CheatStore(game_id, rom, data_dir=self.data_dir)


class TestPerGameIdentity(StorageTestCase):
    def test_paths_are_keyed_by_content_id_not_filename(self):
        one = CheatStore(GAME_A, "Some Game.gba", data_dir=self.data_dir)
        renamed = CheatStore(GAME_A, "renamed.gba", data_dir=self.data_dir)
        other = CheatStore(GAME_B, "Some Game.gba", data_dir=self.data_dir)
        self.assertIn(GAME_A, one.metadata_path)
        self.assertIn(GAME_A, renamed.metadata_path)
        self.assertIn(GAME_B, other.metadata_path)
        self.assertNotEqual(os.path.dirname(one.metadata_path),
                            os.path.dirname(other.metadata_path))
        self.assertTrue(game_data.game_dir(GAME_A, self.data_dir).endswith(GAME_A))

    def test_invalid_identity_is_inert(self):
        for bad in ("", "../escape", "not-a-hash", None, 123):
            self.assertFalse(game_data.valid_game_id(bad))
        self.assertEqual(CheatStore("../escape", data_dir=self.data_dir).cheats(), [])
        self.assertFalse(CheatStore("../escape", data_dir=self.data_dir).set_enabled("x", True))
        self.assertIsNone(CheatStore("../escape", data_dir=self.data_dir).write_native())
        self.assertEqual(len(SaveStateStore("nope", data_dir=self.data_dir).slots()), 9)
        self.assertTrue(SaveStateStore("nope", data_dir=self.data_dir).is_empty(1))
        self.assertFalse(SaveStateStore("nope", data_dir=self.data_dir).save(1, b"state"))
        self.assertFalse(Favorites(self.data_dir).toggle("../escape"))
        self.assertEqual(os.listdir(self.data_dir), [])


class TestCheats(StorageTestCase):
    def test_cheats_are_isolated_by_game_identity(self):
        a, b = self.cheats(GAME_A), self.cheats(GAME_B)
        a.add_cheat("Infinite Lives", "01234567 89ABCDEF")
        b.add_cheat("Max Coins", "FEDCBA98 76543210")
        self.assertEqual([item["name"] for item in a.cheats()], ["Infinite Lives"])
        self.assertEqual([item["name"] for item in b.cheats()], ["Max Coins"])
        self.assertNotEqual(a.metadata_path, b.metadata_path)
        self.assertNotEqual(a.native_path, b.native_path)
        self.assertIn("!disabled", open(a.native_path).read())  # default off
        self.assertNotIn("Max Coins", open(a.native_path).read())

    def test_enable_disable_state_persists(self):
        store = self.cheats()
        cid = store.add_cheat("Infinite Lives", "01234567 89ABCDEF")
        self.assertFalse(store.cheats()[0]["enabled"])
        self.assertTrue(store.set_enabled(cid, True))
        self.assertTrue(store.cheats()[0]["enabled"])
        native = open(store.native_path).read()
        self.assertIn("# Infinite Lives", native)
        self.assertNotIn("!disabled", native)
        # Restart: a fresh store reads the same state from disk.
        restarted = self.cheats()
        self.assertTrue(restarted.cheats()[0]["enabled"])
        self.assertTrue(restarted.set_enabled(cid, False))
        self.assertIn("!disabled", open(restarted.native_path).read())
        self.assertFalse(self.cheats().cheats()[0]["enabled"])

    def test_native_cheat_file_is_imported_not_global(self):
        store = self.cheats()
        os.makedirs(os.path.dirname(store.native_path), exist_ok=True)
        with open(store.native_path, "w", encoding="utf-8") as f:
            f.write("!disabled\n# Moon Jump\nDEADBEEF 00000001\n"
                    "# Always On\nCAFEBABE 00000002\n")
        imported = CheatStore(GAME_A, "game.gba", data_dir=self.data_dir)
        self.assertEqual(len(imported.cheats()), 2)
        self.assertFalse(imported.cheats()[0]["enabled"])
        self.assertTrue(imported.cheats()[1]["enabled"])
        # Enabling once and re-importing must not reset the user's choice.
        imported.set_enabled(imported.cheats()[0]["id"], True)
        self.assertTrue(CheatStore(GAME_A, "game.gba",
                                   data_dir=self.data_dir).cheats()[0]["enabled"])
        self.assertEqual(self.cheats(GAME_B).cheats(), [])

    def test_edit_preserves_enabled_state_and_supports_multiline_codes(self):
        store = self.cheats()
        cid = store.add_cheat("Infinite Lives", "01234567 89ABCDEF", enabled=True)
        self.assertTrue(store.update_cheat(
            cid, "Infinite Lives + Coins", "01234567 89ABCDEF\nCAFEBABE 00000001"))
        restarted = self.cheats()
        self.assertEqual(len(restarted.cheats()), 1)
        item = restarted.cheats()[0]
        self.assertEqual(item["name"], "Infinite Lives + Coins")
        self.assertEqual(item["code"], "01234567 89ABCDEF\nCAFEBABE 00000001")
        self.assertTrue(item["enabled"])
        with open(restarted.native_path, encoding="utf-8") as native:
            self.assertIn("CAFEBABE 00000001", native.read())

    def test_deleting_last_cheat_does_not_reimport_a_stale_native_file(self):
        store = self.cheats()
        cid = store.add_cheat("Temporary", "DEADBEEF 00000001", enabled=True)
        self.assertTrue(os.path.isfile(store.native_path))
        self.assertTrue(store.remove_cheat(cid))
        self.assertEqual(store.cheats(), [])
        self.assertFalse(os.path.exists(store.native_path))
        self.assertEqual(self.cheats().cheats(), [])
        self.assertFalse(store.remove_cheat("missing"))

    def test_corrupt_metadata_is_tolerated(self):
        store = self.cheats()
        os.makedirs(os.path.dirname(store.metadata_path), exist_ok=True)
        with open(store.metadata_path, "w", encoding="utf-8") as f:
            f.write("{broken json")
        self.assertEqual(self.cheats().cheats(), [])


class TestSaveStateStorage(StorageTestCase):
    def test_exactly_nine_slots_all_empty(self):
        store = self.store()
        slots = store.slots()
        self.assertEqual(len(slots), 9)
        self.assertEqual([s["slot"] for s in slots], list(range(1, 10)))
        self.assertTrue(all(not s["occupied"] for s in slots))
        for number in range(1, 10):
            self.assertTrue(store.is_empty(number))
        # Out-of-range/garbage slots are inert, never "real" slots.
        self.assertTrue(store.is_empty(0))
        self.assertTrue(store.is_empty(10))
        self.assertTrue(store.is_empty("bogus"))

    def test_saving_creates_real_state_and_separate_thumbnail(self):
        store = self.store()
        self.assertTrue(store.save(3, b"MGBA_NATIVE_STATE", b"\x89PNG\r\nthumbnail"))
        info = store.slots()[2]
        self.assertTrue(info["occupied"])
        self.assertTrue(info["valid"])
        self.assertTrue(info["has_thumbnail"])
        self.assertTrue(info["timestamp"])
        self.assertIn("slot_3", info["state_path"])
        with open(info["state_path"], "rb") as f:
            self.assertEqual(f.read(), b"MGBA_NATIVE_STATE")
        with open(info["thumbnail_path"], "rb") as f:
            self.assertEqual(f.read(), b"\x89PNG\r\nthumbnail")
        # Metadata is JSON only; no binary state lives in it.
        with open(store.metadata_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        self.assertNotIn("MGBA_NATIVE_STATE", json.dumps(payload))
        self.assertNotIn("thumbnail.png", json.dumps(payload))
        self.assertNotIn("\x89PNG", json.dumps(payload))
        self.assertIn("state_sha256", payload["slots"]["3"])
        self.assertEqual(len(payload["slots"]), 1)

    def test_failed_metadata_write_rolls_back_to_the_previous_good_save(self):
        store = self.store()
        self.assertTrue(store.save(4, b"GOOD_STATE", b"GOOD_PNG",
                                   timestamp=1000.0))
        original = store._save
        calls = []

        def fail_once(payload):
            calls.append(payload)
            return original(payload) if len(calls) > 1 else False

        store._save = fail_once
        self.assertFalse(store.save(4, b"NEW_STATE", b"NEW_PNG",
                                    timestamp=2000.0))
        store._save = original
        info = store.slots()[3]
        self.assertTrue(info["valid"])       # not a torn / corrupt slot
        self.assertEqual(info["timestamp"], 1000.0)
        with open(info["state_path"], "rb") as f:
            self.assertEqual(f.read(), b"GOOD_STATE")
        with open(info["thumbnail_path"], "rb") as f:
            self.assertEqual(f.read(), b"GOOD_PNG")

    def test_loading_selects_the_correct_slot(self):
        store = self.store()
        store.save(1, b"STATE_ONE", b"THUMB_ONE")
        store.save(7, b"STATE_SEVEN", b"THUMB_SEVEN")
        with open(store.state_for_load(7), "rb") as f:
            self.assertEqual(f.read(), b"STATE_SEVEN")
        with open(store.state_for_load(1), "rb") as f:
            self.assertEqual(f.read(), b"STATE_ONE")
        self.assertNotEqual(store.state_for_load(1), store.state_for_load(7))

    def test_overwrite_replaces_state_and_thumbnail_and_metadata(self):
        store = self.store()
        store.save(2, b"OLD_STATE", b"OLD_PNG", timestamp=1000.0)
        first = store.slots()[1]["timestamp"]
        self.assertTrue(store.overwrite(2, b"NEW_STATE_DATA", b"NEW_PNG",
                                        timestamp=2000.0))
        info = store.slots()[1]
        with open(info["state_path"], "rb") as f:
            self.assertEqual(f.read(), b"NEW_STATE_DATA")
        with open(info["thumbnail_path"], "rb") as f:
            self.assertEqual(f.read(), b"NEW_PNG")
        self.assertNotEqual(info["timestamp"], first)
        self.assertTrue(info["valid"])

    def test_state_persists_after_restart(self):
        self.store().save(4, b"PERSISTENT", b"PNG")
        restarted = SaveStateStore(GAME_A, data_dir=self.data_dir)
        info = restarted.slots()[3]
        self.assertTrue(info["occupied"])
        self.assertTrue(info["valid"])
        with open(restarted.state_for_load(4), "rb") as f:
            self.assertEqual(f.read(), b"PERSISTENT")

    def test_game_a_cannot_see_game_b_states(self):
        self.store(GAME_A).save(1, b"A_STATE", b"A_PNG")
        b = self.store(GAME_B)
        self.assertTrue(all(not s["occupied"] for s in b.slots()))
        self.assertIsNone(b.state_for_load(1))
        b.save(1, b"B_STATE", b"B_PNG")
        with open(self.store(GAME_A).state_for_load(1), "rb") as f:
            self.assertEqual(f.read(), b"A_STATE")
        self.assertNotEqual(self.store(GAME_A).state_path(1), b.state_path(1))

    def test_corrupt_and_missing_state_fail_gracefully(self):
        store = self.store()
        store.save(1, b"GOOD", b"PNG")
        with open(store.state_path(1), "wb") as f:
            f.write(b"CORRUPT")
        info = store.slots()[0]
        self.assertTrue(info["occupied"])
        self.assertFalse(info["valid"])
        self.assertTrue(info["corrupt"])
        self.assertIsNone(store.state_for_load(1))
        os.remove(store.state_path(1))
        self.assertFalse(store.slots()[0]["occupied"])
        self.assertIsNone(store.state_for_load(1))
        os.remove(store.metadata_path)
        self.assertTrue(all(not s["occupied"] for s in store.slots()))

    def test_partial_writes_never_create_a_slot(self):
        store = self.store()
        os.makedirs(store.slot_dir(5), exist_ok=True)
        with open(store.state_path(5) + ".tmp", "wb") as f:
            f.write(b"HALF WRITTEN")
        with open(store.thumbnail_path(5) + ".tmp", "wb") as f:
            f.write(b"HALF PNG")
        self.assertTrue(store.slots()[4]["occupied"] is False)
        self.assertFalse(os.path.exists(store.state_path(5) + ".tmp"))
        self.assertFalse(store.save(5, b"", b"PNG"))       # empty state rejected
        self.assertFalse(store.save(5, "/no/such/state.ss", b"PNG"))
        self.assertTrue(store.is_empty(5))
        self.assertFalse(os.path.exists(store.metadata_path))

    def test_missing_thumbnail_is_tolerated(self):
        store = self.store()
        self.assertTrue(store.save(6, b"STATE_ONLY", None))
        info = store.slots()[5]
        self.assertTrue(info["occupied"])
        self.assertTrue(info["valid"])
        self.assertFalse(info["has_thumbnail"])


class TestFavorites(StorageTestCase):
    def _games(self):
        return [{"id": "a" * 16, "title": "Alpha"},
                {"id": "b" * 16, "title": "Bravo"},
                {"id": "c" * 16, "title": "Charlie"},
                {"id": "d" * 16, "title": "Delta"}]

    def test_favorite_status_persists_across_restarts(self):
        favorites = Favorites(self.data_dir)
        self.assertFalse(favorites.is_favorite("c" * 16))
        self.assertTrue(favorites.toggle("c" * 16))
        self.assertTrue(Favorites(self.data_dir).is_favorite("c" * 16))
        self.assertFalse(Favorites(self.data_dir).toggle("c" * 16))
        self.assertFalse(Favorites(self.data_dir).is_favorite("c" * 16))

    def test_favorites_first_preserving_order_and_no_duplicates(self):
        favorites = Favorites(self.data_dir)
        favorites.set_favorite("c" * 16, True)
        favorites.set_favorite("a" * 16, True)
        games = self._games() + [{"id": "c" * 16, "title": "Duplicate"}]
        ordered = favorites.order(games)
        self.assertEqual([g["title"] for g in ordered],
                         ["Alpha", "Charlie", "Bravo", "Delta"])
        self.assertEqual(len(ordered), 4)

    def test_unfavorite_returns_to_normal_position(self):
        favorites = Favorites(self.data_dir)
        favorites.set_favorite("d" * 16, True)
        self.assertEqual(favorites.order(self._games())[0]["title"], "Delta")
        favorites.set_favorite("d" * 16, False)
        self.assertEqual([g["title"] for g in favorites.order(self._games())],
                         ["Alpha", "Bravo", "Charlie", "Delta"])

    def test_order_games_is_stable_without_a_store(self):
        games = self._games()
        self.assertEqual(order_games(games, [])[0]["title"], "Alpha")
        self.assertEqual(order_games(games, ["b" * 16])[0]["title"], "Bravo")

    def test_corrupt_favorites_file_is_tolerated(self):
        path = os.path.join(self.data_dir, "config", "favorites.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("[] not an object")
        self.assertEqual(Favorites(self.data_dir).ids(), [])


class TestGameDeletion(StorageTestCase):
    def _rom(self, name, payload=b"ROM"):
        path = os.path.join(self.games_dir, name)
        with open(path, "wb") as f:
            f.write(payload)
        return path

    def test_delete_removes_rom_data_cheats_states_and_favorite(self):
        path = self._rom("victim.gba")
        game_id = GAME_A
        self.cheats(game_id, path).add_cheat("Cheat", "CODE", enabled=True)
        self.store(game_id).save(1, b"STATE", b"PNG")
        Favorites(self.data_dir).set_favorite(game_id, True)
        library = Library(games_dir=self.games_dir, data_dir=self.data_dir)
        library.register_content([(path, game_id)])

        result = game_data.delete_game(
            {"id": game_id, "content_path": path},
            games_dir=self.games_dir, data_dir=self.data_dir, library=library)
        self.assertTrue(result["rom"])
        self.assertTrue(result["data"])
        self.assertEqual(result["errors"], [])
        self.assertFalse(os.path.exists(path))
        self.assertFalse(os.path.exists(game_data.game_dir(game_id, self.data_dir)))
        self.assertFalse(Favorites(self.data_dir).is_favorite(game_id))
        self.assertNotIn(path, library._cache)

    def test_delete_all_clears_every_imported_game(self):
        paths = [self._rom("one.gba"), self._rom("two.gb")]
        entries = [{"id": GAME_A, "content_path": paths[0]},
                   {"id": GAME_B, "content_path": paths[1]}]
        for entry in entries:
            self.store(entry["id"]).save(9, b"STATE", b"PNG")
        result = game_data.delete_all_games(entries, games_dir=self.games_dir,
                                            data_dir=self.data_dir)
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["roms"], 2)
        self.assertFalse(any(os.path.exists(p) for p in paths))
        self.assertTrue(all(not s["occupied"]
                            for s in self.store(GAME_A).slots()))
        self.assertTrue(all(not s["occupied"]
                            for s in self.store(GAME_B).slots()))

    def test_never_deletes_outside_the_games_folder(self):
        outside = os.path.join(self.root, "precious.txt")
        with open(outside, "w", encoding="utf-8") as f:
            f.write("keep me")
        result = game_data.delete_game(
            {"id": GAME_A, "content_path": outside},
            games_dir=self.games_dir, data_dir=self.data_dir)
        self.assertFalse(result["rom"])
        self.assertTrue(os.path.exists(outside))

    def test_missing_rom_and_bad_identity_are_reported_not_raised(self):
        result = game_data.delete_game(
            {"id": GAME_A, "content_path": os.path.join(self.games_dir, "gone.gba")},
            games_dir=self.games_dir, data_dir=self.data_dir)
        self.assertFalse(result["rom"])
        self.assertTrue(result["data"])
        self.assertEqual(result["errors"], [])
        bad = game_data.delete_game({"id": "filename.gba", "content_path": None},
                                    games_dir=self.games_dir, data_dir=self.data_dir)
        self.assertFalse(bad["rom"])
        self.assertFalse(bad["data"])


if __name__ == "__main__":
    unittest.main()
