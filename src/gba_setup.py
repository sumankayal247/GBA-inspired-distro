#!/usr/bin/env python3
"""
GBA OS guided setup.

This is what a person sees when the GBA OS stick boots. It is deliberately
small: one screen of choices, one confirmation, a progress list, and then the
machine either reboots into GBA OS or tells you plainly that nothing was
changed.

Design rules (see docs/DECISIONS.md D-037..D-041):

* The default is always the *least* destructive option available: an empty
  partition on a disk that is not the one we booted from.
* There is never more than one question whose answer is not a default.
* Every failure prints what happened and what state the machine is in. The
  answer is never "reinstall Windows".
* No question is ever asked twice: declining leaves the machine untouched and
  drops you at a shell.

The real work lives in gba_installer.py; this module only decides *what* to
install onto and talks to the person doing the installing.
"""

from __future__ import annotations

import os
import sys
from typing import List, Optional, Sequence, TextIO

try:
    import gba_installer as installer
except ImportError:  # pragma: no cover - running from another directory
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import gba_installer as installer

BANNER = """
  ####   ####    #     ####    ####   ####
 #    #  #   #  # #   #    #  #    #  #   #
 #  ##   ####  #   #  #    #  #    #  ####
 #   #   #   # #####  #    #  #    #  #
  ###    ####  #   #   ####    ####   #
==============================================="""

WELCOME = """
  Setup takes about five minutes and asks two questions.

  Your current operating system is NOT removed. GBA OS is added next to it,
  and you can still boot it from the boot menu.

  Nothing is written until you confirm, and every step can be undone by
  reinstalling your old boot menu (the setup prints how).
"""


class Candidate:
    """One thing GBA OS could be installed onto."""

    def __init__(self, kind: str, path: str, size: int, label: str,
                 safe: bool, reason: str, removable: bool = False):
        self.kind = kind          # "partition" | "disk"
        self.path = path
        self.size = size
        self.label = label
        self.safe = safe          # True when it cannot destroy another OS
        self.reason = reason
        self.removable = removable

    @property
    def risk(self) -> str:
        return "keeps everything else" if self.safe else "ERASES this whole disk"

    def size_text(self) -> str:
        return installer.Disk(self.path, size=self.size).size_text()

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"Candidate({self.kind}, {self.path}, safe={self.safe})"

    def __eq__(self, other):
        return (isinstance(other, Candidate) and self.path == other.path
                and self.kind == other.kind)


def _empty_enough(partition: "installer.Disk") -> bool:
    """An existing non-OS filesystem is fine; it just gets formatted."""
    return not partition.mountpoints


def candidates(disks: Sequence["installer.Disk"]) -> List[Candidate]:
    """
    Everything installable, safest first.

    Whole disks are only offered when they contain no partitions at all, or
    when every partition on them is empty. A disk holding any OS is never
    listed: the person would have to pass --mode disk explicitly.
    """
    protected = {installer.real_device(installer.parent_disk(path))
                 for path in _live_devices()}

    found: List[Candidate] = []
    for disk in disks:
        if installer.real_device(disk.path) in protected:
            continue
        if disk.mounted or any(child.mounted for child in disk.children):
            continue
        holder = any(_looks_like_os(child) for child in disk.children)

        for child in disk.children:
            if child.size < installer.MIN_ROOT_BYTES:
                continue
            if not _empty_enough(child) or _looks_like_os(child):
                continue
            found.append(Candidate(
                "partition", child.path, child.size,
                label=(child.fstype or "empty").strip() or "empty",
                safe=True,
                reason="a free partition on " + disk.path,
                removable=bool(child.removable or disk.removable)))

        if not holder and disk.size >= installer.MIN_ROOT_BYTES:
            found.append(Candidate(
                "disk", disk.path, disk.size,
                label="whole disk" + (" (removable)" if disk.removable else ""),
                safe=False,
                reason="the entire disk is free",
                removable=bool(disk.removable)))

    # Safest first, then prefer the machine's own disks over removable media,
    # then the largest free space (a bigger target is a better default).
    found.sort(key=lambda c: (0 if c.safe else 1,
                              1 if c.removable else 0,
                              -c.size, c.path))
    return found


def _live_devices() -> List[str]:
    """Every device that is in use right now, so it can never be offered."""
    devices = set(installer._live_or_root_disks())
    for device, mountpoint in installer._mounted_devices().items():
        if not device.startswith("/dev/"):
            continue
        # /, /boot and anything under the live medium belong to the running system.
        if mountpoint in ("/", "/boot", "/boot/efi", "/usr") or \
                "live" in mountpoint or mountpoint.startswith("/run/"):
            devices.add(installer.real_device(device))
    return sorted(devices)


def _looks_like_os(partition: "installer.Disk") -> bool:
    try:
        return bool(installer._partition_has_os(partition.path))
    except Exception:  # never let a probe failure hide a disk from safety
        return True


def recommend(found: Sequence[Candidate]) -> Optional[Candidate]:
    """
    The safest suitable target, or None when only destructive options exist.

    An internal partition always wins over a USB stick: installing onto a stick
    that is then removed leaves the machine unbootable, which is precisely the
    kind of surprise this setup exists to prevent.
    """
    safe = [c for c in found if c.safe]
    internal = [c for c in safe if not c.removable]
    if internal:
        return internal[0]
    if safe:
        return safe[0]
    return None


# --------------------------------------------------------------------------- #
# Talking to the person
# --------------------------------------------------------------------------- #

def _ask(prompt: str, stream_in, stream_out, default: str = "") -> str:
    stream_out.write(prompt)
    stream_out.flush()
    answer = stream_in.readline()
    if not answer:
        return default
    return answer.strip() or default


def show_menu(found: Sequence[Candidate], out: TextIO) -> None:
    out.write("\n  Where should GBA OS be installed?\n\n")
    for index, candidate in enumerate(found, start=1):
        marker = " *" if candidate.safe else "  "
        out.write(f"  {index}){marker} {candidate.path:<16} {candidate.size_text():>9}  "
                  f"{candidate.label:<16} {candidate.risk}\n")
    out.write("\n  *  recommended: these options keep your current system "
              "bootable.\n")


def describe_targets(found: Sequence[Candidate]) -> List[str]:
    """Plain-text listing, used by tests and by --list-targets."""
    return [f"{c.path} {c.size_text()} {c.kind} "
            f"{'safe' if c.safe else 'destructive'}" for c in found]


def _progress(message: str) -> None:
    print("   " + message, flush=True)


def _confirm(choice: Candidate, stream_in, stream_out) -> bool:
    if choice.kind == "partition":
        answer = _ask(f"\n  Install GBA OS on {choice.path}, keeping your current "
                      "system? [Y/n] ", stream_in, stream_out, "y")
        return answer.lower() in ("y", "yes", "")
    phrase = installer.CONFIRM_DISK.format(disk=choice.path)
    stream_out.write(f"\n  !! Everything on {choice.path} will be erased, including "
                     "any partitions.\n")
    stream_out.write("  !! Your other disks are not touched.\n\n")
    answer = _ask(f'  Type "{phrase}" to continue (or press Enter to cancel): ',
                  stream_in, stream_out)
    return answer == phrase


def explain_exit(code: int, out: TextIO) -> None:
    messages = {
        installer.EXIT_REFUSED: (
            "Setup stopped before writing anything.",
            "Your computer is exactly as it was. Nothing was changed."),
        installer.EXIT_STEP_FAILED: (
            "Setup could not finish copying GBA OS.",
            "Your boot menu was not modified, so this computer still starts "
            "its usual operating system. You can retry, or choose another "
            "target."),
        installer.EXIT_BOOT_FAILED: (
            "GBA OS was copied, but the boot entry could not be written.",
            "Your existing boot menu is intact, so this computer still starts "
            "normally. Retrying setup is safe."),
        installer.EXIT_USAGE: (
            "Setup was called with an option it does not understand.",
            "Nothing was changed."),
    }
    summary, detail = messages.get(
        code, ("Setup failed unexpectedly.", "Nothing was changed."))
    out.write("\n  " + summary + "\n  " + detail + "\n")
    if code == installer.EXIT_STEP_FAILED:
        out.write("\n  If this keeps happening, check that the target is not "
                  "read-only and run 'gba-selftest' for a hardware check.\n")


def explain_success(plan, out: TextIO) -> None:
    out.write("\n  GBA OS is installed.\n\n")
    out.write("  Next boot, choose \"GBA OS\" in the boot menu.\n")
    out.write("  Remove the installation stick and press Enter to restart.\n\n")
    out.write("  Your previous operating system is still installed; pick it from\n"
              "  the same boot menu at any time.\n")


def run(stream_in=None, stream_out=None, runner=None, dry_run: bool = False,
        source: str = "", source_kind: str = "", mount_root: str = "") -> int:
    stream_in = stream_in or sys.stdin
    stream_out = stream_out or sys.stdout

    stream_out.write(BANNER + WELCOME)

    runner = runner or installer.CommandRunner(dry_run=dry_run, logger=_progress)
    disks = installer.probe_disks(runner)
    found = candidates(disks)

    if not found:
        stream_out.write(
            "\n  No free space was found for GBA OS.\n\n"
            "  This is not a problem with your computer. GBA OS needs either an\n"
            "  empty partition of at least 8 GB or a spare disk. Free some space\n"
            "  (or plug in a second disk), then run 'gba-install' again.\n\n"
            "  Your system was not modified.\n")
        return installer.EXIT_REFUSED

    show_menu(found, stream_out)
    default = recommend(found) or found[0]
    default_index = found.index(default) + 1
    answer = _ask(f"\n  Number [{default_index}], or Q to quit: ",
                  stream_in, stream_out, str(default_index))
    if answer.lower() in ("q", "quit", "exit"):
        stream_out.write("\n  Cancelled. Nothing was changed.\n")
        return installer.EXIT_REFUSED
    try:
        choice = found[int(answer) - 1]
    except (ValueError, IndexError):
        stream_out.write("\n  That was not one of the listed numbers. "
                         "Nothing was changed.\n")
        return installer.EXIT_USAGE

    if choice.removable:
        stream_out.write(f"\n  {choice.path} looks like removable media.\n"
                         "  Installing onto it will not start this computer "
                         "without the stick.\n")
        if _ask("  Continue anyway? [y/N] ", stream_in, stream_out,
                "n").lower() not in ("y", "yes"):
            stream_out.write("\n  Cancelled. Nothing was changed.\n")
            return installer.EXIT_REFUSED

    if not _confirm(choice, stream_in, stream_out):
        stream_out.write("\n  Cancelled. Nothing was changed.\n")
        return installer.EXIT_REFUSED

    source, source_kind = _resolve_source(source, source_kind, stream_out)
    plan = installer.InstallPlan(
        mode=choice.kind,
        target=choice.path,
        source=source,
        source_kind=source_kind,
        efi=_same_disk_efi(disks, choice),
        bootloader="auto",
        mount_root=mount_root or installer.DEFAULT_MOUNT_ROOT,
    )

    stream_out.write("\n  Installing. This takes a few minutes; please do not "
                     "unplug anything.\n\n")
    try:
        installer.validate_plan(plan, runner, disks)
    except installer.SafetyRefusal as exc:
        stream_out.write(f"\n  Safety check refused this install:\n    {exc}\n")
        explain_exit(installer.EXIT_REFUSED, stream_out)
        return installer.EXIT_REFUSED

    code = installer.Installer(plan, runner, confirm=lambda *_: True,
                               progress=_progress).execute()
    if code == installer.EXIT_OK:
        explain_success(plan, stream_out)
    else:
        explain_exit(code, stream_out)
    return code


def _same_disk_efi(disks: Sequence["installer.Disk"], choice: Candidate) -> str:
    """Reuse an EFI partition sitting on the target disk, if there is one."""
    parent = installer.real_device(installer.parent_disk(choice.path))
    for disk in disks:
        if installer.real_device(disk.path) != parent:
            continue
        for child in disk.children:
            if (child.fstype or "").lower() in ("vfat", "fat32", "efi") and \
                    "efi" in " ".join(child.mountpoints + [child.label or ""]).lower():
                return child.path
    return ""


def _resolve_source(source: str, source_kind: str, out: TextIO):
    """The live image ships the rootfs as a squashfs next to this script."""
    if source:
        return source, source_kind or (
            "squashfs" if source.endswith((".squashfs", ".sqfs")) else "dir")
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.join(here, "filesystem.squashfs"),
                      "/run/live/medium/live/filesystem.squashfs",
                      "/lib/live/mount/medium/live/filesystem.squashfs",
                      "/cdrom/live/filesystem.squashfs"):
        if os.path.isfile(candidate):
            return candidate, "squashfs"
    for candidate in (os.path.join(here, "rootfs"), "/opt/gba-distro/rootfs"):
        if os.path.isdir(candidate):
            return candidate, "dir"
    out.write("\n  Warning: the GBA OS filesystem image was not found next to "
              "the installer;\n  setup will copy /opt/gba-distro instead.\n")
    return os.path.join(here, ".."), "dir"


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="gba-setup", description=(
        "Install GBA OS on this computer. Run without arguments for the "
        "guided setup."))
    parser.add_argument("--list-targets", action="store_true",
                        help="show where GBA OS could be installed and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would happen, change nothing")
    parser.add_argument("--source", default="", help="root filesystem to install")
    parser.add_argument("--source-kind", default="", choices=("", "dir", "squashfs", "tarball"))
    parser.add_argument("--mount-root", default="",
                        help="scratch mount point for the target (testing aid)")
    args = parser.parse_args(argv)

    if args.list_targets:
        runner = installer.CommandRunner(dry_run=True, logger=lambda _m: None)
        for line in describe_targets(candidates(installer.probe_disks(runner))):
            print(line)
        return installer.EXIT_OK

    try:
        return run(dry_run=args.dry_run, source=args.source,
                   source_kind=args.source_kind, mount_root=args.mount_root)
    except (EOFError, KeyboardInterrupt):
        print("\n  Cancelled. Nothing was changed.")
        return installer.EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
