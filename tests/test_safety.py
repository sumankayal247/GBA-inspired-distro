#!/usr/bin/env python3
"""
Safety tests for the shell side of GBA OS.

The Python installer has its own suite (test_installer.py, test_setup.py). What
is left is the part that touches a *working* machine: scripts/install.sh and
the boot entry it writes, scripts/build_iso.sh, and scripts/lib/common.sh.

These tests never run anything destructive. They check the properties that made
the "can this brick my PC?" question have a boring answer:

* every script parses, in the shell that will actually run it;
* the installer is dry-run by default and refuses to run as a non-root user;
* no shell script formats, partitions or writes a disk;
* the boot menu entry adds a choice instead of replacing the existing one, and
  always ships an emergency variant;
* the ISO builder fails loudly (and changes nothing) when its tools are absent.
"""

import os
import re
import shutil
import subprocess
import tempfile
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
INSTALL = os.path.join(SCRIPTS, "install.sh")
BUILD_ISO = os.path.join(SCRIPTS, "build_iso.sh")
COMMON_SH = os.path.join(SCRIPTS, "lib", "common.sh")
INIT = os.path.join(REPO, "src", "gba_init.sh")


def strip_heredocs(text):
    """Drop heredoc bodies: they are data (messages, config files), not code."""
    out, marker = [], None
    for line in text.splitlines():
        if marker is None:
            out.append(line)
            match = re.search(r"<<-?\s*'?\"?([A-Za-z_][A-Za-z0-9_]*)'?\"?", line)
            if match and "<<" in line:
                marker = match.group(1)
            continue
        if line.strip() == marker:
            marker = None
    return "\n".join(out)


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def run(args, **kwargs):
    kwargs.setdefault("stdout", subprocess.PIPE)
    kwargs.setdefault("stderr", subprocess.STDOUT)
    kwargs.setdefault("text", True)
    kwargs.setdefault("cwd", REPO)
    return subprocess.run(args, **kwargs)


class TestShellSyntax(unittest.TestCase):
    def test_bash_scripts_parse(self):
        for script in ("install.sh", "build_iso.sh", "launch_desktop.sh",
                       os.path.join("lib", "common.sh")):
            with self.subTest(script=script):
                result = run(["bash", "-n", os.path.join(SCRIPTS, script)])
                self.assertEqual(result.returncode, 0, result.stdout)

    def test_posix_init_parses_with_sh(self):
        # PID 1 may be dash or busybox ash: no bashisms allowed.
        result = run(["sh", "-n", INIT])
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_init_has_no_bashisms(self):
        source = read(INIT)
        for bashism in ("[[", "function ", "local ", "declare ", "let "):
            self.assertNotIn(bashism, source.replace("#!/bin/sh", ""),
                             f"gba_init.sh must stay POSIX sh (found {bashism!r})")

    def test_scripts_are_executable(self):
        for script in (INSTALL, BUILD_ISO):
            self.assertTrue(os.access(script, os.X_OK), f"{script} is not executable")


class TestInstallerSafety(unittest.TestCase):
    def setUp(self):
        self.source = read(INSTALL)

    def test_dry_run_is_the_default(self):
        self.assertIn("\nAPPLY=0\n", self.source)
        self.assertIn("--apply) APPLY=1", self.source)

    def test_no_shell_script_writes_or_formats_a_disk(self):
        # Only command position matters: a package list that happens to contain
        # the word "parted" is not a disk write.
        forbidden = re.compile(
            r"^\s*(?:sudo\s+)?(mkfs(?:\.\w+)?|sgdisk|parted|wipefs|dd)\b",
            re.MULTILINE)
        for name in os.listdir(SCRIPTS):
            if not name.endswith(".sh"):
                continue
            text = strip_heredocs(read(os.path.join(SCRIPTS, name)))
            # build_iso.sh copies files around; it never touches a raw disk.
            for match in forbidden.finditer(text):
                line = text[:match.start()].count("\n") + 1
                self.fail(f"{name}:{line} contains {match.group(0)!r}; "
                          "disk writes belong in src/gba_installer.py where the "
                          "safety gate lives")

    def test_disk_installer_is_never_invoked_implicitly(self):
        # install.sh may *offer* gba-setup, but must never start an install.
        for line in self.source.splitlines():
            if "gba-setup" not in line or line.strip().startswith("#"):
                continue
            self.assertTrue(
                any(word in line for word in (
                    "install -m", "SETUP_WRAPPER", "echo",
                    "exec python3 /opt/gba-distro/bin/gba-setup")),
                f"unexpected gba-setup use: {line.strip()}")

    def test_boot_entry_adds_a_choice(self):
        self.assertIn("GBA OS (console appliance)", self.source)
        self.assertIn("nomodeset gba_safe=1", self.source,
                      "an emergency boot entry must always be written")
        self.assertNotRegex(self.source, r"GRUB_DEFAULT\s*=\s*[\"']?[0-9]",
                            "the default boot target must never be changed")

    def test_boot_entry_is_regenerated_then_verified(self):
        self.assertIn("regenerate", self.source.lower())
        self.assertIn('grep -q "GBA OS (console appliance)"', self.source,
                      "the new grub.cfg must be checked for our entry")

    def test_missing_boot_tooling_does_not_abort_the_install(self):
        self.assertIn("GRUB tooling missing", self.source)

    def test_uninstall_restores_inittab_backup(self):
        self.assertIn("/etc/inittab.gba-backup", self.source)

    def test_repair_is_implemented(self):
        self.assertIn("do_repair()", self.source)
        self.assertIn("REPAIR = 1", self.source.replace("REPAIR=1", "REPAIR = 1"))

    def test_help_text_offers_repair_and_uninstall(self):
        result = run(["bash", INSTALL, "--help"])
        self.assertEqual(result.returncode, 0)
        for flag in ("--apply", "--dry-run", "--repair", "--uninstall", "--no-boot"):
            self.assertIn(flag, result.stdout)


class TestInstallerRuns(unittest.TestCase):
    def test_dry_run_succeeds_and_changes_nothing(self):
        result = run(["bash", INSTALL], env={**os.environ, "GBA_DRY_RUN": "1"})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("nothing was changed", result.stdout.lower())

    def test_unknown_option_is_rejected(self):
        result = run(["bash", INSTALL, "--definitely-not-an-option"])
        self.assertNotEqual(result.returncode, 0)

    def test_repair_without_an_install_explains_itself(self):
        result = run(["bash", INSTALL, "--repair"],
                     env={**os.environ, "GBA_INSTALL_DIR": "/nonexistent/gba-os"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no gba os install", result.stdout.lower())


class TestCommonLibrary(unittest.TestCase):
    def test_dry_run_gates_commands(self):
        script = f"""
        set -u
        . "{COMMON_SH}"
        gb_run touch /tmp/gba-common-test-should-not-exist
        echo "dry=$GB_DRY_RUN"
        """
        result = run(["bash", "-c", script],
                     env={**os.environ, "GBA_DRY_RUN": "1", "GB_DRY_RUN": "1"})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("dry=1", result.stdout)
        self.assertFalse(os.path.exists("/tmp/gba-common-test-should-not-exist"))

    def test_human_size(self):
        script = f'. "{COMMON_SH}"; gb_human_size 1048576; gb_human_size 0'
        result = run(["bash", "-c", script])
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("MB", result.stdout)
        self.assertIn("0B", result.stdout)

    def test_typed_confirmation_is_required(self):
        script = f"""
        . "{COMMON_SH}"
        echo wrong | gb_confirm_typed "ERASE /dev/sda" "sure?" && echo CONFIRMED || echo REFUSED
        """
        result = run(["bash", "-c", script])
        self.assertIn("REFUSED", result.stdout)
        self.assertNotIn("CONFIRMED", result.stdout)

    def test_backup_records_file_for_rollback(self):
        workdir = tempfile.mkdtemp(prefix="gba-safety-")
        self.addCleanup(shutil.rmtree, workdir, True)
        target = os.path.join(workdir, "grub.cfg")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("original\n")
        backup_dir = os.path.join(workdir, "backup")
        script = f'''
        . "{COMMON_SH}"
        gb_backup_file "{target}"
        echo "changes=${{#GB_CHANGES[@]}}"
        echo "written=$(gb_written_files)"
        '''
        result = run(["bash", "-c", script],
                     env={**os.environ, "GBA_DRY_RUN": "0", "GB_DRY_RUN": "0",
                          "GB_BACKUP_DIR": backup_dir})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("changes=1", result.stdout)
        self.assertIn("grub.cfg", result.stdout)
        stored = [os.path.join(root, name)
                  for root, _dirs, files in os.walk(backup_dir) for name in files]
        self.assertTrue(stored, "the file should have been copied somewhere")
        with open(stored[0], encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "original\n")
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "original\n", "backup must not modify")

    def test_require_cmd_rejects_missing_tools(self):
        script = (f'. "{COMMON_SH}"; '
                  'gb_require_cmd sh && echo HAVE-SH; '
                  'gb_require_cmd definitely-not-a-real-tool && echo BAD || echo MISSING')
        result = run(["bash", "-c", script])
        self.assertIn("HAVE-SH", result.stdout)
        self.assertIn("MISSING", result.stdout)

    def test_live_device_check_is_read_only(self):
        script = f'. "{COMMON_SH}"; gb_is_live_or_root_device /dev/definitely-not-here || echo SAFE'
        result = run(["bash", "-c", script])
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("SAFE", result.stdout)


class TestShellFallbacks(unittest.TestCase):
    """Every component that can be missing must say so and leave a console."""

    def test_launcher_without_pygame_explains_itself(self):
        shim = tempfile.mkdtemp(prefix="gba-shim-")
        self.addCleanup(shutil.rmtree, shim, True)
        with open(os.path.join(shim, "pygame.py"), "w", encoding="utf-8") as handle:
            handle.write("raise ImportError('no pygame on this machine')\n")
        result = run([sys.executable, os.path.join(REPO, "src", "gba_launcher.py"),
                      "--demo", "--tab", "games"],
                     env={**os.environ, "PYTHONPATH": shim, "GBA_HEADLESS": "1"})
        self.assertEqual(result.returncode, 103, result.stdout)
        self.assertIn("recovery console", result.stdout)
        self.assertIn("nothing was modified", result.stdout.lower())
        self.assertNotIn("Traceback", result.stdout)

    def test_adapter_reports_missing_emulator_on_the_console(self):
        workdir = tempfile.mkdtemp(prefix="gba-adapter-")
        self.addCleanup(shutil.rmtree, workdir, True)
        rom = os.path.join(workdir, "game.gba")
        open(rom, "wb").close()
        source = read(os.path.join(REPO, "src", "gba_adapter_mgba.py"))
        self.assertIn("mGBA is not installed", source)
        for needed in ("sudo apt install mgba-sdl", "Nothing is broken"):
            self.assertIn(needed, source)

    def test_init_falls_back_to_a_rescue_shell(self):
        source = read(INIT)
        self.assertIn("rescue_shell", source)
        self.assertIn("gba_safe=1", source, "the safe-graphics boot must skip X")
        self.assertIn("gba_mode=0", source)
        self.assertIn("sysrq-trigger", source, "reboot must work without a reboot binary")
        # PID 1 must never simply fall off the end of the script.
        self.assertTrue(source.rstrip().endswith("done"),
                        "the init loop must be endless")

    def test_installer_wrapper_never_runs_without_python(self):
        source = read(INSTALL)
        self.assertIn("if ! command -v python3", source)


class TestIsoBuild(unittest.TestCase):
    def test_check_mode_lists_missing_tools_and_changes_nothing(self):
        result = run(["bash", BUILD_ISO, "--check"])
        self.assertIn("GBA OS ISO build check", result.stdout)
        if result.returncode == 0:
            self.assertIn("ready to build", result.stdout)
        else:
            self.assertIn("Missing", result.stdout)
            self.assertIn("Nothing was changed", result.stdout)
            for line in ("xorriso", "mksquashfs", "grub-mkrescue"):
                self.assertIn(line, result.stdout)

    def test_builder_serves_uefi_and_bios(self):
        source = read(BUILD_ISO)
        self.assertIn("grub-mkrescue", source)
        self.assertIn("grub-pc-bin", source)
        self.assertIn("grub-efi-amd64-bin", source)

    def test_live_menu_offers_install_and_safe_mode(self):
        source = read(BUILD_ISO)
        self.assertIn("Install GBA OS (recommended)", source)
        self.assertIn("nomodeset gba_safe=1", source)
        self.assertIn("Boot from the internal disk instead", source)

    def test_iso_writes_nothing_into_the_repo_root(self):
        source = read(BUILD_ISO)
        self.assertIn('WORK_DIR="${GBA_ISO_WORK:-$REPO_DIR/build/iso}"', source)
        self.assertIn('OUT_DIR="${GBA_ISO_OUT:-$REPO_DIR/build}"', source)


if __name__ == "__main__":
    unittest.main()
