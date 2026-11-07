"""
Library management, scanning, and caching for GBA OS.
Implements Rules L1-L8 and ST2 from CONTRACT.md.
"""

import os
import json
import logging
import threading
from typing import Dict, List, Any, Optional
from common import (
    get_games_dir,
    get_data_dir,
    compute_content_id,
    extract_gba_title,
)

logger = logging.getLogger("gba.library")

ALLOWED_EXTENSIONS = {".gba", ".gbc", ".gb"}

# Directories that never contain user content and are expensive to walk.
SKIP_DIRS = {"lost+found", ".Trash-1000", "$RECYCLE.BIN", "System Volume Information"}

# During a scan the cache is flushed to disk after every N newly hashed files.
# The scanner is a daemon thread: when the user opens a game the launcher
# exits immediately and the thread dies with it, so anything hashed since the
# last flush would be lost and the same ROMs would look "new" to the USB
# importer on the next boot. Flushing mid-scan makes that window small.
CACHE_SAVE_INTERVAL = 16


class Library:
    def __init__(self, games_dir: Optional[str] = None, data_dir: Optional[str] = None):
        self.games_dir = games_dir or get_games_dir()
        self.data_dir = data_dir or get_data_dir()
        self.cache_dir = os.path.join(self.data_dir, "library")
        self.cache_file = os.path.join(self.cache_dir, "cache.json")
        os.makedirs(self.cache_dir, exist_ok=True)
        self._cache = self._load_cache()
        # Guards `_cache` mutations: the scanner thread, `register_content`
        # (USB import worker) and `known_content_ids` (USB watcher thread)
        # all touch it concurrently. Reads of single keys stay lock-free
        # (GIL-atomic dict gets); snapshots take the lock.
        self._cache_lock = threading.Lock()
        self.last_scan_seconds = 0.0

    def _load_cache(self) -> Dict[str, Any]:
        """Loads the library cache. If absent or corrupt, returns empty dict (ST2)."""
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning("Corrupt cache file, resetting: %s", e)
        return {}

    def _save_cache(self) -> None:
        """Persists the scan cache atomically."""
        os.makedirs(self.cache_dir, exist_ok=True)
        tmp_file = self.cache_file + ".tmp"
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, indent=2)
            os.replace(tmp_file, self.cache_file)
        except Exception as e:
            logger.error("Failed to write cache: %s", e)

    def scan_games(self) -> List[Dict[str, Any]]:
        """
        Discovers all .gba, .gbc, .gb files in games_dir (L3).
        Derives ID from SHA-256 content hash (L2).
        Caches results by (path, size, mtime) (L4).
        """
        import time

        started = time.monotonic()
        entries = []
        if not os.path.exists(self.games_dir):
            self.last_scan_seconds = 0.0
            return entries

        cache_updated = not os.path.exists(self.cache_file)
        newly_hashed = 0

        for root, dirs, files in os.walk(self.games_dir, onerror=lambda e: None):
            # Deterministic walk order + skip system junk folders.
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
            for file in sorted(files):
                if file.startswith("."):
                    continue
                ext = os.path.splitext(file)[1].lower()
                if ext not in ALLOWED_EXTENSIONS:
                    continue

                full_path = os.path.join(root, file)
                try:
                    stat = os.stat(full_path)
                    cache_key = full_path
                    mtime = stat.st_mtime
                    size = stat.st_size

                    if size == 0:
                        logger.warning("Skipping empty file (not a ROM): %s", full_path)
                        continue

                    cached_entry = self._cache.get(cache_key)
                    if (
                        cached_entry
                        and cached_entry.get("size") == size
                        and cached_entry.get("mtime") == mtime
                    ):
                        entry = cached_entry["entry"]
                    else:
                        entry_id = compute_content_id(full_path)
                        title = extract_gba_title(full_path)
                        entry = self._make_entry(full_path, file, ext, size,
                                                 entry_id, title)
                        newly_hashed += 1
                        with self._cache_lock:
                            self._cache[cache_key] = {
                                "size": size,
                                "mtime": mtime,
                                "entry": entry
                            }
                            if newly_hashed % CACHE_SAVE_INTERVAL == 0:
                                self._save_cache()
                        cache_updated = True

                    entries.append(entry)
                except Exception as e:
                    logger.error("Error processing ROM %s: %s", full_path, e)

        # Purge deleted files from cache
        with self._cache_lock:
            for key in list(self._cache.keys()):
                if not os.path.exists(key):
                    del self._cache[key]
                    cache_updated = True

            if cache_updated:
                self._save_cache()

        self.last_scan_seconds = time.monotonic() - started
        # Stable, case-insensitive ordering: the same library looks the same
        # every boot regardless of filesystem order (Rule L3).
        entries.sort(key=lambda e: (e.get("title", "").lower(), e.get("filename", "").lower()))
        return entries

    @staticmethod
    def _make_entry(full_path: str, filename: str, ext: str, size: int,
                    entry_id: str, title: str) -> Dict[str, Any]:
        """Builds one library entry (shared by scan and register_content)."""
        return {
            "id": entry_id,
            "title": title,
            "filename": filename,
            "type": "game",
            "format": ext[1:].upper(),
            "content_path": full_path,
            "size_bytes": size,
            "handler": "mgba-adapter",
            "args": []
        }

    def known_content_ids(self) -> set:
        """
        Content ids (Rule L2) of everything the library already knows.

        This is the durable "already imported" set the USB importer must be
        measured against: the same ROM yields the same id regardless of
        filename or location, and the ids live in the on-disk cache, so the
        knowledge survives game returns, launcher restarts and reboots.
        """
        with self._cache_lock:
            records = list(self._cache.values())
        ids = set()
        for record in records:
            if not isinstance(record, dict):
                continue
            entry = record.get("entry")
            if isinstance(entry, dict) and entry.get("id"):
                ids.add(entry["id"])
        return ids

    def register_content(self, imported) -> int:
        """
        Seeds cache entries for freshly imported files whose content ids the
        importer already computed, so imported content is recognised
        immediately (before any rescan finishes) and the next rescan is a
        cache hit instead of a re-hash of every copy.

        `imported` is a sequence of (destination_path, content_id) pairs.
        Returns the number of entries registered.
        """
        added = 0
        for path, content_id in imported:
            if not content_id:
                continue
            try:
                stat = os.stat(path)
            except OSError:
                continue
            ext = os.path.splitext(path)[1].lower()
            if stat.st_size == 0 or ext not in ALLOWED_EXTENSIONS:
                continue
            entry = self._make_entry(path, os.path.basename(path), ext,
                                     stat.st_size, content_id,
                                     extract_gba_title(path))
            with self._cache_lock:
                self._cache[path] = {
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "entry": entry,
                }
            added += 1
        if added:
            with self._cache_lock:
                self._save_cache()
            logger.info("Registered %d imported file(s) in the library cache", added)
        return added

    def forget_path(self, path: str) -> bool:
        """Drops one deleted ROM from the disposable scan cache.

        Game Management removes the file itself; without this, a stale cache
        entry would keep reappearing as a known content id until the next full
        re-hash (and could make a re-imported copy look already present).
        """
        if not path:
            return False
        with self._cache_lock:
            if path not in self._cache:
                return False
            del self._cache[path]
            self._save_cache()
        return True

    def count_content_files(self) -> int:
        """Cheap extension-only count used by the empty state hint."""
        total = 0
        try:
            for _root, dirs, files in os.walk(self.games_dir, onerror=lambda e: None):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
                total += sum(
                    1 for f in files
                    if not f.startswith(".")
                    and os.path.splitext(f)[1].lower() in ALLOWED_EXTENSIONS
                )
        except Exception:
            return 0
        return total

    def start_scan(self) -> "LibraryScanner":
        """
        Kicks a scan off on a worker thread (Rule L4: scanning must not block
        the launcher from rendering or accepting input).
        """
        scanner = LibraryScanner(self)
        scanner.start()
        return scanner


    def get_built_in_apps(self) -> List[Dict[str, Any]]:
        """Returns standard built-in appliances, presented as cartridges."""
        return [
            {
                "id": "app_system_monitor",
                "title": "System Monitor",
                "type": "app",
                "format": "SYS",
                "description": "Live memory, storage and CPU telemetry",
                "handler": "internal_system_monitor",
                "args": []
            },
            {
                "id": "app_controller_tester",
                "title": "Input Diagnostics",
                "type": "app",
                "format": "SYS",
                "description": "Gamepad and keyboard layout testing",
                "handler": "internal_controller_tester",
                "args": []
            },
            {
                "id": "app_save_manager",
                "title": "Save Manager",
                "type": "app",
                "format": "SAV",
                "description": "Inspect and remove per-game save data",
                "handler": "internal_save_manager",
                "args": []
            },
            {
                "id": "app_usb_import",
                "title": "USB Import",
                "type": "app",
                "format": "USB",
                "description": "Copy ROMs from a pendrive or Android MTP phone into the library",
                "handler": "internal_usb_import",
                "args": []
            },
            {
                "id": "app_browser",
                "title": "Web Browser",
                "type": "app",
                "format": "NET",
                "description": "Lightweight text and image browser",
                "handler": "internal_browser",
                "args": []
            },
            {
                "id": "app_terminal",
                "title": "Terminal",
                "type": "app",
                "format": "SH",
                "description": "A real shell on the machine",
                "handler": "internal_terminal",
                "args": []
            },
        ]

    def get_settings_entries(self) -> List[Dict[str, Any]]:
        """Returns settings sections; each opens a real, configurable page."""
        return [
            {
                "id": "setting_display",
                "title": "Display",
                "type": "settings",
                "format": "DSP",
                "description": "Scaling, theme, clock and brightness",
                "handler": "internal_settings_display",
                "args": []
            },
            {
                "id": "setting_audio",
                "title": "Audio",
                "type": "settings",
                "format": "SND",
                "description": "Master volume and mute",
                "handler": "internal_settings_audio",
                "args": []
            },
            {
                "id": "setting_wifi",
                "title": "WiFi",
                "type": "settings",
                "format": "WIF",
                "description": "Connect to a wireless network",
                "handler": "internal_settings_wifi",
                "args": []
            },
            {
                "id": "setting_bluetooth",
                "title": "Bluetooth",
                "type": "settings",
                "format": "BT",
                "description": "Pair and connect Bluetooth devices",
                "handler": "internal_settings_bluetooth",
                "args": []
            },
            {
                "id": "setting_controls",
                "title": "Controls",
                "type": "settings",
                "format": "KEY",
                "description": "Remap every GBA button and check a gamepad",
                "handler": "internal_settings_controls",
                "args": []
            },
            {
                "id": "setting_library",
                "title": "Library",
                "type": "settings",
                "format": "LIB",
                "description": "View mode, USB auto-import and rescanning",
                "handler": "internal_settings_library",
                "args": []
            },
            {
                "id": "setting_game_management",
                "title": "Game Management",
                "type": "settings",
                "format": "GAM",
                "description": "Delete imported ROMs and their per-game data",
                "handler": "internal_settings_games",
                "args": []
            },
            {
                "id": "setting_storage",
                "title": "Storage & Saves",
                "type": "settings",
                "format": "SAV",
                "description": "Save backups, export archives and free space",
                "handler": "internal_settings_storage",
                "args": []
            },
            {
                "id": "setting_system",
                "title": "System",
                "type": "settings",
                "format": "SYS",
                "description": "Logs, recovery tools and contract details",
                "handler": "internal_settings_system",
                "args": []
            },
            {
                "id": "setting_reboot",
                "title": "Reboot",
                "type": "settings",
                "format": "PWR",
                "description": "Restart and return to the GRUB menu",
                "handler": "internal_reboot",
                "args": []
            },
            {
                "id": "setting_poweroff",
                "title": "Power Off",
                "type": "settings",
                "format": "PWR",
                "description": "Flush saves and shut the machine down",
                "handler": "internal_poweroff",
                "args": []
            },
        ]


class LibraryScanner(threading.Thread):
    """
    Non-blocking ROM scan (Rule L4).

    The launcher renders immediately and adopts `entries` once `done` flips,
    so a slow USB stick or a large hashing queue never freezes the shell.
    """

    def __init__(self, library: Library):
        super().__init__(daemon=True, name="gba-library-scan")
        self.library = library
        self.entries: List[Dict[str, Any]] = []
        self.done = False
        self.error: Optional[BaseException] = None

    def run(self) -> None:
        try:
            self.entries = self.library.scan_games()
        except BaseException as exc:  # never let a scan kill the shell
            self.error = exc
            logger.error("Library scan failed: %s", exc)
            self.entries = []
        finally:
            self.done = True
