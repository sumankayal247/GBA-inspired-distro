"""
Library management, scanning, and caching for GBA OS.
Implements Rules L1-L8 and ST2 from CONTRACT.md.
"""

import os
import json
import logging
from typing import Dict, List, Any, Optional
from common import (
    get_games_dir,
    get_data_dir,
    compute_content_id,
    extract_gba_title,
    DEFAULT_BASE_DIR,
)

logger = logging.getLogger("gba.library")

class Library:
    def __init__(self, games_dir: Optional[str] = None, data_dir: Optional[str] = None):
        self.games_dir = games_dir or get_games_dir()
        self.data_dir = data_dir or get_data_dir()
        self.cache_dir = os.path.join(self.data_dir, "library")
        self.cache_file = os.path.join(self.cache_dir, "cache.json")
        os.makedirs(self.cache_dir, exist_ok=True)
        self._cache = self._load_cache()

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
        entries = []
        if not os.path.exists(self.games_dir):
            return entries

        cache_updated = not os.path.exists(self.cache_file)
        allowed_exts = {".gba", ".gbc", ".gb"}

        for root, _, files in os.walk(self.games_dir):
            for file in sorted(files):
                ext = os.path.splitext(file)[1].lower()
                if ext not in allowed_exts:
                    continue

                full_path = os.path.join(root, file)
                try:
                    stat = os.stat(full_path)
                    cache_key = full_path
                    mtime = stat.st_mtime
                    size = stat.st_size

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

        return entries

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
            }
        ]

    def get_settings_entries(self) -> List[Dict[str, Any]]:
        """Returns standard settings options."""
        return [
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
