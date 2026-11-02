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


if __name__ == "__main__":
    unittest.main()
