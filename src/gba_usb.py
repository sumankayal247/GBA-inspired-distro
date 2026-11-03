"""
Removable-media support for GBA OS.

Plug in a pendrive — or an Android phone in File Transfer (MTP) mode — and the
shell offers to import its `.gba`/`.gbc`/`.gb` files into the library — after
asking first, as required by the contract's "no destructive action without
confirmation" rule.

Block-device detection uses only `/proc/mounts` and `/sys/block`, so it works
on a minimal image without udisks, udevadm or lsblk. MTP is *not* a block
device: it has no `/dev/sdX` partition to mount. Those devices are detected
through USB MTP interfaces/libmtp and exposed by a FUSE filesystem (`jmtpfs`
or a GVfs `mtp://` mount). The resulting `MtpVolume` is still an ordinary
`Volume` for scanning, hashing and importing, so both source kinds share one
pipeline and one content-ID system.

The import prompt is gated on *new content*, not mere discovery: every ROM on
a source is compared by content hash (Rule L2) against the ids the library
already knows, so a stick or phone whose contents were imported in a previous
session — or before a launcher restart or reboot — offers nothing, while
genuinely new files are reported by name and count. Nothing about "has a
prompt been shown" is persisted; the persistent state is the library's own
content identity.
"""

import glob
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from common import compute_content_id, get_data_dir, get_run_dir

logger = logging.getLogger("gba.usb")

ROM_EXTENSIONS = (".gba", ".gbc", ".gb")
# MTP scans use exactly the same extension filter as block-media imports.
MTP_ROM_EXTENSIONS = ROM_EXTENSIONS
MTP_EXTENSIONS = ROM_EXTENSIONS
ANDROID_MTP_EXTENSIONS = ROM_EXTENSIONS

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

# Android phones expose a lot of media/app data. Never walk those trees while
# looking for ROMs; `Download`, `Documents`, `ROMs`, `Games` and storage roots
# are deliberately NOT skipped. This set applies to MTP sources only, leaving
# the pendrive scanner's behaviour unchanged.
MTP_SKIP_DIRS = frozenset({
    "Android", "DCIM", "Camera", "Pictures", "Movies", "Music", "Alarms",
    "Audiobooks", "Ringtones", "Notifications", "Podcasts", "Recordings",
    "WhatsApp", "Telegram", "Snapchat", ".thumbnails", "lost.dir",
})

DEFAULT_MTP_SYSFS = "/sys/bus/usb/devices"
MTP_MOUNT_ROOT_ENV = "GBA_MTP_MOUNT_ROOT"
MTP_SYSFS_ENV = "GBA_MTP_SYSFS"
MTP_JMTPFS_ENV = "GBA_MTP_JMTPFS"
MTP_DETECT_ENV = "GBA_MTP_DETECT"
MTP_LIST_TIMEOUT = 8.0
MTP_MOUNT_TIMEOUT = 20.0
MTP_RETRY_INTERVAL = 15.0
MTP_STORAGE_HINTS = ("internal", "storage", "sd", "card", "phone", "memory",
                     "external", "download", "document", "rom", "game")
MTP_BUS_RE = re.compile(r"usb[:/](\d+)[,:](\d+)", re.IGNORECASE)
MTP_USB_PATH_RE = re.compile(r"usb[-_](\d+)[-_](\d+)", re.IGNORECASE)
MTP_GIO_URI_RE = re.compile(r"mtp://\[usb:(\d+),(\d+)\][^\s]*", re.IGNORECASE)


class Volume:
    """One mounted filesystem that may contain ROMs."""

    def __init__(self, device: str, mountpoint: str, label: str = "",
                 removable: bool = False, total: int = 0, free: int = 0,
                 transport: str = "usb", skip_dirs: Optional[Iterable[str]] = None):
        self.device = device
        self.mountpoint = mountpoint
        self.label = label or os.path.basename(mountpoint.rstrip("/")) or device
        self.removable = removable
        self.total = total
        self.free = free
        self.transport = transport
        self.is_mtp = transport == "mtp"
        self.skip_dirs = frozenset(skip_dirs if skip_dirs is not None else _SKIP_DIRS)
        self._roms: Optional[List[Tuple[str, int]]] = None
        # Set by the watcher's new-content analysis: [(path, size)] of ROMs
        # whose content id is not registered in the library. None means
        # "not analysed" (discovery only), and counts fall back to rom_count.
        self.new_roms: Optional[List[Tuple[str, int]]] = None

    @property
    def key(self) -> str:
        """Stable identity for "have I already offered this source?" tracking."""
        return f"{self.device}@{self.mountpoint}" if self.device else self.mountpoint

    @property
    def index_namespace(self) -> str:
        """Optional source-specific namespace for the content-id cache."""
        return f"mtp:{self.key}" if self.is_mtp else ""

    @property
    def kind_label(self) -> str:
        """Short UI badge describing how the source is attached."""
        if self.is_mtp:
            return "ANDROID MTP"
        return "REMOVABLE" if self.removable else "MOUNTED"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"<Volume {self.label} {self.mountpoint} "
                f"transport={self.transport} removable={self.removable}>")

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
                             if d not in self.skip_dirs and not d.startswith("."))
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


# --------------------------------------------------------------------- MTP --
# An Android phone in File Transfer mode is a USB *MTP* device, not a disk.
# There is no /dev/sdX partition, no /sys/block entry and no removable flag.
# libmtp (via `jmtpfs -l`/`mtp-detect`), the USB interface class, or an
# already-mounted GVfs `mtp://` volume identifies it; jmtpfs/GVfs supplies a
# FUSE path whose files can be walked, hashed and copied by Volume's ordinary
# machinery. No ROM bytes are written to GAMES before the user confirms.


def _safe_component(value: str, fallback: str = "device") -> str:
    """Filesystem-safe component that can never be '.' or '..'."""
    cleaned = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in value)
    cleaned = cleaned.strip(" ._")
    while cleaned.startswith("."):
        cleaned = cleaned[1:]
    return cleaned[:64] or fallback


def _safe_mtp_label(value: str, fallback: str = "Android MTP") -> str:
    """A readable UI/folder label that can never become '.' or '..'."""
    cleaned = "".join(c if (c.isalnum() or c in " -_.·") else "_" for c in value).strip()
    while cleaned.startswith("."):
        cleaned = cleaned[1:]
    return cleaned[:96].strip() or fallback


def _hex_id(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("0x"):
        value = value[2:]
    return value.zfill(4) if value and all(c in "0123456789abcdef" for c in value) else ""


def _unescape_mount_field(value: str) -> str:
    """Decodes the four escapes the kernel writes into /proc/mounts fields."""
    escapes = {"040": " ", "011": "\t", "012": "\n", "134": "\\"}
    return re.sub(r"\\(040|011|012|134)", lambda m: escapes[m.group(1)], value)


def _mtp_bus_dev(text: str) -> Tuple[int, int]:
    """Extracts (bus, device) from a GVfs URI, mount name or jmtpfs path."""
    decoded = urllib.parse.unquote(str(text))
    match = MTP_BUS_RE.search(decoded) or MTP_USB_PATH_RE.search(decoded)
    if match:
        return int(match.group(1)), int(match.group(2))
    return 0, 0


def _command_text(output: Any) -> str:
    """Only real command output is parsed; mock/None values behave as empty."""
    if isinstance(output, bytes):
        return output.decode("utf-8", "ignore")
    return output if isinstance(output, str) else ""


def parse_gio_mtp_uris(output: str) -> List[str]:
    """Returns MTP URIs advertised by `gio mount -l`."""
    return [match.group(0).rstrip(".,;")
            for match in MTP_GIO_URI_RE.finditer(_command_text(output))]


@dataclass(frozen=True)
class MtpDevice:
    """One USB MTP device (possibly with several accessible storages)."""

    bus: int = 0
    dev: int = 0
    vendor_id: str = ""
    product_id: str = ""
    manufacturer: str = ""
    product: str = ""
    serial: str = ""
    uri: str = ""
    backend: str = "jmtpfs"

    @property
    def label(self) -> str:
        product = " ".join((self.product or "").split()) or "Android phone"
        vendor = " ".join((self.manufacturer or "").split())
        if vendor and vendor.lower() not in product.lower():
            return f"{vendor} {product}"
        return product

    @property
    def identity(self) -> str:
        # Serial survives a replug; bus/device numbers do not. Fall back to the
        # USB address only for backends (jmtpfs -l/GVfs) that omit the serial.
        vid, pid = self.vendor_id or "vid", self.product_id or "pid"
        if self.serial:
            return "-".join(("usb", vid, pid,
                             _safe_component(self.serial, "serial")))
        return "-".join(("usb", f"{self.bus:03d}", f"{self.dev:03d}", vid, pid))

    def uri_value(self) -> str:
        if self.uri:
            return self.uri
        if self.bus and self.dev:
            return f"mtp://[usb:{self.bus:03d},{self.dev:03d}]/"
        return "mtp://device/"

    def location(self) -> Tuple[int, int]:
        return self.bus, self.dev


class MtpVolume(Volume):
    """
    A phone storage exposed through an MTP FUSE mount.

    It deliberately has `removable=False` and a non-`/dev/` device URI: the
    shell must not apply block-disk heuristics (mounting, partitioning,
    `/sys/block`) to MTP. Files below `mountpoint` are ordinary FUSE paths, so
    `scan_roms`, `SourceIndex`, `find_new_roms` and `import_roms` work exactly
    as they do for a pendrive.
    """

    def __init__(self, device: Any, mountpoint: str, storage: str = "",
                 mount_root: str = "", backend: str = "jmtpfs", label: str = ""):
        if not isinstance(device, MtpDevice):
            # Convenience for callers that only know a USB/GVfs device string.
            text = str(device or "")
            bus, dev = _mtp_bus_dev(text)
            device = MtpDevice(bus=bus, dev=dev, product="Android phone",
                               uri=text if text.startswith("mtp://") else "",
                               backend="fuse")
        storage = " ".join((storage or "").split())
        if not label:
            suffix = f" · {storage}" if storage else ""
            label = _safe_mtp_label(f"{device.label}{suffix}", "Android MTP")
        total, free = _usage(mountpoint)
        super().__init__(device=device.uri_value(), mountpoint=mountpoint,
                         label=label, removable=False, total=total, free=free,
                         transport="mtp", skip_dirs=frozenset(_SKIP_DIRS) | MTP_SKIP_DIRS)
        self.mtp_device = device
        self.storage = storage or os.path.basename(mountpoint.rstrip("/"))
        self.mount_root = mount_root or os.path.dirname(mountpoint.rstrip("/"))
        self.backend = backend
        self._index_namespace = ":".join((
            "mtp", device.identity,
            _safe_component(self.storage, "storage"),
        ))

    @property
    def key(self) -> str:
        # Not device@mountpoint: a phone's USB address can change on replug,
        # and its FUSE mount never has a /dev/sdX identity.
        return f"mtp://{self.mtp_device.identity}/{_safe_component(self.storage, 'storage')}"

    @property
    def index_namespace(self) -> str:
        return self._index_namespace

    @property
    def kind_label(self) -> str:
        return "ANDROID MTP"

    def scan_roms(self, max_files: int = 2000, max_depth: int = 6) -> List[Tuple[str, int]]:
        return super().scan_roms(max_files=max_files, max_depth=max_depth)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<MtpVolume {self.label} {self.mountpoint} storage={self.storage}>"


def _mtp_storage_roots(root: str) -> List[Tuple[str, str]]:
    """
    Splits an MTP FUSE mount into its accessible storages.

    `jmtpfs` normally exposes "Internal storage" and an SD card as top-level
    directories. A direct-to-storage mount (or a test fixture) is returned as
    one volume. Media/app directories are left to `MtpVolume.scan_roms` to
    skip, not treated as separate import sources.
    """
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return []
    storages = []
    for name in entries:
        if name.startswith(".") or name in MTP_SKIP_DIRS:
            continue
        path = os.path.join(root, name)
        if os.path.isdir(path) and not os.path.islink(path):
            storages.append((path, name))
    if storages:
        return storages
    return [(root, os.path.basename(root.rstrip("/")) or "MTP storage")]


def parse_jmtpfs_devices(output: str) -> List[MtpDevice]:
    """
    Parses `jmtpfs -l`'s CSV device table:
    bus, dev, productId, vendorId, product, vendor.
    """
    devices: List[MtpDevice] = []
    for line in _command_text(output).splitlines():
        if not re.match(r"^\s*\d+\s*,", line):
            continue
        parts = [part.strip() for part in line.split(",", 5)]
        if len(parts) < 4:
            continue
        try:
            bus, dev = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        devices.append(MtpDevice(
            bus=bus, dev=dev, product_id=_hex_id(parts[2]),
            vendor_id=_hex_id(parts[3]),
            product=parts[4] if len(parts) > 4 else "",
            manufacturer=parts[5] if len(parts) > 5 else "",
            backend="jmtpfs",
        ))
    return devices


def parse_mtp_detect(output: str) -> List[MtpDevice]:
    """Best-effort parser for `mtp-detect` raw-device listings."""
    devices: Dict[Any, MtpDevice] = {}
    descriptions: Dict[str, Tuple[str, str, str, str]] = {}
    for line in _command_text(output).splitlines():
        header = re.search(
            r"Device\s+\d+\s+\(VID=([0-9a-fA-F]+)\s+and\s+PID=([0-9a-fA-F]+)\)\s+is\s+a\s+(.+)",
            line, re.IGNORECASE)
        if header:
            vid, pid = _hex_id(header.group(1)), _hex_id(header.group(2))
            raw = " ".join(header.group(3).split())
            manufacturer, _, product = raw.partition(" ")
            descriptions[f"{vid}:{pid}"] = (vid, pid, manufacturer.strip(),
                                            product.strip() or manufacturer.strip())
        location = re.search(
            r"\(([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\)\s*@\s*bus\s+(\d+),\s*dev\s+(\d+)",
            line, re.IGNORECASE)
        if not location:
            continue
        vid, pid = _hex_id(location.group(1)), _hex_id(location.group(2))
        bus, dev = int(location.group(3)), int(location.group(4))
        _vid, _pid, manufacturer, product = descriptions.get(
            f"{vid}:{pid}", (vid, pid, "", ""))
        prefix = line.split("(")[0].strip()
        if ":" in prefix:
            vendor, _, model = prefix.partition(":")
            manufacturer = manufacturer or vendor.strip()
            product = product or model.strip()
        devices[(bus, dev)] = MtpDevice(
            bus=bus, dev=dev, vendor_id=vid, product_id=pid,
            manufacturer=manufacturer, product=product, backend="mtp-detect")
    seen = {f"{d.vendor_id}:{d.product_id}" for d in devices.values()}
    for key, (vid, pid, manufacturer, product) in descriptions.items():
        if key not in seen:
            devices[("header", key)] = MtpDevice(
                vendor_id=vid, product_id=pid, manufacturer=manufacturer,
                product=product, backend="mtp-detect")
    return list(devices.values())


def discover_usb_mtp_devices(sysfs_root: str = DEFAULT_MTP_SYSFS) -> List[MtpDevice]:
    """
    Finds USB interfaces that speak MTP/PTP without touching /sys/block.

    This is the fallback when the libmtp tools are not installed, and also
    supplies serial numbers for devices found by `jmtpfs -l`. USB mass-storage
    class 8 is never considered an MTP device.
    """
    try:
        entries = sorted(os.listdir(sysfs_root))
    except OSError:
        return []
    interfaces: Dict[str, List[str]] = {}
    for entry in entries:
        if ":" not in entry:
            continue
        parent, _, _interface = entry.partition(":")
        interfaces.setdefault(parent, []).append(entry)
    devices: List[MtpDevice] = []
    for name in entries:
        if ":" in name or name not in interfaces:
            continue
        device_dir = os.path.join(sysfs_root, name)
        if not os.path.isdir(device_dir):
            continue
        is_mtp = False
        for interface_name in interfaces[name]:
            interface_dir = os.path.join(sysfs_root, interface_name)
            try:
                cls = int(_read(os.path.join(interface_dir, "bInterfaceClass"), "0"), 16)
                subclass = int(_read(os.path.join(interface_dir, "bInterfaceSubClass"), "0"), 16)
                protocol = int(_read(os.path.join(interface_dir, "bInterfaceProtocol"), "0"), 16)
            except ValueError:
                continue
            description = _read(os.path.join(interface_dir, "interface")).lower()
            # PTP cameras share class 6/1/1 with MTP; their interface string
            # distinguishes them and must not become an Android import source.
            if "ptp" in description and "mtp" not in description:
                continue
            # USB class 6/subclass 1/protocol 1 is PTP/MTP; many Android
            # devices additionally advertise "MTP" in the interface string.
            if (cls == 6 and (subclass == 1 and protocol == 1 or "mtp" in description)) \
                    or "mtp" in description:
                is_mtp = True
                break
        if not is_mtp:
            continue
        try:
            bus = int(_read(os.path.join(device_dir, "busnum"), "0"))
            dev = int(_read(os.path.join(device_dir, "devnum"), "0"))
        except ValueError:
            bus = dev = 0
        devices.append(MtpDevice(
            bus=bus, dev=dev,
            vendor_id=_hex_id(_read(os.path.join(device_dir, "idVendor"))),
            product_id=_hex_id(_read(os.path.join(device_dir, "idProduct"))),
            manufacturer=_read(os.path.join(device_dir, "manufacturer")),
            product=_read(os.path.join(device_dir, "product")),
            serial=_read(os.path.join(device_dir, "serial")),
            backend="sysfs",
        ))
    return devices


def parse_mtp_mount_entries(path: str = "/proc/mounts") -> List[Tuple[str, str, str]]:
    """
    Returns [(device, mountpoint, fstype)] for MTP/GVfs FUSE mounts.

    Block-device mounts are excluded even when their mountpoint contains the
    word "mtp"; nothing here treats the phone as a removable disk.
    """
    entries: List[Tuple[str, str, str]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except Exception:
        return entries
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        source, mountpoint, fstype = parts[0], _unescape_mount_field(parts[1]), parts[2]
        if source.startswith("/dev/") and source not in ("/dev/fuse", "/dev/fuse3") \
                and not fstype.startswith("fuse"):
            continue
        marker = f"{source} {mountpoint} {fstype}".lower()
        is_fuse = fstype.startswith("fuse") or "fuse" in source.lower()
        is_mtp = ("mtp" in marker or "gvfs" in marker or "/gvfs" in marker)
        if is_fuse and is_mtp:
            entries.append((source, mountpoint, fstype))
    return entries


def parse_fuse_mount_entries(path: str = "/proc/mounts") -> List[Tuple[str, str, str]]:
    """
    Returns all non-block FUSE mounts, for the generic `/dev/fuse ... fuse`
    line some jmtpfs versions write (with no "mtp" in the mount options).
    """
    entries: List[Tuple[str, str, str]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except Exception:
        return entries
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        source, mountpoint, fstype = parts[0], _unescape_mount_field(parts[1]), parts[2]
        if mountpoint == "/":
            continue
        if source.startswith("/dev/") and source not in ("/dev/fuse", "/dev/fuse3") \
                and not fstype.startswith("fuse"):
            continue
        if fstype.startswith("fuse") or "fuse" in source.lower():
            entries.append((source, mountpoint, fstype))
    return entries


def _looks_like_mtp_root(root: str) -> bool:
    """Recognises phone FUSE layouts when the mount line omits 'jmtpfs'."""
    try:
        entries = os.listdir(root)
    except OSError:
        return False
    for name in entries:
        lower = name.lower()
        if "mtp" in lower or any(hint in lower for hint in MTP_STORAGE_HINTS):
            return True
        path = os.path.join(root, name)
        if os.path.isfile(path) and lower.endswith(ROM_EXTENSIONS):
            return True
    return False


def default_gvfs_roots() -> List[str]:
    """Likely GVfs mount roots, including the current user's session."""
    roots: List[str] = []
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        roots.append(os.path.join(xdg, "gvfs"))
    roots.append(f"/run/user/{os.getuid()}/gvfs")
    roots.extend(sorted(glob.glob("/run/user/*/gvfs")))
    return list(dict.fromkeys(os.path.normpath(root) for root in roots))


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
                # /dev/fuse is a kernel passthrough node, not a block disk.
                # MTP/GVfs mounts are handled by parse_mtp_mount_entries().
                if device in ("/dev/fuse", "/dev/fuse3"):
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
    Lists candidate *block-device* ROM sources.

    `include_any_media` also returns fixed disks mounted under /media or /mnt,
    which is what a manually mounted USB stick looks like on some desktops.
    `/dev/fuse` is not a disk: MTP/GVfs mounts are discovered separately by
    `MtpManager`, so a phone can never enter this block-device path.
    """
    volumes: Dict[str, Volume] = {}
    for device, mountpoint in parse_mounts(mounts_path):
        if device in ("/dev/fuse", "/dev/fuse3"):
            continue
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


class MtpManager:
    """
    Discovers MTP devices and turns their FUSE storages into `MtpVolume`s.

    All detection and mounting happens on the watcher thread. `poll()` is
    idempotent and cheap when no phone is present; failed mounts (a locked
    screen or charging-only mode) are retried only after `MTP_RETRY_INTERVAL`.
    GVfs mounts made by a desktop session are reused instead of claiming the
    same USB device a second time.
    """

    def __init__(self, auto_mount: bool = True, mounts_path: str = "/proc/mounts",
                 sysfs_root: Optional[str] = None, mount_root: Optional[str] = None,
                 gvfs_roots: Optional[Sequence[str]] = None,
                 extra_roots: Optional[Sequence[str]] = None,
                 runner: Optional[Callable[..., Any]] = None,
                 command_exists: Optional[Callable[[str], bool]] = None,
                 list_timeout: float = MTP_LIST_TIMEOUT,
                 mount_timeout: float = MTP_MOUNT_TIMEOUT):
        self.auto_mount = auto_mount
        self.mounts_path = mounts_path
        self.sysfs_root = sysfs_root or os.environ.get(MTP_SYSFS_ENV, DEFAULT_MTP_SYSFS)
        self.jmtpfs_bin = os.environ.get(MTP_JMTPFS_ENV, "jmtpfs")
        self.mtp_detect_bin = os.environ.get(MTP_DETECT_ENV, "mtp-detect")
        self.list_timeout = list_timeout
        self.mount_timeout = mount_timeout
        self.runner = runner
        self.command_exists = command_exists or (
            lambda name: bool(runner) or shutil.which(name) is not None)
        try:
            base = get_run_dir()
        except Exception:  # pragma: no cover - a read-only /run fallback
            import tempfile
            base = tempfile.gettempdir()
        self.mount_root = (mount_root or os.environ.get(MTP_MOUNT_ROOT_ENV)
                           or os.path.join(base, "mtp"))
        self.gvfs_roots = tuple(gvfs_roots) if gvfs_roots is not None \
            else tuple(default_gvfs_roots())
        env_roots = [p for p in os.environ.get("GBA_MTP_ROOTS", "").split(os.pathsep) if p]
        self.extra_roots = tuple(extra_roots or ()) + tuple(env_roots)
        self._lock = threading.RLock()
        self._owned: Dict[str, str] = {}
        self._failed_until: Dict[str, float] = {}
        self._devices: List[MtpDevice] = []
        self._volumes: List[MtpVolume] = []
        self.last_error = ""

    # -- command plumbing -------------------------------------------------
    def _has_command(self, name: str) -> bool:
        try:
            return bool(self.command_exists(name))
        except Exception:
            return False

    def _run(self, argv: Sequence[str], timeout: float) -> Any:
        argv = list(argv)
        if self.runner is subprocess.run:
            return self.runner(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, timeout=timeout, check=False)
        if self.runner is not None:
            try:
                return self.runner(argv, timeout=timeout)
            except TypeError:
                return self.runner(argv)
        return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=timeout, check=False)

    @staticmethod
    def _field(proc: Any, name: str) -> str:
        value = proc.get(name, "") if isinstance(proc, dict) else getattr(proc, name, "")
        if isinstance(value, bytes):
            value = value.decode("utf-8", "ignore")
        return str(value or "").strip()

    @staticmethod
    def _returncode(proc: Any) -> int:
        value = proc.get("returncode", 1) if isinstance(proc, dict) \
            else getattr(proc, "returncode", 1)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 1

    def _run_text(self, argv: Sequence[str], timeout: float) -> Tuple[int, str, str]:
        try:
            proc = self._run(argv, timeout)
            if isinstance(proc, bytes):
                return 0, proc.decode("utf-8", "ignore"), ""
            if isinstance(proc, str):
                return 0, proc, ""
            return (self._returncode(proc), self._field(proc, "stdout"),
                    self._field(proc, "stderr"))
        except Exception as exc:
            logger.debug("MTP command unavailable: %s: %s", " ".join(argv), exc)
            return 1, "", str(exc)

    # -- device discovery -------------------------------------------------
    @staticmethod
    def _device_key(device: MtpDevice) -> Tuple[str, Any, ...]:
        if device.bus and device.dev:
            return ("location", device.bus, device.dev)
        return ("identity", device.vendor_id, device.product_id, device.serial)

    @staticmethod
    def _with_sysfs(primary: MtpDevice, sysfs: MtpDevice) -> MtpDevice:
        return replace(primary,
                       vendor_id=primary.vendor_id or sysfs.vendor_id,
                       product_id=primary.product_id or sysfs.product_id,
                       manufacturer=primary.manufacturer or sysfs.manufacturer,
                       product=primary.product or sysfs.product,
                       serial=primary.serial or sysfs.serial)

    def _discover_devices(self) -> List[MtpDevice]:
        sysfs_devices = discover_usb_mtp_devices(self.sysfs_root)
        sysfs_by_key = {self._device_key(device): device for device in sysfs_devices}
        found: Dict[Tuple[str, Any, ...], MtpDevice] = {}
        if self._has_command(self.jmtpfs_bin):
            _code, out, err = self._run_text([self.jmtpfs_bin, "-l"], self.list_timeout)
            for device in parse_jmtpfs_devices(out):
                key = self._device_key(device)
                sysfs = sysfs_by_key.get(key)
                found[key] = self._with_sysfs(device, sysfs) if sysfs else device
            if not found and err:
                self.last_error = err
        if not found and self._has_command(self.mtp_detect_bin):
            _code, out, _err = self._run_text([self.mtp_detect_bin], self.list_timeout)
            for device in parse_mtp_detect(out):
                key = self._device_key(device)
                sysfs = sysfs_by_key.get(key)
                found[key] = self._with_sysfs(device, sysfs) if sysfs else device
        if not found and self._has_command("gio"):
            _code, out, _err = self._run_text(["gio", "mount", "-l"], self.list_timeout)
            for uri in parse_gio_mtp_uris(out):
                bus, dev = _mtp_bus_dev(uri)
                device = MtpDevice(bus=bus, dev=dev, product="Android phone",
                                   uri=uri, backend="gio")
                key = self._device_key(device)
                sysfs = sysfs_by_key.get(key)
                found[key] = self._with_sysfs(device, sysfs) if sysfs else device
        for device in sysfs_devices:
            key = self._device_key(device)
            found[key] = self._with_sysfs(found[key], device) if key in found else device
        # `mtp-detect` header-only entries have no bus/address. Merge them into
        # a located device with the same VID/PID rather than mounting twice.
        for key, device in list(found.items()):
            if (device.bus and device.dev) or not (device.vendor_id and device.product_id):
                continue
            for other_key, other in list(found.items()):
                if other_key != key and other.bus and other.dev \
                        and other.vendor_id == device.vendor_id \
                        and other.product_id == device.product_id:
                    found[other_key] = self._with_sysfs(other, device)
                    found.pop(key, None)
                    break
        return list(found.values())

    def _device_from_hint(self, hint: Any, backend: str) -> MtpDevice:
        if isinstance(hint, MtpDevice):
            return hint
        bus, dev = _mtp_bus_dev(str(hint or ""))
        if bus and dev:
            for device in self._devices:
                if device.bus == bus and device.dev == dev:
                    return device
            return MtpDevice(bus=bus, dev=dev, product="Android phone", backend=backend)
        return MtpDevice(product="Android phone", backend=backend)

    # -- mounted roots ----------------------------------------------------
    def _find_existing_roots(self) -> List[Tuple[str, MtpDevice, str]]:
        roots: Dict[str, Tuple[str, MtpDevice, str]] = {}

        def add_root(root: str, hint: Any = None, backend: str = "fuse") -> None:
            if not root or not os.path.isdir(root):
                return
            real = os.path.realpath(root)
            device = self._device_from_hint(hint or root, backend)
            existing = roots.get(real)
            if existing is None or (not existing[1].bus and device.bus):
                roots[real] = (root, device, backend)

        def add_gvfs_root(root: str) -> None:
            base = os.path.basename(root.rstrip("/")).lower()
            if "mtp" in base and ("host=" in base or base.startswith("mtp:")):
                add_root(root, root, "gvfs")
            try:
                entries = sorted(os.listdir(root))
            except OSError:
                return
            for name in entries:
                lower = name.lower()
                path = os.path.join(root, name)
                if ("mtp" in lower and ("host=" in lower or lower.startswith("mtp:"))) \
                        and os.path.isdir(path):
                    add_root(path, name, "gvfs")

        for source, mountpoint, fstype in parse_mtp_mount_entries(self.mounts_path):
            marker = f"{source} {fstype}".lower()
            if "gvfs" in marker or "gvfs" in mountpoint.lower():
                add_gvfs_root(mountpoint)
            else:
                add_root(mountpoint, mountpoint, "jmtpfs")
        # Some jmtpfs/FUSE versions show only `/dev/fuse ... fuse` with no MTP
        # marker. Accept such a mount only when its contents look like phone
        # storage, and handle GVfs roots through their `mtp:host=` children.
        for source, mountpoint, fstype in parse_fuse_mount_entries(self.mounts_path):
            marker = f"{source} {mountpoint} {fstype}".lower()
            if "gvfs" in marker:
                add_gvfs_root(mountpoint)
                continue
            if any(tag in marker for tag in ("sshfs", "curlftpfs", "rclone",
                                             "s3fs", "encfs", "ntfs")):
                continue  # another FUSE filesystem, not a phone
            if _looks_like_mtp_root(mountpoint):
                add_root(mountpoint, mountpoint, "jmtpfs")
        for root in self.gvfs_roots:
            add_gvfs_root(root)
        for root in self.extra_roots:
            add_root(root, root, "fuse")
        with self._lock:
            for identity, root in list(self._owned.items()):
                if os.path.isdir(root):
                    hint = next((d for d in self._devices if d.identity == identity), None)
                    add_root(root, hint or root, "jmtpfs")
                else:
                    self._owned.pop(identity, None)
        return list(roots.values())

    # -- mounting ---------------------------------------------------------
    def _mount_one(self, device: MtpDevice) -> Optional[str]:
        if not self._has_command(self.jmtpfs_bin):
            self.last_error = "jmtpfs is not installed"
            return None
        root = os.path.join(self.mount_root, _safe_component(device.identity, "mtp"))
        try:
            os.makedirs(root, exist_ok=True)
        except OSError as exc:
            self.last_error = str(exc)
            return None
        argv = [self.jmtpfs_bin]
        if device.bus and device.dev:
            argv.append(f"-device={device.bus},{device.dev}")
        argv.append(root)
        code, out, err = self._run_text(argv, self.mount_timeout)
        if code != 0 or not os.path.isdir(root):
            self.last_error = " ".join((err, out)).strip() or "jmtpfs could not open the device"
            with self._lock:
                self._failed_until[device.identity] = time.monotonic() + MTP_RETRY_INTERVAL
            try:
                os.rmdir(root)  # only succeeds when the failed mount left it empty
            except OSError:
                pass
            logger.info("MTP mount failed for %s: %s", device.identity, self.last_error)
            return None
        self.last_error = ""
        with self._lock:
            self._owned[device.identity] = root
        logger.info("Mounted Android MTP device %s at %s", device.label, root)
        return root

    def _gio_mount(self, device: MtpDevice) -> bool:
        if not device.uri or not self._has_command("gio"):
            return False
        code, _out, err = self._run_text(["gio", "mount", device.uri], self.mount_timeout)
        if code != 0:
            self.last_error = err or "gio could not mount the MTP device"
            with self._lock:
                self._failed_until[device.identity] = time.monotonic() + MTP_RETRY_INTERVAL
            return False
        self.last_error = ""
        return True

    def _mount_devices(self, devices: List[MtpDevice],
                       roots: List[Tuple[str, MtpDevice, str]]) -> List[Tuple[str, MtpDevice, str]]:
        if not self.auto_mount:
            return roots
        mounted = {(d.bus, d.dev) for _root, d, _backend in roots if d.bus and d.dev}
        now = time.monotonic()
        gio_mounted = False
        for device in devices:
            if device.bus and device.dev and (device.bus, device.dev) in mounted:
                continue  # GVfs/jmtpfs already owns this USB device
            with self._lock:
                owned = self._owned.get(device.identity)
                retry_at = self._failed_until.get(device.identity, 0.0)
            if owned and os.path.isdir(owned):
                roots.append((owned, device, "jmtpfs"))
                continue
            if now < retry_at:
                continue
            if self._has_command(self.jmtpfs_bin):
                root = self._mount_one(device)
                if root:
                    roots.append((root, device, "jmtpfs"))
                    mounted.add((device.bus, device.dev))
                elif self._gio_mount(device):
                    gio_mounted = True
            elif self._gio_mount(device):
                gio_mounted = True
        if gio_mounted:
            # `gio mount` chooses its own GVfs path; rescan to discover it.
            roots = self._find_existing_roots()
        return roots

    # -- public API -------------------------------------------------------
    def discover_devices(self) -> List[MtpDevice]:
        """Detected MTP devices, without mounting or scanning their storages."""
        return self._discover_devices()

    def poll(self) -> List[MtpVolume]:
        with self._lock:
            self._devices = self._discover_devices()
            roots = self._find_existing_roots()
            roots = self._mount_devices(self._devices, roots)
            volumes: Dict[str, MtpVolume] = {}
            for root, device, backend in roots:
                for storage_path, storage_name in _mtp_storage_roots(root):
                    try:
                        volume = MtpVolume(device, storage_path, storage_name,
                                           mount_root=root, backend=backend)
                    except Exception as exc:  # pragma: no cover - broken FUSE path
                        logger.warning("Could not expose MTP storage %s: %s", storage_path, exc)
                        continue
                    volumes[volume.key] = volume
            self._volumes = sorted(volumes.values(), key=lambda v: v.mountpoint)
            return list(self._volumes)

    def cached_volumes(self) -> List[MtpVolume]:
        """Last poll's volumes without running any command or FUSE walk."""
        with self._lock:
            return list(self._volumes)

    def refresh(self) -> None:
        """Lets an explicit R/rescan retry a previously locked MTP session."""
        with self._lock:
            self._failed_until.clear()
            self.last_error = ""

    def status(self) -> str:
        """A short, user-facing hint when MTP is unavailable or needs unlock."""
        with self._lock:
            if self._volumes:
                return ""  # an existing GVfs/FUSE source already works
        if not self._has_command(self.jmtpfs_bin) and not self._has_command("gio"):
            return "Install jmtpfs for Android MTP (USB sticks still work)."
        if self.last_error:
            return "Unlock the phone and choose File Transfer (MTP)."
        return ""

    def close(self) -> None:
        """Unmounts FUSE mounts this manager created; never touches GVfs."""
        with self._lock:
            roots = list(self._owned.values())
            self._owned.clear()
        for root in roots:
            for command in ("fusermount3", "fusermount"):
                if not self._has_command(command):
                    continue
                code, _out, _err = self._run_text([command, "-u", root], 6.0)
                if code == 0:
                    break
            try:
                os.rmdir(root)
            except OSError:
                pass


def list_mtp_devices(sysfs_root: Optional[str] = None, **kwargs) -> List[MtpDevice]:
    """Convenience wrapper around MtpManager's libmtp/sysfs/GVfs discovery."""
    manager = MtpManager(auto_mount=False, sysfs_root=sysfs_root, **kwargs)
    return manager.discover_devices()


def list_mtp_volumes(mounts_path: str = "/proc/mounts", roots: Optional[Sequence[str]] = None,
                     auto_mount: bool = False, manager: Optional[MtpManager] = None,
                     **kwargs) -> List[MtpVolume]:
    """
    Lists Android/MTP import sources without disturbing block-device volumes.

    `roots` is useful for diagnostics and tests: it accepts already-mounted MTP
    FUSE paths. Passing a `manager` shares the watcher's mount state instead of
    creating a second one.
    """
    if roots is None:
        for alias in ("mtp_roots", "mount_roots", "source_roots", "fuse_roots"):
            if alias in kwargs:
                roots = kwargs.pop(alias)
                break
    if manager is None:
        manager = MtpManager(auto_mount=auto_mount, mounts_path=mounts_path,
                             extra_roots=roots, **kwargs)
    return manager.poll()


def scan_mtp_roms(volume: Volume) -> List[Tuple[str, int]]:
    """ROMs on one MTP/phone storage, using the shared extension/depth rules."""
    return volume.scan_roms()


def list_import_sources(mounts_path: str = "/proc/mounts", media_roots=MEDIA_ROOTS,
                        include_any_media: bool = True,
                        mtp_manager: Optional[MtpManager] = None,
                        mtp_roots: Optional[Sequence[str]] = None,
                        auto_mtp_mount: bool = False) -> List[Volume]:
    """All removable import sources: existing block volumes first, then MTP."""
    volumes = list_mounted_volumes(mounts_path, media_roots=media_roots,
                                   include_any_media=include_any_media)
    manager = mtp_manager or MtpManager(auto_mount=auto_mtp_mount,
                                        mounts_path=mounts_path,
                                        extra_roots=mtp_roots)
    try:
        mtp_volumes = manager.poll()
    except Exception as exc:
        logger.warning("Could not scan MTP devices: %s", exc)
        mtp_volumes = []
    seen_keys = {v.key for v in volumes}
    seen_paths = {os.path.realpath(v.mountpoint) for v in volumes}
    for volume in mtp_volumes:
        path = os.path.realpath(volume.mountpoint)
        if volume.key in seen_keys or path in seen_paths:
            continue
        volumes.append(volume)
        seen_keys.add(volume.key)
        seen_paths.add(path)
    return volumes


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
                   root: str, namespace: str = "") -> Optional[str]:
        """
        Content id of one source file, hashing only when its (size, mtime)
        differ from the cached record. Returns None if the file is unreadable.

        `namespace` keeps MTP device/storages with identically named files from
        sharing a cache record. Pendrives pass no namespace, so their cache
        keys and behaviour are unchanged.
        """
        try:
            key = os.path.relpath(path, root).replace(os.sep, "/")
        except ValueError:
            key = path
        if namespace:
            key = f"{namespace}:{key}"
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


def _source_mtime(path: str, is_mtp: bool = False) -> float:
    """
    MTP FUSE backends need not implement a stable modification time. A missing
    stat value is not fatal there: size and content hash still provide identity.
    """
    try:
        return os.path.getmtime(path)
    except OSError:
        if is_mtp:
            return 0.0
        raise


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
    namespace = getattr(volume, "index_namespace", "")
    is_mtp = getattr(volume, "is_mtp", False)
    new: List[Tuple[str, int]] = []
    for path, size in roms:
        try:
            mtime = _source_mtime(path, is_mtp)
        except OSError:
            continue
        content_id = (index.content_id(path, size, mtime, root, namespace)
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


def _copy_source(source: str, target: str, expected_size: int) -> None:
    """
    Copies one source file, tolerating FUSE filesystems that refuse `copystat`.

    MTP FUSE backends often implement no xattrs or chmod. `shutil.copy2` may
    raise after the bytes have already been written; that is still a successful
    copy as long as the destination has the verified source size.
    """
    try:
        shutil.copy2(source, target)
        return
    except OSError:
        if os.path.isfile(target) and os.path.getsize(target) == expected_size:
            return
        try:
            if os.path.exists(target):
                os.remove(target)
        except OSError:
            pass
        shutil.copyfile(source, target)


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

    # MTP labels can contain '·' or other characters a phone reports; the same
    # sanitizer used for pendrives keeps every destination inside GAMES.
    destination = os.path.join(games_dir, _safe_folder(volume.label)) if subfolder else games_dir
    result.destination = destination
    os.makedirs(destination, exist_ok=True)
    root = os.path.realpath(volume.mountpoint)
    namespace = getattr(volume, "index_namespace", "")

    total = len(roms)
    for index_no, (source, size) in enumerate(roms, start=1):
        name = os.path.basename(source)
        target = os.path.join(destination, name)
        if progress:
            progress(index_no, total, name)
        try:
            content_id = None
            if known_ids:
                mtime = _source_mtime(source, getattr(volume, "is_mtp", False))
                content_id = (index.content_id(source, size, mtime, root, namespace)
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
            _copy_source(source, target, size)
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
                 source_index: Optional[SourceIndex] = None,
                 mtp_manager: Optional[MtpManager] = None,
                 mtp_provider: Optional[Callable[[], Iterable[Volume]]] = None,
                 mtp_enabled: bool = True):
        super().__init__(daemon=True, name="gba-usb-watcher")
        self.interval = interval
        self.mounts_path = mounts_path
        self.media_roots = tuple(media_roots)
        self.known_ids_provider = known_ids_provider
        self.source_index = source_index
        self.mtp_manager = mtp_manager
        self.mtp_provider = mtp_provider
        self.mtp_enabled = mtp_enabled
        self.pending: List[Volume] = []
        self.volumes: List[Volume] = []
        self.seen: set = set()
        # NOTE: not `self._stop` — threading.Thread uses that name internally.
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._initialised = False

    def get_mtp_manager(self) -> MtpManager:
        """The process-wide MTP mount manager, created lazily on first poll."""
        if self.mtp_manager is None:
            self.mtp_manager = MtpManager(auto_mount=True,
                                          mounts_path=self.mounts_path)
        return self.mtp_manager

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
        # MTP is discovered separately: it is a USB protocol/FUSE filesystem,
        # not a /dev/sdX volume. Both kinds then share the same analysis and
        # pending/prompt machinery below.
        if self.mtp_enabled:
            try:
                provider = self.mtp_provider
                extra = list(provider() if provider is not None
                             else self.get_mtp_manager().poll())
            except Exception as exc:
                logger.warning("Could not list MTP devices: %s", exc)
                extra = []
            known = {v.key for v in volumes}
            known_paths = {os.path.realpath(v.mountpoint) for v in volumes}
            for volume in extra:
                path = os.path.realpath(volume.mountpoint)
                if volume.key in known or path in known_paths:
                    continue
                volumes.append(volume)
                known.add(volume.key)
                known_paths.add(path)
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
        try:
            while not self._stop_event.is_set():
                self.poll_once()
                self._stop_event.wait(self.interval)
        finally:
            # Unmount FUSE mounts we own on the worker thread, never in stop()
            # (which is called from the UI thread). GVfs mounts are left alone.
            if self.mtp_manager is not None:
                try:
                    self.mtp_manager.close()
                except Exception as exc:  # pragma: no cover - shutdown best effort
                    logger.warning("Could not clean up MTP mounts: %s", exc)

    def take_pending(self) -> List[Volume]:
        with self._lock:
            items, self.pending = self.pending, []
            return items

    def stop(self) -> None:
        self._stop_event.set()
        # A watcher constructed but never started has no run() cleanup.
        if not self.is_alive() and self.mtp_manager is not None:
            try:
                self.mtp_manager.close()
            except Exception:
                pass

    def refresh(self) -> List[Volume]:
        """
        Re-arms detection: every mounted volume is re-analysed on the
        watcher's next poll (used by the manual 'Scan USB' action). The
        analysis itself always runs on the watcher thread — never on the
        caller's — because hashing a large stick takes a long time.
        """
        with self._lock:
            self.seen.clear()
        if self.mtp_manager is not None:
            refresh = getattr(self.mtp_manager, "refresh", None)
            if callable(refresh):
                refresh()
        return list(self.volumes)


# Descriptive aliases for callers that talk about Android phones rather than
# generic MTP transports. The implementation and source objects are the same.
detect_mtp_devices = discover_usb_mtp_devices
detect_android_mtp_devices = discover_usb_mtp_devices
list_android_mtp_volumes = list_mtp_volumes
AndroidMtpVolume = MtpVolume
AndroidMtpDevice = MtpDevice
MTPVolume = MtpVolume
