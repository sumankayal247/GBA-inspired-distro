#!/usr/bin/env python3
"""
Tests for Android/MTP import sources (gba_usb.py).

Everything is simulated: sysfs interface trees, `jmtpfs -l`/mount commands,
GVfs paths and FUSE mount tables. No real phone, block device or FUSE mount is
needed, and normal USB-stick detection is checked alongside each new path.
"""

import os
import shutil
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import gba_usb  # noqa: E402
from common import compute_content_id  # noqa: E402
from library import Library  # noqa: E402


def completed(stdout="", stderr="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


class MtpTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.mkdtemp()
        self.games = os.path.join(self.temp, "games")
        self.data = os.path.join(self.temp, "data")
        self.phone = os.path.join(self.temp, "phone")
        self.usb = os.path.join(self.temp, "PENDRIVE")
        for path in (self.games, self.data, self.phone, self.usb):
            os.makedirs(path, exist_ok=True)
        self.mounts = os.path.join(self.temp, "mounts")
        with open(self.mounts, "w") as f:
            f.write("")

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def rom(self, relative, marker, root=None, size=768):
        path = os.path.join(root or self.phone, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(marker.encode("utf-8").ljust(size, b"\0"))
        return path

    def manager(self, **kwargs):
        options = dict(auto_mount=False, mounts_path=self.mounts,
                       sysfs_root=os.path.join(self.temp, "no-sysfs"),
                       gvfs_roots=(), command_exists=lambda _name: False)
        options.update(kwargs)
        return gba_usb.MtpManager(**options)

    def sysfs_device(self, root, *, name="2-1", bus=2, dev=19, interface_class="08",
                     subclass="06", protocol="50", description="Mass Storage"):
        device_dir = os.path.join(root, name)
        interface_dir = os.path.join(root, name + ":1.0")
        os.makedirs(device_dir, exist_ok=True)
        os.makedirs(interface_dir, exist_ok=True)
        values = {
            os.path.join(device_dir, "busnum"): str(bus),
            os.path.join(device_dir, "devnum"): str(dev),
            os.path.join(device_dir, "idVendor"): "04e8",
            os.path.join(device_dir, "idProduct"): "6860",
            os.path.join(device_dir, "manufacturer"): "Google",
            os.path.join(device_dir, "product"): "Pixel 7",
            os.path.join(device_dir, "serial"): "ANDROID123",
            os.path.join(interface_dir, "bInterfaceClass"): interface_class,
            os.path.join(interface_dir, "bInterfaceSubClass"): subclass,
            os.path.join(interface_dir, "bInterfaceProtocol"): protocol,
            os.path.join(interface_dir, "interface"): description,
        }
        for path, value in values.items():
            with open(path, "w") as f:
                f.write(value)
        return root


class TestMtpParsers(MtpTestCase):
    def test_jmtpfs_device_table(self):
        output = (
            "Device 0 (VID=04e8 and PID=6860) is a Samsung Galaxy.\n"
            "Available devices (busLocation, devNum, productId, vendorId, product, vendor):\n"
            "2, 19, 0x6860, 0x04e8, Galaxy S, Samsung\n"
        )
        (device,) = gba_usb.parse_jmtpfs_devices(output)
        self.assertEqual((device.bus, device.dev), (2, 19))
        self.assertEqual((device.vendor_id, device.product_id), ("04e8", "6860"))
        self.assertIn("Samsung", device.label)
        self.assertEqual(device.uri_value(), "mtp://[usb:002,019]/")

    def test_mtp_detect_and_gio_output(self):
        output = (
            "Listing raw device(s)\n"
            "Device 0 (VID=04e8 and PID=6860) is a Google Pixel 7\n"
            "   Found 1 device(s):\n"
            "   Google: Pixel 7 (04e8:6860) @ bus 2, dev 19\n"
        )
        (device,) = gba_usb.parse_mtp_detect(output)
        self.assertEqual((device.bus, device.dev, device.vendor_id, device.product_id),
                         (2, 19, "04e8", "6860"))
        (header_only,) = gba_usb.parse_mtp_detect(output.splitlines()[1])
        self.assertEqual((header_only.bus, header_only.dev), (0, 0))
        self.assertEqual(header_only.vendor_id, "04e8")
        gio = ("Drive(0): Android\n"
               "  Volume(0): Pixel 7\n"
               "    Mount(0): Pixel 7 -> mtp://[usb:002,019]/\n")
        self.assertEqual(gba_usb.parse_gio_mtp_uris(gio), ["mtp://[usb:002,019]/"])

    def test_sysfs_detects_mtp_but_not_usb_mass_storage(self):
        root = os.path.join(self.temp, "sysfs")
        self.sysfs_device(root, name="2-1", interface_class="06", subclass="01",
                          protocol="01", description="MTP")
        self.sysfs_device(root, name="3-1", bus=3, dev=7)
        self.sysfs_device(root, name="4-1", bus=4, dev=8, interface_class="06",
                          subclass="01", protocol="01", description="PTP Camera")
        devices = gba_usb.discover_usb_mtp_devices(root)
        self.assertEqual(len(devices), 1)
        self.assertEqual((devices[0].bus, devices[0].dev), (2, 19))
        self.assertEqual(devices[0].serial, "ANDROID123")
        self.assertIn("Pixel", devices[0].label)

    def test_missing_mtp_mtime_is_not_fatal(self):
        with patch("gba_usb.os.path.getmtime", side_effect=OSError("no stat")):
            self.assertEqual(gba_usb._source_mtime("/phone/rom.gba", True), 0.0)
            with self.assertRaises(OSError):
                gba_usb._source_mtime("/usb/rom.gba", False)

    def test_mtp_fuse_mount_entries_exclude_block_disks(self):
        with open(self.mounts, "w") as f:
            f.write("jmtpfs /media/PHONE\\040MTP fuse.jmtpfs rw 0 0\n")
            f.write("/dev/sdb1 /media/STICK vfat rw 0 0\n")
            f.write("/dev/fuse /run/user/1000/gvfs fuse.gvfsd-fuse rw 0 0\n")
        entries = gba_usb.parse_mtp_mount_entries(self.mounts)
        self.assertEqual([entry[1] for entry in entries],
                         ["/media/PHONE MTP", "/run/user/1000/gvfs"])
        self.assertEqual(gba_usb.parse_mounts(self.mounts),
                         [("/dev/sdb1", "/media/STICK")])


class TestMtpVolumes(MtpTestCase):
    def test_fuse_storage_scans_only_rom_extensions(self):
        self.rom("Internal storage/Download/alpha.gba", "ALPHA")
        self.rom("Internal storage/Download/beta.gbc", "BETA")
        self.rom("SD card/ROMs/gamma.gb", "GAMMA")
        self.rom("Internal storage/readme.txt", "TEXT")
        self.rom("DCIM/Camera/skip.gba", "SKIP")
        self.rom(".hidden.gba", "HIDDEN")
        manager = self.manager(extra_roots=(self.phone,))
        volumes = manager.poll()
        self.assertEqual(len(volumes), 2)
        names = sorted(os.path.basename(path)
                       for volume in volumes for path, _size in volume.scan_roms())
        self.assertEqual(names, ["alpha.gba", "beta.gbc", "gamma.gb"])
        for volume in volumes:
            self.assertTrue(volume.is_mtp)
            self.assertFalse(volume.removable)
            self.assertFalse(volume.device.startswith("/dev/"))
            self.assertEqual(volume.kind_label, "ANDROID MTP")
            self.assertTrue(volume.key.startswith("mtp://"))
            self.assertTrue(volume.index_namespace.startswith("mtp:"))
        self.assertNotEqual(volumes[0].index_namespace, volumes[1].index_namespace)

    def test_block_usb_detection_unchanged_and_fuse_not_a_disk(self):
        self.rom("stick.gba", "STICK", root=self.usb)
        with open(self.mounts, "w") as f:
            f.write(f"/dev/fuse /media/PHONE fuse.jmtpfs rw 0 0\n")
            f.write(f"/dev/sdb1 {self.usb} vfat rw 0 0\n")
        volumes = gba_usb.list_mounted_volumes(self.mounts, media_roots=(self.temp,))
        self.assertEqual([v.mountpoint for v in volumes], [self.usb])
        self.assertFalse(volumes[0].is_mtp)

        manager = self.manager(extra_roots=(self.phone,), mounts_path=self.mounts)
        for volume in manager.poll():
            self.assertTrue(volume.is_mtp)
        sources = gba_usb.list_import_sources(
            self.mounts, media_roots=(self.temp,), mtp_manager=manager)
        self.assertEqual(len(sources), 2)
        self.assertFalse(sources[0].is_mtp)
        self.assertTrue(sources[1].is_mtp)

    def test_generic_dev_fuse_mount_with_phone_layout(self):
        # Some jmtpfs versions emit only "/dev/fuse ... fuse" with no MTP
        # marker. Storage-shaped contents identify it without treating it as a
        # block volume.
        self.rom("Internal storage/Download/phone.gba", "PHONE")
        with open(self.mounts, "w") as f:
            f.write(f"/dev/fuse {self.phone} fuse rw 0 0\n")
        self.assertEqual(len(gba_usb.parse_fuse_mount_entries(self.mounts)), 1)
        manager = self.manager(mounts_path=self.mounts)
        (volume,) = manager.poll()
        self.assertTrue(volume.is_mtp)
        self.assertEqual([os.path.basename(p) for p, _ in volume.scan_roms()],
                         ["phone.gba"])
        self.assertEqual(gba_usb.list_mounted_volumes(
            self.mounts, media_roots=(self.temp,)), [])

    def test_gvfs_mount_is_recognized_by_usb_location(self):
        gvfs = os.path.join(self.temp, "gvfs")
        encoded = "mtp:host=%5Busb%3A002%2C019%5D"
        root = os.path.join(gvfs, encoded)
        self.rom("Internal storage/pocket.gb", "POCKET", root=root)
        manager = self.manager(gvfs_roots=(gvfs,))
        (volume,) = manager.poll()
        self.assertTrue(volume.is_mtp)
        self.assertIn("usb-002-019", volume.key)
        self.assertEqual(volume.mtp_device.uri_value(), "mtp://[usb:002,019]/")
        self.assertEqual(volume.backend, "gvfs")
        self.assertEqual([os.path.basename(p) for p, _ in volume.scan_roms()],
                         ["pocket.gb"])

    def test_source_index_namespaces_identical_relative_paths(self):
        root_a = os.path.join(self.temp, "phone-a")
        root_b = os.path.join(self.temp, "phone-b")
        a = self.rom("same.gba", "PHONE-A", root=root_a)
        b = self.rom("same.gba", "PHONE-B", root=root_b)
        device_a = gba_usb.MtpDevice(bus=1, dev=2, serial="A")
        device_b = gba_usb.MtpDevice(bus=1, dev=3, serial="B")
        vol_a = gba_usb.MtpVolume(device_a, root_a, "Internal storage")
        vol_b = gba_usb.MtpVolume(device_b, root_b, "Internal storage")
        index = gba_usb.SourceIndex(path=os.path.join(self.temp, "index.json"))
        for volume, path in ((vol_a, a), (vol_b, b)):
            stat = os.stat(path)
            self.assertEqual(
                index.content_id(path, stat.st_size, stat.st_mtime,
                                 volume.mountpoint, volume.index_namespace),
                compute_content_id(path))
        self.assertNotEqual(vol_a.index_namespace, vol_b.index_namespace)
        self.assertEqual(len(index._data), 2)


class TestMtpImportLifecycle(MtpTestCase):
    def test_watcher_detects_phone_and_prompt_is_gated_by_content_id(self):
        already = self.rom("already.gba", "ALREADY")
        fresh = self.rom("fresh.gbc", "FRESH")
        # The same content is already in GAMES under a different filename.
        imported = os.path.join(self.games, "renamed.gba")
        shutil.copy2(already, imported)
        lib = Library(games_dir=self.games, data_dir=self.data)
        lib.scan_games()
        known = lib.known_content_ids()
        self.assertEqual(len(known), 1)

        manager = self.manager(extra_roots=(self.phone,))
        index = gba_usb.SourceIndex(path=os.path.join(self.temp, "index.json"))
        watcher = gba_usb.VolumeWatcher(
            interval=0.05, mounts_path=self.mounts, media_roots=(self.temp,),
            known_ids_provider=lambda: known, source_index=index,
            mtp_provider=manager.poll)
        pending = watcher.poll_once()
        self.assertEqual(len(pending), 1)
        volume = pending[0]
        self.assertTrue(volume.is_mtp)
        self.assertEqual(volume.new_rom_count(), 1)
        self.assertEqual([os.path.basename(p) for p, _ in volume.new_roms],
                         ["fresh.gbc"])
        self.assertEqual(watcher.poll_once(), [])   # still plugged: prompt once

        result = gba_usb.import_roms(volume, self.games, True,
                                     known_ids=known, index=index)
        self.assertEqual(len(result.copied), 1)
        self.assertEqual(len(result.skipped), 1)   # known by content ID, not name
        self.assertEqual(result.failed, [])
        self.assertEqual(result.registered[0][1], compute_content_id(fresh))
        lib.register_content(result.registered)
        self.assertIn(compute_content_id(fresh), lib.known_content_ids())

        # A fresh launcher/watcher and an explicit re-import stay idempotent.
        restarted = gba_usb.VolumeWatcher(
            interval=0.05, mounts_path=self.mounts, media_roots=(self.temp,),
            known_ids_provider=lib.known_content_ids, source_index=index,
            mtp_provider=manager.poll)
        self.assertEqual(restarted.poll_once(), [])
        again = gba_usb.import_roms(
            restarted.volumes[0], self.games, True,
            known_ids=lib.known_content_ids(), index=index)
        self.assertEqual(again.copied, [])
        self.assertEqual(len(lib.scan_games()), 2)

    def test_no_prompt_for_a_phone_whose_roms_are_all_imported(self):
        a = self.rom("one.gba", "ONE")
        b = self.rom("two.gb", "TWO")
        known = {compute_content_id(a), compute_content_id(b)}
        manager = self.manager(extra_roots=(self.phone,))
        watcher = gba_usb.VolumeWatcher(
            interval=0.05, mounts_path=self.mounts, media_roots=(self.temp,),
            known_ids_provider=lambda: known,
            source_index=gba_usb.SourceIndex(path=os.path.join(self.temp, "index.json")),
            mtp_provider=manager.poll)
        self.assertEqual(watcher.poll_once(), [])
        self.assertEqual(os.listdir(self.games), [])

    def test_normal_usb_and_mtp_share_one_watcher_queue(self):
        stick = self.rom("stick.gba", "STICK", root=self.usb)
        self.rom("phone.gba", "PHONE")
        with open(self.mounts, "w") as f:
            f.write(f"/dev/sdb1 {self.usb} vfat rw 0 0\n")
        manager = self.manager(extra_roots=(self.phone,))
        watcher = gba_usb.VolumeWatcher(
            interval=0.05, mounts_path=self.mounts, media_roots=(self.temp,),
            mtp_provider=manager.poll)
        pending = watcher.poll_once()
        self.assertEqual(len(pending), 2)
        self.assertEqual({v.is_mtp for v in pending}, {False, True})
        self.assertEqual(len(watcher.volumes), 2)
        self.assertEqual(watcher.poll_once(), [])


class TestMtpAutoMount(MtpTestCase):
    def test_jmtpfs_auto_mount_uses_usb_address_not_a_block_device(self):
        sysfs = self.sysfs_device(
            os.path.join(self.temp, "sysfs"), interface_class="06",
            subclass="01", protocol="01", description="MTP")
        mount_root = os.path.join(self.temp, "fuse-mounts")
        calls = []

        def runner(argv, timeout=0):
            calls.append(list(argv))
            if argv[1] == "-l":
                return completed(
                    "Available devices (busLocation, devNum, productId, vendorId, product, vendor):\n"
                    "2, 19, 0x6860, 0x04e8, Pixel 7, Google\n")
            if len(argv) >= 3 and argv[1].startswith("-device="):
                root = argv[-1]
                storage = os.path.join(root, "Internal storage", "ROMs")
                os.makedirs(storage, exist_ok=True)
                with open(os.path.join(storage, "android.gba"), "wb") as f:
                    f.write(b"ANDROID ROM".ljust(1024, b"\0"))
                return completed()
            if argv[0].startswith("fusermount"):
                return completed()
            return completed(returncode=1, stderr="unexpected command")

        manager = gba_usb.MtpManager(
            auto_mount=True, mounts_path=self.mounts, sysfs_root=sysfs,
            mount_root=mount_root, gvfs_roots=(), runner=runner,
            command_exists=lambda _name: True)
        volumes = manager.poll()
        mount_commands = [argv for argv in calls if len(argv) > 1
                          and argv[1].startswith("-device=")]
        self.assertEqual(len(mount_commands), 1)
        self.assertIn("-device=2,19", mount_commands[0])
        self.assertFalse(any("/dev/sd" in part for argv in calls for part in argv))
        self.assertEqual(os.listdir(self.games), [])  # no copy before confirmation
        self.assertEqual(len(volumes), 1)
        self.assertTrue(volumes[0].is_mtp)
        self.assertEqual([os.path.basename(p) for p, _ in volumes[0].scan_roms()],
                         ["android.gba"])
        self.assertIn("ANDROID123", volumes[0].key)
        self.assertIn("ANDROID123", volumes[0].index_namespace)

        result = gba_usb.import_roms(
            volumes[0], self.games, True, known_ids={"unrelated"},
            index=gba_usb.SourceIndex(path=os.path.join(self.temp, "index.json")))
        self.assertEqual(len(result.copied), 1)
        self.assertTrue(os.path.exists(os.path.join(self.games,
                                                    gba_usb._safe_folder(volumes[0].label),
                                                    "android.gba")))
        manager.close()
        self.assertTrue(any(argv and argv[0].startswith("fusermount")
                            for argv in calls))


class TestMtpDetectionChain(MtpTestCase):
    """One test per link of sysfs -> libmtp -> GVfs -> FUSE -> watcher."""

    def test_sysfs_accepts_vendor_ptp_protocol_and_names_the_device(self):
        root = os.path.join(self.temp, "sysfs")
        # Some vendor stacks expose class 6/subclass 1/protocol 0.
        self.sysfs_device(root, name="1-2", bus=1, dev=9, interface_class="06",
                          subclass="01", protocol="00", description="")
        (device,) = gba_usb.discover_usb_mtp_devices(root)
        self.assertEqual((device.bus, device.dev), (1, 9))

    def test_quoted_jmtpfs_names_and_blank_output(self):
        output = ("Available devices (busLocation, devNum, productId, vendorId, product, vendor):\n"
                  '2, 19, 0x6860, 0x04e8, "Pixel 7", "Google"\n')
        (device,) = gba_usb.parse_jmtpfs_devices(output)
        self.assertEqual(device.product, "Pixel 7")
        self.assertEqual(device.manufacturer, "Google")
        self.assertEqual(gba_usb.parse_jmtpfs_devices("No devices found.\n"), [])

    def test_gio_fallback_derives_a_uri_for_a_sysfs_only_device(self):
        sysfs = self.sysfs_device(
            os.path.join(self.temp, "sysfs"), name="2-1", bus=2, dev=19,
            interface_class="06", subclass="01", protocol="01", description="MTP")
        calls = []

        def runner(argv, timeout=0):
            calls.append(list(argv))
            if argv[0] == "gio":
                return completed()
            return completed(returncode=1, stderr="jmtpfs: unable to open device")

        manager = gba_usb.MtpManager(
            auto_mount=True, mounts_path=self.mounts, sysfs_root=sysfs,
            mount_root=os.path.join(self.temp, "fuse"), gvfs_roots=(),
            runner=runner,
            command_exists=lambda name: name in ("gio", "jmtpfs"))
        manager.poll()
        # `gio mount -l` is discovery; `gio mount mtp://...` is the fallback.
        gio_calls = [argv for argv in calls
                     if argv[0] == "gio" and "-l" not in argv]
        self.assertEqual(len(gio_calls), 1)
        # libmtp found no device, so the phone is mounted through GVfs using
        # the USB address; before the fix a sysfs-only device had no URI and
        # `gio mount` was never even attempted.
        self.assertIn("mtp://[usb:002,019]/", gio_calls[0])

    def test_locked_phone_retries_quickly_then_backs_off(self):
        sysfs = self.sysfs_device(
            os.path.join(self.temp, "sysfs"), interface_class="06",
            subclass="01", protocol="01", description="MTP")
        attempts = []

        def runner(argv, timeout=0):
            if argv[1:2] == ["-l"]:
                return completed()
            attempts.append(list(argv))
            return completed(returncode=1, stderr="jmtpfs: Unable to open MTP device")

        manager = gba_usb.MtpManager(
            auto_mount=True, mounts_path=self.mounts, sysfs_root=sysfs,
            mount_root=os.path.join(self.temp, "fuse"), gvfs_roots=(),
            runner=runner, command_exists=lambda name: name == "jmtpfs")
        manager.poll()
        self.assertEqual(len(attempts), 1)
        identity = manager._devices[0].identity
        first_delay = manager._failed_until[identity] - time.monotonic()
        self.assertLessEqual(first_delay, gba_usb.MTP_QUICK_RETRY + 0.5)
        # The retry gate keeps a locked phone from being re-mounted every tick.
        manager.poll()
        self.assertEqual(len(attempts), 1)
        for _ in range(gba_usb.MTP_QUICK_RETRIES + 1):
            manager._backoff(identity, failed=True)
        steady = manager._failed_until[identity] - time.monotonic()
        self.assertGreaterEqual(steady, gba_usb.MTP_RETRY_INTERVAL - 0.5)
        # Unlocking the phone and pressing R retries immediately.
        manager.refresh()
        manager.poll()
        self.assertEqual(len(attempts), 2)

    def test_mount_error_is_translated_into_actionable_status(self):
        sysfs = self.sysfs_device(
            os.path.join(self.temp, "sysfs"), interface_class="06",
            subclass="01", protocol="01", description="MTP")

        def runner(argv, timeout=0):
            if argv[1:2] == ["-l"]:
                return completed()
            return completed(returncode=1,
                             stderr="jmtpfs: Unable to open MTP device")

        manager = gba_usb.MtpManager(
            auto_mount=True, mounts_path=self.mounts, sysfs_root=sysfs,
            mount_root=os.path.join(self.temp, "fuse"), gvfs_roots=(),
            runner=runner, command_exists=lambda name: name == "jmtpfs")
        manager.poll()
        status = manager.status()
        self.assertIn("Unlock", status)
        self.assertIn("MTP", status)

    def test_locked_phone_prompts_once_it_unlocks(self):
        """Mounted while locked (no files) -> later unlock must still prompt."""
        sysfs = self.sysfs_device(
            os.path.join(self.temp, "sysfs"), interface_class="06",
            subclass="01", protocol="01", description="MTP")
        mount_root = os.path.join(self.temp, "fuse")
        unlocked = {"value": False}

        def runner(argv, timeout=0):
            if argv[1:2] == ["-l"]:
                return completed("Available devices (busLocation, devNum, "
                                 "productId, vendorId, product, vendor):\n"
                                 "2, 19, 0x6860, 0x04e8, Pixel 7, Google\n")
            root = argv[-1]
            os.makedirs(os.path.join(root, "Internal storage"), exist_ok=True)
            if unlocked["value"]:
                path = os.path.join(root, "Internal storage", "Download", "late.gba")
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(b"LATE ROM".ljust(512, b"\0"))
            return completed()

        manager = gba_usb.MtpManager(
            auto_mount=True, mounts_path=self.mounts, sysfs_root=sysfs,
            mount_root=mount_root, gvfs_roots=(), runner=runner,
            command_exists=lambda name: name == "jmtpfs")
        watcher = gba_usb.VolumeWatcher(
            interval=0.05, mounts_path=self.mounts, media_roots=(self.temp,),
            mtp_provider=manager.poll)
        self.assertEqual(watcher.poll_once(), [])          # locked: nothing to offer
        self.assertFalse(watcher.seen)                     # ... so it is re-checked
        # The user unlocks the phone: the FUSE view now shows the storage.
        (locked_volume,) = manager.poll()
        path = os.path.join(locked_volume.mountpoint, "Download", "late.gba")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"LATE ROM".ljust(512, b"\0"))
        unlocked["value"] = True
        pending = watcher.poll_once()
        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0].is_mtp)
        self.assertEqual([os.path.basename(p) for p, _ in pending[0].new_roms],
                         ["late.gba"])

    def test_mtp_rom_with_unknown_size_is_offered_and_copied(self):
        self.rom("Internal storage/Download/quirky.gba", "QUIRKY")
        source = os.path.join(self.phone, "Internal storage", "Download", "quirky.gba")
        real_getsize = os.path.getsize

        def getsize(path, *args, **kwargs):
            # This FUSE backend reports 0 until the file is read.
            if os.path.abspath(path) == os.path.abspath(source):
                return 0
            return real_getsize(path, *args, **kwargs)

        device = gba_usb.MtpDevice(bus=2, dev=19, serial="XYZ")
        volume = gba_usb.MtpVolume(device, os.path.dirname(os.path.dirname(source)),
                                   "Internal storage")
        with patch("gba_usb.os.path.getsize", side_effect=getsize):
            roms = volume.scan_roms()
        self.assertEqual([os.path.basename(p) for p, _ in roms], ["quirky.gba"])
        result = gba_usb.import_roms(volume, self.games, True)
        self.assertEqual(len(result.copied), 1)
        self.assertEqual(result.failed, [])
        with open(result.copied[0], "rb") as f:
            self.assertGreater(len(f.read()), 0)

    def test_mounted_only_listing_never_runs_a_command(self):
        self.rom("Internal storage/quiet.gba", "QUIET")
        with patch("gba_usb.subprocess.run",
                   side_effect=AssertionError("must not run a command")):
            volumes = gba_usb.list_mounted_mtp_volumes(
                self.mounts, roots=[self.phone], extra_roots=None)
        self.assertEqual(len(volumes), 1)
        self.assertTrue(volumes[0].is_mtp)

    def test_diagnostics_report_every_stage(self):
        sysfs = self.sysfs_device(
            os.path.join(self.temp, "sysfs"), interface_class="06",
            subclass="01", protocol="01", description="MTP")

        def runner(argv, timeout=0):
            return completed(returncode=1, stderr="jmtpfs: unable to open device")

        manager = gba_usb.MtpManager(
            auto_mount=True, mounts_path=self.mounts, sysfs_root=sysfs,
            mount_root=os.path.join(self.temp, "fuse"), gvfs_roots=(),
            runner=runner, command_exists=lambda name: name == "jmtpfs")
        manager.poll()
        report = manager.diagnose()
        self.assertEqual(report["sysfs_mtp_interfaces"], 1)
        self.assertTrue(report["jmtpfs"])
        self.assertFalse(report["gio"])
        self.assertIn("unable to open", report["last_error"])
        text = gba_usb.mtp_diagnostics(manager=manager)
        for needle in ("sysfs MTP interfaces", "tools", "mount root",
                       "devices", "import volumes", "hint"):
            self.assertIn(needle, text)
        self.assertIn("Pixel 7", text)


class TestIsoDependencies(unittest.TestCase):
    """The image must ship the MTP tools the detection chain actually uses."""

    def read(self, relative):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            relative)
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def iso_packages(self) -> list:
        iso = self.read("scripts/build_iso.sh")
        for line in iso.splitlines():
            if "--include=" in line:
                return line.split("--include=", 1)[1].split()[0].split(",")
        return []

    def test_iso_ships_the_libmtp_tools_the_detection_chain_uses(self):
        packages = self.iso_packages()
        self.assertIn("jmtpfs", packages)      # FUSE mount backend
        self.assertIn("mtp-tools", packages)   # mtp-detect (libmtp probing)

    def test_installer_diagnoses_missing_mtp_and_fuse(self):
        installer = self.read("scripts/install.sh")
        self.assertTrue("jmtpfs" in installer, "installer must mention jmtpfs")
        self.assertTrue("mtp-tools" in installer, "installer must name mtp-tools")
        self.assertTrue("gb_warn" in installer, "MTP must stay a warning, not a failure")
        # jmtpfs can be installed and still unable to mount: /dev/fuse and the
        # FUSE userland are checked explicitly.
        self.assertTrue("/dev/fuse" in installer, "installer must check /dev/fuse")
        self.assertTrue("fusermount" in installer,
                        "installer must check the fusermount userland")

    def test_no_extra_python_dependency_was_added(self):
        requirements = self.read("requirements.txt")
        for forbidden in ("pyudev", "dbus", "pymtp", "libmtp"):
            self.assertNotIn(forbidden, requirements.lower())


class TestMtpShellLabels(unittest.TestCase):
    def test_prompt_and_manager_label_an_mtp_source(self):
        import gba_launcher
        import gba_mtp
        self.assertIs(gba_mtp.MtpVolume, gba_usb.MtpVolume)
        self.assertIs(gba_mtp.parse_mtp_mounts, gba_usb.parse_mtp_mount_entries)
        device = gba_usb.MtpDevice(bus=2, dev=19, manufacturer="Google",
                                   product="Pixel 7", serial="ABC")
        volume = gba_usb.MtpVolume(device, "/run/gba/mtp/Pixel 7",
                                   "Internal storage")
        prompt = gba_launcher.UsbPromptOverlay(volume)
        self.assertEqual(prompt.title, "ANDROID MTP DETECTED")
        manager = gba_launcher.UsbManagerOverlay(volumes=[volume])
        self.assertIn("MTP", manager.title)
        self.assertEqual(manager.volumes, [volume])
        volume._roms = []
        watcher = SimpleNamespace(volumes=[volume])
        status = gba_launcher.GBALauncher.usb_status_text(
            SimpleNamespace(usb_watcher=watcher))
        self.assertTrue(status.startswith("MTP "))


if __name__ == "__main__":
    unittest.main()
