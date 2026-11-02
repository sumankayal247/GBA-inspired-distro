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


class Library:
    def __init__(self, games_dir: Optional[str] = None, data_dir: Optional[str] = None):
        self.games_dir = games_dir or get_games_dir()
        self.data_dir = data_dir or get_data_dir()
        self.cache_dir = os.path.join(self.data_dir, "library")
        self.cache_file = os.path.join(self.cache_dir, "cache.json")
        os.makedirs(self.cache_dir, exist_ok=True)
        self._cache = self._load_cache()
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
                        entry = {
                            "id": entry_id,
                            "title": title,
                            "filename": file,
                            "type": "game",
                            "format": ext[1:].upper(),
                            "content_path": full_path,
                            "size_bytes": size,
                            "handler": "mgba-adapter",
                            "args": []
                        }
                        self._cache[cache_key] = {
                            "size": size,
                            "mtime": mtime,
                            "entry": entry
                        }
                        cache_updated = True

                    entries.append(entry)
                except Exception as e:
                    logger.error("Error processing ROM %s: %s", full_path, e)

        # Purge deleted files from cache
        current_keys = set(self._cache.keys())
        for key in list(current_keys):
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
        """Returns standard built-in appliance apps."""
        return [
            {
                "id": "app_system_monitor",
                "title": "System Monitor",
                "type": "app",
                "format": "SYS",
                "description": "Appliance memory, CPU and disk diagnostics",
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
            }
        ]

    def get_settings_entries(self) -> List[Dict[str, Any]]:
        """Returns standard settings options."""
        return [
            {
                "id": "setting_controls",
                "title": "Remap Keys",
                "type": "settings",
                "format": "KEY",
                "description": "Customize keys for A, B, L, R, D-Pad, Start & Select",
                "handler": "internal_settings_controls",
                "args": []
            },
            {
                "id": "setting_scale",
                "title": "Display Scaling",
                "type": "settings",
                "format": "CFG",
                "description": "Switch between Integer Aspect and Full Scale",
                "handler": "internal_settings_scale",
                "args": []
            },
            {
                "id": "setting_rescan",
                "title": "Rescan Library",
                "type": "settings",
                "format": "LIB",
                "description": "Re-index /games and refresh playable titles",
                "handler": "internal_rescan_library",
                "args": []
            },
            {
                "id": "setting_reboot",
                "title": "Reboot to GRUB Menu",
                "type": "settings",
                "format": "SYS",
                "description": "Cleanly restart PC and return to boot selection",
                "handler": "internal_reboot",
                "args": []
            },
            {
                "id": "setting_poweroff",
                "title": "Shutdown System",
                "type": "settings",
                "format": "SYS",
                "description": "Flush saves and power off the machine",
                "handler": "internal_poweroff",
                "args": []
            }
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
