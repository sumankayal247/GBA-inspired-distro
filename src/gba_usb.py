"""
Removable-media support for GBA OS.

Plug in a pendrive and the shell offers to import its `.gba`/`.gbc`/`.gb`
files into the library — after asking first, as required by the contract's
"no destructive action without confirmation" rule.

Detection uses only `/proc/mounts` and `/sys`, so it works on a minimal image
without udisks, udevadm or lsblk.
"""

import os
import shutil
import threading
from typing import Callable, Dict, List, Optional, Tuple

ROM_EXTENSIONS = (".gba", ".gbc", ".gb")

# Mount points that typically hold user media on appliance and desktop images.
MEDIA_ROOTS = ("/media", "/run/media", "/mnt", "/var/media")

_SKIP_DIRS = {
    "lost+found", "System Volume Information", "$RECYCLE.BIN",
    ".Trash", ".Trash-1000", "node_modules", ".git", ".github",
    "venv", ".venv", "env", ".env", "__pycache__", "cache", ".cache",
    "site-packages", "dist-packages", "models", "torch", "lib", "bin",
    "include", "share", "proc", "sys", "dev"
}


class Volume:
    """One mounted filesystem that may contain ROMs."""

    def __init__(self, device: str, mountpoint: str, label: str = "",
                 removable: bool = False, total: int = 0, free: int = 0):
        self.device = device
        self.mountpoint = mountpoint
        self.label = label or os.path.basename(mountpoint.rstrip("/")) or device
        self.removable = removable
        self.total = total
        self.free = free
        self._roms: Optional[List[Tuple[str, int]]] = None

    @property
    def key(self) -> str:
        """Stable identity for "have I already offered this stick?" tracking."""
        return f"{self.device}@{self.mountpoint}" if self.device else self.mountpoint

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Volume {self.label} {self.mountpoint} removable={self.removable}>"

    def scan_roms(self, max_files: int = 2000, max_depth: int = 5) -> List[Tuple[str, int]]:
        """Returns [(absolute_path, size_bytes)] of ROM files on the volume."""
        if self._roms is not None:
            return self._roms

        found: List[Tuple[str, int]] = []
        root = os.path.realpath(self.mountpoint)
        if not os.path.isdir(root):
            self._roms = found
            return found

        root_depth = root.rstrip(os.sep).count(os.sep)

        def onerror(_exc):
            return

        for directory, dirs, files in os.walk(root, onerror=onerror):
            cur_depth = directory.rstrip(os.sep).count(os.sep) - root_depth
            if cur_depth >= max_depth:
                dirs[:] = []
                continue
            dirs[:] = sorted(d for d in dirs
                             if d not in _SKIP_DIRS and not d.startswith("."))
            for name in sorted(files):
                if name.startswith("."):
                    continue
                if os.path.splitext(name)[1].lower() not in ROM_EXTENSIONS:
                    continue
                path = os.path.join(directory, name)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                if size <= 0:
                    continue
                found.append((path, size))
                if len(found) >= max_files:
                    self._roms = found
                    return found
        self._roms = found
        return found

    def rom_count(self) -> int:
        return len(self.scan_roms())


def _read(path: str, default: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read().strip()
    except Exception:
        return default


def _parent_block_device(device: str) -> str:
    """/dev/sdb1 -> sdb, /dev/nvme0n1p1 -> nvme0n1."""
    name = os.path.basename(device.rstrip("0123456789"))
    base = os.path.basename(device)
    if base.startswith("nvme") or base.startswith("mmcblk"):
        base = base.split("p")[0]
    else:
        base = name
    return base


def _is_removable(device: str) -> bool:
    if not device.startswith("/dev/"):
        return False
    block = _parent_block_device(device)
    flag = _read(f"/sys/block/{block}/removable", "0")
    if flag == "1":
        return True
    # USB-attached storage does not always set the removable flag.
    link = os.path.realpath(f"/sys/block/{block}")
    return "/usb" in link


def _label_for(device: str, mountpoint: str) -> str:
    if device.startswith("/dev/"):
        by_label = "/dev/disk/by-label"
        try:
            for entry in os.listdir(by_label):
                if os.path.realpath(os.path.join(by_label, entry)) == os.path.realpath(device):
                    return entry.replace("\\x20", " ")
        except Exception:
            pass
    return os.path.basename(mountpoint.rstrip("/")) or os.path.basename(device)


def _usage(path: str) -> Tuple[int, int]:
    try:
        usage = shutil.disk_usage(path)
        return usage.total, usage.free
    except Exception:
        return 0, 0


def parse_mounts(path: str = "/proc/mounts") -> List[Tuple[str, str]]:
    """Returns [(device, mountpoint)] for real block devices."""
    entries: List[Tuple[str, str]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 2:
                    continue
                device, mountpoint = parts[0], parts[1].replace("\\040", " ")
                if not device.startswith("/dev/"):
                    continue
                if mountpoint == "/":
                    continue
                entries.append((device, mountpoint))
    except Exception:
        pass
    return entries


def list_mounted_volumes(mounts_path: str = "/proc/mounts",
                         media_roots=MEDIA_ROOTS,
                         include_any_media: bool = True) -> List[Volume]:
    """
    Lists candidate ROM sources.

    `include_any_media` also returns fixed disks mounted under /media or /mnt,
    which is what a manually mounted USB stick looks like on some desktops.
    """
    volumes: Dict[str, Volume] = {}
    for device, mountpoint in parse_mounts(mounts_path):
        removable = _is_removable(device)
        in_media = any(mountpoint == root or mountpoint.startswith(root + "/")
                       for root in media_roots)
        if not removable and not (include_any_media and in_media):
            continue
        total, free = _usage(mountpoint)
        volume = Volume(device=device, mountpoint=mountpoint,
                        label=_label_for(device, mountpoint),
                        removable=removable, total=total, free=free)
        volumes[volume.key] = volume
    return sorted(volumes.values(), key=lambda v: v.mountpoint)


class ImportResult:
    def __init__(self):
        self.copied: List[str] = []
        self.skipped: List[str] = []
        self.failed: List[Tuple[str, str]] = []
        self.destination = ""

    @property
    def ok(self) -> bool:
        return not self.failed

    def summary(self) -> str:
        parts = [f"{len(self.copied)} imported"]
        if self.skipped:
            parts.append(f"{len(self.skipped)} already present")
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        return " · ".join(parts)


def _safe_folder(name: str) -> str:
    cleaned = "".join(c if (c.isalnum() or c in " -_.") else "_" for c in name).strip()
    return cleaned or "USB"


def import_roms(volume: Volume, games_dir: str, subfolder: bool = True,
                progress: Optional[Callable[[int, int, str], None]] = None) -> ImportResult:
    """
    Copies the volume's ROMs into the games directory.

    Existing files are skipped by (name, size), so re-importing the same stick
    is free and never duplicates a 32 MB ROM. Every copy is verified by size.
    """
    result = ImportResult()
    roms = volume.scan_roms()
    if not roms:
        return result

    destination = os.path.join(games_dir, _safe_folder(volume.label)) if subfolder else games_dir
    result.destination = destination
    os.makedirs(destination, exist_ok=True)

    total = len(roms)
    for index, (source, size) in enumerate(roms, start=1):
        name = os.path.basename(source)
        target = os.path.join(destination, name)
        if progress:
            progress(index, total, name)
        try:
            if os.path.exists(target) and os.path.getsize(target) == size:
                result.skipped.append(name)
                continue
            suffix = 1
            while os.path.exists(target) and os.path.getsize(target) != size:
                stem, ext = os.path.splitext(name)
                target = os.path.join(destination, f"{stem} ({suffix}){ext}")
                suffix += 1
            shutil.copy2(source, target)
            if os.path.getsize(target) != size:
                raise IOError("size mismatch after copy")
            result.copied.append(target)
        except Exception as exc:
            result.failed.append((name, str(exc)))
            try:
                if os.path.exists(target) and os.path.getsize(target) != size:
                    os.remove(target)
            except Exception:
                pass
    return result


class VolumeWatcher(threading.Thread):
    """
    Background poller that reports newly plugged-in volumes once.

    The shell consumes `pending` inside its own loop (on the main thread), so
    the confirmation dialog is never raised from a worker thread.
    """

    def __init__(self, interval: float = 2.0, mounts_path: str = "/proc/mounts",
                 media_roots=MEDIA_ROOTS):
        super().__init__(daemon=True, name="gba-usb-watcher")
        self.interval = interval
        self.mounts_path = mounts_path
        self.media_roots = tuple(media_roots)
        self.pending: List[Volume] = []
        self.volumes: List[Volume] = []
        self.seen: set = set()
        # NOTE: not `self._stop` — threading.Thread uses that name internally.
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._initialised = False

    def poll_once(self) -> List[Volume]:
        """Synchronous single poll; returns volumes not seen before."""
        try:
            volumes = list_mounted_volumes(self.mounts_path, media_roots=self.media_roots)
        except Exception:
            volumes = []
        new: List[Volume] = []
        with self._lock:
            current_keys = {v.key for v in volumes}
            for volume in volumes:
                if volume.key not in self.seen and volume.rom_count() > 0:
                    new.append(volume)
                self.seen.add(volume.key)
            # Forget sticks that were unplugged so re-inserting re-prompts.
            self.seen &= current_keys
            self.volumes = volumes
            self.pending.extend(new)
        if not self._initialised:
            # Devices already present at boot are reported too: the user may
            # have booted with the cartridge-in-a-stick already plugged in.
            self._initialised = True
        return new

    def run(self) -> None:
        while not self._stop_event.is_set():
            self.poll_once()
            self._stop_event.wait(self.interval)

    def take_pending(self) -> List[Volume]:
        with self._lock:
            items, self.pending = self.pending, []
            return items

    def stop(self) -> None:
        self._stop_event.set()

    def refresh(self) -> List[Volume]:
        """Re-reads the volume list (used by the manual 'Scan USB' action)."""
        with self._lock:
            self.seen.clear()
        return self.poll_once()
