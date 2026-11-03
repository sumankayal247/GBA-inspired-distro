#!/usr/bin/env python3
"""
Tests for pendrive detection and ROM import (gba_usb.py).

Nothing here touches real hardware: mount tables and sysfs helpers are faked,
and imports run between temp directories.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import gba_usb  # noqa: E402


class UsbTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.mkdtemp()
        self.games = os.path.join(self.temp, "games")
        os.makedirs(self.games, exist_ok=True)
        self.usb = os.path.join(self.temp, "PENDRIVE")
        os.makedirs(self.usb, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def make_roms(self, names=("alpha.gba", "beta.gbc"), size=2048):
        paths = []
        for name in names:
            path = os.path.join(self.usb, name)
            with open(path, "wb") as f:
                f.write(b"\x00" * size)
            paths.append(path)
        return paths


class TestMountParsing(UsbTestCase):
    def write_mounts(self, content):
        path = os.path.join(self.temp, "mounts")
        with open(path, "w") as f:
            f.write(content)
        return path

    def test_parse_handles_escaped_spaces(self):
        table = self.write_mounts(
            "/dev/sdb1 /media/user/My\\040Stick vfat rw,nosuid 0 0\n"
            "proc /proc proc rw 0 0\n")
        mounts = gba_usb.parse_mounts(table)
        self.assertIn("/media/user/My Stick", [m[1] for m in mounts])

    def test_volumes_require_media_roots_or_removable(self):
        table = self.write_mounts(
            "/dev/sda1 / ext4 rw 0 0\n"
            f"/dev/sdb1 {self.usb} vfat rw 0 0\n")
        volumes = gba_usb.list_mounted_volumes(table, media_roots=(self.temp,))
        mountpoints = [v.mountpoint for v in volumes]
        self.assertIn(self.usb, mountpoints)
        self.assertNotIn("/", mountpoints)

    def test_device_name_derivation(self):
        self.assertEqual(gba_usb._parent_block_device("/dev/sdb1"), "sdb")
        self.assertEqual(gba_usb._parent_block_device("/dev/nvme0n1p2"), "nvme0n1")
        self.assertEqual(gba_usb._parent_block_device("/dev/mmcblk0p1"), "mmcblk0")

    def test_safe_folder_strips_path_characters(self):
        self.assertEqual(gba_usb._safe_folder("../bad:name?"), ".._bad_name_")
        self.assertEqual(gba_usb._safe_folder(""), "USB")


class TestScan(UsbTestCase):
    def test_scan_finds_only_roms(self):
        self.make_roms()
        with open(os.path.join(self.usb, "readme.txt"), "w") as f:
            f.write("not a rom")
        os.makedirs(os.path.join(self.usb, ".Trash-1000"), exist_ok=True)
        with open(os.path.join(self.usb, ".Trash-1000", "old.gba"), "wb") as f:
            f.write(b"x")

        volume = gba_usb.Volume("/dev/sdb1", self.usb, "PENDRIVE", True)
        roms = volume.scan_roms()
        names = sorted(os.path.basename(path) for path, _size in roms)
        self.assertEqual(names, ["alpha.gba", "beta.gbc"])
        self.assertEqual(volume.rom_count(), 2)

    def test_nested_folders_are_scanned(self):
        nested = os.path.join(self.usb, "roms", "gba")
        os.makedirs(nested, exist_ok=True)
        with open(os.path.join(nested, "deep.gba"), "wb") as f:
            f.write(b"\x00" * 16)
        volume = gba_usb.Volume("/dev/sdb1", self.usb, "PENDRIVE", True)
        self.assertEqual(volume.rom_count(), 1)

    def test_missing_mountpoint_is_empty_not_fatal(self):
        volume = gba_usb.Volume("/dev/sdz1", os.path.join(self.temp, "gone"), "GONE", True)
        self.assertEqual(volume.scan_roms(), [])


class TestImport(UsbTestCase):
    def test_import_copies_and_reports(self):
        self.make_roms()
        volume = gba_usb.Volume("/dev/sdb1", self.usb, "PENDRIVE", True)
        result = gba_usb.import_roms(volume, self.games, subfolder=True)
        self.assertEqual(len(result.copied), 2)
        self.assertEqual(result.failed, [])
        destination = os.path.join(self.games, "PENDRIVE")
        self.assertTrue(os.path.exists(os.path.join(destination, "alpha.gba")))
        self.assertIn("2 imported", result.summary())

    def test_import_without_subfolder_uses_games_root(self):
        self.make_roms(("solo.gba",))
        volume = gba_usb.Volume("/dev/sdb1", self.usb, "PENDRIVE", True)
        result = gba_usb.import_roms(volume, self.games, subfolder=False)
        self.assertEqual(result.destination, self.games)
        self.assertTrue(os.path.exists(os.path.join(self.games, "solo.gba")))

    def test_reimport_skips_identical_files(self):
        self.make_roms()
        volume = gba_usb.Volume("/dev/sdb1", self.usb, "PENDRIVE", True)
        first = gba_usb.import_roms(volume, self.games, subfolder=True)
        self.assertEqual(len(first.copied), 2)

        second = gba_usb.import_roms(volume, self.games, subfolder=True)
        self.assertEqual(second.copied, [])
        self.assertEqual(len(second.skipped), 2)

    def test_conflicting_file_gets_a_suffix(self):
        volume = gba_usb.Volume("/dev/sdb1", self.usb, "PENDRIVE", True)
        destination = os.path.join(self.games, "PENDRIVE")
        os.makedirs(destination, exist_ok=True)
        with open(os.path.join(destination, "alpha.gba"), "wb") as f:
            f.write(b"\x01" * 10)
        self.make_roms(("alpha.gba",))
        result = gba_usb.import_roms(volume, self.games, subfolder=True)
        self.assertEqual(len(result.copied), 1)
        self.assertTrue(os.path.exists(os.path.join(destination, "alpha (1).gba")))

    def test_progress_callback_is_invoked(self):
        self.make_roms()
        seen = []
        volume = gba_usb.Volume("/dev/sdb1", self.usb, "PENDRIVE", True)
        gba_usb.import_roms(volume, self.games, True,
                            progress=lambda index, total, name: seen.append((index, total, name)))
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[-1][0], seen[-1][1])

    def test_empty_stick_is_a_no_op(self):
        volume = gba_usb.Volume("/dev/sdb1", self.usb, "PENDRIVE", True)
        result = gba_usb.import_roms(volume, self.games, True)
        self.assertEqual(result.copied, [])
        self.assertEqual(result.destination, "")


class TestWatcher(UsbTestCase):
    def test_poll_reports_new_volumes_once(self):
        self.make_roms()
        table = os.path.join(self.temp, "mounts")
        with open(table, "w") as f:
            f.write(f"/dev/sdb1 {self.usb} vfat rw 0 0\n")
        watcher = gba_usb.VolumeWatcher(interval=0.05, mounts_path=table,
                                        media_roots=(self.temp,))
        pending = watcher.poll_once()
        self.assertEqual(len(pending), 1)
        self.assertEqual(watcher.poll_once(), [])
        self.assertEqual(len(watcher.take_pending()), 1)
        self.assertEqual(watcher.take_pending(), [])

    def test_removal_is_detected(self):
        table = os.path.join(self.temp, "mounts")
        with open(table, "w") as f:
            f.write(f"/dev/sdb1 {self.usb} vfat rw 0 0\n")
        watcher = gba_usb.VolumeWatcher(interval=0.05, mounts_path=table,
                                        media_roots=(self.temp,))
        watcher.poll_once()
        with open(table, "w") as f:
            f.write("")
        watcher.poll_once()
        self.assertEqual(watcher.volumes, [])


class TestNewContentDetection(UsbTestCase):
    """
    The import prompt must be gated on content identity, not discovery:
    a stick whose ROMs are already registered in the library offers nothing,
    even across watcher/launcher restarts, while genuinely new files are
    reported individually.
    """

    def make_unique_roms(self, names, size=2048):
        paths = []
        for name in names:
            path = os.path.join(self.usb, name)
            with open(path, "wb") as f:
                f.write(b"\x00" * size)
                f.write(name.encode("utf-8"))
            paths.append(path)
        return paths

    def write_mounts(self, device="/dev/sdb1"):
        table = os.path.join(self.temp, "mounts")
        with open(table, "w") as f:
            f.write(f"{device} {self.usb} vfat rw 0 0\n")
        return table

    def index(self):
        return gba_usb.SourceIndex(path=os.path.join(self.temp, "index.json"))

    def ids_for(self, *paths):
        from common import compute_content_id
        return {compute_content_id(p) for p in paths}

    def watcher(self, known_ids=(), index=None):
        return gba_usb.VolumeWatcher(interval=0.05, mounts_path=self.write_mounts(),
                                     media_roots=(self.temp,),
                                     known_ids_provider=lambda: set(known_ids),
                                     source_index=index)

    def test_fresh_os_offers_everything(self):
        self.make_unique_roms(("alpha.gba", "beta.gbc"))
        pending = self.watcher().poll_once()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].new_rom_count(), 2)
        self.assertEqual(len(pending[0].new_roms), 2)

    def test_imported_content_never_prompts_even_after_restart(self):
        a, b = self.make_unique_roms(("alpha.gba", "beta.gbc"))
        known = self.ids_for(a, b)
        w = self.watcher(known_ids=known, index=self.index())
        self.assertEqual(w.poll_once(), [])
        # Simulated launcher restart (the supervisor relaunches after every
        # game): a brand-new watcher instance must stay quiet too.
        w2 = self.watcher(known_ids=known, index=self.index())
        self.assertEqual(w2.poll_once(), [])
        self.assertEqual(w2.take_pending(), [])

    def test_only_genuinely_new_roms_are_reported(self):
        a, b = self.make_unique_roms(("alpha.gba", "beta.gbc"))
        self.make_unique_roms(("gamma.gba",))
        w = self.watcher(known_ids=self.ids_for(a, b), index=self.index())
        pending = w.poll_once()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].new_rom_count(), 1)
        self.assertEqual([os.path.basename(p) for p, _ in pending[0].new_roms],
                         ["gamma.gba"])

    def test_renamed_copy_of_imported_rom_is_not_new(self):
        (a,) = self.make_unique_roms(("alpha.gba",))
        renamed = os.path.join(self.usb, "totally_different_name.gba")
        shutil.copy2(a, renamed)
        w = self.watcher(known_ids=self.ids_for(a), index=self.index())
        self.assertEqual(w.poll_once(), [])

    def test_unplug_and_replug_reanalyses(self):
        self.make_unique_roms(("alpha.gba",))
        table = self.write_mounts()
        w = gba_usb.VolumeWatcher(interval=0.05, mounts_path=table,
                                  media_roots=(self.temp,),
                                  known_ids_provider=lambda: set(),
                                  source_index=self.index())
        self.assertEqual(len(w.poll_once()), 1)
        self.assertEqual(w.poll_once(), [])          # still plugged: no re-prompt
        with open(table, "w") as f:                  # unplugged
            f.write("")
        w.poll_once()
        with open(table, "w") as f:                  # plugged again
            f.write(f"/dev/sdb1 {self.usb} vfat rw 0 0\n")
        self.assertEqual(len(w.poll_once()), 1)

    def test_refresh_rearms_without_blocking_the_caller(self):
        self.make_unique_roms(("alpha.gba",))
        w = self.watcher(index=self.index())
        self.assertEqual(len(w.poll_once()), 1)
        self.assertEqual(len(w.take_pending()), 1)   # drain the first prompt
        w.refresh()
        self.assertEqual(w.take_pending(), [])       # refresh enqueues nothing itself
        self.assertEqual(len(w.poll_once()), 1)      # next poll re-analyses


class TestSourceIndex(UsbTestCase):
    def test_index_persists_and_avoids_rehashing(self):
        (a,) = self.make_roms(("alpha.gba",))
        path = os.path.join(self.temp, "index.json")
        index = gba_usb.SourceIndex(path=path)
        st = os.stat(a)
        cid = index.content_id(a, st.st_size, st.st_mtime, self.usb)
        index.save()
        self.assertTrue(os.path.exists(path))

        calls = []
        original = gba_usb.compute_content_id

        def counting(p):
            calls.append(p)
            return original(p)

        gba_usb.compute_content_id = counting
        try:
            reloaded = gba_usb.SourceIndex(path=path)
            self.assertEqual(reloaded.content_id(a, st.st_size, st.st_mtime, self.usb), cid)
            self.assertEqual(calls, [])              # cache hit: no re-hash
            os.utime(a, (st.st_atime + 5, st.st_mtime + 5))
            st2 = os.stat(a)
            self.assertEqual(reloaded.content_id(a, st2.st_size, st2.st_mtime, self.usb), cid)
            self.assertEqual(len(calls), 1)          # mtime changed: re-hashed once
        finally:
            gba_usb.compute_content_id = original

    def test_corrupt_index_resets_to_empty(self):
        path = os.path.join(self.temp, "index.json")
        with open(path, "w") as f:
            f.write("{not json")
        index = gba_usb.SourceIndex(path=path)
        self.assertEqual(index._data, {})


class TestImportIdentity(UsbTestCase):
    def test_import_skips_content_already_registered_under_another_name(self):
        (a,) = self.make_roms(("alpha.gba",))
        renamed = os.path.join(self.usb, "alpha_renamed.gba")
        shutil.copy2(a, renamed)
        from common import compute_content_id
        volume = gba_usb.Volume("/dev/sdb1", self.usb, "PENDRIVE", True)
        result = gba_usb.import_roms(
            volume, self.games, True, known_ids={compute_content_id(a)},
            index=gba_usb.SourceIndex(path=os.path.join(self.temp, "index.json")))
        self.assertEqual(result.copied, [])
        self.assertEqual(len(result.skipped), 2)
        self.assertEqual(os.listdir(os.path.join(self.games, "PENDRIVE")), [])

    def test_import_registers_copied_content_ids(self):
        (a,) = self.make_roms(("alpha.gba",))
        from common import compute_content_id
        volume = gba_usb.Volume("/dev/sdb1", self.usb, "PENDRIVE", True)
        result = gba_usb.import_roms(volume, self.games, True,
                                     known_ids={"deadbeefdeadbeef"})
        self.assertEqual(len(result.copied), 1)
        target, cid = result.registered[0]
        self.assertEqual(cid, compute_content_id(a))
        self.assertTrue(target.endswith("alpha.gba"))

    def test_import_is_idempotent_after_registration(self):
        self.make_roms(("alpha.gba",))
        from common import compute_content_id
        volume = gba_usb.Volume("/dev/sdb1", self.usb, "PENDRIVE", True)
        first = gba_usb.import_roms(volume, self.games, True)
        self.assertEqual(len(first.copied), 1)
        known = {compute_content_id(os.path.join(self.games, "PENDRIVE", "alpha.gba"))}
        second = gba_usb.import_roms(
            volume, self.games, True, known_ids=known,
            index=gba_usb.SourceIndex(path=os.path.join(self.temp, "index.json")))
        self.assertEqual(second.copied, [])
        self.assertEqual(len(second.skipped), 1)


class TestImportLifecycle(UsbTestCase):
    """
    End-to-end acceptance flow for import detection using the real Library
    as the identity store: first-boot prompt, silence after returning from a
    game / restarting / rebooting, rescan idempotence, and "1 new" after one
    ROM is added.
    """

    def setUp(self):
        super().setUp()
        self.data = os.path.join(self.temp, "data")
        os.makedirs(self.data, exist_ok=True)
        from library import Library
        self.Library = Library
        self.table = os.path.join(self.temp, "mounts")
        with open(self.table, "w") as f:
            f.write(f"/dev/sdb1 {self.usb} vfat rw 0 0\n")
        self.index_path = os.path.join(self.data, "library", "usb_sources.json")

    def rom(self, name, marker):
        path = os.path.join(self.usb, name)
        with open(path, "wb") as f:
            f.write(b"\x00" * 0xA0)
            f.write(marker.encode("utf-8").ljust(12, b"\x00")[:12])
            f.write(b"\x00" * 512)
        return path

    def watcher(self, lib):
        return gba_usb.VolumeWatcher(
            interval=0.05, mounts_path=self.table, media_roots=(self.temp,),
            known_ids_provider=lib.known_content_ids,
            source_index=gba_usb.SourceIndex(path=self.index_path))

    def test_full_lifecycle(self):
        # 1) First boot with the stick already plugged in (test A): the
        #    library is empty, so everything on the stick is offered.
        self.rom("one.gba", "ONE")
        self.rom("two.gba", "TWO")
        self.rom("three.gbc", "THREE")
        lib = self.Library(games_dir=self.games, data_dir=self.data)
        self.assertEqual(lib.scan_games(), [])
        pending = self.watcher(lib).poll_once()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].new_rom_count(), 3)

        # 2) The user confirms the import (test A).
        result = gba_usb.import_roms(pending[0], self.games, True)
        self.assertEqual(len(result.copied), 3)
        self.assertEqual(len(lib.scan_games()), 3)

        # 3) Returning from a game and restarting the OS both mean a fresh
        #    launcher process with a fresh watcher — and no prompt (B + C).
        for _restart in range(2):
            restarted = self.Library(games_dir=self.games, data_dir=self.data)
            self.assertEqual(len(restarted.known_content_ids()), 3)
            self.assertEqual(self.watcher(restarted).poll_once(), [])

        # 4) A rescan of the library changes nothing (test D context).
        self.assertEqual(len(self.Library(games_dir=self.games,
                                          data_dir=self.data).scan_games()), 3)

        # 5) One genuinely new ROM is added to the stick (test E).
        self.rom("four.gba", "FOUR")
        lib3 = self.Library(games_dir=self.games, data_dir=self.data)
        pending = self.watcher(lib3).poll_once()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].new_rom_count(), 1)
        self.assertEqual(os.path.basename(pending[0].new_roms[0][0]), "four.gba")

        # 6) Importing again copies only the new file (test F).
        result = gba_usb.import_roms(pending[0], self.games, True)
        self.assertEqual(len(result.copied), 1)
        entries = lib3.scan_games()
        self.assertEqual(len(entries), 4)
        self.assertEqual(len({e["id"] for e in entries}), 4)

        # 7) Repeated scans/imports never duplicate anything (test F).
        result = gba_usb.import_roms(
            pending[0], self.games, True, known_ids=lib3.known_content_ids(),
            index=gba_usb.SourceIndex(path=self.index_path))
        self.assertEqual(result.copied, [])
        self.assertEqual(len(lib3.scan_games()), 4)


if __name__ == "__main__":
    unittest.main()
