#!/usr/bin/env python3
"""
GBA OS Shell / Launcher.

Implements Rules L1-L8, I1-I4, D1-D4, S6 from CONTRACT.md.
Provides a rich retro console user experience inspired by the Game Boy Advance.

Highlights
----------
* Rendering uses a fixed 480x320 logical canvas, scaled with integer factors (D1, D2).
* The ROM scan runs off-thread, so the shell is interactive immediately (L4).
* One input profile: keyboard bindings live in `common.DEFAULT_KEYBINDINGS` and are
  shared with the mGBA adapter, gamepads work out of the box, hot-plug included (I3, I4).
* Destructive actions always need an explicit confirmation (F2, SV6).
"""

import os
import sys
import time
import json
import math
import argparse
import datetime
import shutil

# Ensure local src directory is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pygame
from typing import List, Dict, Any, Optional, Tuple

from common import (
    get_run_dir,
    get_data_dir,
    get_games_dir,
    setup_logging,
    SYS_CONTRACT_VERSION,
    load_keybindings,
    save_keybindings,
    DEFAULT_KEYBINDINGS,
    GBA_BUTTON_ORDER,
    EXIT_CODE_LAUNCH_REQUEST,
    EXIT_CODE_REBOOT,
    EXIT_CODE_POWEROFF,
    load_play_history,
    record_play_session,
    format_duration,
    get_battery_status,
    get_uptime_seconds,
    get_load_average,
    get_memory_usage,
    load_display_config,
    save_display_config,
    display_scaling_label,
    DISPLAY_SCALING_MODES,
    keybindings_conflict_error,
    sanitize_keybindings,
)
from library import Library

logger = setup_logging("gba.launcher")

# Virtual Canvas Dimensions (2x native GBA 240x160 resolution)
CANVAS_WIDTH = 480
CANVAS_HEIGHT = 320

# Palette
COLOR_BG_DARK = (28, 23, 44)         # Deep slate violet
COLOR_BG_PANEL = (39, 32, 60)        # Indigo container
COLOR_BG_CARD = (49, 41, 74)         # Card surface
COLOR_BG_CARD_SEL = (76, 61, 115)    # Card selected
COLOR_ACCENT_GOLD = (246, 173, 85)   # Warm GBA accent gold
COLOR_ACCENT_CYAN = (99, 179, 237)   # Indigo/Cyan highlight
COLOR_ACCENT_MINT = (104, 211, 145)  # Success / save green
COLOR_ACCENT_RED = (245, 101, 101)   # Danger / power red
COLOR_TEXT_WHITE = (247, 250, 252)   # Pure text
COLOR_TEXT_MUTED = (160, 174, 192)   # Muted subtext
COLOR_BORDER = (60, 50, 90)          # Soft border
COLOR_CART_BODY = (128, 134, 150)    # GBA cartridge plastic grey
COLOR_CART_LABEL = (43, 30, 75)      # Cartridge sticker background

# Layout
LIST_RECT = pygame.Rect(10, 38, 250, 244)
DETAIL_RECT = pygame.Rect(268, 38, 202, 244)
LIST_HEADER_H = 24
LIST_ROW_H = 40
LIST_ROW_GAP = 3
LIST_VISIBLE_ROWS = 5
BOTTOM_BAR_Y = 286

# Analog stick navigation feel
AXIS_DEADZONE = 0.65
AXIS_INITIAL_DELAY = 0.28
AXIS_REPEAT_DELAY = 0.10

# Held-key navigation repeat. Implemented by the shell (not pygame's global key
# repeat) so typing a search query never produces repeated characters.
KEY_REPEAT_INITIAL_DELAY = 0.30
KEY_REPEAT_DELAY = 0.08
REPEATABLE_KEYS = {
    pygame.K_UP, pygame.K_DOWN, pygame.K_LEFT, pygame.K_RIGHT,
    pygame.K_PAGEUP, pygame.K_PAGEDOWN,
}

DEMO_GAMES = [
    {"id": "44636205c3e1a1b2", "title": "Celeste Classic", "format": "GBA",
     "size_bytes": int(5.2 * 1024 * 1024), "demo_save": True},
    {"id": "11125a10d4f8b0c1", "title": "Wonkle Guy", "format": "GBA",
     "size_bytes": int(2.2 * 1024 * 1024)},
    {"id": "80a88361e29f4a77", "title": "Yahtzee", "format": "GBC",
     "size_bytes": int(0.2 * 1024 * 1024)},
    {"id": "9f13ce44a1b27d90", "title": "Pocket Rally Advance", "format": "GBA",
     "size_bytes": int(8.0 * 1024 * 1024)},
    {"id": "5ac0b7e2f3d41c68", "title": "Block Drop DX", "format": "GB",
     "size_bytes": int(0.5 * 1024 * 1024)},
    {"id": "7b2e91d0c4a58f36", "title": "Neon Drift", "format": "GBA",
     "size_bytes": int(4.0 * 1024 * 1024)},
]


FONT_CANDIDATES = [
    # Bundled first (installer copies assets into /opt/gba-distro/assets).
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "fonts"),
    "/opt/gba-distro/assets/fonts",
]
FONT_FILES = ["DejaVuSans-Bold.ttf", "DejaVuSans.ttf"]
FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu",
             "/usr/share/fonts/TTF", "/usr/share/fonts/truetype/liberation"]


def _find_font_file(bold: bool) -> Optional[str]:
    names = FONT_FILES if bold else list(reversed(FONT_FILES))
    for directory in FONT_CANDIDATES + FONT_DIRS:
        for name in names:
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                return path
    return None


# TTF point sizes are visually smaller than pygame's built-in `size` metric,
# which the 480x320 layout was originally tuned against.
_TTF_SCALE = 1.0


def load_font(size: int, bold: bool = False) -> "pygame.font.Font":
    """Loads a TTF at a size comparable to pygame's built-in `size` metric."""
    path = _find_font_file(bold)
    if path:
        try:
            return pygame.font.Font(path, max(8, int(round(size * _TTF_SCALE))))
        except Exception:
            pass
    return pygame.font.Font(None, size)


class Toast:
    """Small transient status message shown above the bottom bar."""

    def __init__(self, text: str, color: Tuple[int, int, int], seconds: float = 2.6):
        self.text = text
        self.color = color
        self.expires = time.monotonic() + seconds


class GBALauncher:
    def __init__(
        self,
        focus_id: Optional[str] = None,
        last_outcome: Optional[str] = None,
        windowed: bool = False,
        screenshot_path: Optional[str] = None,
        initial_tab: int = 0,
        appliance_mode: bool = False,
        demo: bool = False,
    ):
        self.focus_id = focus_id
        self.last_outcome = last_outcome
        self.windowed = windowed
        self.screenshot_path = screenshot_path
        self.demo = demo
        self.appliance_mode = (
            appliance_mode
            or (not windowed and not screenshot_path)
            or (os.environ.get("GBA_APPLIANCE") == "1")
        )

        self.library = Library()
        self.games: List[Dict[str, Any]] = []
        self.apps = self.library.get_built_in_apps()
        self.settings = self.library.get_settings_entries()
        self.keybindings = load_keybindings()
        self.history = load_play_history()
        self.display_config = load_display_config()

        # Tabs: 0: GAMES, 1: APPS, 2: SETTINGS
        self.tabs = ["GAMES", "APPS", "SETTINGS"]
        self.current_tab = max(0, min(len(self.tabs) - 1, initial_tab))
        self.selected_indices = [0, 0, 0]
        self.scroll_offsets = [0, 0, 0]

        self.filter_text = ""
        self.toast: Optional[Toast] = None
        self.scan_started_at = time.monotonic()
        self.scanner = None
        self._focus_applied = False

        # Analog stick / held-key navigation state
        self.axis_gate = {"y": 0.0, "x": 0.0}
        self.axis_dir = {"y": 0, "x": 0}
        self.held_keys: Dict[int, float] = {}

        # Pygame setup
        if self.screenshot_path:
            os.environ["SDL_VIDEODRIVER"] = "dummy"
            os.environ["SDL_AUDIODRIVER"] = "dummy"

        pygame.init()
        pygame.joystick.init()
        self.joysticks: List["pygame.joystick.Joystick"] = []
        self._refresh_joysticks()

        self.canvas = pygame.Surface((CANVAS_WIDTH, CANVAS_HEIGHT))

        # Fonts: prefer a bundled/system TTF for crisp, consistent typography and
        # fall back to pygame's built-in font so the shell never hard-fails.
        pygame.font.init()
        self.font_title = load_font(13, bold=True)
        self.font_header = load_font(12, bold=True)
        self.font_body = load_font(11)
        self.font_small = load_font(9)

        # Target window/display setup
        if not self.screenshot_path:
            if self.windowed:
                self.screen_w, self.screen_h = 960, 640
                self.window = pygame.display.set_mode((self.screen_w, self.screen_h), pygame.RESIZABLE)
            else:
                display_info = pygame.display.Info()
                self.screen_w = display_info.current_w if display_info.current_w > 0 else 1024
                self.screen_h = display_info.current_h if display_info.current_h > 0 else 768
                self.window = pygame.display.set_mode((self.screen_w, self.screen_h), pygame.FULLSCREEN)
            pygame.display.set_caption("GBA OS Shell")
            # Blitting a converted surface is considerably faster at 60 fps.
            self.canvas = self.canvas.convert()
        else:
            self.screen_w, self.screen_h = CANVAS_WIDTH, CANVAS_HEIGHT
            self.window = self.canvas

        # Tab hit boxes are measured from the font so nothing clips at any size.
        self.tab_rects: List[pygame.Rect] = []
        x = 112
        for name in self.tabs:
            width = max(62, self.font_small.size(name)[0] + 18)
            self.tab_rects.append(pygame.Rect(x, 5, width, 22))
            x += width + 6

        if demo:
            self.games = self._demo_entries()
        else:
            self.scanner = self.library.start_scan()

        self.clock = pygame.time.Clock()
        self.running = True

    # ------------------------------------------------------------------ data
    def _demo_entries(self) -> List[Dict[str, Any]]:
        """Synthetic library used for documentation screenshots (`--demo`)."""
        entries = []
        for item in DEMO_GAMES:
            entry = dict(item)
            entry.update({
                "filename": f"{item['title'].replace(' ', '_')}.{item['format'].lower()}",
                "type": "game",
                "content_path": os.path.join(get_games_dir(), f"{item['title']}.{item['format'].lower()}"),
                "handler": "demo-preview",
                "args": [],
            })
            entries.append(entry)
        # Seed a plausible history so previews show the recent-play affordances.
        now = time.time()
        self.history = {
            DEMO_GAMES[0]["id"]: {"last_played": now - 7200, "sessions": 5, "seconds": 5400},
            DEMO_GAMES[2]["id"]: {"last_played": now - 3 * 86400, "sessions": 2, "seconds": 900},
        }
        return entries

    def update(self) -> None:
        """Per-frame bookkeeping: adopt the async scan, expire toasts."""
        if self.scanner is not None and self.scanner.done:
            self.games = self.scanner.entries
            self.scanner = None
            if self.games:
                logger.info("Library ready: %d entries in %.2fs",
                            len(self.games), self.library.last_scan_seconds)
            self._clamp_selection()
            if not self._focus_applied:
                self._apply_focus()
            else:
                self._clamp_selection()

        if self.toast is not None and time.monotonic() > self.toast.expires:
            self.toast = None

        self._poll_key_repeat()

    def _apply_focus(self) -> None:
        """Selects the previously played entry (or `--focus`) on first render."""
        items = self.games
        if not items:
            return
        self._focus_applied = True

        target = self.focus_id
        if not target and self.history:
            playable = {g["id"] for g in items}
            recent = sorted(
                self.history.items(),
                key=lambda kv: kv[1].get("last_played", 0),
                reverse=True,
            )
            for entry_id, _record in recent:
                if entry_id in playable:
                    target = entry_id
                    break

        if target:
            for idx, game in enumerate(items):
                if game["id"] == target:
                    self.current_tab = 0
                    self.selected_indices[0] = idx
                    return

    def _wait_for_scan(self, timeout: float = 3.0) -> None:
        """Used by `--screenshot` so previews render a populated library."""
        deadline = time.monotonic() + timeout
        while self.scanner is not None and not self.scanner.done:
            if time.monotonic() > deadline:
                break
            time.sleep(0.02)
        self.update()

    def visible_games(self) -> List[Dict[str, Any]]:
        """Game list after applying the incremental search filter."""
        if not self.filter_text:
            return self.games
        needle = self.filter_text.lower()
        return [
            g for g in self.games
            if needle in g.get("title", "").lower()
            or needle in g.get("filename", "").lower()
        ]

    def get_current_items(self) -> List[Dict[str, Any]]:
        if self.current_tab == 0:
            return self.visible_games()
        elif self.current_tab == 1:
            return self.apps
        return self.settings

    def current_item(self) -> Optional[Dict[str, Any]]:
        items = self.get_current_items()
        idx = self.selected_indices[self.current_tab]
        if 0 <= idx < len(items):
            return items[idx]
        return None

    def _clamp_selection(self) -> None:
        items = self.get_current_items()
        if not items:
            self.selected_indices[self.current_tab] = 0
            return
        self.selected_indices[self.current_tab] = max(
            0, min(len(items) - 1, self.selected_indices[self.current_tab])
        )

    def toast_message(self, text: str, color: Tuple[int, int, int] = COLOR_ACCENT_CYAN) -> None:
        self.toast = Toast(text, color)
        logger.info("Toast: %s", text)

    def save_exists(self, entry: Dict[str, Any]) -> bool:
        if entry.get("demo_save"):
            return True
        try:
            save_dir = os.path.join(get_data_dir(), "saves", entry["id"])
            return os.path.isdir(save_dir) and bool(os.listdir(save_dir))
        except Exception:
            return False

    def save_summary(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Bytes, file count and age of an entry's save directory."""
        if entry.get("demo_save"):
            return {
                "files": 1,
                "bytes": 8192,
                "age": 2 * 3600,
                "path": os.path.join(get_data_dir(), "saves", entry.get("id", "demo")),
            }
        summary = {"files": 0, "bytes": 0, "age": None, "path": ""}
        try:
            save_dir = os.path.join(get_data_dir(), "saves", entry["id"])
            summary["path"] = save_dir
            if not os.path.isdir(save_dir):
                return summary
            newest = None
            for item in os.scandir(save_dir):
                if item.is_file():
                    summary["files"] += 1
                    summary["bytes"] += item.stat().st_size
                    mtime = item.stat().st_mtime
                    newest = mtime if newest is None else max(newest, mtime)
            if newest:
                summary["age"] = max(0.0, time.time() - newest)
        except Exception:
            pass
        return summary

    # -------------------------------------------------------------- helpers
    def truncate(self, text: str, limit: int) -> str:
        return text if len(text) <= limit else text[: max(1, limit - 2)] + ".."

    def draw_panel(self, rect: pygame.Rect, border: Optional[Tuple[int, int, int]] = None,
                   fill: Optional[Tuple[int, int, int]] = None, radius: int = 6) -> None:
        pygame.draw.rect(self.canvas, fill or COLOR_BG_PANEL, rect, border_radius=radius)
        pygame.draw.rect(self.canvas, border or COLOR_BORDER, rect, width=1, border_radius=radius)

    def draw_button(self, rect: pygame.Rect, label: str,
                    accent: Tuple[int, int, int] = COLOR_ACCENT_GOLD,
                    filled: bool = True, text_color: Optional[Tuple[int, int, int]] = None) -> None:
        pygame.draw.rect(self.canvas, (50, 40, 85) if filled else (35, 25, 55),
                         rect, border_radius=4)
        pygame.draw.rect(self.canvas, accent, rect, 2 if filled else 1, border_radius=4)
        surface = self.font_small.render(label, True, text_color or accent)
        self.canvas.blit(surface, (
            rect.centerx - surface.get_width() // 2,
            rect.centery - surface.get_height() // 2,
        ))

    def draw_bar(self, rect: pygame.Rect, fraction: float,
                 color: Tuple[int, int, int], track: Tuple[int, int, int] = (20, 16, 32)) -> None:
        pygame.draw.rect(self.canvas, track, rect, border_radius=3)
        width = int(rect.width * max(0.0, min(1.0, fraction)))
        if 0 < width < 3:
            width = 3
        if width > 1:
            pygame.draw.rect(self.canvas, color,
                             pygame.Rect(rect.x, rect.y, width, rect.height), border_radius=3)

    def draw_chip_grid(self, x: int, y: int, items, columns: int,
                       row_height: int = 17, col_width: int = 0,
                       label_color: Tuple[int, int, int] = COLOR_ACCENT_CYAN,
                       value_color: Tuple[int, int, int] = COLOR_TEXT_WHITE) -> int:
        """
        Draws `[(label, value), ...]` in a tidy two-tone grid and returns next y.

        Used for control maps: labels align in a column, values in the next, so
        long key names ("BACKSPACE") never overrun a neighbour.
        """
        if not items:
            return y
        if not col_width:
            col_width = int((480 - 2 * x) / max(1, columns))
        label_width = int(col_width * 0.62)
        for index, (label, value) in enumerate(items):
            column = index % columns
            row = index // columns
            cell_x = x + column * col_width
            cell_y = y + row * row_height
            label_surf = self.font_small.render(label, True, label_color)
            value_surf = self.font_small.render(value, True, value_color)
            self.canvas.blit(label_surf, (cell_x, cell_y))
            self.canvas.blit(value_surf, (cell_x + label_width, cell_y))
        rows = (len(items) + columns - 1) // columns
        return y + rows * row_height

    def draw_kv(self, x: int, y: int, label: str, value: str,
                value_color: Tuple[int, int, int] = COLOR_TEXT_WHITE,
                label_color: Tuple[int, int, int] = COLOR_TEXT_MUTED) -> int:
        """Draws a `label: value` row and returns the next y coordinate."""
        label_surf = self.font_small.render(label, True, label_color)
        value_surf = self.font_small.render(value, True, value_color)
        self.canvas.blit(label_surf, (x, y))
        self.canvas.blit(value_surf, (x + 74, y))
        return y + 16

    def draw_modal_shell(self, rect: pygame.Rect, title: str,
                         accent: Tuple[int, int, int] = COLOR_ACCENT_GOLD,
                         subtitle: Optional[str] = None,
                         subtitle_color: Tuple[int, int, int] = COLOR_TEXT_MUTED) -> int:
        """Draws backdrop + modal frame + title; returns the content y offset."""
        self.render_base()
        overlay = pygame.Surface((CANVAS_WIDTH, CANVAS_HEIGHT), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 200))
        self.canvas.blit(overlay, (0, 0))

        pygame.draw.rect(self.canvas, COLOR_BG_PANEL, rect, border_radius=8)
        pygame.draw.rect(self.canvas, accent, rect, 2, border_radius=8)

        title_surf = self.font_header.render(title, True, accent)
        self.canvas.blit(title_surf, (rect.centerx - title_surf.get_width() // 2, rect.y + 10))
        if subtitle:
            sub_surf = self.font_small.render(subtitle, True, subtitle_color)
            self.canvas.blit(sub_surf, (rect.centerx - sub_surf.get_width() // 2, rect.y + 28))
        pygame.draw.line(self.canvas, COLOR_BORDER,
                         (rect.x + 15, rect.y + 44), (rect.right - 15, rect.y + 44), 1)
        return rect.y + 50

    def _refresh_joysticks(self) -> None:
        """Re-enumerate pads so controllers connected after boot just work (I4)."""
        try:
            self.joysticks = [
                pygame.joystick.Joystick(i) for i in range(pygame.joystick.get_count())
            ]
            for joy in self.joysticks:
                joy.init()
        except Exception as exc:
            logger.warning("Joystick enumeration failed: %s", exc)
            self.joysticks = []

    # --------------------------------------------------------------- chrome
    def draw_top_bar(self):
        """Retro console top bar: logo, categories, clock, battery."""
        pygame.draw.rect(self.canvas, (20, 16, 32), (0, 0, CANVAS_WIDTH, 33))
        pygame.draw.line(self.canvas, COLOR_BORDER, (0, 33), (CANVAS_WIDTH, 33), 1)

        logo = self.font_header.render("GBA", True, COLOR_ACCENT_GOLD)
        os_text = self.font_header.render("OS", True, COLOR_ACCENT_CYAN)
        self.canvas.blit(logo, (10, 8))
        self.canvas.blit(os_text, (48, 8))
        pygame.draw.circle(self.canvas, (180, 80, 120), (74, 15), 3)
        pygame.draw.circle(self.canvas, (100, 180, 240), (83, 15), 3)

        for i, rect in enumerate(self.tab_rects):
            is_active = (i == self.current_tab)
            if is_active:
                pygame.draw.rect(self.canvas, COLOR_BG_CARD_SEL, rect, border_radius=4)
                pygame.draw.rect(self.canvas, COLOR_ACCENT_GOLD, rect, width=1, border_radius=4)
            color = COLOR_TEXT_WHITE if is_active else COLOR_TEXT_MUTED
            label = self.font_small.render(self.tabs[i], True, color)
            self.canvas.blit(label, (
                rect.centerx - label.get_width() // 2,
                rect.centery - label.get_height() // 2,
            ))

        # Right cluster: search chip, clock, power/battery.
        right = CANVAS_WIDTH - 8
        battery = get_battery_status()
        if battery:
            level = battery["capacity"]
            color = COLOR_ACCENT_MINT if level > 30 else COLOR_ACCENT_RED
            pygame.draw.rect(self.canvas, COLOR_TEXT_MUTED, (right - 24, 8, 22, 13), width=1, border_radius=2)
            pygame.draw.rect(self.canvas, COLOR_TEXT_MUTED, (right - 3, 11, 2, 7))
            inner = int(18 * level / 100.0)
            if inner > 0:
                pygame.draw.rect(self.canvas, color, (right - 22, 10, inner, 9))
        else:
            # Mains powered: draw a small AC plug instead of a fake full battery.
            pygame.draw.circle(self.canvas, COLOR_ACCENT_MINT, (right - 8, 15), 6, width=1)
            pygame.draw.line(self.canvas, COLOR_ACCENT_MINT, (right - 12, 15), (right - 16, 15), 2)
            pygame.draw.line(self.canvas, COLOR_TEXT_MUTED, (right - 30, 15), (right - 22, 15), 2)

        now_str = datetime.datetime.now().strftime("%H:%M")
        clock = self.font_small.render(now_str, True, COLOR_TEXT_MUTED)
        self.canvas.blit(clock, (right - 34 - clock.get_width(), 10))

        if self.filter_text and self.current_tab == 0:
            chip_text = f"/{self.filter_text}"
            chip = self.font_small.render(chip_text, True, COLOR_BG_DARK)
            chip_rect = pygame.Rect(0, 8, chip.get_width() + 14, 16)
            chip_rect.right = right - 40 - clock.get_width() - 8
            pygame.draw.rect(self.canvas, COLOR_ACCENT_CYAN, chip_rect, border_radius=8)
            self.canvas.blit(chip, (chip_rect.x + 7, chip_rect.y + 3))

    def draw_cartridge_mockup(self, rect: pygame.Rect, title: str, format_str: str, entry_id: str):
        """Stylized pixel Game Boy Advance cartridge illustration."""
        cx, cy, cw, ch = rect.x, rect.y, rect.width, rect.height

        pygame.draw.rect(self.canvas, (15, 12, 22), (cx + 3, cy + 3, cw, ch), border_radius=6)
        pygame.draw.rect(self.canvas, COLOR_CART_BODY, (cx, cy, cw, ch), border_radius=6)
        pygame.draw.rect(self.canvas, (52, 56, 70), (cx + 8, cy, cw - 16, 7))
        for r in range(4):
            pygame.draw.line(self.canvas, (95, 102, 120),
                             (cx + 12 + r * 6, cy + 10), (cx + 12 + r * 6, cy + 18), 2)

        label_x, label_y, label_w, label_h = cx + 8, cy + 22, cw - 16, ch - 30
        pygame.draw.rect(self.canvas, COLOR_CART_LABEL,
                         (label_x, label_y, label_w, label_h), border_radius=4)
        pygame.draw.rect(self.canvas, COLOR_ACCENT_GOLD,
                         (label_x, label_y, label_w, label_h), width=1, border_radius=4)

        pygame.draw.rect(self.canvas, (92, 45, 115), (label_x, label_y, label_w, 20),
                         border_top_left_radius=4, border_top_right_radius=4)
        fmt_surf = self.font_small.render(f"GAME BOY {format_str}", True, COLOR_TEXT_WHITE)
        self.canvas.blit(fmt_surf, (label_x + 6, label_y + 4))

        title_surf = self.font_header.render(self.truncate(title, 15), True, COLOR_ACCENT_GOLD)
        self.canvas.blit(title_surf, (label_x + 6, label_y + 26))

        pygame.draw.circle(self.canvas, COLOR_ACCENT_GOLD,
                           (label_x + label_w - 18, label_y + label_h - 18), 10, width=1)
        seal_text = self.font_small.render("GBA", True, COLOR_ACCENT_GOLD)
        self.canvas.blit(seal_text, (label_x + label_w - 28, label_y + label_h - 22))

        id_text = self.font_small.render(f"ID:{entry_id[:8]}", True, COLOR_TEXT_MUTED)
        self.canvas.blit(id_text, (label_x + 6, label_y + label_h - 14))

    def draw_app_preview(self, rect: pygame.Rect, item: Dict[str, Any]):
        """Dynamic graphics for the built-in apps and settings pages."""
        handler = item.get("handler", "")
        px, py, pw = rect.x, rect.y, rect.width

        if handler in ("internal_system_monitor",):
            pygame.draw.rect(self.canvas, (45, 36, 70), (px, py, pw, 22),
                             border_top_left_radius=4, border_top_right_radius=4)
            hdr = self.font_small.render("RESOURCE TELEMETRY", True, COLOR_ACCENT_GOLD)
            self.canvas.blit(hdr, (px + 8, py + 5))

            memory = get_memory_usage()
            lbl1 = self.font_small.render(
                f"RAM: {memory['used_mb']}MB / {memory['total_mb']}MB ({memory['percent']}%)",
                True, COLOR_TEXT_WHITE)
            self.canvas.blit(lbl1, (px + 8, py + 30))
            self.draw_bar(pygame.Rect(px + 8, py + 44, pw - 16, 10),
                          memory["percent"] / 100.0, COLOR_ACCENT_CYAN)

            try:
                disk = shutil.disk_usage(get_data_dir())
                lbl2 = self.font_small.render(
                    f"DATA free: {disk.free / (1024 ** 3):.1f} GB", True, COLOR_TEXT_WHITE)
            except Exception:
                lbl2 = self.font_small.render("DATA: ready", True, COLOR_TEXT_WHITE)
            self.canvas.blit(lbl2, (px + 8, py + 60))

            load = get_load_average()[0]
            lbl3 = self.font_small.render(f"Load: {load:.2f}   Up: {format_duration(get_uptime_seconds())}",
                                          True, COLOR_ACCENT_MINT)
            self.canvas.blit(lbl3, (px + 8, py + 78))

        elif handler == "internal_controller_tester":
            pygame.draw.rect(self.canvas, (45, 36, 70), (px, py, pw, 22),
                             border_top_left_radius=4, border_top_right_radius=4)
            hdr = self.font_small.render("CONTROLLER DIAGNOSTIC", True, COLOR_ACCENT_GOLD)
            self.canvas.blit(hdr, (px + 8, py + 5))

            dpad_center_x, dpad_center_y = px + 40, py + 62
            pygame.draw.rect(self.canvas, (60, 65, 80),
                             (dpad_center_x - 6, dpad_center_y - 20, 12, 40), border_radius=2)
            pygame.draw.rect(self.canvas, (60, 65, 80),
                             (dpad_center_x - 20, dpad_center_y - 6, 40, 12), border_radius=2)

            btn_x, btn_y = px + 120, py + 62
            pygame.draw.circle(self.canvas, (180, 50, 70), (btn_x + 18, btn_y - 6), 10)
            pygame.draw.circle(self.canvas, (180, 50, 70), (btn_x - 6, btn_y + 8), 10)
            self.canvas.blit(self.font_small.render("A", True, COLOR_TEXT_WHITE), (btn_x + 14, btn_y - 12))
            self.canvas.blit(self.font_small.render("B", True, COLOR_TEXT_WHITE), (btn_x - 10, btn_y + 2))

            joy_count = len(self.joysticks)
            pad_str = f"Gamepad: {joy_count} detected" if joy_count else "Keyboard mode active"
            pad_lbl = self.font_small.render(
                pad_str, True, COLOR_ACCENT_CYAN if joy_count else COLOR_TEXT_MUTED)
            self.canvas.blit(pad_lbl, (px + 8, py + 90))

        elif handler == "internal_save_manager":
            pygame.draw.rect(self.canvas, (45, 36, 70), (px, py, pw, 22),
                             border_top_left_radius=4, border_top_right_radius=4)
            hdr = self.font_small.render("SAVE STORAGE", True, COLOR_ACCENT_GOLD)
            self.canvas.blit(hdr, (px + 8, py + 5))

            with_saves = [g for g in self.games if self.save_exists(g)]
            total_bytes = 0
            for game in with_saves:
                total_bytes += self.save_summary(game)["bytes"]
            lines = [
                f"Games with saves: {len(with_saves)}",
                f"Total save size: {total_bytes / 1024:.1f} KB",
                "Location: DATA/saves/",
                "A: inspect & manage",
            ]
            y = py + 30
            for i, line in enumerate(lines):
                color = COLOR_TEXT_WHITE if i < 3 else COLOR_ACCENT_CYAN
                self.canvas.blit(self.font_small.render(line, True, color), (px + 8, y))
                y += 17

        else:
            pygame.draw.rect(self.canvas, (45, 36, 70), (px, py, pw, 22),
                             border_top_left_radius=4, border_top_right_radius=4)
            headers = {
                "internal_settings_scale": "DISPLAY OUTPUT",
                "internal_settings_controls": "INPUT PROFILE",
                "internal_rescan_library": "LIBRARY INDEX",
                "internal_reboot": "BOOT CONTROL",
                "internal_poweroff": "POWER CONTROL",
            }
            hdr = self.font_small.render(headers.get(handler, "SYSTEM ACTION"), True, COLOR_ACCENT_GOLD)
            self.canvas.blit(hdr, (px + 8, py + 5))

            if handler == "internal_settings_scale":
                mode = self.display_config.get("scaling", "integer")
                info1 = self.font_small.render("Active mode:", True, COLOR_TEXT_MUTED)
                info2 = self.font_small.render(
                    self.truncate(display_scaling_label(mode), 28), True, COLOR_TEXT_WHITE)
                info3 = self.font_small.render("Saved to DATA/config", True, COLOR_ACCENT_MINT)
            elif handler == "internal_settings_controls":
                info1 = self.font_small.render("One profile for shell +", True, COLOR_TEXT_MUTED)
                info2 = self.font_small.render("mGBA emulator", True, COLOR_TEXT_WHITE)
                info3 = self.font_small.render(
                    f"{len(GBA_BUTTON_ORDER)} buttons mappable", True, COLOR_ACCENT_MINT)
            elif handler == "internal_rescan_library":
                lib = self.library
                info1 = self.font_small.render(f"Scan time: {lib.last_scan_seconds:.2f}s", True, COLOR_TEXT_MUTED)
                info2 = self.font_small.render(f"{len(self.games)} games indexed", True, COLOR_TEXT_WHITE)
                info3 = self.font_small.render("Cache: DATA/library", True, COLOR_ACCENT_MINT)
            elif handler == "internal_reboot":
                info1 = self.font_small.render("Flush saves -> restart", True, COLOR_TEXT_MUTED)
                info2 = self.font_small.render("GRUB menu appears next", True, COLOR_TEXT_WHITE)
                info3 = self.font_small.render("Primary OS stays default", True, COLOR_ACCENT_MINT)
            else:
                info1 = self.font_small.render("Flush saves -> power off", True, COLOR_TEXT_MUTED)
                info2 = self.font_small.render("Safe to unplug afterwards", True, COLOR_TEXT_WHITE)
                info3 = self.font_small.render("Hold power to force off", True, COLOR_ACCENT_GOLD)

            self.canvas.blit(info1, (px + 8, py + 32))
            self.canvas.blit(info2, (px + 8, py + 50))
            self.canvas.blit(info3, (px + 8, py + 74))

    def draw_empty_state(self, list_rect: pygame.Rect) -> None:
        """Explicit, actionable empty state (Rule L6)."""
        scanning = self.scanner is not None
        if scanning:
            head = "Scanning Library..."
            lines = [
                f"Elapsed {time.monotonic() - self.scan_started_at:.1f}s",
                "Large libraries hash on first boot.",
                "Cached results make later boots instant.",
            ]
            head_color = COLOR_ACCENT_CYAN
        elif self.filter_text:
            head = f"No matches for '{self.truncate(self.filter_text, 14)}'"
            lines = ["Press [Esc] to clear the", "search filter."]
            head_color = COLOR_ACCENT_GOLD
        else:
            head = "No Games Found"
            lines = [
                "Copy .gba, .gbc or .gb files",
                "into the games folder:",
                self.truncate(get_games_dir(), 26),
            ]
            head_color = COLOR_TEXT_WHITE

        head_surf = self.font_body.render(head, True, head_color)
        self.canvas.blit(head_surf, (list_rect.x + 20, list_rect.y + 42))
        y = list_rect.y + 70
        for line in lines:
            color = COLOR_ACCENT_GOLD if line.endswith("/") else COLOR_TEXT_MUTED
            self.canvas.blit(self.font_small.render(line, True, color), (list_rect.x + 20, y))
            y += 18

        if not scanning and not self.filter_text:
            hint = self.font_small.render("Then: SETTINGS > Rescan Library", True, COLOR_ACCENT_CYAN)
            self.canvas.blit(hint, (list_rect.x + 20, y + 8))

    def draw_library_view(self):
        """Left: entry list. Right: detail card for the highlighted entry."""
        items = self.get_current_items()
        selected_idx = self.selected_indices[self.current_tab]
        tab = self.current_tab

        # ---- list panel
        self.draw_panel(LIST_RECT)
        header = self.font_small.render(self.tabs[tab], True, COLOR_ACCENT_GOLD)
        self.canvas.blit(header, (LIST_RECT.x + 8, LIST_RECT.y + 6))
        count_text = f"{len(items)} item" + ("" if len(items) == 1 else "s")
        count = self.font_small.render(count_text, True, COLOR_TEXT_MUTED)
        self.canvas.blit(count, (LIST_RECT.right - count.get_width() - 10, LIST_RECT.y + 6))
        pygame.draw.line(self.canvas, COLOR_BORDER,
                         (LIST_RECT.x + 6, LIST_RECT.y + LIST_HEADER_H),
                         (LIST_RECT.right - 6, LIST_RECT.y + LIST_HEADER_H), 1)

        if not items:
            self.draw_empty_state(LIST_RECT)
        else:
            if selected_idx < self.scroll_offsets[tab]:
                self.scroll_offsets[tab] = selected_idx
            elif selected_idx >= self.scroll_offsets[tab] + LIST_VISIBLE_ROWS:
                self.scroll_offsets[tab] = selected_idx - LIST_VISIBLE_ROWS + 1

            start_idx = max(0, self.scroll_offsets[tab])
            end_idx = min(len(items), start_idx + LIST_VISIBLE_ROWS)
            row_top = LIST_RECT.y + LIST_HEADER_H + 4

            for i in range(start_idx, end_idx):
                item = items[i]
                is_selected = (i == selected_idx)
                card_y = row_top + (i - start_idx) * (LIST_ROW_H + LIST_ROW_GAP)
                card_rect = pygame.Rect(LIST_RECT.x + 6, card_y,
                                        LIST_RECT.width - 18, LIST_ROW_H)

                bg_col = COLOR_BG_CARD_SEL if is_selected else COLOR_BG_CARD
                border_col = COLOR_ACCENT_GOLD if is_selected else COLOR_BORDER
                pygame.draw.rect(self.canvas, bg_col, card_rect, border_radius=4)
                pygame.draw.rect(self.canvas, border_col, card_rect, width=1, border_radius=4)

                if is_selected:
                    arrow = self.font_small.render(">", True, COLOR_ACCENT_GOLD)
                    self.canvas.blit(arrow, (card_rect.x + 5, card_rect.y + 13))

                badge_x = card_rect.x + (17 if is_selected else 7)
                pygame.draw.rect(self.canvas, (25, 20, 36),
                                 (badge_x, card_rect.y + 7, 30, 25), border_radius=3)
                fmt = item.get("format", "GBA")
                fmt_surf = self.font_small.render(fmt, True, COLOR_ACCENT_CYAN)
                self.canvas.blit(fmt_surf, (badge_x + (30 - fmt_surf.get_width()) // 2,
                                            card_rect.y + 13))

                text_x = badge_x + 36
                title_surf = self.font_body.render(
                    self.truncate(item.get("title", "Untitled"), 17), True,
                    COLOR_TEXT_WHITE if is_selected else COLOR_TEXT_MUTED)
                self.canvas.blit(title_surf, (text_x, card_rect.y + 6))

                if item.get("size_bytes"):
                    sub_str = f"{item['size_bytes'] / (1024 * 1024):.1f} MB  ·  {item['id'][:8]}"
                else:
                    sub_str = self.truncate(item.get("description", ""), 26)
                meta_surf = self.font_small.render(sub_str, True, COLOR_TEXT_MUTED)
                self.canvas.blit(meta_surf, (text_x, card_rect.y + 22))

                record = self.history.get(item.get("id", ""))
                if record:
                    badge = self.font_small.render("RECENT", True, COLOR_ACCENT_MINT)
                    self.canvas.blit(badge, (card_rect.right - badge.get_width() - 6,
                                             card_rect.y + 22))

            # Scroll indicator makes long libraries obvious.
            if len(items) > LIST_VISIBLE_ROWS:
                track = pygame.Rect(LIST_RECT.right - 8, row_top, 3,
                                    LIST_VISIBLE_ROWS * (LIST_ROW_H + LIST_ROW_GAP) - LIST_ROW_GAP)
                pygame.draw.rect(self.canvas, (25, 20, 36), track, border_radius=2)
                thumb_h = max(12, int(track.height * LIST_VISIBLE_ROWS / len(items)))
                max_offset = max(1, len(items) - LIST_VISIBLE_ROWS)
                thumb_y = track.y + int((track.height - thumb_h) * start_idx / max_offset)
                pygame.draw.rect(self.canvas, COLOR_ACCENT_GOLD,
                                 pygame.Rect(track.x, thumb_y, 3, thumb_h), border_radius=2)

        # ---- detail panel
        self.draw_panel(DETAIL_RECT)
        cur = self.current_item()
        if not cur:
            hint = self.font_small.render("Nothing selected", True, COLOR_TEXT_MUTED)
            self.canvas.blit(hint, (DETAIL_RECT.x + 12, DETAIL_RECT.y + 20))
            return

        if tab == 0:
            self.draw_cartridge_mockup(
                pygame.Rect(DETAIL_RECT.x + 16, DETAIL_RECT.y + 12, 170, 110),
                cur.get("title", "GBA Game"), cur.get("format", "GBA"), cur.get("id", "--------"))
        else:
            self.draw_app_preview(
                pygame.Rect(DETAIL_RECT.x + 12, DETAIL_RECT.y + 10, 178, 112), cur)

        meta_y = DETAIL_RECT.y + 130
        title_surf = self.font_header.render(
            self.truncate(cur.get("title", ""), 19), True, COLOR_TEXT_WHITE)
        self.canvas.blit(title_surf, (DETAIL_RECT.x + 12, meta_y))

        type_lbl = self.font_small.render(
            f"Type: {cur.get('type', 'game').upper()} ({cur.get('format', 'SYS')})",
            True, COLOR_TEXT_MUTED)
        self.canvas.blit(type_lbl, (DETAIL_RECT.x + 12, meta_y + 18))

        y = meta_y + 32
        if cur.get("type") == "game":
            has_save = self.save_exists(cur)
            if has_save:
                summary = self.save_summary(cur)
                save_text = f"Save: {summary['bytes'] / 1024:.1f} KB"
                save_color = COLOR_ACCENT_MINT
            else:
                save_text = "Save: none yet"
                save_color = COLOR_TEXT_MUTED
            self.canvas.blit(self.font_small.render(save_text, True, save_color),
                             (DETAIL_RECT.x + 12, y))
            y += 15

            record = self.history.get(cur.get("id", ""))
            if record:
                played = record.get("last_played", 0)
                ago = max(0.0, time.time() - played)
                play_text = f"Played {format_duration(ago)} ago  ·  {record.get('sessions', 1)}x"
                self.canvas.blit(self.font_small.render(play_text, True, COLOR_ACCENT_CYAN),
                                 (DETAIL_RECT.x + 12, y))
                y += 15
        else:
            builtin = self.font_small.render("Built-in component", True, COLOR_ACCENT_CYAN)
            self.canvas.blit(builtin, (DETAIL_RECT.x + 12, y))
            y += 15

        handler = cur.get("handler", "")
        if cur.get("type") == "game":
            action, color = "[A] LAUNCH", COLOR_ACCENT_GOLD
        elif "reboot" in handler:
            action, color = "[A] REBOOT", COLOR_ACCENT_CYAN
        elif "poweroff" in handler:
            action, color = "[A] POWER OFF", COLOR_ACCENT_RED
        else:
            action, color = "[A] SELECT", COLOR_ACCENT_GOLD

        btn_rect = pygame.Rect(DETAIL_RECT.x + 12, DETAIL_RECT.bottom - 34, 110, 24)
        self.draw_button(btn_rect, action, color)

    def draw_bottom_bar(self):
        """Hints, outcome banner and transient toasts."""
        pygame.draw.rect(self.canvas, (20, 16, 32), (0, BOTTOM_BAR_Y, CANVAS_WIDTH,
                                                     CANVAS_HEIGHT - BOTTOM_BAR_Y))
        pygame.draw.line(self.canvas, COLOR_BORDER, (0, BOTTOM_BAR_Y),
                         (CANVAS_WIDTH, BOTTOM_BAR_Y), 1)

        hint_y = BOTTOM_BAR_Y + 10
        if self.last_outcome and self.last_outcome not in ("clean", "unknown"):
            alert_text = f"Previous session: {self.last_outcome}"
            self.canvas.blit(self.font_small.render(alert_text, True, COLOR_ACCENT_GOLD), (12, 290))
            hint_y = BOTTOM_BAR_Y + 18
            self.canvas.blit(self.font_small.render(
                "Session recovered - save data was synced.", True, COLOR_TEXT_MUTED), (12, 302))
            return

        if self.filter_text and self.current_tab == 0:
            hints = [
                ("[Esc]", "Clear search", COLOR_ACCENT_CYAN),
                ("[Enter]", "Launch", COLOR_ACCENT_GOLD),
                ("[Arrows]", "Navigate", COLOR_TEXT_MUTED),
                ("[Tab]", "Category", COLOR_ACCENT_CYAN),
            ]
        else:
            hints = [
                ("(A)/[Enter]", "Select", COLOR_ACCENT_GOLD),
                ("(B)/[Esc]", "Back", COLOR_TEXT_MUTED),
                ("(D-Pad)", "Navigate", COLOR_TEXT_MUTED),
                ("[Tab]", "Category", COLOR_ACCENT_CYAN),
                ("[type]", "Search", COLOR_TEXT_MUTED),
            ]

        hx = 12
        for key, desc, color in hints:
            key_surf = self.font_small.render(key, True, color)
            if hx + key_surf.get_width() > CANVAS_WIDTH - 8:
                break
            self.canvas.blit(key_surf, (hx, hint_y))
            hx += key_surf.get_width() + 3
            desc_surf = self.font_small.render(desc, True, COLOR_TEXT_MUTED)
            if hx + desc_surf.get_width() > CANVAS_WIDTH - 8:
                break
            self.canvas.blit(desc_surf, (hx, hint_y))
            hx += desc_surf.get_width() + 12

        if self.toast is not None:
            text_surf = self.font_small.render(self.toast.text, True, self.toast.color)
            bubble = pygame.Rect(0, 0, text_surf.get_width() + 16, 18)
            bubble.centerx = CANVAS_WIDTH // 2
            bubble.bottom = BOTTOM_BAR_Y - 6
            pygame.draw.rect(self.canvas, (18, 14, 30), bubble, border_radius=9)
            pygame.draw.rect(self.canvas, self.toast.color, bubble, width=1, border_radius=9)
            self.canvas.blit(text_surf, (bubble.x + 8, bubble.y + 3))

    def _draw_scan_banner(self):
        if self.scanner is None:
            return
        pulse = 0.5 + 0.5 * math.sin(time.monotonic() * 4)
        text = self.font_small.render("Scanning library...", True, COLOR_ACCENT_CYAN)
        rect = pygame.Rect(0, 0, text.get_width() + 16, 18)
        rect.right = DETAIL_RECT.right - 6
        rect.bottom = BOTTOM_BAR_Y - 6
        pygame.draw.rect(self.canvas, (18, 14, 30), rect, border_radius=9)
        pygame.draw.rect(self.canvas, (60 * pulse + 40,) * 3, rect, width=1, border_radius=9)
        self.canvas.blit(text, (rect.x + 8, rect.y + 3))

    def render_base(self):
        self.canvas.fill(COLOR_BG_DARK)
        self.draw_top_bar()
        self.draw_library_view()
        self.draw_bottom_bar()

    def present(self):
        """Scales the canvas to the display with integer factors (D1, D2)."""
        if self.screenshot_path:
            return
        scale = min(self.screen_w // CANVAS_WIDTH, self.screen_h // CANVAS_HEIGHT)
        scale = max(1, scale)
        scaled_w, scaled_h = CANVAS_WIDTH * scale, CANVAS_HEIGHT * scale
        offset_x = (self.screen_w - scaled_w) // 2
        offset_y = (self.screen_h - scaled_h) // 2
        if scale != 1:
            scaled_canvas = pygame.transform.scale(self.canvas, (scaled_w, scaled_h))
        else:
            scaled_canvas = self.canvas
        self.window.fill((0, 0, 0))
        self.window.blit(scaled_canvas, (offset_x, offset_y))
        pygame.display.flip()

    def render(self):
        self.render_base()
        self._draw_scan_banner()
        self.present()

    def screen_to_canvas(self, mx: int, my: int) -> Tuple[int, int]:
        scale = max(1, min(self.screen_w // CANVAS_WIDTH, self.screen_h // CANVAS_HEIGHT))
        offset_x = (self.screen_w - CANVAS_WIDTH * scale) // 2
        offset_y = (self.screen_h - CANVAS_HEIGHT * scale) // 2
        return (mx - offset_x) // scale, (my - offset_y) // scale

    # ---------------------------------------------------------------- input
    def navigate(self, delta: int) -> None:
        items = self.get_current_items()
        if not items:
            return
        idx = self.selected_indices[self.current_tab]
        self.selected_indices[self.current_tab] = max(0, min(len(items) - 1, idx + delta))

    def switch_tab(self, delta: int) -> None:
        self.current_tab = (self.current_tab + delta) % len(self.tabs)
        self._clamp_selection()

    def _clear_filter(self) -> bool:
        if self.filter_text:
            self.filter_text = ""
            self.selected_indices[0] = 0
            self.scroll_offsets[0] = 0
            return True
        return False

    def _handle_printable(self, key: int) -> bool:
        """Incremental search; only alphanumerics and '-'/'_' participate."""
        if self.current_tab != 0:
            return False
        try:
            char = pygame.key.name(key)
        except Exception:
            return False
        if len(char) == 1 and (char.isalnum() or char in "-_."):
            self.filter_text += char.lower()
            self.selected_indices[0] = 0
            self.scroll_offsets[0] = 0
            return True
        return False

    def _poll_key_repeat(self) -> None:
        """Repeats held navigation keys; typing is never affected."""
        if not self.held_keys:
            return
        now = time.monotonic()
        for key, next_time in list(self.held_keys.items()):
            if now < next_time:
                continue
            self.held_keys[key] = now + KEY_REPEAT_DELAY
            if key == pygame.K_UP:
                self.navigate(-1)
            elif key == pygame.K_DOWN:
                self.navigate(1)
            elif key == pygame.K_LEFT:
                self.switch_tab(-1)
            elif key == pygame.K_RIGHT:
                self.switch_tab(1)
            elif key == pygame.K_PAGEUP:
                self.navigate(-LIST_VISIBLE_ROWS)
            elif key == pygame.K_PAGEDOWN:
                self.navigate(LIST_VISIBLE_ROWS)

    def _poll_analog_nav(self) -> None:
        """Held stick moves the selection with an initial delay then fast repeat (I1)."""
        if not self.joysticks or self.current_tab < 0:
            return
        try:
            joy = self.joysticks[0]
            x_axis = joy.get_axis(0) if joy.get_numaxes() > 0 else 0.0
            y_axis = joy.get_axis(1) if joy.get_numaxes() > 1 else 0.0
        except Exception:
            return

        now = time.monotonic()
        for axis, value in (("y", y_axis), ("x", x_axis)):
            direction = 0
            if value <= -AXIS_DEADZONE:
                direction = -1
            elif value >= AXIS_DEADZONE:
                direction = 1
            if direction == 0:
                self.axis_gate[axis] = 0.0
                self.axis_dir[axis] = 0
                continue
            if self.axis_dir[axis] != direction:
                self.axis_dir[axis] = direction
                self.axis_gate[axis] = now + AXIS_INITIAL_DELAY
                if axis == "y":
                    self.navigate(direction)
                else:
                    self.switch_tab(direction)
            elif now >= self.axis_gate[axis]:
                self.axis_gate[axis] = now + AXIS_REPEAT_DELAY
                if axis == "y":
                    self.navigate(direction)
                else:
                    self.switch_tab(direction)

    def handle_input(self):
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                logger.info("Window close event received.")
                if not self.appliance_mode:
                    self.running = False
                else:
                    logger.info("Appliance mode active: ignoring window close event.")

            elif event.type == pygame.JOYDEVICEADDED:
                self._refresh_joysticks()
                self.toast_message(f"Gamepad connected ({len(self.joysticks)} total)")

            elif event.type == pygame.JOYDEVICEREMOVED:
                self._refresh_joysticks()
                self.toast_message("Gamepad disconnected", COLOR_ACCENT_GOLD)

            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                self._handle_click(*event.pos)

            elif event.type == pygame.KEYDOWN:
                if event.key in REPEATABLE_KEYS:
                    # First press acts immediately; repeats come from update().
                    self.held_keys.setdefault(
                        event.key, time.monotonic() + KEY_REPEAT_INITIAL_DELAY)
                self._handle_key(event.key)

            elif event.type == pygame.KEYUP:
                self.held_keys.pop(event.key, None)

            elif event.type == pygame.JOYHATMOTION:
                hat_x, hat_y = event.value
                if hat_y == 1:
                    self.navigate(-1)
                elif hat_y == -1:
                    self.navigate(1)
                if hat_x == 1:
                    self.switch_tab(1)
                elif hat_x == -1:
                    self.switch_tab(-1)

            elif event.type == pygame.JOYBUTTONDOWN:
                if event.button == 0:
                    self.trigger_action()
                elif event.button == 1:
                    self._back()
                elif event.button in (7, 9):  # START
                    self.switch_tab(1)
                elif event.button in (6, 8):  # SELECT
                    self.toast_message("SELECT: search is keyboard-only")

        self._poll_analog_nav()

    def _back(self) -> None:
        if self._clear_filter():
            return
        if self.current_tab != 0:
            self.current_tab = 0
            self._clamp_selection()
            return
        if not self.appliance_mode:
            logger.info("User requested exit from launcher.")
            self.running = False
        else:
            self.toast_message("Already on the games tab", COLOR_TEXT_MUTED)

    def _handle_key(self, key: int) -> None:
        if key == pygame.K_TAB:
            self.switch_tab(1)
        elif key == pygame.K_RIGHT:
            self.switch_tab(1)
        elif key == pygame.K_LEFT:
            self.switch_tab(-1)
        elif key == pygame.K_UP:
            self.navigate(-1)
        elif key == pygame.K_DOWN:
            self.navigate(1)
        elif key == pygame.K_PAGEUP:
            self.navigate(-LIST_VISIBLE_ROWS)
        elif key == pygame.K_PAGEDOWN:
            self.navigate(LIST_VISIBLE_ROWS)
        elif key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_z, pygame.K_SPACE):
            self.trigger_action()
        elif key == pygame.K_ESCAPE:
            self._back()
        elif key == pygame.K_BACKSPACE:
            if self.filter_text:
                self.filter_text = self.filter_text[:-1]
                self.selected_indices[0] = 0
                self.scroll_offsets[0] = 0
            else:
                self._back()
        elif self._handle_printable(key):
            pass

    def _handle_click(self, mx: int, my: int) -> None:
        cx, cy = self.screen_to_canvas(mx, my)

        for i, rect in enumerate(self.tab_rects):
            if rect.collidepoint(cx, cy):
                self.current_tab = i
                self._clamp_selection()
                logger.info("Mouse switched to tab %d (%s)", i, self.tabs[i])
                return

        if LIST_RECT.collidepoint(cx, cy):
            row_top = LIST_RECT.y + LIST_HEADER_H + 4
            if cy >= row_top - 4:
                clicked = self.scroll_offsets[self.current_tab] + (cy - row_top) // (LIST_ROW_H + LIST_ROW_GAP)
                items = self.get_current_items()
                if 0 <= clicked < len(items):
                    if self.selected_indices[self.current_tab] == clicked:
                        self.trigger_action()
                    else:
                        self.selected_indices[self.current_tab] = clicked
                        logger.info("Mouse selected item %d (%s)", clicked,
                                    items[clicked].get("title"))
            return

        if DETAIL_RECT.collidepoint(cx, cy) and cy >= DETAIL_RECT.bottom - 40:
            self.trigger_action()

    # --------------------------------------------------------------- actions
    def trigger_action(self):
        item = self.current_item()
        if not item:
            return

        handler = item.get("handler", "")
        logger.info("Trigger action: tab=%d item=%s handler=%s",
                    self.current_tab, item.get("title"), handler)

        if item.get("type") == "game":
            if handler == "demo-preview":
                self.toast_message("Demo preview - no ROM installed", COLOR_ACCENT_GOLD)
                return
            if not self.show_launch_confirmation(item):
                logger.info("Game launch aborted by user.")
                return

            run_dir = get_run_dir()
            req_path = os.path.join(run_dir, "launch_request.json")
            req_data = {
                "entry_id": item["id"],
                "content_path": item["content_path"],
                "handler": handler,
                "title": item["title"],
            }
            logger.info("Writing launch request to %s: %s", req_path, req_data)
            try:
                tmp = req_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(req_data, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, req_path)
            except Exception as exc:
                logger.error("Could not write launch request: %s", exc)
                self.toast_message("Launch failed: run dir not writable", COLOR_ACCENT_RED)
                return
            record_play_session(item["id"])
            self.running = False
            sys.exit(EXIT_CODE_LAUNCH_REQUEST)

        elif handler == "internal_reboot":
            logger.info("User requested system reboot.")
            sys.exit(EXIT_CODE_REBOOT)

        elif handler == "internal_poweroff":
            logger.info("User requested system poweroff.")
            sys.exit(EXIT_CODE_POWEROFF)

        elif handler == "internal_system_monitor":
            self.show_system_monitor()

        elif handler == "internal_controller_tester":
            self.show_controller_tester()

        elif handler == "internal_settings_controls":
            self.show_controls_config()

        elif handler == "internal_settings_scale":
            self.cycle_display_scaling()

        elif handler == "internal_save_manager":
            self.show_save_manager()

        elif handler == "internal_rescan_library":
            self.rescan_library()

    def rescan_library(self) -> None:
        self.library = Library()
        self.scanner = self.library.start_scan()
        self.scan_started_at = time.monotonic()
        self.filter_text = ""
        self.toast_message("Rescanning games folder...")

    def cycle_display_scaling(self) -> None:
        modes = list(DISPLAY_SCALING_MODES)
        current = self.display_config.get("scaling", "integer")
        nxt = modes[(modes.index(current) + 1) % len(modes)] if current in modes else modes[0]
        self.display_config["scaling"] = nxt
        save_display_config(self.display_config)
        label = display_scaling_label(nxt)
        logger.info("Switched display scaling mode to: %s", label)
        self.toast_message(f"Scaling: {label}", COLOR_ACCENT_MINT)

    # ---------------------------------------------------------------- modals
    def show_launch_confirmation(self, item: Dict[str, Any]) -> bool:
        """Pre-flight modal: controls reference and where saves will live."""
        if self.screenshot_path:
            return True

        logger.info("Showing launch confirmation for: %s", item.get("title"))
        summary = self.save_summary(item)
        record = self.history.get(item.get("id", ""), {})
        result = False

        while True:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    if not self.appliance_mode:
                        return False
                elif event.type == pygame.KEYDOWN:
                    if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER,
                                     pygame.K_SPACE, pygame.K_z):
                        result = True
                        return result
                    if event.key in (pygame.K_ESCAPE, pygame.K_BACKSPACE, pygame.K_x):
                        logger.info("User cancelled launch via key.")
                        return False
                elif event.type == pygame.JOYBUTTONDOWN:
                    if event.button == 0:
                        return True
                    if event.button == 1:
                        return False
                elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    cx, cy = self.screen_to_canvas(*event.pos)
                    if 62 <= cx <= 232 and 262 <= cy <= 288:
                        return True
                    if 250 <= cx <= 420 and 262 <= cy <= 288:
                        return False

            modal = pygame.Rect(35, 16, 410, 288)
            content_y = self.draw_modal_shell(
                modal, "READY TO PLAY", COLOR_ACCENT_GOLD,
                subtitle=f"{item.get('title', 'GBA Game')}  ·  {item.get('format', 'GBA')}")

            # Save info box
            info = pygame.Rect(modal.x + 20, content_y, modal.width - 40, 46)
            pygame.draw.rect(self.canvas, (18, 14, 30), info, border_radius=6)
            pygame.draw.rect(self.canvas, (50, 40, 75), info, 1, border_radius=6)
            if summary["files"]:
                status = f"{summary['files']} save file(s), {summary['bytes'] / 1024:.1f} KB"
                status_color = COLOR_ACCENT_MINT
            else:
                status = "New game - a save is created on first write"
                status_color = COLOR_TEXT_MUTED
            self.canvas.blit(self.font_small.render("SAVE DATA", True, COLOR_ACCENT_GOLD),
                             (info.x + 10, info.y + 6))
            self.canvas.blit(self.font_small.render(status, True, status_color),
                             (info.x + 10, info.y + 22))
            if record.get("sessions"):
                played = self.font_small.render(
                    f"Played {record['sessions']}x  ·  {format_duration(record.get('seconds', 0))} total",
                    True, COLOR_ACCENT_CYAN)
                self.canvas.blit(played, (info.right - played.get_width() - 10, info.y + 6))

            # Controls quick reference
            ctrl = pygame.Rect(modal.x + 20, info.bottom + 8, modal.width - 40, 132)
            pygame.draw.rect(self.canvas, (18, 14, 30), ctrl, border_radius=6)
            pygame.draw.rect(self.canvas, (50, 40, 75), ctrl, 1, border_radius=6)
            self.canvas.blit(self.font_small.render("CONTROLS QUICK REFERENCE", True,
                                                    COLOR_ACCENT_GOLD),
                             (ctrl.x + 12, ctrl.y + 7))

            k_start = self.format_key_name(self.keybindings.get("keyStart", 13))
            k_a = self.format_key_name(self.keybindings.get("keyA", 120))
            k_b = self.format_key_name(self.keybindings.get("keyB", 122))
            k_l = self.format_key_name(self.keybindings.get("keyL", 97))
            k_r = self.format_key_name(self.keybindings.get("keyR", 115))

            controls = [
                (f"[{k_start}]", "START - open the in-game menu", COLOR_ACCENT_CYAN),
                ("[Arrows / D-Pad]", "D-Pad movement", COLOR_TEXT_WHITE),
                (f"[{k_a}]", "(A) Action / confirm", COLOR_TEXT_WHITE),
                (f"[{k_b}]", "(B) Cancel / run", COLOR_TEXT_WHITE),
                (f"[{k_l}] / [{k_r}]", "(L) / (R) shoulder buttons", COLOR_TEXT_WHITE),
                ("[Esc]", "Quit game -> back to this library", COLOR_ACCENT_RED),
            ]
            row_y = ctrl.y + 24
            for key_lbl, desc_lbl, color in controls:
                self.canvas.blit(self.font_small.render(key_lbl, True, color), (ctrl.x + 12, row_y))
                self.canvas.blit(self.font_small.render(desc_lbl, True, color), (ctrl.x + 118, row_y))
                row_y += 17

            self.draw_button(pygame.Rect(modal.x + 27, 262, 170, 26),
                             "[A] / [Enter] START", COLOR_ACCENT_GOLD)
            self.draw_button(pygame.Rect(modal.right - 197, 262, 170, 26),
                             "[B] / [Esc] CANCEL", (110, 100, 140), filled=False,
                             text_color=COLOR_TEXT_MUTED)

            note = self.font_small.render(
                "Saves are stored per game in DATA/saves - renames never orphan them.",
                True, COLOR_TEXT_MUTED)
            self.canvas.blit(note, (modal.centerx - note.get_width() // 2, modal.bottom - 12))

            self.present()
            self.clock.tick(30)

        return result

    def format_key_name(self, keycode: int) -> str:
        """Clean uppercase human-readable name for an SDL2/pygame keycode."""
        special = {
            13: "RETURN",
            8: "BACKSPACE",
            32: "SPACE",
            9: "TAB",
            27: "ESCAPE",
            1073741906: "UP",
            1073741905: "DOWN",
            1073741904: "LEFT",
            1073741903: "RIGHT",
            1073742048: "L-CTRL",
            1073742049: "L-SHIFT",
            1073742050: "L-ALT",
            1073742052: "R-CTRL",
            1073742053: "R-SHIFT",
            1073742054: "R-ALT",
        }
        if keycode in special:
            return special[keycode]
        try:
            name = pygame.key.name(keycode).upper()
            return name if len(name) > 1 else f"{name} KEY"
        except Exception:
            return f"KEY {keycode}"

    def show_controls_config(self):
        """Interactive remap modal with validation against reserved/duplicate keys."""
        logger.info("Opening keyboard controls configuration modal...")
        sel_idx = 0
        listening = False
        error_msg: Optional[str] = None

        while True:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    if not self.appliance_mode:
                        return

                elif event.type == pygame.KEYDOWN:
                    if listening:
                        if event.key == pygame.K_ESCAPE:
                            listening = False
                            error_msg = None
                        else:
                            btn_id, btn_label = GBA_BUTTON_ORDER[sel_idx]
                            reason = keybindings_conflict_error(
                                self.keybindings, btn_id, event.key)
                            if reason:
                                error_msg = f"{reason} Try again."
                                continue
                            self.keybindings[btn_id] = event.key
                            self.keybindings = sanitize_keybindings(self.keybindings)
                            save_keybindings(self.keybindings)
                            logger.info("Mapped %s -> %s", btn_id, pygame.key.name(event.key))
                            error_msg = None
                            listening = False
                    else:
                        if event.key == pygame.K_UP:
                            sel_idx = (sel_idx - 1) % 10
                        elif event.key == pygame.K_DOWN:
                            sel_idx = (sel_idx + 1) % 10
                        elif event.key in (pygame.K_LEFT, pygame.K_RIGHT):
                            sel_idx = (sel_idx + 5) % 10
                        elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
                            listening = True
                            error_msg = None
                        elif event.key == pygame.K_r:
                            self.keybindings = dict(DEFAULT_KEYBINDINGS)
                            save_keybindings(self.keybindings)
                            error_msg = None
                            logger.info("Reset keybindings to defaults.")
                        elif event.key == pygame.K_ESCAPE:
                            return

                elif event.type == pygame.JOYBUTTONDOWN and not listening:
                    if event.button == 0:
                        listening = True
                        error_msg = None
                    elif event.button == 1:
                        return
                elif event.type == pygame.JOYHATMOTION and not listening:
                    hat_x, hat_y = event.value
                    if hat_y == 1:
                        sel_idx = (sel_idx - 1) % 10
                    elif hat_y == -1:
                        sel_idx = (sel_idx + 1) % 10
                    if hat_x:
                        sel_idx = (sel_idx + 5) % 10

                elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    cx, cy = self.screen_to_canvas(*event.pos)
                    clicked = None
                    if 40 <= cx <= 235 and 74 <= cy <= 244:
                        row = (cy - 74) // 34
                        if 0 <= row <= 4:
                            clicked = row
                    elif 245 <= cx <= 440 and 74 <= cy <= 244:
                        row = (cy - 74) // 34
                        if 0 <= row <= 4:
                            clicked = row + 5
                    if clicked is not None:
                        if sel_idx == clicked:
                            listening = not listening
                        else:
                            sel_idx = clicked
                            listening = False
                        error_msg = None
                    elif 40 <= cx <= 220 and 252 <= cy <= 280:
                        self.keybindings = dict(DEFAULT_KEYBINDINGS)
                        save_keybindings(self.keybindings)
                        error_msg = None
                        logger.info("Reset keybindings to defaults via click.")
                    elif 260 <= cx <= 440 and 252 <= cy <= 280:
                        return

            modal = pygame.Rect(25, 16, 430, 288)
            subtitle = (
                f"PRESS A KEY FOR {GBA_BUTTON_ORDER[sel_idx][1]} (ESC TO CANCEL)"
                if listening else "Arrows/Mouse select - [Enter] remap - [R] defaults"
            )
            self.draw_modal_shell(modal, "KEYBOARD CONTROLS", COLOR_ACCENT_GOLD,
                                  subtitle, COLOR_ACCENT_GOLD if listening else COLOR_TEXT_MUTED)

            for slot, (btn_id, btn_label) in enumerate(GBA_BUTTON_ORDER):
                column = slot // 5
                row = slot % 5
                is_selected = (sel_idx == slot)
                rect = pygame.Rect(40 + column * 205, 74 + row * 34, 195, 30)
                bg = (60, 50, 95) if is_selected else (26, 21, 40)
                border_col = COLOR_ACCENT_GOLD if is_selected else COLOR_BORDER
                pygame.draw.rect(self.canvas, bg, rect, border_radius=5)
                pygame.draw.rect(self.canvas, border_col, rect, 1, border_radius=5)

                if is_selected and listening:
                    value_str, value_col = "PRESS KEY...", COLOR_ACCENT_GOLD
                else:
                    code = self.keybindings.get(btn_id, DEFAULT_KEYBINDINGS[btn_id])
                    value_str = self.format_key_name(code)
                    value_col = COLOR_ACCENT_GOLD if is_selected else COLOR_TEXT_MUTED

                label_surf = self.font_small.render(
                    btn_label, True, COLOR_ACCENT_CYAN if is_selected else COLOR_TEXT_WHITE)
                value_surf = self.font_small.render(value_str, True, value_col)
                self.canvas.blit(label_surf, (rect.x + 10, rect.centery - label_surf.get_height() // 2))
                self.canvas.blit(value_surf, (rect.right - value_surf.get_width() - 10,
                                              rect.centery - value_surf.get_height() // 2))

            if error_msg:
                err = self.font_small.render(self.truncate(error_msg, 62), True, COLOR_ACCENT_RED)
                self.canvas.blit(err, (modal.centerx - err.get_width() // 2, 242))

            self.draw_button(pygame.Rect(40, 258, 180, 28), "[R] RESET DEFAULTS",
                             (110, 100, 140), filled=False, text_color=COLOR_TEXT_MUTED)
            self.draw_button(pygame.Rect(260, 258, 180, 28), "[Esc] SAVE & RETURN",
                             COLOR_ACCENT_GOLD)

            self.present()
            self.clock.tick(30)

    def show_system_monitor(self):
        """Live appliance telemetry (refreshes while open)."""
        logger.info("Opening system monitor modal...")
        while True:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    if not self.appliance_mode:
                        return
                elif event.type == pygame.KEYDOWN and event.key in (
                        pygame.K_ESCAPE, pygame.K_x, pygame.K_b, pygame.K_RETURN,
                        pygame.K_SPACE, pygame.K_BACKSPACE):
                    return
                elif event.type in (pygame.MOUSEBUTTONDOWN, pygame.JOYBUTTONDOWN):
                    return

            modal = pygame.Rect(35, 20, 410, 280)
            self.draw_modal_shell(modal, "SYSTEM DIAGNOSTICS", COLOR_ACCENT_GOLD,
                                  "Esc / B / click to return")

            memory = get_memory_usage()
            y = modal.y + 54
            self.canvas.blit(self.font_small.render(
                f"MEMORY  {memory['used_mb']} MB / {memory['total_mb']} MB", True, COLOR_TEXT_WHITE),
                (modal.x + 24, y))
            self.canvas.blit(self.font_small.render(f"{memory['percent']}%", True, COLOR_ACCENT_CYAN),
                             (modal.right - 60, y))
            self.draw_bar(pygame.Rect(modal.x + 24, y + 15, modal.width - 48, 8),
                          memory["percent"] / 100.0,
                          COLOR_ACCENT_MINT if memory["percent"] < 75 else COLOR_ACCENT_RED)

            y += 34
            try:
                disk = shutil.disk_usage(get_data_dir())
                used_frac = disk.used / disk.total if disk.total else 0
                disk_text = (f"STORAGE  {disk.free / (1024 ** 3):.1f} GB free "
                             f"of {disk.total / (1024 ** 3):.1f} GB")
            except Exception:
                used_frac, disk_text = 0.0, "STORAGE  unavailable"
            self.canvas.blit(self.font_small.render(disk_text, True, COLOR_TEXT_WHITE),
                             (modal.x + 24, y))
            self.draw_bar(pygame.Rect(modal.x + 24, y + 15, modal.width - 48, 8),
                          used_frac, COLOR_ACCENT_CYAN)

            y += 34
            load1, load5, load15 = get_load_average()
            rows = [
                ("CPU load", f"{load1:.2f}  {load5:.2f}  {load15:.2f}"),
                ("Uptime", format_duration(get_uptime_seconds())),
                ("Display", f"{self.screen_w}x{self.screen_h} ({pygame.display.get_driver()})"),
                ("Logical", f"{CANVAS_WIDTH}x{CANVAS_HEIGHT} integer-scaled"),
                ("ROM library", f"{len(self.games)} titles in {get_games_dir()}"),
                ("Gamepads", f"{len(self.joysticks)} connected"),
                ("Contract", f"version {SYS_CONTRACT_VERSION}"),
            ]
            for label, value in rows:
                y = self.draw_kv(modal.x + 24, y, label, self.truncate(value, 44))

            self.draw_button(pygame.Rect(modal.centerx - 70, modal.bottom - 34, 140, 24),
                             "[Esc] RETURN", COLOR_ACCENT_GOLD)
            self.present()
            self.clock.tick(15)

    def show_controller_tester(self):
        """Input diagnostics: pads, axes, hats and mapped keys (I2, I3, I4)."""
        logger.info("Opening input diagnostics modal...")
        last_input = "waiting for input..."
        pressed = set()
        hat_value = (0, 0)
        axes = [0.0, 0.0]

        while True:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    if not self.appliance_mode:
                        return
                elif event.type == pygame.KEYDOWN:
                    if event.key in (pygame.K_ESCAPE, pygame.K_BACKSPACE):
                        return
                    last_input = f"key {pygame.key.name(event.key).upper()}"
                elif event.type == pygame.JOYDEVICEADDED:
                    self._refresh_joysticks()
                    last_input = "gamepad connected"
                elif event.type == pygame.JOYDEVICEREMOVED:
                    self._refresh_joysticks()
                    last_input = "gamepad removed"
                elif event.type == pygame.JOYBUTTONDOWN:
                    pressed.add(event.button)
                    last_input = f"gamepad button #{event.button}"
                elif event.type == pygame.JOYBUTTONUP:
                    pressed.discard(event.button)
                elif event.type == pygame.JOYHATMOTION:
                    hat_value = event.value
                    last_input = f"d-pad {event.value}"
                elif event.type == pygame.JOYAXISMOTION:
                    if event.axis < 2:
                        axes[event.axis] = event.value
                    last_input = f"axis {event.axis} = {event.value:+.2f}"
                elif event.type == pygame.MOUSEBUTTONDOWN:
                    return

            modal = pygame.Rect(35, 14, 410, 292)
            self.draw_modal_shell(modal, "INPUT DIAGNOSTICS", COLOR_ACCENT_CYAN,
                                  "Press buttons - Esc to return")

            # Controller visual
            vis = pygame.Rect(modal.x + 20, modal.y + 52, 180, 104)
            pygame.draw.rect(self.canvas, (26, 21, 40), vis, border_radius=10)
            pygame.draw.rect(self.canvas, COLOR_BORDER, vis, 1, border_radius=10)

            cx, cy = vis.centerx - 10, vis.y + 44
            # D-pad lights up with the hat / keyboard arrows
            lit = {
                "up": hat_value[1] == 1, "down": hat_value[1] == -1,
                "left": hat_value[0] == -1, "right": hat_value[0] == 1,
            }
            for name, rect in {
                "up": pygame.Rect(cx - 6, cy - 26, 12, 20),
                "down": pygame.Rect(cx - 6, cy + 6, 12, 20),
                "left": pygame.Rect(cx - 26, cy - 6, 20, 12),
                "right": pygame.Rect(cx + 6, cy - 6, 20, 12),
            }.items():
                pygame.draw.rect(self.canvas, COLOR_ACCENT_GOLD if lit[name] else (60, 65, 80),
                                 rect, border_radius=2)

            a_on = 0 in pressed
            b_on = 1 in pressed
            pygame.draw.circle(self.canvas, COLOR_ACCENT_RED if a_on else (120, 50, 70),
                               (vis.right - 34, cy - 14), 11)
            pygame.draw.circle(self.canvas, COLOR_ACCENT_RED if b_on else (120, 50, 70),
                               (vis.right - 58, cy + 8), 11)
            self.canvas.blit(self.font_small.render("A", True, COLOR_TEXT_WHITE),
                             (vis.right - 38, cy - 20))
            self.canvas.blit(self.font_small.render("B", True, COLOR_TEXT_WHITE),
                             (vis.right - 62, cy + 2))

            for idx, axis_fraction in enumerate(axes):
                bar = pygame.Rect(vis.x + 12, vis.bottom - 26 + idx * 10, vis.width - 24, 5)
                self.draw_bar(bar, (axis_fraction + 1) / 2, COLOR_ACCENT_CYAN)
            self.canvas.blit(self.font_small.render("STICK", True, COLOR_TEXT_MUTED),
                             (vis.x + 8, vis.bottom - 40))

            # Details
            info_x = vis.right + 16
            self.canvas.blit(self.font_small.render(
                f"Gamepads: {len(self.joysticks)}", True, COLOR_TEXT_WHITE), (info_x, modal.y + 52))
            name = "Keyboard only"
            if self.joysticks:
                try:
                    name = self.truncate(self.joysticks[0].get_name(), 24)
                except Exception:
                    name = "Gamepad"
            self.canvas.blit(self.font_small.render(name, True, COLOR_ACCENT_CYAN),
                             (info_x, modal.y + 68))
            self.canvas.blit(self.font_small.render(
                f"Last: {self.truncate(last_input, 26)}", True, COLOR_ACCENT_GOLD),
                (info_x, modal.y + 90))
            if not self.joysticks:
                self.canvas.blit(self.font_small.render(
                    "Keyboard fallback active", True, COLOR_TEXT_MUTED), (info_x, modal.y + 108))

            self.canvas.blit(self.font_small.render("CONTROL MAP", True, COLOR_TEXT_MUTED),
                             (modal.x + 20, modal.y + 162))
            pygame.draw.line(self.canvas, COLOR_BORDER, (modal.x + 20, modal.y + 172),
                             (modal.right - 20, modal.y + 172), 1)
            compact = {
                "keyUp": "UP", "keyDown": "DOWN", "keyLeft": "LEFT", "keyRight": "RIGHT",
                "keySelect": "SELECT", "keyA": "A", "keyB": "B",
                "keyL": "L", "keyR": "R", "keyStart": "START",
            }
            chips = []
            for key_id, _label in GBA_BUTTON_ORDER:
                code = self.keybindings.get(key_id, DEFAULT_KEYBINDINGS[key_id])
                chips.append((compact.get(key_id, key_id), self.format_key_name(code)))
            self.draw_chip_grid(modal.x + 20, modal.y + 178, chips, columns=2,
                                row_height=15, col_width=185)

            self.draw_button(pygame.Rect(modal.centerx - 70, modal.bottom - 32, 140, 24),
                             "[Esc] RETURN", COLOR_ACCENT_CYAN)
            self.present()
            self.clock.tick(20)

    def show_confirm(self, title: str, message: str, confirm_label: str = "DELETE",
                     accent: Tuple[int, int, int] = COLOR_ACCENT_RED) -> bool:
        """Generic explicit-confirmation modal (F2 / SV6)."""
        while True:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    return False
                elif event.type == pygame.KEYDOWN:
                    if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_z, pygame.K_SPACE):
                        return True
                    if event.key in (pygame.K_ESCAPE, pygame.K_BACKSPACE, pygame.K_x):
                        return False
                elif event.type == pygame.JOYBUTTONDOWN:
                    return event.button == 0
                elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    cx, cy = self.screen_to_canvas(*event.pos)
                    if 130 <= cx <= 240 and 200 <= cy <= 228:
                        return True
                    if 250 <= cx <= 360 and 200 <= cy <= 228:
                        return False

            modal = pygame.Rect(50, 110, 380, 130)
            self.draw_modal_shell(modal, title, accent)
            for idx, line in enumerate(self.wrap_text(message, 46)):
                self.canvas.blit(self.font_small.render(line, True, COLOR_TEXT_WHITE),
                                 (modal.x + 20, modal.y + 54 + idx * 16))
            self.draw_button(pygame.Rect(130, 200, 110, 26), f"[A] {confirm_label}", accent)
            self.draw_button(pygame.Rect(250, 200, 110, 26), "[B] CANCEL", (110, 100, 140),
                             filled=False, text_color=COLOR_TEXT_MUTED)
            self.present()
            self.clock.tick(30)

    def wrap_text(self, text: str, width: int) -> List[str]:
        words, lines, current = text.split(), [], ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if len(candidate) <= width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines[:4]

    def show_save_manager(self):
        """Per-game save inspector with explicit delete confirmation (SV6)."""
        logger.info("Opening save manager modal...")
        sel = 0
        while True:
            games = [g for g in self.games if self.save_exists(g)]
            items = games or self.games
            if items:
                sel = max(0, min(len(items) - 1, sel))
            current = items[sel] if items else None

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    if not self.appliance_mode:
                        return
                elif event.type == pygame.KEYDOWN:
                    if event.key in (pygame.K_ESCAPE, pygame.K_BACKSPACE, pygame.K_x):
                        return
                    if event.key in (pygame.K_UP,):
                        sel = max(0, sel - 1)
                    elif event.key == pygame.K_DOWN:
                        sel = min(len(items) - 1, sel + 1) if items else 0
                    elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_z,
                                       pygame.K_SPACE) and current:
                        if current.get("demo_save"):
                            self.toast_message("Demo entry - no real save data", COLOR_ACCENT_GOLD)
                            continue
                        summary = self.save_summary(current)
                        if not summary["files"]:
                            self.toast_message("This game has no save data yet", COLOR_TEXT_MUTED)
                            continue
                        if self.show_confirm(
                            "DELETE SAVE DATA?",
                            f"{current.get('title')} - {summary['files']} file(s), "
                            f"{summary['bytes'] / 1024:.1f} KB will be removed permanently.",
                            "DELETE", COLOR_ACCENT_RED,
                        ):
                            shutil.rmtree(summary["path"], ignore_errors=True)
                            logger.warning("User deleted save data for %s", current.get("id"))
                            self.toast_message("Save data deleted", COLOR_ACCENT_RED)
                            sel = max(0, sel - 1)
                elif event.type == pygame.JOYBUTTONDOWN:
                    if event.button == 0 and current:
                        pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN))
                    elif event.button == 1:
                        return
                elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    cx, cy = self.screen_to_canvas(*event.pos)
                    if 40 <= cx <= 220 and 60 <= cy <= 250:
                        row = (cy - 60) // 30
                        if 0 <= row < len(items):
                            sel = row
                    elif cy > 258:
                        return

            modal = pygame.Rect(35, 20, 410, 280)
            self.draw_modal_shell(modal, "SAVE MANAGER", COLOR_ACCENT_MINT,
                                  "Up/Down select - [A] inspect/delete - Esc return")

            list_rect = pygame.Rect(modal.x + 16, modal.y + 52, 210, 190)
            self.draw_panel(list_rect, fill=(26, 21, 40))
            if not items:
                self.canvas.blit(self.font_small.render("No games installed yet", True, COLOR_TEXT_MUTED),
                                 (list_rect.x + 10, list_rect.y + 10))
            for idx, game in enumerate(items[:6]):
                row = pygame.Rect(list_rect.x + 5, list_rect.y + 6 + idx * 30, list_rect.width - 10, 27)
                is_sel = (idx == sel)
                pygame.draw.rect(self.canvas, (60, 50, 95) if is_sel else (36, 30, 54),
                                 row, border_radius=4)
                has = self.save_exists(game)
                pygame.draw.circle(self.canvas, COLOR_ACCENT_MINT if has else (70, 65, 90),
                                   (row.x + 10, row.centery), 3)
                self.canvas.blit(self.font_small.render(
                    self.truncate(game.get("title", ""), 20), True,
                    COLOR_TEXT_WHITE if is_sel else COLOR_TEXT_MUTED), (row.x + 20, row.y + 7))

            detail = pygame.Rect(list_rect.right + 8, list_rect.y, modal.width - 240, 190)
            self.draw_panel(detail, fill=(26, 21, 40))
            if current:
                summary = self.save_summary(current)
                dy = detail.y + 10
                self.canvas.blit(self.font_small.render(
                    self.truncate(current.get("title", ""), 22), True, COLOR_ACCENT_GOLD),
                    (detail.x + 8, dy))
                dy += 20
                lines = [
                    f"Files: {summary['files']}",
                    f"Size: {summary['bytes'] / 1024:.1f} KB",
                    f"Last write: {format_duration(summary['age']) + ' ago' if summary['age'] is not None else '-'}",
                    "",
                    "Path:",
                ]
                for line in lines:
                    self.canvas.blit(self.font_small.render(line, True, COLOR_TEXT_WHITE),
                                     (detail.x + 8, dy))
                    dy += 16
                for line in self.wrap_text(summary["path"], 27):
                    self.canvas.blit(self.font_small.render(line, True, COLOR_TEXT_MUTED),
                                     (detail.x + 8, dy))
                    dy += 14
                if summary["files"]:
                    note = self.font_small.render("[A] delete (confirm)", True, COLOR_ACCENT_RED)
                    self.canvas.blit(note, (detail.x + 8, detail.bottom - 20))
            else:
                self.canvas.blit(self.font_small.render("Nothing to manage", True, COLOR_TEXT_MUTED),
                                 (detail.x + 8, detail.y + 10))

            self.draw_button(pygame.Rect(modal.centerx - 80, modal.bottom - 34, 160, 24),
                             "[Esc] RETURN", COLOR_ACCENT_MINT)
            self.present()
            self.clock.tick(20)

    # ------------------------------------------------------------------- run
    def run(self):
        if self.screenshot_path:
            self._wait_for_scan()
            self.render()
            pygame.image.save(self.canvas, self.screenshot_path)
            print(f"UI preview captured to {self.screenshot_path}")
            return

        while self.running:
            self.update()
            self.handle_input()
            self.render()
            self.clock.tick(60)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GBA OS Launcher")
    parser.add_argument("--focus", type=str, help="Entry ID to focus")
    parser.add_argument("--outcome", type=str, help="Previous session outcome class")
    parser.add_argument("--tab", type=int, default=0, help="Initial tab index (0-2)")
    parser.add_argument("--windowed", action="store_true", help="Run in a window")
    parser.add_argument("--appliance", action="store_true", help="Run in dedicated appliance mode")
    parser.add_argument("--screenshot", type=str, help="Save screenshot to file and exit")
    parser.add_argument("--demo", action="store_true",
                        help="Render a synthetic library (documentation previews)")
    return parser


def main():
    args = build_arg_parser().parse_args()
    launcher = GBALauncher(
        focus_id=args.focus,
        last_outcome=args.outcome,
        windowed=args.windowed,
        screenshot_path=args.screenshot,
        initial_tab=args.tab,
        appliance_mode=args.appliance,
        demo=args.demo,
    )
    launcher.run()


if __name__ == "__main__":
    main()
