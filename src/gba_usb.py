"""
Removable-media support for GBA OS.

Plug in a pendrive and the shell offers to import its `.gba`/`.gbc`/`.gb`
files into the library — after asking first, as required by the contract's
"no destructive action without confirmation" rule.

Detection uses only `/proc/mounts` and `/sys`, so it works on a minimal image
without udisks, udevadm or lsblk.

The import prompt is gated on *new content*, not mere discovery: every ROM on
a stick is compared by content hash (Rule L2) against the ids the library
already knows, so a stick whose contents were imported in a previous session
— or before a launcher restart or reboot — offers nothing, while genuinely
new files are reported by name and count. Nothing about "has a prompt been
shown" is persisted; the persistent state is the library's own content
identity.
"""

import json
import logging
import os
import shutil
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from common import compute_content_id, get_data_dir

logger = logging.getLogger("gba.usb")

ROM_EXTENSIONS = (".gba", ".gbc", ".gb")

# How many newly hashed source files are tolerated before the source index
# flushes itself to disk (same reasoning as the library's cache flush).
INDEX_SAVE_INTERVAL = 16

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
        # Set by the watcher's new-content analysis: [(path, size)] of ROMs
        # whose content id is not registered in the library. None means
        # "not analysed" (discovery only), and counts fall back to rom_count.
        self.new_roms: Optional[List[Tuple[str, int]]] = None

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

    def new_rom_count(self) -> int:
        """
        How many ROMs on this volume are new to the library. Falls back to
        the plain discovery count when the volume was never analysed (no
        library identity available, or analysis failed) — prompting for
        everything is the safe default in that case.
        """
        if self.new_roms is not None:
            return len(self.new_roms)
        return self.rom_count()


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


def default_index_path() -> str:
    """Where the source index lives: `DATA/library/usb_sources.json`."""
    return os.path.join(get_data_dir(), "library", "usb_sources.json")


class SourceIndex:
    """
    Persistent content-id cache for ROM files discovered on removable media.

    Hashing a stick full of ROMs is the expensive part of new-content
    detection, and it must not be repeated on every boot — but it also must
    not run on the UI thread. The index applies the library's own rules to
    source files: identity is the SHA-256 content hash (Rule L2), cached by
    (relative path, size, mtime) (Rule L4), flushed every `INDEX_SAVE_INTERVAL`
    fresh hashes so a mid-analysis interruption loses at most that many.
    Like the library cache it lives under `DATA/library/` and is disposable
    (ST2): deleting it only forces re-hashing.
    """

    def __init__(self, path: Optional[str] = None):
        self.path = path or default_index_path()
        self._lock = threading.Lock()
        self._pending_saves = 0
        self._data: Dict[str, Dict[str, object]] = self._load()

    def _load(self) -> Dict[str, Dict[str, object]]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("Corrupt USB source index, resetting: %s", exc)
        return {}

    def content_id(self, path: str, size: int, mtime: float,
                   root: str) -> Optional[str]:
        """
        Content id of one source file, hashing only when its (size, mtime)
        differ from the cached record. Returns None if the file is unreadable.
        """
        try:
            key = os.path.relpath(path, root).replace(os.sep, "/")
        except ValueError:
            key = path
        with self._lock:
            record = self._data.get(key)
            if (isinstance(record, dict) and record.get("size") == size
                    and record.get("mtime") == mtime and record.get("id")):
                return str(record["id"])
        try:
            content_id = compute_content_id(path)
        except OSError as exc:
            logger.warning("Could not hash %s: %s", path, exc)
            return None
        with self._lock:
            self._data[key] = {"size": size, "mtime": mtime, "id": content_id}
            self._pending_saves += 1
            due = self._pending_saves >= INDEX_SAVE_INTERVAL
        if due:
            self.save()
        return content_id

    def save(self) -> None:
        """Flushes the index atomically; safe to call at any time."""
        with self._lock:
            self._pending_saves = 0
            payload = dict(self._data)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, self.path)
        except Exception as exc:
            logger.warning("Could not write USB source index: %s", exc)


def _hash_or_none(path: str) -> Optional[str]:
    try:
        return compute_content_id(path)
    except OSError as exc:
        logger.warning("Could not hash %s: %s", path, exc)
        return None


def find_new_roms(volume: "Volume", known_ids,
                  index: Optional[SourceIndex] = None) -> List[Tuple[str, int]]:
    """
    Returns [(path, size)] of the volume's ROMs that do not correspond to
    already-registered library content.

    This is the separation the import prompt has to make: discovery (files
    present on the drive) minus the known library (content ids the OS already
    registered) equals new content. Identity is the content hash (Rule L2),
    so a renamed copy of an imported ROM is still recognised.

    When `known_ids` is empty nothing has ever been registered, so every
    discovered ROM is provably new and no hashing is needed at all — the
    first-boot prompt stays instant even for a huge stick.
    """
    roms = volume.scan_roms()
    if not known_ids:
        return list(roms)
    root = os.path.realpath(volume.mountpoint)
    new: List[Tuple[str, int]] = []
    for path, size in roms:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        content_id = (index.content_id(path, size, mtime, root)
                      if index is not None else _hash_or_none(path))
        if content_id is None:
            continue  # unreadable file: nothing to offer for import
        if content_id not in known_ids:
            new.append((path, size))
    if index is not None:
        index.save()
    return new


class ImportResult:
    def __init__(self):
        self.copied: List[str] = []
        self.skipped: List[str] = []
        self.failed: List[Tuple[str, str]] = []
        # [(destination_path, content_id)] for copies whose content id was
        # computed during the import, so the library can register them
        # immediately instead of waiting for a rescan to re-hash.
        self.registered: List[Tuple[str, str]] = []
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
                progress: Optional[Callable[[int, int, str], None]] = None,
                known_ids=None,
                index: Optional[SourceIndex] = None) -> ImportResult:
    """
    Copies the volume's ROMs into the games directory.

    A file is skipped when its content is already registered in the library
    (`known_ids`, compared by Rule L2 content hash — so a renamed duplicate
    is not re-copied), or when a same-named file of the same size already
    exists at the destination. Repeated imports of the same content are
    therefore idempotent: Import(Import(x)) leaves the same library state as
    Import(x). Every copy is verified by size. Copies whose content id was
    computed here are reported in `result.registered` so the caller can
    register them with the library right away.
    """
    result = ImportResult()
    roms = volume.scan_roms()
    if not roms:
        return result

    destination = os.path.join(games_dir, _safe_folder(volume.label)) if subfolder else games_dir
    result.destination = destination
    os.makedirs(destination, exist_ok=True)
    root = os.path.realpath(volume.mountpoint)

    total = len(roms)
    for index_no, (source, size) in enumerate(roms, start=1):
        name = os.path.basename(source)
        target = os.path.join(destination, name)
        if progress:
            progress(index_no, total, name)
        try:
            content_id = None
            if known_ids:
                mtime = os.path.getmtime(source)
                content_id = (index.content_id(source, size, mtime, root)
                              if index is not None else _hash_or_none(source))
                if content_id is not None and content_id in known_ids:
                    result.skipped.append(name)
                    continue
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
            if content_id:
                result.registered.append((target, content_id))
        except Exception as exc:
            result.failed.append((name, str(exc)))
            try:
                if os.path.exists(target) and os.path.getsize(target) != size:
                    os.remove(target)
            except Exception:
                pass
    if index is not None:
        index.save()
    return result


class VolumeWatcher(threading.Thread):
    """
    Background poller that reports volumes holding *new* content once.

    A volume is offered to the shell only when it holds ROMs whose content
    ids are not in the library's known-id set (via `known_ids_provider`).
    That distinction is durable: it survives launcher restarts and reboots
    because the ids come from the library's on-disk cache, and the source
    side is memoised in a `SourceIndex` so a stick is fully hashed at most
    once per machine.

    The shell consumes `pending` inside its own loop (on the main thread), so
    the confirmation dialog is never raised from a worker thread — and all
    walking/hashing happens here, off the lock, so the shell's per-frame
    `take_pending()` never waits on a slow stick.
    """

    def __init__(self, interval: float = 2.0, mounts_path: str = "/proc/mounts",
                 media_roots=MEDIA_ROOTS,
                 known_ids_provider: Optional[Callable[[], set]] = None,
                 source_index: Optional[SourceIndex] = None):
        super().__init__(daemon=True, name="gba-usb-watcher")
        self.interval = interval
        self.mounts_path = mounts_path
        self.media_roots = tuple(media_roots)
        self.known_ids_provider = known_ids_provider
        self.source_index = source_index
        self.pending: List[Volume] = []
        self.volumes: List[Volume] = []
        self.seen: set = set()
        # NOTE: not `self._stop` — threading.Thread uses that name internally.
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._initialised = False

    def get_source_index(self) -> SourceIndex:
        """The process-wide source index, created lazily on first real use."""
        if self.source_index is None:
            self.source_index = SourceIndex()
        return self.source_index

    def _known_ids(self) -> set:
        if self.known_ids_provider is None:
            return set()
        try:
            return set(self.known_ids_provider() or ())
        except Exception as exc:
            logger.warning("Could not read known library ids: %s", exc)
            return set()

    def _analyse(self, volume: Volume) -> None:
        """Fills `volume.new_roms` with the ROMs new to the library."""
        known = self._known_ids()
        # No index when nothing is registered: the empty-known fast path in
        # find_new_roms needs no hashing, and constructing the index would
        # only create a cache file for no reason.
        index = self.get_source_index() if known else None
        started = time.monotonic()
        volume.new_roms = find_new_roms(volume, known, index)
        logger.info("Volume %s: %d new of %d ROMs (known ids=%d, %.1fs)",
                    volume.mountpoint, len(volume.new_roms), volume.rom_count(),
                    len(known), time.monotonic() - started)

    def poll_once(self) -> List[Volume]:
        """Synchronous single poll; returns volumes with new content."""
        try:
            volumes = list_mounted_volumes(self.mounts_path, media_roots=self.media_roots)
        except Exception:
            volumes = []
        with self._lock:
            current_keys = {v.key for v in volumes}
            candidates = [v for v in volumes if v.key not in self.seen]
            for volume in volumes:
                self.seen.add(volume.key)
            # Forget sticks that were unplugged so re-inserting re-prompts.
            self.seen &= current_keys
            self.volumes = volumes
        # Walking and hashing run OUTSIDE the lock: take_pending() is called
        # every UI frame and must never block on a slow analysis.
        new: List[Volume] = []
        for volume in candidates:
            try:
                if volume.rom_count() <= 0:
                    continue
                self._analyse(volume)
            except Exception as exc:
                logger.warning("Could not analyse %s: %s", volume.mountpoint, exc)
                volume.new_roms = None  # fall back to the discovery count
            if volume.new_rom_count() > 0:
                new.append(volume)
        with self._lock:
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
        """
        Re-arms detection: every mounted volume is re-analysed on the
        watcher's next poll (used by the manual 'Scan USB' action). The
        analysis itself always runs on the watcher thread — never on the
        caller's — because hashing a large stick takes a long time.
        """
        with self._lock:
            self.seen.clear()
        return list(self.volumes)
