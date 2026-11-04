#!/usr/bin/env python3
"""
Android/MTP import API.

The implementation lives in `gba_usb.py` so phones and pendrives share one
watcher, content-ID cache, prompt and verified-copy pipeline. This module is a
small compatibility facade for callers that address MTP support directly.
"""

from gba_usb import (  # noqa: F401
    ANDROID_MTP_EXTENSIONS,
    AndroidMtpDevice,
    AndroidMtpVolume,
    MTP_EXTENSIONS,
    MTP_ROM_EXTENSIONS,
    MTPVolume,
    MtpDevice,
    MtpManager,
    MtpVolume,
    Volume,
    detect_android_mtp_devices,
    detect_mtp_devices,
    discover_usb_mtp_devices,
    find_new_roms,
    import_roms,
    list_android_mtp_volumes,
    list_import_sources,
    list_mounted_mtp_volumes,
    list_mtp_devices,
    list_mtp_volumes,
    mtp_diagnostics,
    parse_gio_mtp_uris,
    parse_jmtpfs_devices,
    parse_mtp_detect,
    parse_mtp_mount_entries,
    scan_mtp_roms,
)

# Alternative spellings used by MTP-focused callers/tests.
AndroidMTPDevice = AndroidMtpDevice
AndroidMTPVolume = AndroidMtpVolume
import_mtp_roms = import_roms
scan_android_mtp_roms = scan_mtp_roms
parse_mtp_mounts = parse_mtp_mount_entries

__all__ = [
    "ANDROID_MTP_EXTENSIONS", "AndroidMtpDevice", "AndroidMtpVolume",
    "AndroidMTPDevice", "AndroidMTPVolume", "MTP_EXTENSIONS",
    "MTP_ROM_EXTENSIONS", "MTPVolume", "MtpDevice", "MtpManager",
    "MtpVolume", "Volume", "detect_android_mtp_devices",
    "detect_mtp_devices", "discover_usb_mtp_devices", "find_new_roms",
    "import_mtp_roms", "import_roms", "list_android_mtp_volumes",
    "list_import_sources", "list_mtp_devices", "list_mtp_volumes",
    "parse_gio_mtp_uris", "parse_jmtpfs_devices", "parse_mtp_detect",
    "parse_mtp_mount_entries", "parse_mtp_mounts", "scan_android_mtp_roms",
    "scan_mtp_roms",
]
