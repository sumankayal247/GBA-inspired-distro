#!/usr/bin/env python3
"""
Tests for the disk installer (src/gba_installer.py).

The point of these tests is not to prove that mkfs works — it is to prove that
the installer refuses to do anything dangerous, that a dry run touches nothing,
and that a failure part-way through leaves the machine's boot configuration
alone. All commands are faked; no test needs root or a real disk.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import gba_installer as gi  # noqa: E402


class FakeRunner(gi.CommandRunner):
    """Records commands and optionally emulates their filesystem effects."""

    def __init__(self, dry_run=False, fail_on=(), effects=None):
        super().__init__(dry_run=dry_run, logger=lambda _m: None)
        self.fail_on = fail_on
        self.effects = effects or {}
        self.ran = []

    def has(self, program):
        return True

    def run(self, args, check=True, input_text=None):
        self.history.append(list(args))
        if self.dry_run:
            return 0, "", ""
        self.ran.append(list(args))
        program = args[0]
        if program in self.fail_on:
            raise gi.StepFailed(f"{program} failed (simulated)")
        effect = self.effects.get(program)
        if effect:
            return effect(args)
        return 0, "", ""

    def programs(self):
        return [cmd[0] for cmd in self.ran]


def make_disks(prefix):
    """A believable layout: root disk + an empty target disk + a tiny stick."""
    return [
        gi.Disk(f"{prefix}/sda", size=256 * 1024 ** 3, model="Root SSD",
                children=[gi.Disk(f"{prefix}/sda1", size=254 * 1024 ** 3, fstype="ext4",
                                  mountpoints=["/"]),
                          gi.Disk(f"{prefix}/sda2", size=512 * 1024 ** 2, fstype="vfat",
                                  mountpoints=["/boot/efi"])]),
        gi.Disk(f"{prefix}/sdb", size=128 * 1024 ** 3, model="Target SSD",
                children=[gi.Disk(f"{prefix}/sdb1", size=128 * 1024 ** 3, fstype="",
                                  mountpoints=[])]),
        gi.Disk(f"{prefix}/sdc", size=16 * 1024 ** 3, model="USB Stick", removable=True,
                children=[gi.Disk(f"{prefix}/sdc1", size=16 * 1024 ** 3, removable=True)]),
        gi.Disk(f"{prefix}/sdd", size=4 * 1024 ** 3, model="Tiny Stick",
                children=[gi.Disk(f"{prefix}/sdd1", size=4 * 1024 ** 3)]),
    ]


class InstallerTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.mkdtemp()
        self.source = os.path.join(self.temp, "rootfs")
        os.makedirs(os.path.join(self.source, "etc"), exist_ok=True)
        os.makedirs(os.path.join(self.source, "boot"), exist_ok=True)
        self.mount_root = os.path.join(self.temp, "target")
        os.makedirs(self.mount_root, exist_ok=True)
        # Simulated block devices: everything under <temp>/dev is "a device".
        self.devdir = os.path.join(self.temp, "dev")
        os.makedirs(self.devdir, exist_ok=True)
        self.disks = make_disks(self.devdir)
        for name in ("sda", "sda1", "sdb", "sdb1", "sdc", "sdc1", "sdd", "sdd1"):
            open(os.path.join(self.devdir, name), "w").close()
        self.target = os.path.join(self.devdir, "sdb1")

        self.patches = [
            mock.patch.object(gi, "_is_block_device", lambda p: p.startswith(self.devdir)),
            mock.patch.object(gi, "device_for_path",
                              lambda p: os.path.join(self.devdir, "sda1")
                              if p in ("/", "/run/live/medium") else ""),
            mock.patch.object(gi, "_mounted_devices", lambda: {}),
            mock.patch.object(gi, "_partition_has_os", lambda p: False),
            mock.patch.object(gi, "probe_disks", lambda runner: self.disks),
            mock.patch.object(gi, "parent_disk", gi.parent_disk),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        shutil.rmtree(self.temp, ignore_errors=True)

    def plan(self, **kwargs):
        kwargs.setdefault("mode", "partition")
        kwargs.setdefault("target", self.target)
        kwargs.setdefault("source", self.source)
        kwargs.setdefault("source_kind", "dir")
        kwargs.setdefault("mount_root", self.mount_root)
        return gi.InstallPlan(**kwargs)

    def fake_uuid(self, args):
        if args[:2] == ["blkid", "-s"]:
            return 0, "1111-2222-3333-4444", ""
        return 0, "", ""

    def emulate_copy(self, mount_root):
        """Pretends the rootfs copy created the installed tree."""
        def effect(args):
            for relative in ("etc", "boot/grub", "opt/gba-distro/bin", "opt/gba-distro/src"):
                os.makedirs(os.path.join(mount_root, relative), exist_ok=True)
            for relative in ("opt/gba-distro/bin/gba-launcher",
                             "opt/gba-distro/bin/gba-setup",
                             "opt/gba-distro/src/gba_launcher.py"):
                open(os.path.join(mount_root, relative), "w").close()
            return 0, "", ""
        return effect

    def validate(self, plan, runner=None, allow_removable=False):
        gi.validate_plan(plan, runner or FakeRunner(), self.disks,
                         allow_removable=allow_removable)


class TestSafetyRefusals(InstallerTestCase):
    def test_refuses_target_that_is_the_running_root(self):
        plan = self.plan(target=os.path.join(self.devdir, "sda1"))
        with self.assertRaises(gi.SafetyRefusal) as ctx:
            self.validate(plan)
        self.assertIn("running system", str(ctx.exception))

    def test_refuses_disk_mode_on_the_running_disk(self):
        plan = self.plan(mode="disk", target=os.path.join(self.devdir, "sda"))
        with self.assertRaises(gi.SafetyRefusal):
            self.validate(plan)

    def test_refuses_missing_target(self):
        plan = self.plan(target=os.path.join(self.devdir, "nope"))
        with self.assertRaises(gi.SafetyRefusal) as ctx:
            self.validate(plan)
        self.assertIn("does not exist", str(ctx.exception))

    def test_refuses_non_block_device(self):
        plan = self.plan(target=self.source)
        with self.assertRaises(gi.SafetyRefusal) as ctx:
            self.validate(plan)
        self.assertIn("not a block device", str(ctx.exception))

    def test_refuses_mounted_target(self):
        with mock.patch.object(gi, "_mounted_devices",
                               lambda: {self.target: "/mnt/something"}):
            with self.assertRaises(gi.SafetyRefusal) as ctx:
                self.validate(self.plan())
        self.assertIn("mounted", str(ctx.exception))

    def test_refuses_partition_holding_an_operating_system(self):
        with mock.patch.object(gi, "_partition_has_os", lambda p: True):
            with self.assertRaises(gi.SafetyRefusal) as ctx:
                self.validate(self.plan())
        self.assertIn("operating system", str(ctx.exception))

    def test_refuses_whole_disk_in_partition_mode(self):
        plan = self.plan(target=os.path.join(self.devdir, "sdb"))
        with self.assertRaises(gi.SafetyRefusal) as ctx:
            self.validate(plan)
        self.assertIn("needs a partition", str(ctx.exception))

    def test_refuses_target_that_is_too_small(self):
        plan = self.plan(target=os.path.join(self.devdir, "sdd1"))
        with self.assertRaises(gi.SafetyRefusal) as ctx:
            self.validate(plan, allow_removable=True)
        self.assertIn("needs at least", str(ctx.exception))

    def test_refuses_removable_target_without_opt_in(self):
        plan = self.plan(target=os.path.join(self.devdir, "sdc1"))
        with self.assertRaises(gi.SafetyRefusal) as ctx:
            self.validate(plan)
        self.assertIn("removable", str(ctx.exception))

    def test_refuses_missing_source(self):
        plan = self.plan(source=os.path.join(self.temp, "missing"))
        with self.assertRaises(gi.SafetyRefusal) as ctx:
            self.validate(plan)
        self.assertIn("not found", str(ctx.exception))

    def test_refuses_efi_on_another_disk(self):
        efi = os.path.join(self.devdir, "sda1")
        plan = self.plan(efi=efi)
        with self.assertRaises(gi.SafetyRefusal) as ctx:
            self.validate(plan)
        self.assertIn("same disk", str(ctx.exception))

    def test_accepts_a_clean_plan(self):
        self.validate(self.plan())  # must not raise


class TestDryRun(InstallerTestCase):
    def test_dry_run_runs_no_commands_on_a_disk(self):
        runner = FakeRunner(dry_run=True)
        output = []
        plan = self.plan(mode="disk", target=os.path.join(self.devdir, "sdb"))
        installer = gi.Installer(plan, runner, confirm=lambda *a: True,
                                 progress=output.append)
        code = installer.execute()
        self.assertEqual(code, gi.EXIT_OK)
        self.assertEqual(runner.ran, [], "a dry run must never execute a command")
        self.assertEqual(runner.history, [])
        # The commands it *would* run are printed for the user.
        printed = "\n".join(output)
        self.assertIn("wipefs", printed)
        self.assertIn("grub-install", printed)

    def test_plan_only_mode_changes_nothing(self):
        with mock.patch.object(gi, "CommandRunner") as runner_cls:
            runner_cls.return_value.history = []
            runner_cls.return_value.dry_run = False
            code = gi.main(["--plan", "--target", self.target, "--source", self.source,
                            "--source-kind", "dir", "--mount-root", self.mount_root])
        self.assertEqual(code, gi.EXIT_OK)


class TestConfirmation(InstallerTestCase):
    def test_declining_the_phrase_writes_nothing(self):
        runner = FakeRunner()
        installer = gi.Installer(self.plan(), runner, confirm=lambda phrase, q: False)
        code = installer.execute()
        self.assertEqual(code, gi.EXIT_REFUSED)
        self.assertEqual(runner.ran, [])

    def test_confirmation_phrase_mentions_the_exact_target(self):
        seen = {}

        def confirm(phrase, question):
            seen["phrase"] = phrase
            return False

        gi.Installer(self.plan(), FakeRunner(), confirm=confirm).execute()
        self.assertIn("sdb1", seen["phrase"])

    def test_disk_mode_phrase_requires_the_disk_path(self):
        seen = {}

        def confirm(phrase, question):
            seen["phrase"] = phrase
            return False

        plan = self.plan(mode="disk", target=os.path.join(self.devdir, "sdb"))
        gi.Installer(plan, FakeRunner(), confirm=confirm).execute()
        self.assertEqual(seen["phrase"], "ERASE " + os.path.join(self.devdir, "sdb"))


class TestExecution(InstallerTestCase):
    def test_partition_install_writes_system_and_bootloader(self):
        runner = FakeRunner(effects={"cp": self.emulate_copy(self.mount_root),
                                     "blkid": self.fake_uuid})
        code = gi.Installer(self.plan(), runner, confirm=lambda *a: True).execute()
        self.assertEqual(code, gi.EXIT_OK, runner.programs())
        self.assertIn("mkfs.ext4", runner.programs())
        self.assertIn("mount", runner.programs())
        self.assertIn("grub-install", runner.programs())
        self.assertIn("umount", runner.programs())
        # No partition table was touched in partition mode.
        for destructive in ("sgdisk", "parted", "wipefs"):
            self.assertNotIn(destructive, runner.programs())
        fstab = open(os.path.join(self.mount_root, "etc", "fstab"), encoding="utf-8").read()
        self.assertIn("  /  ext4", fstab)
        self.assertIn("1111-2222-3333-4444", fstab)
        install_marker = json.load(open(os.path.join(self.mount_root, "etc",
                                                     "gba-install.json")))
        self.assertEqual(install_marker["mode"], "partition")

    def test_disk_install_partitions_before_formatting(self):
        runner = FakeRunner(effects={"cp": self.emulate_copy(self.mount_root),
                                     "blkid": self.fake_uuid})
        plan = self.plan(mode="disk", target=os.path.join(self.devdir, "sdb"))
        code = gi.Installer(plan, runner, confirm=lambda *a: True).execute()
        self.assertEqual(code, gi.EXIT_OK, runner.programs())
        programs = runner.programs()
        self.assertLess(programs.index("sgdisk"), programs.index("mkfs.ext4"))
        self.assertIn("mkfs.vfat", programs)
        self.assertIn("grub-install", programs)
        grub_call = [cmd for cmd in runner.ran if cmd[0] == "grub-install"][0]
        self.assertIn("--target=x86_64-efi", grub_call)
        self.assertIn("--no-nvram", grub_call, "firmware must never be modified")

    def test_failure_during_copy_never_touches_the_bootloader(self):
        runner = FakeRunner(fail_on=("cp",))
        code = gi.Installer(self.plan(), runner, confirm=lambda *a: True).execute()
        self.assertEqual(code, gi.EXIT_STEP_FAILED)
        self.assertNotIn("grub-install", runner.programs())
        self.assertIn("umount", runner.programs(), "the target must be unmounted on failure")

    def test_bootloader_failure_leaves_a_bootable_machine(self):
        runner = FakeRunner(effects={"cp": self.emulate_copy(self.mount_root),
                                     "blkid": self.fake_uuid})
        runner.fail_on = ("grub-install",)
        code = gi.Installer(self.plan(), runner, confirm=lambda *a: True).execute()
        self.assertEqual(code, gi.EXIT_BOOT_FAILED)
        self.assertIn("umount", runner.programs())

    def test_verification_catches_an_incomplete_copy(self):
        runner = FakeRunner()  # cp does nothing at all
        code = gi.Installer(self.plan(), runner, confirm=lambda *a: True).execute()
        self.assertEqual(code, gi.EXIT_STEP_FAILED)
        self.assertNotIn("grub-install", runner.programs())

    def test_bootloader_none_skips_grub(self):
        runner = FakeRunner(effects={"cp": self.emulate_copy(self.mount_root),
                                     "blkid": self.fake_uuid})
        plan = self.plan(bootloader="none")
        code = gi.Installer(plan, runner, confirm=lambda *a: True).execute()
        self.assertEqual(code, gi.EXIT_OK)
        self.assertNotIn("grub-install", runner.programs())


class TestExistingEfiPartition(InstallerTestCase):
    """A dual-boot machine's ESP belongs to Windows too: never format it."""

    def efi_plan(self):
        efi = os.path.join(self.devdir, "sdb2")
        open(efi, "w").close()
        self.disks[1].children.append(
            gi.Disk(efi, size=512 * 1024 ** 2, fstype="vfat", mountpoints=[],
                    label="SYSTEM"))
        return self.plan(efi=efi)

    def test_existing_esp_is_not_formatted(self):
        runner = FakeRunner(effects={"cp": self.emulate_copy(self.mount_root),
                                     "blkid": self.fake_uuid})
        code = gi.Installer(self.efi_plan(), runner, confirm=lambda *a: True).execute()
        self.assertEqual(code, gi.EXIT_OK, runner.programs())
        self.assertNotIn("mkfs.vfat", runner.programs())
        self.assertFalse(gi.InstallPlan("partition", "/dev/x", "/s", "dir").created_efi)
        self.assertTrue(gi.InstallPlan("disk", "/dev/x", "/s", "dir").created_efi)

    def test_foreign_fallback_loader_is_backed_up(self):
        plan = self.efi_plan()
        boot_dir = os.path.join(self.mount_root, "boot", "efi", "EFI", "BOOT")
        os.makedirs(boot_dir, exist_ok=True)
        with open(os.path.join(boot_dir, "BOOTX64.EFI"), "wb") as handle:
            handle.write(b"windows-ish loader")
        runner = FakeRunner(effects={"cp": self.emulate_copy(self.mount_root),
                                     "blkid": self.fake_uuid})
        code = gi.Installer(plan, runner, confirm=lambda *a: True).execute()
        self.assertEqual(code, gi.EXIT_OK, runner.programs())
        efi_root = os.path.join(self.mount_root, "boot", "efi", "EFI")
        backups = [name for name in os.listdir(efi_root) if name.startswith("BOOT.gba-backup")]
        self.assertEqual(len(backups), 1)
        saved = os.path.join(efi_root, backups[0], "BOOTX64.EFI")
        self.assertTrue(os.path.isfile(saved), "the other OS's loader must survive")

    def test_our_own_esp_from_disk_mode_is_formatted(self):
        runner = FakeRunner(effects={"cp": self.emulate_copy(self.mount_root),
                                     "blkid": self.fake_uuid})
        plan = self.plan(mode="disk", target=os.path.join(self.devdir, "sdb"))
        code = gi.Installer(plan, runner, confirm=lambda *a: True).execute()
        self.assertEqual(code, gi.EXIT_OK, runner.programs())
        self.assertIn("mkfs.vfat", runner.programs())


class TestInstalledBootConfiguration(InstallerTestCase):
    """The installed system must boot the shell, not the installer again."""

    def install(self):
        runner = FakeRunner(effects={"cp": self.emulate_copy(self.mount_root),
                                     "blkid": self.fake_uuid})
        # The image we copy is the installation medium: it carries the setup unit.
        os.makedirs(os.path.join(self.mount_root, "etc", "systemd", "system"),
                    exist_ok=True)
        os.makedirs(os.path.join(self.mount_root, "etc", "systemd", "system",
                                 "multi-user.target.wants"), exist_ok=True)
        open(os.path.join(self.mount_root, "etc", "systemd", "system",
                          "gba-setup.service"), "w").close()
        os.symlink("/etc/systemd/system/gba-setup.service",
                   os.path.join(self.mount_root, "etc", "systemd", "system",
                                "multi-user.target.wants", "gba-setup.service"))
        code = gi.Installer(self.plan(), runner, confirm=lambda *a: True).execute()
        self.assertEqual(code, gi.EXIT_OK, runner.programs())

    def test_setup_unit_is_removed_from_the_installed_system(self):
        self.install()
        units = os.path.join(self.mount_root, "etc", "systemd", "system")
        self.assertFalse(os.path.exists(os.path.join(units, "gba-setup.service")))
        self.assertFalse(os.path.exists(os.path.join(
            units, "multi-user.target.wants", "gba-setup.service")))

    def test_appliance_unit_is_enabled(self):
        self.install()
        units = os.path.join(self.mount_root, "etc", "systemd", "system")
        unit = open(os.path.join(units, "gba-appliance.service"), encoding="utf-8").read()
        self.assertIn("ExecStart=/usr/local/bin/gba-tty1-wrapper", unit)
        self.assertIn("TTYPath=/dev/tty1", unit)
        link = os.path.join(units, "multi-user.target.wants", "gba-appliance.service")
        self.assertTrue(os.path.islink(link))

    def test_grub_defaults_ask_for_the_appliance(self):
        self.install()
        defaults = open(os.path.join(self.mount_root, "etc", "default", "grub.d",
                                     "99_gba-appliance.cfg"), encoding="utf-8").read()
        self.assertIn("gba_mode=1", defaults)

    def test_boot_menu_has_an_escape_hatch(self):
        self.install()
        menu = open(os.path.join(self.mount_root, "boot", "grub", "grub.cfg"),
                    encoding="utf-8").read()
        self.assertIn("GBA OS (safe graphics / recovery)", menu)
        self.assertIn("nomodeset gba_safe=1", menu)
        self.assertIn("Boot the primary operating system", menu)
        self.assertNotIn("set default=1", menu)


class TestDiscovery(InstallerTestCase):
    def test_lsblk_json_is_parsed(self):
        payload = json.dumps({"blockdevices": [{
            "name": "sdb", "path": "/dev/sdb", "size": 128 * 1024 ** 3, "type": "disk",
            "model": "Target  ", "rm": False, "tran": "sata",
            "mountpoints": [None], "fstype": None, "label": None,
            "children": [{"name": "sdb1", "path": "/dev/sdb1", "size": 100 * 1024 ** 3,
                          "type": "part", "model": None, "rm": False, "tran": "sata",
                          "mountpoints": ["/mnt/old"], "fstype": "ext4", "label": "old"}],
        }]})
        disks = gi._parse_lsblk(payload)
        self.assertEqual(len(disks), 1)
        self.assertEqual(disks[0].path, "/dev/sdb")
        self.assertEqual(disks[0].model, "Target")
        self.assertFalse(disks[0].mounted)
        self.assertTrue(disks[0].children[0].mounted)

    def test_parent_disk_names(self):
        self.assertEqual(gi.parent_disk("/dev/sda1"), "/dev/sda")
        self.assertEqual(gi.parent_disk("/dev/nvme0n1p2"), "/dev/nvme0n1")
        self.assertEqual(gi.parent_disk("/dev/mmcblk0p1"), "/dev/mmcblk0")
        self.assertEqual(gi.parent_disk("/dev/sda"), "/dev/sda")

    def test_size_formatting(self):
        self.assertEqual(gi.Disk("/dev/x", size=128 * 1024 ** 3).size_text(), "128.0 GB")
        self.assertEqual(gi.Disk("/dev/x", size=512).size_text(), "512 B")


if __name__ == "__main__":
    unittest.main()
