"""
Per-game data for GBA OS: cheats, nine save-state slots, favorites and deletion.

Everything here is keyed by the library's *stable content identity* (the
16-character SHA-256 prefix produced by `common.compute_content_id`, Rule L2),
never by the ROM filename.  It lives under the existing per-game DATA folder
that the supervisor already creates for the mGBA handler:

    DATA/saves/<game-id>/cheats.json            cheat metadata (small JSON)
    DATA/saves/<game-id>/cheats/<rom>.cheats    mGBA's native cheat file
    DATA/saves/<game-id>/states/slots.json      slot metadata (small JSON)
    DATA/saves/<game-id>/states/slot_1/state.ss        native mGBA state
    DATA/saves/<game-id>/states/slot_1/thumbnail.png   visual thumbnail only
    ...
    DATA/saves/<game-id>/states/slot_9/...
    DATA/config/favorites.json                  favorite content ids

Binary state data and screenshots are always separate files; JSON holds only
metadata.  Every read tolerates missing/corrupt files and every write is
atomic (tmp file + fsync + replace), so a torn write cannot poison a slot or
crash the emulator/supervisor.
"""

import hashlib
import json
import os
import re
import shutil
import time
from typing import Any, Dict, Iterable, List, Optional, Union

import common

CONTENT_ID_RE = re.compile(r"^[0-9a-f]{16}$")

SLOT_COUNT = 9
STATE_FILENAME = "state.ss"
THUMBNAIL_FILENAME = "thumbnail.png"
SLOTS_METADATA = "slots.json"
CHEATS_METADATA = "cheats.json"
CHEATS_DIRNAME = "cheats"
STATES_DIRNAME = "states"
FAVORITES_FILENAME = "favorites.json"

BytesOrPath = Union[bytes, str]


# ---------------------------------------------------------------------------
# Identity + atomic IO helpers
# ---------------------------------------------------------------------------

def valid_game_id(game_id: Any) -> bool:
    """True for a real library content id (Rule L2), never for a filename."""
    return isinstance(game_id, str) and bool(CONTENT_ID_RE.match(game_id))


def game_dir(game_id: str, data_dir: Optional[str] = None) -> str:
    """The existing per-game DATA directory (`DATA/saves/<game-id>`)."""
    if not valid_game_id(game_id):
        raise ValueError("invalid game identity")
    return os.path.join(data_dir or common.get_data_dir(),
                        common.SYS_SAVE_DIR_NAME, game_id)


def _atomic_json(path: str, payload: Dict[str, Any]) -> bool:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            if os.path.exists(path + ".tmp"):
                os.remove(path + ".tmp")
        except OSError:
            pass
        return False


def _load_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _payload_bytes(value: BytesOrPath) -> Optional[bytes]:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        try:
            with open(value, "rb") as f:
                return f.read()
        except Exception:
            return None
    return None


def _stage(path: str, payload: BytesOrPath) -> bool:
    """Writes a fully fsynced `<path>.tmp`, ready for an atomic commit."""
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if isinstance(payload, str):
            shutil.copyfile(payload, tmp)
        elif isinstance(payload, bytes):
            with open(tmp, "wb") as f:
                f.write(payload)
        else:
            return False
        with open(tmp, "rb") as f:
            os.fsync(f.fileno())
        return True
    except Exception:
        _discard(tmp)
        return False


def _discard(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _commit(path: str) -> bool:
    try:
        os.replace(path + ".tmp", path)
        return True
    except Exception:
        _discard(path + ".tmp")
        return False


def _atomic_write(path: str, payload: BytesOrPath) -> bool:
    """Writes bytes (or copies a file) atomically; never leaves a .tmp behind."""
    return _stage(path, payload) and _commit(path)


def _restore_payload(path: str, payload: Optional[bytes]) -> None:
    """Undo a committed file when a later step of a save fails."""
    if payload is None:
        _discard(path)
        return
    if _stage(path, payload):
        _commit(path)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _timestamp_label(timestamp: Optional[float]) -> str:
    if not timestamp:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Cheats — one list per game, applied through mGBA's native .cheats format
# ---------------------------------------------------------------------------

class CheatStore:
    """Per-game cheat list with persisted enabled/disabled state.

    The authoritative state is `cheats.json` (stable ids, enabled flags).
    `write_native()` renders exactly the same list into mGBA's documented
    `.cheats` text format (`!disabled` / `# Name` / code lines) so the handler
    can hand it to mgba-sdl with `-c <file>` instead of executing cheats
    itself.  A native cheat file dropped into the per-game folder is imported
    on first use, so no global cheat list is ever created.
    """

    def __init__(self, game_id: str, rom_path: Optional[str] = None,
                 data_dir: Optional[str] = None):
        self.game_id = game_id
        self.ok = valid_game_id(game_id)
        self.root = game_dir(game_id, data_dir) if self.ok else ""
        self.metadata_path = os.path.join(self.root, CHEATS_METADATA) if self.ok else ""
        self.cheats_dir = os.path.join(self.root, CHEATS_DIRNAME) if self.ok else ""
        base = "game"
        if rom_path:
            base = os.path.splitext(os.path.basename(rom_path))[0] or "game"
        self._base = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in base)
        self.native_path = os.path.join(self.cheats_dir, self._base + ".cheats") \
            if self.ok else ""
        self._data = self._load()
        if self.ok and os.path.isfile(self.native_path):
            self.import_native(self.native_path)

    def _load(self) -> Dict[str, Any]:
        data = _load_json(self.metadata_path) if self.ok else {}
        cheats = data.get("cheats") if isinstance(data.get("cheats"), list) else []
        cleaned = []
        seen = set()
        for item in cheats:
            if not isinstance(item, dict):
                continue
            cid = item.get("id")
            name = item.get("name")
            code = item.get("code")
            if not isinstance(cid, str) or not cid or cid in seen or \
                    not isinstance(name, str) or not isinstance(code, str):
                continue
            seen.add(cid)
            cleaned.append({"id": cid, "name": name, "code": code,
                            "enabled": bool(item.get("enabled"))})
        return {"version": 1, "game_id": self.game_id, "cheats": cleaned}

    def _save(self) -> bool:
        if not self.ok:
            return False
        return _atomic_json(self.metadata_path, self._data)

    @staticmethod
    def _cheat_id(name: str, code: str) -> str:
        return hashlib.sha256((name + "\n" + code).encode("utf-8",
                                                          errors="ignore")).hexdigest()[:12]

    def cheats(self) -> List[Dict[str, Any]]:
        """Cheats belonging to this game only (never a global list)."""
        return [dict(item) for item in self._data["cheats"]]

    def add_cheat(self, name: str, code: str, enabled: bool = False) -> Optional[str]:
        if not self.ok or not isinstance(name, str) or not isinstance(code, str) or not name:
            return None
        cid = self._cheat_id(name, code)
        for item in self._data["cheats"]:
            if item["id"] == cid:
                return cid
        self._data["cheats"].append({"id": cid, "name": name, "code": code,
                                     "enabled": bool(enabled)})
        self._save()
        self.write_native()
        return cid

    def update_cheat(self, cheat_id: str, name: str, code: str) -> bool:
        """Edits a cheat in place while keeping its enabled/disabled choice."""
        if not self.ok or not isinstance(name, str) or not isinstance(code, str):
            return False
        name = name.strip()
        code = "\n".join(line.strip() for line in code.splitlines() if line.strip())
        if not name or not code:
            return False
        new_id = self._cheat_id(name, code)
        for item in self._data["cheats"]:
            if item["id"] == new_id and item["id"] != cheat_id:
                return False               # do not create a duplicate entry
        for item in self._data["cheats"]:
            if item["id"] != cheat_id:
                continue
            previous = dict(item)
            item.update({"id": new_id, "name": name, "code": code})
            if not self._save():
                item.clear()
                item.update(previous)
                return False
            self.write_native()
            return True
        return False

    def remove_cheat(self, cheat_id: str) -> bool:
        """Deletes one cheat and refreshes (or removes) mGBA's native file."""
        if not self.ok:
            return False
        for index, item in enumerate(self._data["cheats"]):
            if item["id"] != cheat_id:
                continue
            removed = self._data["cheats"].pop(index)
            if not self._save():
                self._data["cheats"].insert(index, removed)
                return False
            self.write_native()
            return True
        return False

    def import_native(self, path: Optional[str] = None) -> int:
        """Imports a native mGBA .cheats file without losing known enabled state."""
        path = path or self.native_path
        if not self.ok or not path or not os.path.isfile(path):
            return 0
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.read().splitlines()
        except Exception:
            return 0
        known = {item["id"]: item for item in self._data["cheats"]}
        imported: List[Dict[str, Any]] = []
        name, code_lines, enabled = "", [], True
        next_disabled = False

        def commit() -> None:
            nonlocal name, code_lines, enabled
            if not name and not code_lines:
                return
            cheat_name = name or "Cheat"
            code = "\n".join(line for line in code_lines if line)
            cid = self._cheat_id(cheat_name, code)
            if cid in known:                 # keep the user's persisted state
                imported.append(dict(known[cid]))
            else:
                imported.append({"id": cid, "name": cheat_name, "code": code,
                                 "enabled": enabled})
            name, code_lines, enabled = "", [], True

        for raw in lines:
            line = raw.strip()
            if line.startswith("!disabled"):
                next_disabled = True
                continue
            if line.startswith("!"):        # directives (!reset, !...)
                continue
            if line.startswith("#"):
                commit()
                name, enabled = line[1:].strip(), not next_disabled
                next_disabled = False
                continue
            if line and not line.startswith("cheats = ") and not line.startswith("["):
                code_lines.append(raw.rstrip())
        commit()
        self._data["cheats"] = imported
        self._save()
        return len(imported)

    def set_enabled(self, cheat_id: str, enabled: bool) -> bool:
        """Persists one cheat's state and refreshes mGBA's native cheat file."""
        if not self.ok:
            return False
        for item in self._data["cheats"]:
            if item["id"] == cheat_id:
                item["enabled"] = bool(enabled)
                self._save()
                self.write_native()
                return True
        return False

    def write_native(self) -> Optional[str]:
        """Renders the per-game list in mGBA's native `.cheats` format.

        Removing the final cheat also removes the old native file. Leaving it
        behind would make CheatStore re-import the deleted cheat next boot.
        """
        if not self.ok:
            return None
        if not self._data["cheats"]:
            try:
                if os.path.isfile(self.native_path):
                    os.remove(self.native_path)
            except OSError:
                pass
            return None
        lines: List[str] = []
        for item in self._data["cheats"]:
            if not item["enabled"]:
                lines.append("!disabled")
            lines.append("# " + item["name"].replace("\n", " "))
            lines.extend(item["code"].splitlines())
        payload = ("\n".join(lines) + "\n").encode("utf-8", errors="ignore")
        return self.native_path if _atomic_write(self.native_path, payload) else None


# ---------------------------------------------------------------------------
# Nine save-state slots
# ---------------------------------------------------------------------------

class SaveStateStore:
    """Nine per-game slots holding an mGBA state file and a PNG thumbnail.

    `slots.json` is the only authority for "occupied".  Its checksum makes a
    corrupt or truncated state detectable, and a missing file simply makes the
    slot empty again — no crash, no half-visible slot.
    """

    def __init__(self, game_id: str, data_dir: Optional[str] = None):
        self.game_id = game_id
        self.ok = valid_game_id(game_id)
        self.root = os.path.join(game_dir(game_id, data_dir), STATES_DIRNAME) \
            if self.ok else ""
        self.metadata_path = os.path.join(self.root, SLOTS_METADATA) if self.ok else ""

    @staticmethod
    def slot_number(slot: Any) -> Optional[int]:
        try:
            number = int(slot)
        except (TypeError, ValueError):
            return None
        return number if 1 <= number <= SLOT_COUNT else None

    def slot_dir(self, slot: int) -> str:
        return os.path.join(self.root, f"slot_{slot}")

    def state_path(self, slot: int) -> str:
        return os.path.join(self.slot_dir(slot), STATE_FILENAME)

    def thumbnail_path(self, slot: int) -> str:
        return os.path.join(self.slot_dir(slot), THUMBNAIL_FILENAME)

    def _load(self) -> Dict[str, Any]:
        data = _load_json(self.metadata_path) if self.ok else {}
        raw = data.get("slots") if isinstance(data.get("slots"), dict) else {}
        slots: Dict[str, Dict[str, Any]] = {}
        for key, value in raw.items():
            number = self.slot_number(key)
            if number is None or not isinstance(value, dict):
                continue
            slots[str(number)] = {
                "timestamp": value.get("timestamp") if isinstance(
                    value.get("timestamp"), (int, float)) else None,
                "state_bytes": int(value.get("state_bytes") or 0),
                "state_sha256": value.get("state_sha256")
                if isinstance(value.get("state_sha256"), str) else "",
                "has_thumbnail": bool(value.get("has_thumbnail")),
            }
        return {"version": 1, "game_id": self.game_id, "slots": slots}

    def _save(self, metadata: Dict[str, Any]) -> bool:
        if not self.ok:
            return False
        return _atomic_json(self.metadata_path, metadata)

    def slots(self) -> List[Dict[str, Any]]:
        """Exactly nine slot descriptors, in slot order (1..9)."""
        metadata = self._load()
        result = []
        for number in range(1, SLOT_COUNT + 1):
            record = metadata["slots"].get(str(number))
            state_path = self.state_path(number) if self.ok else ""
            thumb_path = self.thumbnail_path(number) if self.ok else ""
            has_state = bool(state_path) and os.path.isfile(state_path)
            valid = False
            corrupt = False
            if has_state and record:
                try:
                    with open(state_path, "rb") as f:
                        payload = f.read()
                    valid = (len(payload) == record["state_bytes"] and
                             _sha256(payload) == record["state_sha256"])
                    corrupt = not valid
                except Exception:
                    corrupt = True
            has_thumb = bool(thumb_path) and os.path.isfile(thumb_path)
            result.append({
                "slot": number,
                "occupied": bool(record and has_state),
                "valid": bool(record and has_state and valid),
                "corrupt": corrupt,
                "timestamp": record["timestamp"] if record else None,
                "timestamp_label": _timestamp_label(record["timestamp"] if record else None),
                "state_bytes": record["state_bytes"] if record else 0,
                "has_thumbnail": bool(record and record["has_thumbnail"] and has_thumb),
                "state_path": state_path,
                "thumbnail_path": thumb_path,
            })
        # A partially written save leaves a .tmp behind; discard it quietly.
        if self.ok and os.path.isdir(self.root):
            for number in range(1, SLOT_COUNT + 1):
                _discard(self.state_path(number) + ".tmp")
                _discard(self.thumbnail_path(number) + ".tmp")
        return result

    def is_empty(self, slot: Any) -> bool:
        number = self.slot_number(slot)
        if number is None or not self.ok:
            return True
        return not self.slots()[number - 1]["occupied"]

    def state_for_load(self, slot: Any) -> Optional[str]:
        """Path of a verified native state file, or None when missing/corrupt."""
        number = self.slot_number(slot)
        if number is None:
            return None
        info = self.slots()[number - 1]
        return info["state_path"] if info["valid"] else None

    def save(self, slot: Any, state: BytesOrPath,
             thumbnail: Optional[BytesOrPath] = None,
             timestamp: Optional[float] = None) -> bool:
        """Atomically stores the real state and its visual thumbnail.

        Metadata is updated only after *both* files are safely in place, so a
        failed screenshot or a torn write cannot mark the slot as populated.
        """
        number = self.slot_number(slot)
        if number is None or not self.ok:
            return False
        state_bytes = _payload_bytes(state)
        if state_bytes is None or not state_bytes:
            return False
        thumb_bytes = _payload_bytes(thumbnail) if thumbnail is not None else None
        # Keep the previous slot contents in memory so a failed commit or a
        # failed metadata write cannot destroy an already-good save.
        state_path = self.state_path(number)
        thumb_path = self.thumbnail_path(number)
        previous = self._load()
        old_state = _payload_bytes(state_path) if os.path.isfile(state_path) else None
        old_thumb = _payload_bytes(thumb_path) if os.path.isfile(thumb_path) else None

        def rollback() -> None:
            _restore_payload(state_path, old_state)
            _restore_payload(thumb_path, old_thumb)
            _discard(state_path + ".tmp")
            _discard(thumb_path + ".tmp")
            self._save(previous)       # best effort: restore old timestamps

        # Stage both files first. An earlier good save stays untouched unless
        # every new part is ready to be committed.
        staged = _stage(state_path, state_bytes)
        if staged and thumb_bytes:
            staged = _stage(thumb_path, thumb_bytes)
        if not staged:
            _discard(state_path + ".tmp")
            _discard(thumb_path + ".tmp")
            return False
        if not _commit(state_path):
            _discard(thumb_path + ".tmp")
            return False
        if thumb_bytes and not _commit(thumb_path):
            _discard(thumb_path + ".tmp")
            rollback()
            return False
        metadata = self._load()
        metadata["slots"][str(number)] = {
            "timestamp": float(timestamp if timestamp is not None else time.time()),
            "state_bytes": len(state_bytes),
            "state_sha256": _sha256(state_bytes),
            "has_thumbnail": bool(thumb_bytes),
        }
        if not self._save(metadata):
            rollback()
            return False
        return True

    # Overwriting is the same atomic operation; the UI owns the confirmation.
    overwrite = save

    def clear(self, slot: Any) -> bool:
        number = self.slot_number(slot)
        if number is None or not self.ok:
            return False
        shutil.rmtree(self.slot_dir(number), ignore_errors=True)
        metadata = self._load()
        metadata["slots"].pop(str(number), None)
        return self._save(metadata)


# ---------------------------------------------------------------------------
# Favorites
# ---------------------------------------------------------------------------

def order_games(entries: Iterable[Dict[str, Any]],
                favorite_ids: Iterable[str]) -> List[Dict[str, Any]]:
    """Stable favorites-first ordering, preserving each group's own order.

    Duplicate entries are collapsed by content id, so sorting can never make
    a game appear twice.
    """
    favorites = set(favorite_ids or ())
    favs, rest, seen = [], [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_id = entry.get("id")
        if not entry_id or entry_id in seen:
            continue
        seen.add(entry_id)
        (favs if entry_id in favorites else rest).append(entry)
    return favs + rest


class Favorites:
    """Favorite content ids, persisted across restarts and OS reboots."""

    def __init__(self, data_dir: Optional[str] = None):
        self.path = os.path.join(data_dir or common.get_data_dir(),
                                 "config", FAVORITES_FILENAME)

    def ids(self) -> List[str]:
        data = _load_json(self.path)
        raw = data.get("ids") if isinstance(data.get("ids"), list) else []
        result, seen = [], set()
        for value in raw:
            if valid_game_id(value) and value not in seen:
                seen.add(value)
                result.append(value)
        return result

    def is_favorite(self, game_id: str) -> bool:
        return bool(valid_game_id(game_id) and game_id in self.ids())

    def set_favorite(self, game_id: str, favorite: bool) -> bool:
        if not valid_game_id(game_id):
            return False
        ids = [value for value in self.ids() if value != game_id]
        if favorite:
            ids.append(game_id)
        return _atomic_json(self.path, {"version": 1, "ids": ids})

    def toggle(self, game_id: str) -> bool:
        """Returns the new favorite state (False for an invalid identity)."""
        if not valid_game_id(game_id):
            return False
        new_state = not self.is_favorite(game_id)
        self.set_favorite(game_id, new_state)
        return new_state

    def remove(self, game_id: str) -> bool:
        return self.set_favorite(game_id, False)

    def order(self, entries: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return order_games(entries, self.ids())


# ---------------------------------------------------------------------------
# Game Management (delete selected / delete all)
# ---------------------------------------------------------------------------

def delete_game(entry: Dict[str, Any], games_dir: Optional[str] = None,
                data_dir: Optional[str] = None, library=None,
                favorites: Optional[Favorites] = None) -> Dict[str, Any]:
    """Permanently removes one imported ROM and all its per-game data.

    Only files inside the games directory with a ROM extension are ever
    unlinked, so a crafted library entry cannot delete arbitrary files.
    Missing files, bad identities and IO errors are reported, never raised.
    """
    result: Dict[str, Any] = {"id": entry.get("id", ""), "rom": False,
                              "data": False, "errors": []}
    if not isinstance(entry, dict):
        result["errors"].append("invalid entry")
        return result
    game_id = entry.get("id")
    content_path = entry.get("content_path") or ""
    games_root = os.path.realpath(games_dir or common.get_games_dir())

    if content_path and isinstance(content_path, str):
        try:
            real = os.path.realpath(content_path)
            ext = os.path.splitext(real)[1].lower()
            inside = os.path.commonpath((real, games_root)) == games_root
            if inside and ext in (".gba", ".gb", ".gbc") and os.path.isfile(real):
                os.remove(real)
                result["rom"] = True
            elif not inside or ext not in (".gba", ".gb", ".gbc"):
                result["errors"].append("ROM outside the games folder")
        except Exception as exc:
            result["errors"].append(f"ROM: {exc}")

    if not valid_game_id(game_id):
        result["errors"].append("invalid game identity")
        return result
    try:
        shutil.rmtree(game_dir(game_id, data_dir), ignore_errors=True)
        result["data"] = True
    except Exception as exc:
        result["errors"].append(f"data: {exc}")
    try:
        (favorites or Favorites(data_dir)).remove(game_id)
    except Exception:
        pass
    if library is not None and content_path:
        try:
            library.forget_path(content_path)
        except Exception:
            pass
    return result


def delete_all_games(entries: Iterable[Dict[str, Any]],
                     games_dir: Optional[str] = None,
                     data_dir: Optional[str] = None,
                     library=None,
                     favorites: Optional[Favorites] = None) -> Dict[str, Any]:
    """Deletes every imported game; returns counts for the UI confirmation."""
    results = [delete_game(entry, games_dir=games_dir, data_dir=data_dir,
                           library=library, favorites=favorites)
               for entry in (entries or []) if isinstance(entry, dict)]
    return {
        "count": len(results),
        "roms": sum(1 for r in results if r["rom"]),
        "data": sum(1 for r in results if r["data"]),
        "errors": [error for r in results for error in r["errors"]],
    }
