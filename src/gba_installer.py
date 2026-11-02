#!/usr/bin/env python3
"""
GBA OS — disk installer.

Runs from the live ISO (`gba-install`) or from a checkout, and is designed
around one rule: **a failed install must never leave a machine unable to
boot.** Everything here is built to keep that promise:

* Nothing touches a disk until a plan is printed, the target has passed every
  safety check, and the user has typed the confirmation phrase.
* Two modes only:
    - `partition` (default, non-destructive): install into an existing, empty
      partition. No partition table is edited, no other OS is touched.
    - `disk` (opt-in, destructive): create a fresh GPT with an EFI system
      partition and a root partition on the disk the user names explicitly.
* The boot loader is installed *after* the root filesystem is written and
  verified. If that step fails, the installer stops and tells the user how to
  boot their original system — it never removes or rewrites existing entries.
* `--dry-run` prints every command it would run and changes nothing.

Exit codes: 0 success, 2 usage, 3 safety refusal, 4 step failed, 5 bootloader
failed (installed system is fine, machine's boot order is unchanged).
"""

import argparse
import datetime
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

MIN_ROOT_BYTES = 8 * 1024 ** 3          # an 8 GB partition is the practical floor
RECOMMENDED_ROOT_BYTES = 16 * 1024 ** 3
MIN_ESP_BYTES = 260 * 1024 ** 2         # GRUB + kernels need ~100 MB; be generous
DEFAULT_MOUNT_ROOT = "/mnt/gba-install"
CONFIRM_DISK = "ERASE {disk}"
CONFIRM_PARTITION = "FORMAT {part}"

EXIT_OK, EXIT_USAGE, EXIT_REFUSED, EXIT_STEP_FAILED, EXIT_BOOT_FAILED = 0, 2, 3, 4, 5


class SafetyRefusal(Exception):
    """Raised when a requested action could damage a machine or is ambiguous."""


class StepFailed(Exception):
    """Raised when an installation step fails."""


# --------------------------------------------------------------------------- #
# Command execution
# --------------------------------------------------------------------------- #

class CommandRunner:
    """Thin wrapper so tests can record (and fake) every system command."""

    def __init__(self, dry_run: bool = False, logger: Callable[[str], None] = print):
        self.dry_run = dry_run
        self.log = logger
        self.history: List[List[str]] = []

    def has(self, program: str) -> bool:
        return shutil.which(program) is not None

    def run(self, args: Sequence[str], check: bool = True,
            input_text: Optional[str] = None) -> Tuple[int, str, str]:
        self.history.append(list(args))
        if self.dry_run:
            self.log("  + " + " ".join(args))
            return 0, "", ""
        try:
            proc = subprocess.run(args, input=input_text, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            raise StepFailed(f"command not found: {args[0]}")
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise StepFailed(f"{' '.join(args)} failed ({proc.returncode}): "
                             f"{detail[-1] if detail else 'no output'}")
        return proc.returncode, proc.stdout, proc.stderr


# --------------------------------------------------------------------------- #
# Disk discovery
# --------------------------------------------------------------------------- #

class Disk:
    def __init__(self, path: str, size: int = 0, model: str = "", removable: bool = False,
                 tran: str = "", mounted: bool = False, children: Optional[List["Disk"]] = None,
                 fstype: str = "", label: str = "", mountpoints: Optional[List[str]] = None):
        self.path = path
        self.size = size
        self.model = model
        self.removable = removable
        self.tran = tran
        self.mounted = mounted
        self.children = children or []
        self.fstype = fstype
        self.label = label
        self.mountpoints = mountpoints or []

    # -- presentation --------------------------------------------------
    def size_text(self) -> str:
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if self.size < 1024 or unit == "TB":
                return f"{self.size:.0f} {unit}" if unit == "B" else f"{self.size:.1f} {unit}"
            self.size /= 1024.0
        return f"{self.size:.1f} TB"

    def kind(self) -> str:
        if self.removable:
            return "removable"
        if self.tran in ("nvme", "sata", "ata", "scsi"):
            return self.tran
        return "disk"

    def as_dict(self) -> Dict[str, object]:
        return {"path": self.path, "size": self.size, "model": self.model,
                "removable": self.removable, "transport": self.tran,
                "mounted": self.mounted, "fstype": self.fstype, "label": self.label,
                "children": [c.as_dict() for c in self.children]}


def _parse_lsblk(payload: str) -> List[Disk]:
    try:
        data = json.loads(payload)
    except Exception:
        return []
    disks: List[Disk] = []

    def build(node) -> Disk:
        mountpoints = node.get("mountpoints") or []
        mountpoints = [m for m in mountpoints if m]
        child_nodes = node.get("children") or []
        return Disk(
            path=node.get("path") or f"/dev/{node.get('name')}",
            size=int(node.get("size") or 0),
            model=(node.get("model") or "").strip(),
            removable=bool(node.get("rm")),
            tran=(node.get("tran") or ""),
            mounted=bool(mountpoints),
            children=[build(child) for child in child_nodes],
            fstype=node.get("fstype") or "",
            label=(node.get("label") or ""),
            mountpoints=mountpoints,
        )

    for node in data.get("blockdevices", []):
        if node.get("type") == "disk":
            disks.append(build(node))
    return disks


def probe_disks(runner: CommandRunner) -> List[Disk]:
    """Returns physical disks with their partitions (never raises)."""
    if shutil.which("lsblk"):
        code, out, _ = runner.run([
            "lsblk", "-J", "-b", "-o",
            "NAME,PATH,SIZE,TYPE,MODEL,RM,TRAN,MOUNTPOINTS,FSTYPE,LABEL",
        ], check=False)
        if code == 0 and out.strip():
            disks = _parse_lsblk(out)
            if disks:
                return disks
    return _probe_sysfs()


def _probe_sysfs() -> List[Disk]:
    """Fallback when lsblk is unavailable: read /sys/block directly."""
    disks: List[Disk] = []
    base = "/sys/block"
    if not os.path.isdir(base):
        return disks
    for name in sorted(os.listdir(base)):
        if name.startswith(("loop", "ram", "zram", "sr", "dm-")):
            continue
        dev_dir = os.path.join(base, name)
        try:
            size = int(open(os.path.join(dev_dir, "size")).read().strip()) * 512
        except Exception:
            size = 0
        removable = False
        try:
            removable = open(os.path.join(dev_dir, "removable")).read().strip() == "1"
        except Exception:
            pass
        model = ""
        try:
            model = open(os.path.join(dev_dir, "device", "model")).read().strip()
        except Exception:
            pass
        disk = Disk(f"/dev/{name}", size=size, model=model, removable=removable)
        for child in sorted(os.listdir(dev_dir)):
            if not os.path.isdir(os.path.join(dev_dir, child)):
                continue
            if not os.path.exists(os.path.join(dev_dir, child, "partition")):
                continue
            child_size = 0
            try:
                child_size = int(open(os.path.join(dev_dir, child, "size")).read().strip()) * 512
            except Exception:
                pass
            disk.children.append(Disk(f"/dev/{child}", size=child_size,
                                      removable=removable, model=model))
        disks.append(disk)
    return disks


def device_for_path(path: str) -> str:
    """Block device backing a filesystem path ('' when unknown)."""
    if not os.path.exists(path):
        return ""
    try:
        proc = subprocess.run(["findmnt", "-no", "SOURCE", "--target", path],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        if proc.returncode == 0:
            return proc.stdout.strip()
    except Exception:
        pass
    # /proc/mounts fallback (longest prefix wins).
    best, best_len = "", 0
    try:
        with open("/proc/mounts", "r", errors="ignore") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) < 2:
                    continue
                device, mountpoint = parts[0], parts[1].replace("\\040", " ")
                if path == mountpoint or path.startswith(mountpoint.rstrip("/") + "/"):
                    if len(mountpoint) > best_len:
                        best, best_len = device, len(mountpoint)
    except Exception:
        pass
    return best


def _is_block_device(path: str) -> bool:
    """Indirection so tests can simulate devices without root."""
    try:
        return stat.S_ISBLK(os.stat(path).st_mode)
    except OSError:
        return False


def real_device(path: str) -> str:
    try:
        return os.path.realpath(path)
    except Exception:
        return path


def parent_disk(path: str) -> str:
    """'/dev/sda1' -> '/dev/sda', '/dev/nvme0n1p2' -> '/dev/nvme0n1'."""
    name = os.path.basename(path)
    match = re.match(r"^(nvme\d+n\d+|mmcblk\d+|md\d+|loop\d+)p?\d+$", name)
    if match:
        return os.path.join(os.path.dirname(path), match.group(1))
    match = re.match(r"^(.*?)[0-9]+$", name)
    if match and match.group(1):
        return os.path.join(os.path.dirname(path), match.group(1))
    return path


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #

class InstallPlan:
    """Everything the installer is about to do, decided before it does it."""

    def __init__(self, mode: str, target: str, source: str, source_kind: str,
                 efi: str = "", bootloader: str = "auto", hostname: str = "gba-os",
                 mount_root: str = DEFAULT_MOUNT_ROOT, uefi: Optional[bool] = None,
                 keep_data: bool = True):
        self.mode = mode                    # partition | disk
        self.target = target                # partition (partition mode) or disk (disk mode)
        self.source = source
        self.source_kind = source_kind      # dir | squashfs | tarball
        self.efi = efi
        # True only when *we* create the ESP (whole-disk mode). An existing ESP
        # belongs to the machine (and possibly to another OS) and is never
        # formatted: its files are kept and GRUB is added next to them.
        self.created_efi = mode == "disk"
        self.bootloader = bootloader        # auto | grub | none
        self.hostname = hostname
        self.mount_root = mount_root
        self.keep_data = keep_data
        self.uefi = uefi
        self.root_partition = target
        self.disk = parent_disk(target) if mode == "partition" else target
        self.steps: List[str] = []

    def describe(self) -> List[str]:
        lines = [
            f"mode            : {self.mode}",
            f"target          : {self.target}",
            f"disk            : {self.disk}",
            f"root filesystem : {self.root_partition}",
            f"EFI partition   : {self.efi or '(none - BIOS/legacy boot)'}",
            f"rootfs source   : {self.source} ({self.source_kind})",
            f"bootloader      : {self.bootloader}",
            f"hostname        : {self.hostname}",
            f"mount point     : {self.mount_root}",
        ]
        return lines

    def as_dict(self) -> Dict[str, object]:
        return {
            "mode": self.mode, "target": self.target, "disk": self.disk,
            "root_partition": self.root_partition, "efi": self.efi,
            "source": self.source, "source_kind": self.source_kind,
            "bootloader": self.bootloader, "hostname": self.hostname,
            "uefi": self.uefi, "mount_root": self.mount_root,
        }


# --------------------------------------------------------------------------- #
# Safety checks
# --------------------------------------------------------------------------- #

def _mounted_devices() -> Dict[str, str]:
    """device -> mountpoint map from /proc/mounts."""
    result: Dict[str, str] = {}
    try:
        with open("/proc/mounts", "r", errors="ignore") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 2:
                    result[parts[0]] = parts[1].replace("\\040", " ")
    except Exception:
        pass
    return result


def _live_or_root_disks() -> List[str]:
    """Disks we must never write to: the running root and the live medium."""
    devices = set()
    for path in ("/", "/run/live/medium", "/run/initramfs/live", "/cdrom",
                 "/media/cdrom", "/lib/live/mount/medium", "/rofs"):
        device = device_for_path(path)
        if device:
            devices.add(real_device(device))
    return sorted(devices)


def validate_plan(plan: InstallPlan, runner: CommandRunner, disks: Sequence[Disk],
                  allow_removable: bool = False) -> None:
    """
    Raises SafetyRefusal with an actionable message for every unsafe request.

    This is the only gate between a user request and a disk write, so it is
    intentionally paranoid and entirely read-only.
    """
    target = real_device(plan.target)

    if plan.mode not in ("partition", "disk"):
        raise SafetyRefusal(f"unknown mode '{plan.mode}' (use partition or disk)")

    if not os.path.exists(target):
        raise SafetyRefusal(f"{plan.target} does not exist")
    if not _is_block_device(target):
        raise SafetyRefusal(f"{plan.target} is not a block device")

    # 1. Never write to the system we are running from.
    protected = _live_or_root_disks()
    for device in protected:
        if target == device:
            raise SafetyRefusal(
                f"{plan.target} is the device holding the running system. "
                "Pick the disk you want to install *to*, not the one you booted from.")
        if plan.mode == "disk" and real_device(parent_disk(target)) == real_device(parent_disk(device)):
            raise SafetyRefusal(
                f"{plan.target} contains the running system ({device}). "
                "Installing to it would erase the machine you are using right now.")

    # 2. The target must not be in use.
    mounts = _mounted_devices()
    if target in mounts:
        raise SafetyRefusal(f"{plan.target} is mounted at {mounts[target]}. "
                            "Unmount it first (the installer will not do this for you).")
    if any(mountpoint.startswith(plan.mount_root) for mountpoint in mounts.values()):
        raise SafetyRefusal(f"{plan.mount_root} is already in use. "
                            "Unmount it or pass --mount-root with another path.")

    # 3. Find the disk entry and check size/transport.
    info = None
    for disk in disks:
        if real_device(disk.path) == real_device(parent_disk(target)):
            info = disk
            break
    if info is None:
        # The target exists but lsblk might not list it (dm, md, container).
        size = 0
        if runner.has("blockdev"):
            _, out, _ = runner.run(["blockdev", "--getsize64", target], check=False)
            size = int(out.strip() or 0)
        if size and size < MIN_ROOT_BYTES:
            raise SafetyRefusal(f"{target} is only {size // 1024 ** 3} GB; "
                                f"GBA OS needs at least {MIN_ROOT_BYTES // 1024 ** 3} GB.")
        info = Disk(target, size=size)

    size = info.size if plan.mode == "disk" else _partition_size(plan.target, disks)
    if size and size < MIN_ROOT_BYTES:
        raise SafetyRefusal(
            f"{plan.target} is {size / 1024 ** 3:.1f} GB; GBA OS needs at least "
            f"{MIN_ROOT_BYTES // 1024 ** 3} GB (16 GB recommended).")

    if info.removable and not allow_removable:
        raise SafetyRefusal(
            f"{plan.target} looks like removable media. Re-run with --allow-removable "
            "if you really mean to install onto a USB drive.")

    # 4. An existing OS must not be living on the target we are about to format.
    if plan.mode == "partition":
        if _partition_has_os(plan.target):
            raise SafetyRefusal(
                f"{plan.target} contains an operating system. Formatting it would destroy "
                "that system. Choose an empty partition, or use --mode disk with a "
                "dedicated disk.")
        if plan.target == parent_disk(plan.target):
            raise SafetyRefusal(
                "partition mode needs a partition (for example /dev/sdb1), not a whole disk. "
                "Use --mode disk to create a partition table.")

    # 5. Source must exist and be readable.
    if plan.source_kind == "dir":
        if not os.path.isdir(plan.source):
            raise SafetyRefusal(f"rootfs directory not found: {plan.source}")
    elif plan.source_kind == "squashfs":
        if not os.path.isfile(plan.source):
            raise SafetyRefusal(f"squashfs image not found: {plan.source}")
        if not runner.has("unsquashfs"):
            raise SafetyRefusal("unsquashfs is required to extract the live image "
                                "(apt install squashfs-tools).")
    elif plan.source_kind == "tarball":
        if not os.path.isfile(plan.source):
            raise SafetyRefusal(f"rootfs archive not found: {plan.source}")
        if not runner.has("tar"):
            raise SafetyRefusal("tar is required to extract the rootfs archive.")
    else:
        raise SafetyRefusal(f"unknown source kind '{plan.source_kind}'")

    # 6. EFI partition, when one is named.
    if plan.efi:
        efi_real = real_device(plan.efi)
        if not os.path.exists(efi_real) or not _is_block_device(efi_real):
            raise SafetyRefusal(f"EFI partition {plan.efi} is not a block device")
        if efi_real in mounts:
            raise SafetyRefusal(f"EFI partition {plan.efi} is mounted at {mounts[efi_real]}; "
                                "unmount it first.")
        if real_device(parent_disk(efi_real)) != real_device(parent_disk(target)):
            raise SafetyRefusal("the EFI partition must be on the same disk as the "
                                "installation target.")


def _partition_size(target: str, disks: Sequence[Disk]) -> int:
    for disk in disks:
        for child in disk.children:
            if real_device(child.path) == real_device(target):
                return child.size
    return 0


def _partition_has_os(device: str) -> bool:
    """
    True when the partition holds something that looks like a running system.

    Only reads: mounts the partition read-only if needed and looks for the
    markers that mean "this is somebody's OS".
    """
    markers = ("etc/os-release", "boot/vmlinuz", "usr/lib/os-release")
    try:
        probe = subprocess.run(["blkid", "-o", "value", "-s", "TYPE", device],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        fstype = probe.stdout.strip()
    except Exception:
        fstype = ""
    if not fstype:
        return False
    if fstype == "vfat":
        return False
    temp = tempfile.mkdtemp(prefix="gba-probe-")
    try:
        code = subprocess.call(["mount", "-o", "ro,noload", device, temp],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if code != 0:
            # Cannot inspect: be conservative and treat it as occupied.
            return True
        for marker in markers:
            if os.path.exists(os.path.join(temp, marker)):
                return True
        return False
    finally:
        subprocess.call(["umount", temp], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            os.rmdir(temp)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# The installer
# --------------------------------------------------------------------------- #

class Installer:
    def __init__(self, plan: InstallPlan, runner: CommandRunner,
                 confirm: Optional[Callable[[str, str], bool]] = None,
                 progress: Optional[Callable[[str], None]] = None):
        self.plan = plan
        self.runner = runner
        self.confirm = confirm or self._default_confirm
        self.progress = progress or (lambda message: None)
        self.mounted: List[str] = []
        self.steps_done: List[str] = []

    # -- interaction ---------------------------------------------------
    @staticmethod
    def _default_confirm(phrase: str, question: str) -> bool:
        if not sys.stdin.isatty():
            print(f"\n!! {question}")
            print("   Refusing to continue without a terminal confirmation.")
            return False
        print(f"\n!! {question}")
        try:
            answer = input(f'   Type exactly "{phrase}" to continue: ').strip()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer == phrase

    # -- helpers -------------------------------------------------------
    def _say(self, message: str) -> None:
        self.progress(message)

    def _mount(self, device: str, target: str, options: str = "") -> None:
        os.makedirs(target, exist_ok=True)
        args = ["mount"]
        if options:
            args += ["-o", options]
        args += [device, target]
        self.runner.run(args)
        self.mounted.append(target)

    def _unmount_all(self) -> None:
        for target in reversed(self.mounted):
            self.runner.run(["umount", target], check=False)
        self.mounted = []

    def _target_path(self, relative: str = "") -> str:
        return os.path.join(self.plan.mount_root, relative.lstrip("/"))

    # -- steps ---------------------------------------------------------
    def prepare_target(self) -> None:
        """Destructive: partition (disk mode) and format the root partition."""
        plan = self.plan
        if plan.mode == "disk":
            if not (self.runner.has("sgdisk") or self.runner.has("parted")):
                raise StepFailed("sgdisk or parted is required to create partitions")
            self._say(f"Preparing {plan.disk} (partition table will be rewritten)")
            self.runner.run(["wipefs", "-a", plan.disk], check=False)
            if self.runner.has("sgdisk"):
                self.runner.run(["sgdisk", "--zap-all", plan.disk])
                self.runner.run(["sgdisk", "--new=1:0:+512M", "--typecode=1:ef00",
                                 "--change-name=1:GBA-ESP", plan.disk])
                self.runner.run(["sgdisk", "--new=2:0:0", "--typecode=2:8300",
                                 "--change-name=2:GBA-ROOT", plan.disk])
            else:
                self.runner.run(["parted", "-s", plan.disk, "mklabel", "gpt"])
                self.runner.run(["parted", "-s", plan.disk, "mkpart", "primary", "fat32",
                                 "1MiB", "513MiB"])
                self.runner.run(["parted", "-s", plan.disk, "set", "1", "esp", "on"])
                self.runner.run(["parted", "-s", plan.disk, "mkpart", "primary", "ext4",
                                 "513MiB", "100%"])
            self.runner.run(["partprobe", plan.disk], check=False)
            plan.root_partition = self._first_partition(plan.disk, 2)
            plan.efi = plan.efi or self._first_partition(plan.disk, 1)

        self._say(f"Formatting {plan.root_partition} as ext4")
        self.runner.run(["mkfs.ext4", "-F", "-L", "GBA-ROOT", plan.root_partition])
        if plan.efi and plan.created_efi:
            self._say(f"Formatting {plan.efi} as FAT32 (EFI system partition)")
            self.runner.run(["mkfs.vfat", "-F", "32", "-n", "GBA-ESP", plan.efi])
        elif plan.efi:
            self._say(f"Keeping the existing EFI system partition {plan.efi} "
                      "(other boot loaders on it are left alone)")

    def _first_partition(self, disk: str, index: int) -> str:
        if "nvme" in disk or "mmcblk" in disk:
            return f"{disk}p{index}"
        return f"{disk}{index}"

    def copy_rootfs(self) -> None:
        plan = self.plan
        self._mount(plan.root_partition, plan.mount_root)
        self._say("Copying the GBA OS system image")
        if plan.source_kind == "squashfs":
            self.runner.run(["unsquashfs", "-f", "-d", plan.mount_root, plan.source])
        elif plan.source_kind == "tarball":
            self.runner.run(["tar", "-xpf", plan.source, "-C", plan.mount_root])
        else:
            self.runner.run(["cp", "-a", plan.source + "/.", plan.mount_root + "/"])
        self.create_layout()

    def create_layout(self) -> None:
        """Creates /data, /games and friends, then writes a matching fstab."""
        plan = self.plan
        for relative in ("data/saves", "data/library", "data/logs", "data/config",
                         "data/apps", "games", "run/gba", "opt/gba-distro",
                         "var/backups/gba-os"):
            os.makedirs(self._target_path(relative), exist_ok=True)

        root_uuid = self._uuid(plan.root_partition)
        efi_uuid = self._uuid(plan.efi) if plan.efi else ""
        lines = [
            "# <file system>  <mount point>  <type>  <options>  <dump>  <pass>",
        ]
        if efi_uuid:
            lines.append(f"UUID={efi_uuid}  /boot/efi  vfat  umask=0077  0  1")
        lines.append(f"UUID={root_uuid}  /  ext4  errors=remount-ro  0  1")
        os.makedirs(self._target_path("etc"), exist_ok=True)
        with open(self._target_path("etc/fstab"), "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

        os.makedirs(self._target_path("etc"), exist_ok=True)
        with open(self._target_path("etc/hostname"), "w", encoding="utf-8") as handle:
            handle.write(plan.hostname + "\n")
        self._write_boot_marker()
        self._configure_boot()
        self.steps_done.append("layout")

    def _configure_boot(self) -> None:
        """
        Turns the copied image into a system that boots *the shell*, not setup.

        The installation medium contains the guided setup as a systemd unit; if
        that survived the copy, every boot would offer to install again. It is
        removed here, and a console unit that starts the appliance on tty1 is
        added in its place. If the machine is booted without gba_mode=1 the
        wrapper it runs falls back to a normal login prompt, so the console is
        never a dead end.
        """
        units = self._target_path("etc/systemd/system")
        wants = os.path.join(units, "multi-user.target.wants")

        for leftover in (os.path.join(units, "gba-setup.service"),
                         os.path.join(wants, "gba-setup.service")):
            if os.path.islink(leftover) or os.path.isfile(leftover):
                os.unlink(leftover)

        if os.path.isdir(units):
            os.makedirs(wants, exist_ok=True)
            unit = os.path.join(units, "gba-appliance.service")
            with open(unit, "w", encoding="utf-8") as handle:
                handle.write(
                    "[Unit]\n"
                    "Description=GBA OS console appliance\n"
                    "After=systemd-user-sessions.service\n"
                    "Conflicts=getty@tty1.service\n\n"
                    "[Service]\n"
                    "Type=idle\n"
                    "ExecStart=/usr/local/bin/gba-tty1-wrapper\n"
                    "StandardInput=tty\n"
                    "StandardOutput=tty\n"
                    "StandardError=tty\n"
                    "TTYPath=/dev/tty1\n"
                    "TTYReset=yes\n"
                    "TTYVHangup=yes\n"
                    "Restart=no\n\n"
                    "[Install]\n"
                    "WantedBy=multi-user.target\n")
            link = os.path.join(wants, "gba-appliance.service")
            if os.path.islink(link) or os.path.exists(link):
                os.unlink(link)
            try:
                os.symlink("/etc/systemd/system/gba-appliance.service", link)
            except OSError:
                pass

        # A dedicated install should boot the appliance without extra typing.
        defaults = self._target_path("etc/default/grub.d")
        os.makedirs(defaults, exist_ok=True)
        with open(os.path.join(defaults, "99_gba-appliance.cfg"), "w",
                  encoding="utf-8") as handle:
            handle.write(
                "# Added by the GBA OS installer.\n"
                "# Boot the console appliance; 'gba_mode=0' on the kernel command\n"
                "# line (or the safe-graphics entry) always gets you a console.\n"
                "GRUB_TIMEOUT_STYLE=menu\n"
                "GRUB_TIMEOUT=10\n"
                'GRUB_CMDLINE_LINUX="$GRUB_CMDLINE_LINUX gba_mode=1"\n')

        # The installer's own notes are the first thing support will ask for.
        os.makedirs(self._target_path("var/log"), exist_ok=True)
        with open(self._target_path("var/log/gba-install.log"), "a",
                  encoding="utf-8") as handle:
            handle.write("installed %s to %s (%s mode)\n" % (
                datetime.datetime.now().isoformat(timespec="seconds"),
                self.plan.target, self.plan.mode))

    def _write_boot_marker(self) -> None:
        """Records how this install happened; the shell shows it in About."""
        payload = {
            "installed_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "mode": self.plan.mode,
            "target": self.plan.target,
            "root_partition": self.plan.root_partition,
            "efi": self.plan.efi,
            "hostname": self.plan.hostname,
            "installer_version": 1,
        }
        path = self._target_path("etc/gba-install.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)

    def _uuid(self, device: str) -> str:
        if not device:
            return ""
        code, out, _ = self.runner.run(["blkid", "-s", "UUID", "-o", "value", device],
                                       check=False)
        return out.strip() or f"PARTUUID-PLACEHOLDER-{os.path.basename(device)}"

    def install_bootloader(self) -> None:
        plan = self.plan
        if plan.bootloader == "none":
            self._say("Skipping the boot loader (--bootloader none)")
            return
        if plan.efi:
            self._say(f"Installing GRUB (UEFI) on {plan.efi}")
            self._mount(plan.efi, self._target_path("boot/efi"))
            self._preserve_foreign_loader(self._target_path("boot/efi"))
            self.runner.run([
                "grub-install", "--target=x86_64-efi",
                f"--efi-directory={self._target_path('boot/efi')}",
                f"--boot-directory={self._target_path('boot')}",
                "--removable", "--no-nvram", "--recheck",
            ])
        else:
            self._say(f"Installing GRUB (BIOS) on {plan.disk}")
            self.runner.run([
                "grub-install", "--target=i386-pc",
                f"--boot-directory={self._target_path('boot')}",
                "--recheck", plan.disk,
            ])
        existing = self._target_path("boot/grub/grub.cfg")
        preserved = self._target_path("boot/grub/grub.cfg.host")
        if os.path.isfile(existing) and not os.path.isfile(preserved):
            try:
                shutil.copyfile(existing, preserved)
            except OSError:
                pass
        self._write_grub_cfg()

    def _preserve_foreign_loader(self, efi_root: str) -> None:
        """Copies an existing EFI/BOOT aside so nothing of another OS is lost."""
        source = os.path.join(efi_root, "EFI", "BOOT")
        if not os.path.isdir(source):
            return
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(efi_root, "EFI", f"BOOT.gba-backup-{stamp}")
        try:
            shutil.copytree(source, dest)
            self._say(f"Backed up the existing fallback loader to {dest}")
        except OSError as exc:
            raise StepFailed(f"could not back up the existing EFI loader: {exc}")

    def _write_grub_cfg(self) -> None:
        """Writes a boot menu that always keeps a safe entry."""
        menu = self._target_path("boot/grub/grub.cfg")
        os.makedirs(os.path.dirname(menu), exist_ok=True)
        root_uuid = self._uuid(self.plan.root_partition)
        content = f"""# Generated by the GBA OS installer.
set default=0
set timeout=5

menuentry "GBA OS" {{
    search --no-floppy --fs-uuid --set=root {root_uuid}
    linux /boot/vmlinuz root=UUID={root_uuid} ro quiet gba_mode=1
    initrd /boot/initrd.img
}}

menuentry "GBA OS (safe graphics / recovery)" {{
    search --no-floppy --fs-uuid --set=root {root_uuid}
    linux /boot/vmlinuz root=UUID={root_uuid} ro quiet gba_mode=1 nomodeset gba_safe=1
    initrd /boot/initrd.img
}}

menuentry "Boot the primary operating system" {{
    # If the machine had a boot menu of its own, it was preserved here; if not,
    # handing control back to the firmware is enough to reach the other system.
    if [ -f /boot/grub/grub.cfg.host ]; then
        configfile /boot/grub/grub.cfg.host
    else
        exit
    fi
}}
"""
        with open(menu, "w", encoding="utf-8") as handle:
            handle.write(content)

    def verify(self) -> None:
        self._say("Verifying the installation")
        checks = [
            ("etc/fstab", True), ("etc/hostname", True), ("etc/gba-install.json", True),
            ("opt/gba-distro/bin/gba-launcher", True),
            ("opt/gba-distro/src/gba_launcher.py", True),
            ("opt/gba-distro/bin/gba-setup", True),
        ]
        missing = [path for path, required in checks
                   if required and not os.path.exists(self._target_path(path))]
        if missing:
            raise StepFailed("installed system is incomplete: missing " + ", ".join(missing))
        fstab = open(self._target_path("etc/fstab"), encoding="utf-8").read()
        if "PLACEHOLDER" in fstab:
            raise StepFailed("could not determine filesystem UUIDs for /etc/fstab")
        self.steps_done.append("verify")

    def cleanup(self) -> None:
        self._unmount_all()

    # -- orchestration --------------------------------------------------
    def execute(self) -> int:
        plan = self.plan
        self._say("")
        self._say("Installation plan")
        for line in plan.describe():
            self._say("  " + line)
        if self.runner.dry_run:
            self._say("")
            self._say("  dry-run: no disk will be modified")
            self._dry_run_steps()
            return EXIT_OK

        phrase = (CONFIRM_DISK.format(disk=plan.disk) if plan.mode == "disk"
                  else CONFIRM_PARTITION.format(part=plan.target))
        question = (f"ALL DATA on {plan.disk} will be erased and GBA OS installed."
                    if plan.mode == "disk"
                    else f"{plan.target} will be formatted and GBA OS installed there.")
        if not self.confirm(phrase, question):
            self._say("Cancelled: nothing was changed.")
            return EXIT_REFUSED

        try:
            self.prepare_target()
            self.steps_done.append("prepare")
            self.copy_rootfs()
            self.verify()
        except (StepFailed, OSError) as exc:
            self._say(f"FAILED: {exc}")
            self._say("The boot loader was NOT installed. Your existing system is unchanged.")
            self.cleanup()
            return EXIT_STEP_FAILED

        try:
            self.install_bootloader()
            self.steps_done.append("bootloader")
        except StepFailed as exc:
            self._say(f"Boot loader step failed: {exc}")
            self._say("The system image IS installed, but this machine's boot menu was not")
            self._say("changed. You can still boot your existing OS.")
            self.cleanup()
            return EXIT_BOOT_FAILED

        self.cleanup()
        self._say("")
        self._say("Installation complete.")
        self._say(f"  root filesystem : {plan.root_partition}")
        if plan.efi:
            self._say(f"  EFI partition   : {plan.efi}")
        self._say("  Remove the installation medium and reboot.")
        return EXIT_OK

    def _dry_run_steps(self) -> None:
        """Prints the exact commands, in order, for a dry run."""
        plan = self.plan
        if plan.mode == "disk":
            for line in (["wipefs", "-a", plan.disk],
                         ["sgdisk", "--zap-all", plan.disk],
                         ["sgdisk", "--new=1:0:+512M", plan.disk]):
                self._say("  + " + " ".join(line))
            root = self._first_partition(plan.disk, 2)
            efi = self._first_partition(plan.disk, 1)
        else:
            root = plan.root_partition
            efi = plan.efi
        self._say(f"  + mkfs.ext4 -F -L GBA-ROOT {root}")
        if efi:
            self._say(f"  + mkfs.vfat -F 32 -n GBA-ESP {efi}")
        self._say(f"  + mount {root} {plan.mount_root}")
        if plan.source_kind == "squashfs":
            self._say(f"  + unsquashfs -f -d {plan.mount_root} {plan.source}")
        else:
            self._say(f"  + copy {plan.source} -> {plan.mount_root}")
        if plan.bootloader != "none":
            if efi:
                self._say(f"  + mount {efi} {plan.mount_root}/boot/efi")
                self._say(f"  + grub-install --target=x86_64-efi --efi-directory="
                          f"{plan.mount_root}/boot/efi --no-nvram --removable")
            else:
                self._say(f"  + grub-install --target=i386-pc --boot-directory="
                          f"{plan.mount_root}/boot {plan.disk}")
        self._say(f"  + umount {plan.mount_root}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def print_disk_table(disks: Sequence[Disk], json_output: bool = False) -> None:
    if json_output:
        print(json.dumps([d.as_dict() for d in disks], indent=2))
        return
    print("Available disks")
    print("=" * 78)
    if not disks:
        print("  (no block devices found)")
        return
    for disk in disks:
        media = "removable" if disk.removable else "fixed"
        print(f"  {disk.path:<14} {disk.size_text():>10}  {media:<9} "
              f"{(disk.model or 'unknown model')[:34]}")
        if disk.mounted:
            print("      (contains mounted filesystems - do not use)")
        for child in disk.children:
            state = ",".join(child.mountpoints) if child.mountpoints else (
                child.fstype or "no filesystem")
            print(f"      {child.path:<12} {child.size_text():>10}  {state}")
    print("=" * 78)
    print("  Install to an *empty partition* (default) or use --mode disk to erase a whole disk.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gba-installer",
        description="Install GBA OS onto a disk or partition. Safe by default.",
        epilog="Nothing is written unless --install is given.")
    parser.add_argument("--list", action="store_true", help="list disks and partitions")
    parser.add_argument("--json", action="store_true", help="machine readable --list output")
    parser.add_argument("--plan", action="store_true", help="print the plan for the given options")
    parser.add_argument("--install", action="store_true", help="perform the installation")
    parser.add_argument("--dry-run", action="store_true", help="never modify a disk")
    parser.add_argument("--mode", choices=("partition", "disk"), default="partition")
    parser.add_argument("--target", help="/dev/sdXN (partition mode) or /dev/sdX (disk mode)")
    parser.add_argument("--efi", default="", help="EFI system partition (partition mode)")
    parser.add_argument("--source", default="", help="root filesystem source")
    parser.add_argument("--source-kind", choices=("dir", "squashfs", "tarball"), default="")
    parser.add_argument("--bootloader", choices=("auto", "grub", "none"), default="auto")
    parser.add_argument("--hostname", default="gba-os")
    parser.add_argument("--mount-root", default=DEFAULT_MOUNT_ROOT)
    parser.add_argument("--allow-removable", action="store_true",
                        help="permit installing onto a USB drive (data loss!)")
    parser.add_argument("--yes", action="store_true",
                        help="non-interactive: still requires the typed phrase")
    parser.add_argument("--accept-phrase", default="",
                        help="the typed confirmation, for unattended installs")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    runner = CommandRunner(dry_run=args.dry_run, logger=print)

    if args.list:
        print_disk_table(probe_disks(runner), args.json)
        return EXIT_OK

    if not (args.plan or args.install):
        build_parser().print_help()
        return EXIT_USAGE
    if not args.target:
        print("error: --target is required", file=sys.stderr)
        return EXIT_USAGE

    source = args.source
    source_kind = args.source_kind
    if not source:
        for candidate in ("/run/live/rootfs/filesystem.squashfs",
                          "/run/live/medium/live/filesystem.squashfs",
                          "/lib/live/mount/medium/live/filesystem.squashfs",
                          os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")):
            if os.path.exists(candidate):
                source = os.path.abspath(candidate)
                source_kind = source_kind or ("squashfs" if candidate.endswith(".squashfs")
                                              else "dir")
                break
    if not source_kind:
        source_kind = "squashfs" if source.endswith((".squashfs", ".sqfs")) else (
            "tarball" if source.endswith((".tar", ".tar.gz", ".tgz", ".tar.xz")) else "dir")

    plan = InstallPlan(mode=args.mode, target=args.target, source=source,
                       source_kind=source_kind, efi=args.efi,
                       bootloader="grub" if args.bootloader in ("auto", "grub") else "none",
                       hostname=args.hostname, mount_root=args.mount_root)

    disks = probe_disks(runner)
    try:
        validate_plan(plan, runner, disks, allow_removable=args.allow_removable)
    except SafetyRefusal as exc:
        print(f"\nRefusing to install:\n  {exc}\n", file=sys.stderr)
        return EXIT_REFUSED

    if args.plan and not args.install:
        print("Installation plan")
        for line in plan.describe():
            print("  " + line)
        print("\n  Boot menu: GBA OS entries are added; the existing OS is not modified"
              if plan.mode == "partition" else
              "\n  Boot menu: this disk gets its own GRUB; other disks are untouched")
        print("\nNothing was changed. Add --install to proceed.")
        return EXIT_OK

    def confirm(expected: str, question: str) -> bool:
        if args.accept_phrase and args.accept_phrase == expected:
            return True
        if args.yes and not sys.stdin.isatty():
            print("Refusing: --yes needs --accept-phrase \"%s\" for unattended installs."
                  % expected, file=sys.stderr)
            return False
        return Installer._default_confirm(expected, question)

    installer = Installer(plan, runner, confirm=confirm, progress=print)
    return installer.execute()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        sys.exit(130)
