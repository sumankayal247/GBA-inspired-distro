#!/usr/bin/env python3
"""
GBA OS Shell.

The library is the desktop: games, built-in appliances and settings are drawn
as Game Boy Advance cartridges on a shelf, with a terminal and a lightweight
browser as first-class tabs.

Design notes
------------
* Rendering happens at the display's native resolution. Everything is laid out
  in design units against a 320-unit reference height and multiplied by the UI
  scale, so the shell fills the whole screen and text stays crisp on a 768p
  laptop as well as a 4K panel. "Integer scale" is available as a retro option.
* Modals are overlays in a single event loop, which is what makes global
  shortcuts (Ctrl+Alt+T, Alt+Tab, F12) work from anywhere instead of only on
  the library screen.
* Contract rules referenced: L1-L6 (library), I1-I4 (input), D1-D4 (display),
  S6 (outcome reporting), F2/SV6 (explicit confirmation before destructive acts).
"""

import argparse
import datetime
import json
import os
import shutil
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

# Ensure local src directory is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# pygame is the one hard dependency of the shell. If it is missing the machine is
# still perfectly usable, so say exactly what to install and exit with the
# dedicated "missing dependencies" code (Rule F3) instead of a traceback.
try:
    import pygame
except Exception as _pygame_error:  # pragma: no cover - broken installs only
    sys.stderr.write(
        "\n  GBA OS cannot start the graphical shell.\n"
        f"  Reason: {_pygame_error}\n\n"
        "  This computer is fine and nothing was modified.\n"
        "  Fix it by installing python3-pygame (or python3 -m pip install pygame)\n"
        "  and starting GBA OS again. The recovery console is still available.\n\n")
    sys.exit(103)  # common.EXIT_CODE_MISSING_DEPS (imported further down)

import config as settings_store
from common import (
    DEFAULT_KEYBINDINGS,
    EXIT_CODE_LAUNCH_REQUEST,
    EXIT_CODE_POWEROFF,
    EXIT_CODE_REBOOT,
    GBA_BUTTON_ORDER,
    SYS_CONTRACT_VERSION,
    format_duration,
    get_battery_status,
    get_data_dir,
    get_games_dir,
    get_load_average,
    get_logs_dir,
    get_memory_usage,
    get_run_dir,
    get_uptime_seconds,
    keybindings_conflict_error,
    load_keybindings,
    load_play_history,
    record_play_session,
    sanitize_keybindings,
    save_keybindings,
    setup_logging,
)
import game_data
from gba_browser import Browser
from gba_ingame import button_from_key, button_from_joy_button, buttons_from_hat
from gba_terminal import TerminalScreen, TerminalSession, TerminalView, key_to_bytes
from gba_usb import (MtpManager, VolumeWatcher, import_roms, list_import_sources,
                     list_mounted_mtp_volumes, list_mounted_volumes,
                     mtp_diagnostics)
from library import Library
from theme import THEME_ORDER, Theme

logger = setup_logging("gba.launcher")

TABS = ["GAMES", "APPS", "BROWSER", "TERMINAL", "SETTINGS"]
TAB_GAMES, TAB_APPS, TAB_BROWSER, TAB_TERMINAL, TAB_SETTINGS = range(5)

TOAST_INFO = "info"
TOAST_OK = "ok"
TOAST_WARN = "warn"
TOAST_ERROR = "error"

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
    {"id": "2c8f4b7a91d3e605", "title": "Ridge Racer Advance", "format": "GBA",
     "size_bytes": int(8.0 * 1024 * 1024)},
    {"id": "6d1a9f30b7c2e48a", "title": "Metroid Fusion", "format": "GBA",
     "size_bytes": int(8.0 * 1024 * 1024)},
]


# =============================================================================
# Utilities
# =============================================================================

def wrap_text(font, text: str, width: int, max_lines: int = 3) -> List[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if font.size(candidate)[0] <= width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
            if len(lines) >= max_lines:
                break
    if current and len(lines) < max_lines:
        lines.append(current)
    return lines


def truncate(font, text: str, width: int) -> str:
    if font.size(text)[0] <= width:
        return text
    while text and font.size(text + "..")[0] > width:
        text = text[:-1]
    return text + ".." if text else ""


def human_size(size: float) -> str:
    if size < 1024:
        return f"{int(size)} B"
    if size < 1024 ** 2:
        return f"{size / 1024:.0f} KB"
    if size < 1024 ** 3:
        return f"{size / 1024 ** 2:.1f} MB"
    return f"{size / 1024 ** 3:.1f} GB"


def tail_file(path: str, lines: int = 200) -> List[str]:
    try:
        with open(path, "r", errors="ignore") as f:
            return f.readlines()[-lines:]
    except Exception:
        return []


# =============================================================================
# Overlays (modals) — one event loop, many screens
# =============================================================================

class Overlay:
    """A screen drawn on top of the library. Subclasses own their input."""

    title = ""
    subtitle = ""
    accent = "primary"
    width = 300.0
    height = 200.0

    def __init__(self):
        self.closed = False
        self.entered_at = time.monotonic()

    # -- geometry ------------------------------------------------------
    def rect(self, shell) -> pygame.Rect:
        theme = shell.theme
        # Centre against the *actual* design height. On a >1920px-tall panel
        # ui_scale_for() clamps at 6.0, so design_h grows past the 320-unit
        # reference; using a literal 320 would leave the popup off-centre.
        design_h = getattr(shell, "design_h", 320) or 320
        w = min(self.width, shell.design_w - 16)
        h = min(self.height, design_h - 10)
        x = (shell.design_w - w) / 2
        y = (design_h - h) / 2
        return theme.rect(x, y, w, h)

    def content_rect(self, shell) -> pygame.Rect:
        theme = shell.theme
        rect = self.rect(shell)
        top = rect.y + theme.px(30)
        return pygame.Rect(rect.x + theme.px(8), top,
                           rect.width - theme.px(16),
                           rect.bottom - theme.px(10) - top)

    # -- lifecycle -----------------------------------------------------
    def close(self, shell) -> None:
        self.closed = True
        shell.pop_overlay(self)

    def handle(self, event, shell) -> None:
        if event.type == pygame.QUIT:
            if not shell.appliance_mode:
                self.close(shell)
        elif event.type == pygame.KEYDOWN and event.key in (pygame.K_ESCAPE, pygame.K_BACKSPACE):
            self.close(shell)
        elif event.type == pygame.JOYBUTTONDOWN and event.button == 1:
            self.close(shell)

    # -- drawing -------------------------------------------------------
    def draw(self, shell) -> None:
        theme = shell.theme
        shell.dim_background()
        rect = self.rect(shell)
        theme.panel(shell.surface, rect, fill=theme.colors.panel,
                    border=getattr(theme.colors, self.accent) if self.accent else theme.colors.primary,
                    radius=8, width=1.4)
        if self.title:
            theme.text(shell.surface, self.title, (rect.centerx / theme.scale, rect.y / theme.scale + 9),
                       "header", getattr(theme.colors, self.accent), align="center")
        if self.subtitle:
            theme.label(shell.surface, self.subtitle,
                        (rect.centerx / theme.scale, rect.y / theme.scale + 22),
                        theme.colors.text_dim, align="center")
        pygame.draw.line(shell.surface, theme.colors.border_soft,
                         (rect.x + theme.px(8), rect.y + theme.px(28)),
                         (rect.right - theme.px(8), rect.y + theme.px(28)), 1)
        self.draw_body(shell)

    def draw_body(self, shell) -> None:  # pragma: no cover - abstract
        pass


class ConfirmOverlay(Overlay):
    """Explicit confirmation for anything destructive (F2 / SV6)."""

    def __init__(self, title: str, message: str, confirm_label: str = "CONFIRM",
                 danger: bool = True, on_confirm=None):
        super().__init__()
        self.title = title
        self.message = message
        self.confirm_label = confirm_label
        self.danger = danger
        self.on_confirm = on_confirm
        self.accent = "danger" if danger else "primary"
        self.width = 320.0
        self.height = 118.0

    def handle(self, event, shell) -> None:
        if event.type == pygame.QUIT:
            if not shell.appliance_mode:
                self.close(shell)
            return
        if event.type == pygame.KEYDOWN:
            if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_z, pygame.K_SPACE):
                self._accept(shell)
            elif event.key in (pygame.K_ESCAPE, pygame.K_BACKSPACE, pygame.K_x):
                self.close(shell)
        elif event.type == pygame.JOYBUTTONDOWN:
            if event.button == 0:
                self._accept(shell)
            elif event.button == 1:
                self.close(shell)
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            rect = self.rect(shell)
            theme = shell.theme
            yes = pygame.Rect(rect.centerx - theme.px(74), rect.bottom - theme.px(30),
                              theme.px(66), theme.px(20))
            no = pygame.Rect(rect.centerx + theme.px(8), rect.bottom - theme.px(30),
                             theme.px(66), theme.px(20))
            if yes.collidepoint(event.pos):
                self._accept(shell)
            elif no.collidepoint(event.pos):
                self.close(shell)

    def _accept(self, shell) -> None:
        self.close(shell)
        if self.on_confirm:
            self.on_confirm()

    def draw_body(self, shell) -> None:
        theme = shell.theme
        rect = self.rect(shell)
        font = theme.font("body")
        lines = wrap_text(font, self.message, rect.width - theme.px(24), max_lines=4)
        y = rect.y + theme.px(38)
        for line in lines:
            surf = font.render(line, True, theme.colors.text)
            shell.surface.blit(surf, (rect.x + theme.px(12), y))
            y += font.get_linesize() + theme.px(1)
        theme.button(shell.surface, pygame.Rect(rect.centerx - theme.px(74),
                                                rect.bottom - theme.px(30),
                                                theme.px(66), theme.px(20)),
                     f"[A] {self.confirm_label}", theme.colors.danger if self.danger else theme.colors.primary)
        theme.button(shell.surface, pygame.Rect(rect.centerx + theme.px(8),
                                                rect.bottom - theme.px(30),
                                                theme.px(66), theme.px(20)),
                     "[B] CANCEL", theme.colors.text_dim, filled=False)


class TextOverlay(Overlay):
    """Scrollable read-only text (logs, about, help)."""

    def __init__(self, title: str, lines: List[str], subtitle: str = ""):
        super().__init__()
        self.title = title
        self.subtitle = subtitle
        self.lines = lines
        self.scroll = 0
        self.width = 380.0
        self.height = 270.0

    def handle(self, event, shell) -> None:
        if event.type == pygame.QUIT:
            if not shell.appliance_mode:
                self.close(shell)
            return
        if event.type == pygame.KEYDOWN:
            if event.key in (pygame.K_ESCAPE, pygame.K_BACKSPACE, pygame.K_x, pygame.K_RETURN):
                self.close(shell)
            elif event.key == pygame.K_UP:
                self.scroll = max(0, self.scroll - 1)
            elif event.key == pygame.K_DOWN:
                self.scroll = min(max(0, len(self.lines) - 1), self.scroll + 1)
            elif event.key == pygame.K_PAGEUP:
                self.scroll = max(0, self.scroll - 10)
            elif event.key == pygame.K_PAGEDOWN:
                self.scroll += 10
        elif event.type == pygame.MOUSEBUTTONDOWN:
            if event.button == 1:
                self.close(shell)
            elif event.button == 4:
                self.scroll = max(0, self.scroll - 2)
            elif event.button == 5:
                self.scroll += 2
        elif event.type == pygame.JOYBUTTONDOWN and event.button == 1:
            self.close(shell)

    def draw_body(self, shell) -> None:
        theme = shell.theme
        rect = self.content_rect(shell)
        font = theme.mono(8)
        line_h = font.get_linesize() + theme.px(1)
        rows = max(1, rect.height // line_h)
        self.scroll = max(0, min(self.scroll, max(0, len(self.lines) - rows)))
        for index in range(rows):
            line_index = self.scroll + index
            if line_index >= len(self.lines):
                break
            text = self.lines[line_index].rstrip("\n")
            color = theme.colors.text_muted
            lower = text.lower()
            if "error" in lower or "critical" in lower:
                color = theme.colors.danger
            elif "warning" in lower:
                color = theme.colors.highlight
            surf = font.render(truncate(font, text, rect.width), True, color)
            shell.surface.blit(surf, (rect.x, rect.y + index * line_h))


# ---------------------------------------------------------------- launch ----

class LaunchOverlay(Overlay):
    """Pre-flight card: what is about to run, where saves live, and controls."""

    def __init__(self, item: Dict[str, Any]):
        super().__init__()
        self.item = item
        self.title = "READY TO PLAY"
        self.width = 340.0
        self.height = 250.0
        self.accent = "highlight"

    def handle(self, event, shell) -> None:
        if event.type == pygame.QUIT:
            if not shell.appliance_mode:
                self.close(shell)
            return
        if event.type == pygame.KEYDOWN:
            if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_z, pygame.K_SPACE):
                self.close(shell)
                shell.launch_game(self.item)
            elif event.key in (pygame.K_ESCAPE, pygame.K_BACKSPACE, pygame.K_x):
                self.close(shell)
        elif event.type == pygame.JOYBUTTONDOWN:
            if event.button == 0:
                self.close(shell)
                shell.launch_game(self.item)
            elif event.button == 1:
                self.close(shell)
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            rect = self.rect(shell)
            theme = shell.theme
            start = pygame.Rect(rect.x + theme.px(14), rect.bottom - theme.px(30),
                                theme.px(140), theme.px(22))
            cancel = pygame.Rect(rect.right - theme.px(154), rect.bottom - theme.px(30),
                                 theme.px(140), theme.px(22))
            if start.collidepoint(event.pos):
                self.close(shell)
                shell.launch_game(self.item)
            elif cancel.collidepoint(event.pos):
                self.close(shell)

    def draw_body(self, shell) -> None:
        theme = shell.theme
        c = theme.colors
        rect = self.rect(shell)
        item = self.item
        summary = shell.save_summary(item)
        record = shell.history.get(item.get("id", ""), {})

        # Left: the cartridge itself.
        cart_w, cart_h = 92.0, 106.0
        cart = theme.rect(rect.x / theme.scale + 14, rect.y / theme.scale + 36, cart_w, cart_h)
        theme.cartridge(shell.surface, cart, item.get("title", "Game"),
                        item.get("format", "GBA"), item.get("id", ""))

        info_x = (cart.right / theme.scale) + 10
        top = rect.y / theme.scale + 36
        width = (rect.right / theme.scale) - info_x - 12
        font = theme.font("body_bold")
        for index, line in enumerate(wrap_text(font, item.get("title", ""),
                                               theme.px(width), max_lines=2)):
            theme.text(shell.surface, line, (info_x, top + index * 11), "body_bold")

        rows = [
            ("Format", f"{item.get('format', 'GBA')} · {human_size(item.get('size_bytes', 0))}"),
            ("Save", f"{summary['files']} file(s), {human_size(summary['bytes'])}"
                     if summary["files"] else "new game"),
            ("Played", f"{record.get('sessions', 0)}x · {format_duration(record.get('seconds', 0))}"
                       if record else "first time"),
        ]
        y = top + (24 if len(item.get("title", "")) > 22 else 14)
        for label, value in rows:
            theme.label(shell.surface, label, (info_x, y), c.text_dim)
            theme.label(shell.surface, truncate(theme.font("small"), value, theme.px(width - 34)),
                        (info_x + 34, y), c.text)
            y += 11

        # Controls strip.
        strip = theme.rect(rect.x / theme.scale + 12, rect.y / theme.scale + 156,
                           (rect.width / theme.scale) - 24, 52)
        pygame.draw.rect(shell.surface, c.panel_alt, strip, border_radius=theme.px(5))
        pygame.draw.rect(shell.surface, c.border_soft, strip, width=1, border_radius=theme.px(5))
        keys = [
            ("A / Enter", "action"),
            ("B / Esc", "cancel"),
            ("Start", "menu"),
            ("Esc", "quit to library"),
        ]
        theme.label(shell.surface, "CONTROLS", (strip.x / theme.scale + 6, strip.y / theme.scale + 4),
                    c.highlight)
        for index, (key, desc) in enumerate(keys):
            col = index % 2
            row = index // 2
            x = strip.x / theme.scale + 6 + col * (strip.width / theme.scale / 2)
            y = strip.y / theme.scale + 16 + row * 11
            theme.label(shell.surface, f"{key}", (x, y), c.info)
            theme.label(shell.surface, desc, (x + 52, y), c.text_muted)

        theme.button(shell.surface,
                     pygame.Rect(rect.x + theme.px(14), rect.bottom - theme.px(30),
                                 theme.px(140), theme.px(22)), "[A] START GAME", c.highlight)
        theme.button(shell.surface,
                     pygame.Rect(rect.right - theme.px(154), rect.bottom - theme.px(30),
                                 theme.px(140), theme.px(22)), "[B] CANCEL", c.text_dim, filled=False)


# ------------------------------------------------------------------ apps ----

class MonitorOverlay(Overlay):
    def __init__(self):
        super().__init__()
        self.title = "SYSTEM MONITOR"
        self.subtitle = "live telemetry · Esc to close"
        self.width = 360.0
        self.height = 250.0
        self.history: List[float] = []

    def handle(self, event, shell) -> None:
        super().handle(event, shell)

    def draw_body(self, shell) -> None:
        theme = shell.theme
        c = theme.colors
        rect = self.content_rect(shell)
        memory = get_memory_usage()
        self.history.append(memory["percent"])
        del self.history[:-60]

        x0 = rect.x / theme.scale
        y0 = rect.y / theme.scale
        width = rect.width / theme.scale

        theme.label(shell.surface, "MEMORY", (x0, y0), c.text_dim)
        theme.label(shell.surface, f"{memory['used_mb']} / {memory['total_mb']} MB",
                    (x0 + width, y0), c.text, align="right")
        bar = theme.rect(x0, y0 + 9, width, 7)
        theme.bar(shell.surface, bar, memory["percent"] / 100.0,
                  c.success if memory["percent"] < 75 else c.danger)

        # Sparkline of recent memory usage.
        graph = theme.rect(x0, y0 + 20, width, 26)
        pygame.draw.rect(shell.surface, c.panel_alt, graph, border_radius=theme.px(3))
        if len(self.history) > 1:
            points = []
            for index, value in enumerate(self.history):
                px = graph.x + int(graph.width * index / max(1, len(self.history) - 1))
                py = graph.bottom - int(graph.height * (value / 100.0))
                points.append((px, py))
            if len(points) > 1:
                pygame.draw.lines(shell.surface, c.info, False, points, max(1, theme.px(1)))

        storage = settings_store.storage_info(get_data_dir()) or {}
        y = y0 + 52
        rows = [
            ("DATA", f"{storage.get('free_gb', 0):.1f} GB free of {storage.get('total_gb', 0):.1f} GB"),
            ("Games", f"{len(shell.games)} in {get_games_dir()}"),
            ("Load", "  ".join(f"{v:.2f}" for v in get_load_average())),
            ("Uptime", format_duration(get_uptime_seconds())),
            ("Display", f"{shell.surface.get_width()}x{shell.surface.get_height()} "
                        f"({pygame.display.get_driver()})"),
            ("UI scale", f"{theme.scale:.2f}x · {shell.settings['scaling']}"),
            ("Gamepads", f"{len(shell.joysticks)} connected"),
            ("USB", shell.usb_status_text()),
            ("Contract", f"v{SYS_CONTRACT_VERSION}"),
        ]
        for label, value in rows:
            theme.label(shell.surface, label, (x0, y), c.text_dim)
            theme.label(shell.surface, truncate(theme.font("small"), value, theme.px(width - 52)),
                        (x0 + 52, y), c.text)
            y += 11


class InputDiagOverlay(Overlay):
    def __init__(self, shell):
        super().__init__()
        self.title = "INPUT DIAGNOSTICS"
        self.subtitle = "press buttons · Esc to close"
        self.width = 380.0
        self.height = 260.0
        self.last = "waiting for input..."
        self.pressed = set()
        self.hat = (0, 0)
        self.axes = [0.0, 0.0]
        shell.keybindings = load_keybindings()

    def handle(self, event, shell) -> None:
        if event.type == pygame.QUIT:
            if not shell.appliance_mode:
                self.close(shell)
            return
        if event.type == pygame.KEYDOWN:
            if event.key in (pygame.K_ESCAPE, pygame.K_BACKSPACE):
                self.close(shell)
            else:
                self.last = f"key {pygame.key.name(event.key).upper()}"
            self.pressed.clear()
        elif event.type == pygame.JOYBUTTONDOWN:
            self.pressed.add(event.button)
            self.last = f"button #{event.button}"
        elif event.type == pygame.JOYBUTTONUP:
            self.pressed.discard(event.button)
        elif event.type == pygame.JOYHATMOTION:
            self.hat = event.value
            self.last = f"d-pad {event.value}"
        elif event.type == pygame.JOYAXISMOTION:
            if event.axis < 2:
                self.axes[event.axis] = event.value
            self.last = f"axis {event.axis} {event.value:+.2f}"
        elif event.type == pygame.JOYDEVICEADDED:
            shell.refresh_joysticks()
            self.last = "gamepad connected"
        elif event.type == pygame.JOYDEVICEREMOVED:
            shell.refresh_joysticks()
            self.last = "gamepad removed"
        elif event.type == pygame.MOUSEBUTTONDOWN:
            self.close(shell)

    def draw_body(self, shell) -> None:
        theme = shell.theme
        c = theme.colors
        rect = self.content_rect(shell)
        x0, y0 = rect.x / theme.scale, rect.y / theme.scale
        width = rect.width / theme.scale

        # Pad visual.
        pad = theme.rect(x0, y0, 150, 108)
        pygame.draw.rect(shell.surface, c.panel_alt, pad, border_radius=theme.px(10))
        pygame.draw.rect(shell.surface, c.border_soft, pad, width=1, border_radius=theme.px(10))
        cx, cy = pad.centerx, pad.y + theme.px(46)
        dx, dy = theme.px(6), theme.px(18)
        lit = {"up": self.hat[1] == 1, "down": self.hat[1] == -1,
               "left": self.hat[0] == -1, "right": self.hat[0] == 1}
        for name, rr in {
            "up": (cx - dx, cy - dy - dx, dx * 2, dy),
            "down": (cx - dx, cy + dx, dx * 2, dy),
            "left": (cx - dy - dx, cy - dx, dy, dx * 2),
            "right": (cx + dx, cy - dx, dy, dx * 2),
        }.items():
            pygame.draw.rect(shell.surface, c.highlight if lit[name] else c.border,
                             rr, border_radius=theme.px(2))
        pygame.draw.circle(shell.surface, c.danger if 0 in self.pressed else c.accent_soft,
                           (pad.right - theme.px(26), cy - theme.px(8)), theme.px(10))
        pygame.draw.circle(shell.surface, c.danger if 1 in self.pressed else c.accent_soft,
                           (pad.right - theme.px(48), cy + theme.px(12)), theme.px(10))
        theme.label(shell.surface, "A", ((pad.right / theme.scale) - 28, (cy / theme.scale) - 14),
                    c.text, key="micro")
        theme.label(shell.surface, "B", ((pad.right / theme.scale) - 50, (cy / theme.scale) + 6),
                    c.text, key="micro")

        for index, value in enumerate(self.axes):
            bar = theme.rect(x0 + 6, (pad.bottom / theme.scale) - 34 + index * 9, 138, 5)
            theme.bar(shell.surface, bar, (value + 1) / 2, c.info)
        theme.label(shell.surface, "STICK", (x0 + 6, (pad.bottom / theme.scale) - 46), c.text_dim)

        # Details.
        info_x = x0 + 160
        theme.label(shell.surface, f"Gamepads: {len(shell.joysticks)}", (info_x, y0), c.text)
        name = shell.joysticks[0].get_name() if shell.joysticks else "Keyboard only"
        theme.label(shell.surface, truncate(theme.font("small"), name, theme.px(width - 160)),
                    (info_x, y0 + 10), c.info)
        theme.label(shell.surface, truncate(theme.font("small"), f"Last: {self.last}",
                                            theme.px(width - 160)),
                    (info_x, y0 + 20), c.highlight)

        theme.label(shell.surface, "CONTROL MAP", (info_x, y0 + 34), c.text_dim)
        pygame.draw.line(shell.surface, c.border_soft,
                         (theme.px(info_x), theme.px(y0 + 43)),
                         (rect.right - theme.px(4), theme.px(y0 + 43)), 1)
        compact = {"keyUp": "UP", "keyDown": "DOWN", "keyLeft": "LEFT", "keyRight": "RIGHT",
                   "keySelect": "SELECT", "keyA": "A", "keyB": "B", "keyL": "L",
                   "keyR": "R", "keyStart": "START"}
        for index, (key_id, _label) in enumerate(GBA_BUTTON_ORDER):
            col, row = index % 2, index // 2
            x = info_x + col * (width - 160) / 2
            y = y0 + 48 + row * 10
            code = shell.keybindings.get(key_id, DEFAULT_KEYBINDINGS[key_id])
            theme.label(shell.surface, compact.get(key_id, key_id), (x, y), c.text_dim)
            theme.label(shell.surface, shell.format_key_name(code), (x + 32, y), c.text)


class SaveManagerOverlay(Overlay):
    def __init__(self):
        super().__init__()
        self.title = "SAVE MANAGER"
        self.subtitle = "A inspect/delete · Esc back"
        self.width = 360.0
        self.height = 260.0
        self.accent = "success"
        self.index = 0

    def entries(self, shell) -> List[Dict[str, Any]]:
        games = [g for g in shell.games if shell.save_exists(g)]
        return games or shell.games

    def handle(self, event, shell) -> None:
        if event.type == pygame.QUIT:
            if not shell.appliance_mode:
                self.close(shell)
            return
        items = self.entries(shell)
        if event.type == pygame.KEYDOWN:
            if event.key in (pygame.K_ESCAPE, pygame.K_BACKSPACE, pygame.K_x):
                self.close(shell)
            elif event.key == pygame.K_UP:
                self.index = max(0, self.index - 1)
            elif event.key == pygame.K_DOWN and items:
                self.index = min(len(items) - 1, self.index + 1)
            elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_z, pygame.K_SPACE):
                self.prompt_delete(shell, items)
        elif event.type == pygame.JOYBUTTONDOWN:
            if event.button == 0:
                self.prompt_delete(shell, items)
            elif event.button == 1:
                self.close(shell)
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            rect = self.content_rect(shell)
            row = (event.pos[1] - rect.y) // shell.theme.px(13)
            if 0 <= row < len(items):
                self.index = row
            else:
                self.close(shell)

    def prompt_delete(self, shell, items) -> None:
        if not items:
            return
        item = items[min(self.index, len(items) - 1)]
        summary = shell.save_summary(item)
        if item.get("demo_save"):
            shell.toast("Demo entry has no real save data", TOAST_WARN)
            return
        if not summary["files"]:
            shell.toast("This game has no save data yet", TOAST_WARN)
            return

        def do_delete():
            shutil.rmtree(summary["path"], ignore_errors=True)
            logger.warning("User deleted save data for %s", item.get("id"))
            shell.toast("Save data deleted", TOAST_ERROR)

        shell.push_overlay(ConfirmOverlay(
            "DELETE SAVE DATA?",
            f"{item.get('title')} · {summary['files']} file(s), "
            f"{human_size(summary['bytes'])} will be removed permanently.",
            "DELETE", True, do_delete))

    def draw_body(self, shell) -> None:
        theme = shell.theme
        c = theme.colors
        rect = self.content_rect(shell)
        items = self.entries(shell)
        self.index = max(0, min(self.index, max(0, len(items) - 1)))
        x0, y0 = rect.x / theme.scale, rect.y / theme.scale
        width = rect.width / theme.scale

        if not items:
            theme.text(shell.surface, "No games installed yet", (x0 + 4, y0 + 4), "body", c.text_muted)
            return

        list_w = width * 0.52
        for index, game in enumerate(items[:11]):
            y = y0 + index * 13
            selected = index == self.index
            row = theme.rect(x0, y, list_w, 11.5)
            if selected:
                pygame.draw.rect(shell.surface, c.card_sel, row, border_radius=theme.px(3))
            has = shell.save_exists(game)
            pygame.draw.circle(shell.surface, c.success if has else c.border,
                               (row.x + theme.px(6), row.centery), max(2, theme.px(2)))
            theme.label(shell.surface,
                        truncate(theme.font("small"), game.get("title", ""), theme.px(list_w - 14)),
                        (x0 + 10, y + 2), c.text if selected else c.text_muted)

        current = items[self.index]
        summary = shell.save_summary(current)
        dx = x0 + list_w + 8
        theme.label(shell.surface, truncate(theme.font("body_bold"), current.get("title", ""),
                                            theme.px(width - list_w - 10)),
                    (dx, y0), c.highlight, key="body_bold")
        lines = [
            f"Files: {summary['files']}",
            f"Size: {human_size(summary['bytes'])}",
            f"Updated: {format_duration(summary['age']) + ' ago' if summary['age'] else 'never'}",
        ]
        y = y0 + 14
        for line in lines:
            theme.label(shell.surface, line, (dx, y), c.text)
            y += 10
        theme.label(shell.surface, "Location", (dx, y + 4), c.text_dim)
        y += 14
        for line in wrap_text(theme.font("micro"), summary["path"], theme.px(width - list_w - 10),
                              max_lines=3):
            theme.label(shell.surface, line, (dx, y), c.text_muted, key="micro")
            y += 9
        if summary["files"]:
            theme.label(shell.surface, "[A] delete (asks first)", (dx, y + 6), c.danger)


class KeyRemapOverlay(Overlay):
    def __init__(self, shell):
        super().__init__()
        self.title = "CONTROLS"
        self.width = 380.0
        self.height = 250.0
        self.index = 0
        self.listening = False
        self.error = ""
        shell.keybindings = load_keybindings()

    def handle(self, event, shell) -> None:
        if event.type == pygame.QUIT:
            if not shell.appliance_mode:
                self.close(shell)
            return
        if event.type == pygame.KEYDOWN:
            if self.listening:
                if event.key == pygame.K_ESCAPE:
                    self.listening = False
                    self.error = ""
                else:
                    key_id = GBA_BUTTON_ORDER[self.index][0]
                    reason = keybindings_conflict_error(shell.keybindings, key_id, event.key)
                    if reason:
                        self.error = reason
                    else:
                        shell.keybindings[key_id] = event.key
                        shell.keybindings = sanitize_keybindings(shell.keybindings)
                        save_keybindings(shell.keybindings)
                        self.error = ""
                        self.listening = False
                return
            if event.key == pygame.K_UP:
                self.index = (self.index - 1) % len(GBA_BUTTON_ORDER)
            elif event.key == pygame.K_DOWN:
                self.index = (self.index + 1) % len(GBA_BUTTON_ORDER)
            elif event.key in (pygame.K_LEFT, pygame.K_RIGHT):
                self.index = (self.index + len(GBA_BUTTON_ORDER) // 2) % len(GBA_BUTTON_ORDER)
            elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
                self.listening = True
                self.error = ""
            elif event.key == pygame.K_r:
                shell.keybindings = dict(DEFAULT_KEYBINDINGS)
                save_keybindings(shell.keybindings)
                self.error = ""
                shell.toast("Key bindings reset to defaults", TOAST_OK)
            elif event.key in (pygame.K_ESCAPE, pygame.K_BACKSPACE):
                self.close(shell)
        elif event.type == pygame.JOYBUTTONDOWN and not self.listening:
            if event.button == 0:
                self.listening = True
            elif event.button == 1:
                self.close(shell)

    def draw_body(self, shell) -> None:
        theme = shell.theme
        c = theme.colors
        rect = self.content_rect(shell)
        x0, y0 = rect.x / theme.scale, rect.y / theme.scale
        width = rect.width / theme.scale

        prompt = ("PRESS A KEY FOR " + GBA_BUTTON_ORDER[self.index][1]) if self.listening else \
                 "Enter remap · R reset · Esc done"
        theme.label(shell.surface, prompt, (x0, y0), c.highlight if self.listening else c.text_dim)

        col_w = width / 2
        for index, (key_id, label) in enumerate(GBA_BUTTON_ORDER):
            col, row = index // 5, index % 5
            x = x0 + col * col_w
            y = y0 + 15 + row * 14
            selected = index == self.index
            box = theme.rect(x, y, col_w - 6, 12)
            if selected:
                pygame.draw.rect(shell.surface, c.card_sel, box, border_radius=theme.px(3))
                pygame.draw.rect(shell.surface, c.highlight, box, width=1, border_radius=theme.px(3))
            theme.label(shell.surface, label, (x + 4, y + 2), c.text if selected else c.text_muted)
            if selected and self.listening:
                value, color = "PRESS...", c.highlight
            else:
                code = shell.keybindings.get(key_id, DEFAULT_KEYBINDINGS[key_id])
                value, color = shell.format_key_name(code), (c.highlight if selected else c.info)
            theme.label(shell.surface, value, (x + col_w - 10, y + 2), color, align="right")

        if self.error:
            theme.label(shell.surface, truncate(theme.font("small"), self.error, theme.px(width)),
                        (x0, y0 + 15 + 5 * 14), c.danger)


class ShortcutOverlay(Overlay):
    """Configure the three in-game action combos through the settings store.

    Controller-first: press two controller buttons (D-pad included), or use
    the keyboard equivalents from the shared keybinding profile (I3).
    """

    def __init__(self, shell):
        super().__init__()
        self.title = "IN-GAME SHORTCUTS"
        self.subtitle = "A capture two buttons · R reset · B close"
        self.width = 360.0
        self.height = 210.0
        self.accent = "info"
        self.actions = list(settings_store.SHORTCUT_ACTIONS)
        self.index = 0
        self.listening = False
        self.captured: List[str] = []
        self.message = ""
        self._reload(shell)

    def _reload(self, shell) -> None:
        self.shortcuts = settings_store.load_shortcuts(shell.settings)

    def handle(self, event, shell) -> None:
        if event.type == pygame.QUIT:
            if not shell.appliance_mode:
                self.close(shell)
            return
        if event.type == pygame.KEYDOWN:
            if self.listening:
                if event.key == pygame.K_ESCAPE:
                    self.listening = False
                    self.captured = []
                    return
                button = button_from_key(event.key, shell.keybindings)
                self._capture(button, shell)
                return
            if event.key in (pygame.K_ESCAPE, pygame.K_BACKSPACE, pygame.K_x):
                self.close(shell)
            elif event.key == pygame.K_UP:
                self.index = (self.index - 1) % len(self.actions)
            elif event.key == pygame.K_DOWN:
                self.index = (self.index + 1) % len(self.actions)
            elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_z,
                               pygame.K_SPACE):
                self.listening = True
                self.captured = []
                self.message = ""
            elif event.key == pygame.K_r:
                self._reset(shell)
            return
        if event.type == pygame.JOYBUTTONDOWN:
            if self.listening:
                self._capture(button_from_joy_button(event.button), shell)
            elif event.button == 0:
                self.listening = True
                self.captured = []
                self.message = ""
            elif event.button == 1:
                self.close(shell)
            return
        if event.type == pygame.JOYHATMOTION and self.listening:
            for button in buttons_from_hat(event.value):
                self._capture(button, shell)
            return
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            rect = self.content_rect(shell)
            row = (event.pos[1] - rect.y) // shell.theme.px(30)
            if 0 <= row < len(self.actions):
                if row == self.index:
                    self.listening = True
                    self.captured = []
                    self.message = ""
                else:
                    self.index = row

    def _capture(self, button: Optional[str], shell) -> None:
        if not button or button in self.captured:
            return
        self.captured.append(button)
        if len(self.captured) == 2:
            combo = tuple(self.captured)
            action = self.actions[self.index]
            payload = dict(self.shortcuts)
            payload[action] = combo
            # Persist through the single settings document (settings.json).
            settings = settings_store.save_shortcuts(payload, shell.settings)
            shell.settings.update(settings)
            self._reload(shell)
            self.listening = False
            self.captured = []
            self.message = f"Saved {settings_store.shortcut_label(list(combo))}"

    def _reset(self, shell) -> None:
        defaults = {action: settings_store.DEFAULT_SHORTCUTS[
            settings_store.SHORTCUT_SETTING_FOR_ACTION[action]]
            for action in self.actions}
        settings = settings_store.save_shortcuts(defaults, shell.settings)
        shell.settings.update(settings)
        self._reload(shell)
        self.message = "Shortcuts reset to defaults"
        shell.toast("In-game shortcuts reset", TOAST_OK)

    def draw_body(self, shell) -> None:
        theme, c = shell.theme, shell.theme.colors
        rect = self.content_rect(shell)
        x0, y0 = rect.x / theme.scale, rect.y / theme.scale
        width = rect.width / theme.scale
        for index, action in enumerate(self.actions):
            y = y0 + index * 30
            row = theme.rect(x0, y, width, 26)
            selected = index == self.index
            if selected:
                pygame.draw.rect(shell.surface, c.card_sel, row,
                                 border_radius=theme.px(4))
                pygame.draw.rect(shell.surface, c.info, row, width=1,
                                 border_radius=theme.px(4))
            theme.label(shell.surface, settings_store.SHORTCUT_LABELS[action],
                        (x0 + 6, y + 4), c.text if selected else c.text_muted,
                        key="body")
            if selected and self.listening:
                value = " + ".join(settings_store.SHORTCUT_BUTTON_LABELS.get(b, b)
                                   for b in self.captured) + " ..."
            else:
                value = settings_store.shortcut_label(list(self.shortcuts[action]))
            theme.label(shell.surface, value, (x0 + width - 6, y + 4),
                        c.highlight if selected else c.info, align="right")
        if self.listening:
            theme.label(shell.surface, "PRESS TWO DISTINCT BUTTONS",
                        (x0 + 6, y0 + 3 * 30 + 6), c.highlight)
        elif self.message:
            theme.label(shell.surface, self.message,
                        (x0 + 6, y0 + 3 * 30 + 6), c.success)


class StrongConfirmOverlay(Overlay):
    """Delete-all guard: hold A / Enter for two seconds, with a visible bar."""

    HOLD_SECONDS = 2.0

    def __init__(self, title: str, message: str, on_confirm=None,
                 confirm_label: str = "HOLD TO DELETE"):
        super().__init__()
        self.title = title
        self.message = message
        self.on_confirm = on_confirm
        self.confirm_label = confirm_label
        self.accent = "danger"
        self.width = 340.0
        self.height = 150.0
        self.hold_started = None
        self.done = False

    def handle(self, event, shell) -> None:
        if self.done:
            return
        if event.type == pygame.QUIT:
            if not shell.appliance_mode:
                self.close(shell)
            return
        if event.type == pygame.KEYDOWN:
            if event.key in (pygame.K_ESCAPE, pygame.K_BACKSPACE, pygame.K_x):
                self.close(shell)
            elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER,
                               pygame.K_z, pygame.K_SPACE):
                self.hold_started = time.monotonic()
        elif event.type == pygame.KEYUP:
            if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER,
                             pygame.K_z, pygame.K_SPACE):
                self.hold_started = None
        elif event.type == pygame.JOYBUTTONDOWN:
            if event.button == 0:
                self.hold_started = time.monotonic()
            elif event.button == 1:
                self.close(shell)
        elif event.type == pygame.JOYBUTTONUP and event.button == 0:
            self.hold_started = None
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            self.hold_started = time.monotonic()
        elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            self.hold_started = None

    def draw_body(self, shell) -> None:
        theme, c = shell.theme, shell.theme.colors
        rect = self.rect(shell)
        font = theme.font("body")
        for index, line in enumerate(wrap_text(font, self.message,
                                               rect.width - theme.px(24), 3)):
            surf = font.render(line, True, c.text)
            shell.surface.blit(surf, (rect.x + theme.px(12),
                                      rect.y + theme.px(38 + index * 12)))
        bar = theme.rect(rect.x / theme.scale + 12,
                         rect.bottom / theme.scale - 36,
                         rect.width / theme.scale - 24, 9)
        progress = 0.0
        if self.hold_started is not None:
            progress = min(1.0, (time.monotonic() - self.hold_started) /
                           self.HOLD_SECONDS)
        theme.bar(shell.surface, bar, progress, c.danger)
        theme.label(shell.surface, self.confirm_label,
                    (rect.centerx / theme.scale,
                     rect.bottom / theme.scale - 22), c.danger,
                    key="micro", align="center")
        if progress >= 1.0 and not self.done:
            self.done = True
            self.close(shell)
            if self.on_confirm:
                self.on_confirm()


class GameManagementOverlay(Overlay):
    """Multi-select list for permanently deleting imported games."""

    def __init__(self, shell):
        super().__init__()
        self.title = "DELETE SELECTED GAMES"
        self.subtitle = "A toggle · Start/D delete · B back"
        self.accent = "danger"
        self.width = 380.0
        self.height = 260.0
        self.index = 0
        self.selected: set = set()
        self.items = self._candidates(shell)

    @staticmethod
    def _candidates(shell) -> List[Dict[str, Any]]:
        return [g for g in shell.games
                if g.get("type") == "game" and g.get("content_path")
                and os.path.isfile(g["content_path"])]

    def handle(self, event, shell) -> None:
        if event.type == pygame.QUIT:
            if not shell.appliance_mode:
                self.close(shell)
            return
        if event.type == pygame.KEYDOWN:
            if event.key in (pygame.K_ESCAPE, pygame.K_BACKSPACE, pygame.K_x):
                self.close(shell)
            elif event.key == pygame.K_UP:
                self.index = (self.index - 1) % max(1, len(self.items))
            elif event.key == pygame.K_DOWN:
                self.index = (self.index + 1) % max(1, len(self.items))
            elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER,
                               pygame.K_z, pygame.K_SPACE) and self.items:
                self._toggle(self.index)
            elif event.key in (pygame.K_d, pygame.K_DELETE):
                self._confirm(shell)
        elif event.type == pygame.JOYBUTTONDOWN:
            if event.button == 0 and self.items:
                self._toggle(self.index)
            elif event.button == 1:
                self.close(shell)
            elif event.button in (7, 9):
                self._confirm(shell)
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            rect = self.content_rect(shell)
            row = (event.pos[1] - rect.y) // shell.theme.px(20)
            if 0 <= row < len(self.items):
                self._toggle(row)

    def _toggle(self, index: int) -> None:
        if index in self.selected:
            self.selected.discard(index)
        else:
            self.selected.add(index)
        self.index = index

    def _confirm(self, shell) -> None:
        if not self.selected:
            shell.toast("Select at least one game", TOAST_WARN)
            return
        chosen = [self.items[i] for i in sorted(self.selected)]

        def do_delete():
            result = game_data.delete_all_games(
                chosen, library=shell.library, favorites=shell.favorites)
            logger.warning("Deleted %d selected game(s): %s",
                           result["roms"], result["errors"])
            shell.close_overlays()
            shell.rescan_library(quiet=True)
            shell.toast(f"Deleted {result['roms']} game(s)", TOAST_OK)

        shell.push_overlay(ConfirmOverlay(
            "DELETE SELECTED GAMES?",
            f"Permanently remove {len(chosen)} ROM(s) and all their cheats "
            f"and save states. This cannot be undone.",
            "DELETE", True, do_delete))

    def draw_body(self, shell) -> None:
        theme, c = shell.theme, shell.theme.colors
        rect = self.content_rect(shell)
        x0, y0 = rect.x / theme.scale, rect.y / theme.scale
        width = rect.width / theme.scale
        if not self.items:
            theme.text(shell.surface, "No imported ROMs to delete",
                       (x0 + 4, y0 + 6), "body", c.text_muted)
            return
        visible = min(len(self.items), 9)
        start = max(0, min(self.index - visible + 1, len(self.items) - visible))
        for offset, index in enumerate(range(start, start + visible)):
            item = self.items[index]
            y = y0 + offset * 20
            row = theme.rect(x0, y, width, 18)
            checked = index in self.selected
            if index == self.index:
                pygame.draw.rect(shell.surface, c.card_sel, row,
                                 border_radius=theme.px(3))
                pygame.draw.rect(shell.surface, c.danger, row, width=1,
                                 border_radius=theme.px(3))
            theme.label(shell.surface, "[X]" if checked else "[ ]",
                        (x0 + 5, y + 4), c.danger if checked else c.text_dim,
                        key="small")
            theme.label(shell.surface, truncate(theme.font("small"),
                                                item.get("title", ""),
                                                theme.px(width - 40)),
                        (x0 + 26, y + 4), c.text if index == self.index
                        else c.text_muted)
        theme.label(shell.surface,
                    f"{len(self.selected)} selected · Start/D delete (asks first)",
                    (x0 + 4, y0 + visible * 20 + 4), c.highlight, key="micro")


class UsbManagerOverlay(Overlay):
    """Manual import surface: lists block volumes and Android MTP storages."""

    def __init__(self, shell=None, volumes=None):
        super().__init__()
        self.shell = shell
        self.title = "USB / MTP IMPORT"
        self.width = 380.0
        self.height = 230.0
        self.accent = "info"
        self.subtitle = "drive or phone · A import · R rescan · Esc close"
        if volumes is not None:
            self.volumes = list(volumes)
        elif shell is not None and callable(getattr(shell, "import_sources", None)):
            self.volumes = shell.import_sources()
        else:
            # No shell/watcher: list what is already mounted. Never mount and
            # never run `jmtpfs -l` here — this can be the UI thread.
            self.volumes = list_mounted_volumes() + list_mounted_mtp_volumes()
        self.index = 0
        self.message = ""

    def set_volumes(self, volumes, message: str = "") -> None:
        self.volumes = list(volumes)
        self.index = min(self.index, max(0, len(self.volumes) - 1))
        self.message = message

    def handle(self, event, shell) -> None:
        if event.type == pygame.QUIT:
            if not shell.appliance_mode:
                self.close(shell)
            return
        if event.type == pygame.KEYDOWN:
            if event.key in (pygame.K_ESCAPE, pygame.K_BACKSPACE):
                self.close(shell)
            elif event.key == pygame.K_UP:
                self.index = max(0, self.index - 1)
            elif event.key == pygame.K_DOWN and self.volumes:
                self.index = min(len(self.volumes) - 1, self.index + 1)
            elif event.key == pygame.K_r:
                self.message = "Scanning USB and Android MTP sources..."
                if shell is not None and hasattr(shell, "refresh_import_sources"):
                    shell.refresh_import_sources(self)
                else:
                    self.volumes = list_import_sources(auto_mtp_mount=False)
                    self.message = f"{len(self.volumes)} source(s) found"
            elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_z, pygame.K_SPACE):
                if self.volumes:
                    shell.start_usb_import(self, self.volumes[self.index])
        elif event.type == pygame.JOYBUTTONDOWN:
            if event.button == 0 and self.volumes:
                shell.start_usb_import(self, self.volumes[self.index])
            elif event.button == 1:
                self.close(shell)

    def draw_body(self, shell) -> None:
        theme = shell.theme
        c = theme.colors
        rect = self.content_rect(shell)
        x0, y0 = rect.x / theme.scale, rect.y / theme.scale
        width = rect.width / theme.scale

        if not self.volumes:
            theme.text(shell.surface, "No import sources detected", (x0 + 4, y0 + 6), "body", c.text)
            notes = [
                "Plug in a pendrive, or unlock an Android",
                "phone and choose File Transfer (MTP).",
                "Press R to rescan; MTP needs jmtpfs/GVfs.",
            ]
            y = y0 + 22
            for note in notes:
                theme.label(shell.surface, note, (x0 + 4, y), c.text_muted)
                y += 11
            return

        for index, volume in enumerate(self.volumes[:8]):
            selected = index == self.index
            row = theme.rect(x0, y0 + index * 22, width, 20)
            if selected:
                pygame.draw.rect(shell.surface, c.card_sel, row, border_radius=theme.px(4))
                pygame.draw.rect(shell.surface, c.info, row, width=1, border_radius=theme.px(4))
            count = volume.rom_count()
            theme.label(shell.surface, truncate(theme.font("body_bold"), volume.label, theme.px(width - 92)),
                        (x0 + 4, y0 + index * 22 + 3), c.text, key="body_bold")
            free = human_size(volume.free) if volume.free else "MTP"
            theme.label(shell.surface, f"{count} ROM{'s' if count != 1 else ''} · {free}",
                        (x0 + 4, y0 + index * 22 + 12), c.text_muted)
            badge = getattr(volume, "kind_label", "REMOVABLE" if volume.removable else "MOUNTED")
            theme.label(shell.surface, badge,
                        (x0 + width - 4, y0 + index * 22 + 3),
                        c.highlight if getattr(volume, "is_mtp", False)
                        else c.success if volume.removable else c.text_dim, align="right")

        if self.message:
            theme.label(shell.surface, self.message, (x0 + 4, y0 + 8 * 22 + 4), c.highlight)
        theme.label(shell.surface, "[A] import ROMs from selected source", (x0 + 4, y0 + 8 * 22 + 14),
                    c.info)


class UsbPromptOverlay(Overlay):
    """Raised automatically when a stick with new (unimported) ROMs appears."""

    def __init__(self, volume):
        super().__init__()
        self.volume = volume
        self.title = ("ANDROID MTP DETECTED" if getattr(volume, "is_mtp", False)
                      else "PENDRIVE DETECTED")
        self.accent = "info"
        self.width = 320.0
        self.height = 150.0

    def handle(self, event, shell) -> None:
        if event.type == pygame.QUIT:
            if not shell.appliance_mode:
                self.close(shell)
            return
        if event.type == pygame.KEYDOWN:
            if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_z, pygame.K_SPACE):
                self.close(shell)
                shell.start_usb_import(None, self.volume)
            elif event.key in (pygame.K_ESCAPE, pygame.K_BACKSPACE, pygame.K_x):
                self.close(shell)
        elif event.type == pygame.JOYBUTTONDOWN:
            if event.button == 0:
                self.close(shell)
                shell.start_usb_import(None, self.volume)
            elif event.button == 1:
                self.close(shell)
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            rect = self.rect(shell)
            theme = shell.theme
            yes = pygame.Rect(rect.x + theme.px(14), rect.bottom - theme.px(30),
                              theme.px(120), theme.px(22))
            no = pygame.Rect(rect.right - theme.px(134), rect.bottom - theme.px(30),
                             theme.px(120), theme.px(22))
            if yes.collidepoint(event.pos):
                self.close(shell)
                shell.start_usb_import(None, self.volume)
            elif no.collidepoint(event.pos):
                self.close(shell)

    def draw_body(self, shell) -> None:
        theme = shell.theme
        c = theme.colors
        rect = self.rect(shell)
        # Report the NEW content the watcher analysed, not everything on the
        # drive: a stick with 1130 imported ROMs and 5 new ones offers 5.
        # Unanalysed volumes (discovery only) fall back to the full listing.
        items = (self.volume.new_roms if self.volume.new_roms is not None
                 else self.volume.scan_roms())
        count = len(items)
        total = sum(size for _, size in items)
        message = (f"{self.volume.label} has {count} new Game Boy ROM"
                   f"{'s' if count != 1 else ''} ({human_size(total)}).")
        font = theme.font("body")
        for index, line in enumerate(wrap_text(font, message, rect.width - theme.px(24), 2)):
            theme.text(shell.surface, line, (rect.x / theme.scale + 12,
                                             rect.y / theme.scale + 38 + index * 12), "body")
        dest = shell.usb_destination(self.volume)
        theme.label(shell.surface, truncate(theme.font("small"), f"Copy to {dest}",
                                            theme.px((rect.width / theme.scale) - 24)),
                    (rect.x / theme.scale + 12, rect.y / theme.scale + 66), c.text_dim)
        theme.button(shell.surface,
                     pygame.Rect(rect.x + theme.px(14), rect.bottom - theme.px(30),
                                 theme.px(120), theme.px(22)), "[A] IMPORT", c.info)
        theme.button(shell.surface,
                     pygame.Rect(rect.right - theme.px(134), rect.bottom - theme.px(30),
                                 theme.px(120), theme.px(22)), "[B] NOT NOW", c.text_dim, filled=False)


class UsbProgressOverlay(Overlay):
    """Progress + result for a running import (copy happens on a worker thread)."""

    def __init__(self, volume):
        super().__init__()
        self.volume = volume
        self.title = "IMPORTING ROMS"
        self.accent = "info"
        self.width = 320.0
        self.height = 120.0
        self.current = 0
        self.total = max(1, volume.rom_count())
        self.filename = "preparing..."
        self.done = False
        self.result = None
        self.error = ""

    def progress(self, index: int, total: int, name: str) -> None:
        self.current, self.total, self.filename = index, max(1, total), name

    def finish(self, result) -> None:
        self.result = result
        self.done = True
        self.title = "IMPORT COMPLETE"

    def fail(self, message: str) -> None:
        self.error = message
        self.done = True
        self.title = "IMPORT FAILED"

    def handle(self, event, shell) -> None:
        if not self.done:
            return
        if event.type in (pygame.KEYDOWN, pygame.JOYBUTTONDOWN) or \
                (event.type == pygame.MOUSEBUTTONDOWN):
            self.close(shell)

    def draw_body(self, shell) -> None:
        theme = shell.theme
        c = theme.colors
        rect = self.rect(shell)
        x0, y0 = rect.x / theme.scale + 12, rect.y / theme.scale + 36
        width = rect.width / theme.scale - 24

        if not self.done:
            theme.label(shell.surface, truncate(theme.font("small"), self.filename, theme.px(width)),
                        (x0, y0), c.text)
            bar = theme.rect(x0, y0 + 14, width, 8)
            theme.bar(shell.surface, bar, self.current / self.total, c.info)
            theme.label(shell.surface, f"{self.current} / {self.total}", (x0, y0 + 26), c.text_dim)
            return

        if self.error:
            theme.text(shell.surface, truncate(theme.font("body"), self.error, theme.px(width)),
                       (x0, y0), "body", c.danger)
        else:
            theme.text(shell.surface, self.result.summary(), (x0, y0), "body_bold", c.success)
            theme.label(shell.surface,
                        truncate(theme.font("small"), self.result.destination, theme.px(width)),
                        (x0, y0 + 14), c.text_muted)
            if self.result.failed:
                first = self.result.failed[0]
                theme.label(shell.surface, truncate(theme.font("small"),
                                                    f"{first[0]}: {first[1]}", theme.px(width)),
                            (x0, y0 + 26), c.danger)
        theme.label(shell.surface, "Press any key to continue", (x0, y0 + 42), c.text_dim)


class SettingsOverlay(Overlay):
    """
    Declarative settings page: rows may be toggles, choices, sliders, actions
    or read-only info. Everything edits the real settings store immediately.
    """

    def __init__(self, section: str, title: str, rows: List[dict]):
        super().__init__()
        self.section = section
        self.title = title
        self.rows = rows
        self.index = 0
        self.width = 340.0
        self.height = 260.0

    def handle(self, event, shell) -> None:
        if event.type == pygame.QUIT:
            if not shell.appliance_mode:
                self.close(shell)
            return
        if not self.rows:
            if event.type in (pygame.KEYDOWN, pygame.MOUSEBUTTONDOWN) or \
                    (event.type == pygame.JOYBUTTONDOWN and event.button == 1):
                self.close(shell)
            return
        if event.type == pygame.KEYDOWN:
            if event.key in (pygame.K_ESCAPE, pygame.K_BACKSPACE):
                self.close(shell)
            elif event.key == pygame.K_UP:
                self.index = (self.index - 1) % len(self.rows)
            elif event.key == pygame.K_DOWN:
                self.index = (self.index + 1) % len(self.rows)
            elif event.key == pygame.K_PAGEUP:
                self.index = (self.index - 5) % len(self.rows)
            elif event.key == pygame.K_PAGEDOWN:
                self.index = (self.index + 5) % len(self.rows)
            elif event.key == pygame.K_LEFT:
                self.adjust(shell, -1)
            elif event.key == pygame.K_RIGHT:
                self.adjust(shell, 1)
            elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_z, pygame.K_SPACE):
                self.activate(shell)
        elif event.type == pygame.JOYHATMOTION:
            dx, dy = event.value
            if dy == 1:
                self.index = (self.index - 1) % len(self.rows)
            elif dy == -1:
                self.index = (self.index + 1) % len(self.rows)
            if dx:
                self.adjust(shell, dx)
        elif event.type == pygame.JOYBUTTONDOWN:
            if event.button == 0:
                self.activate(shell)
            elif event.button == 1:
                self.close(shell)
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            rect = self.content_rect(shell)
            row = (event.pos[1] - rect.y) // shell.theme.px(24)
            if 0 <= row < len(self.rows):
                self.index = row
                self.activate(shell)

    # -- row semantics -------------------------------------------------
    def adjust(self, shell, direction: int) -> None:
        row = self.rows[self.index]
        kind = row["type"]
        if kind == "choice":
            values = row["values"]
            current = values.index(row["get"]()) if row["get"]() in values else 0
            row["set"](values[(current + direction) % len(values)])
        elif kind == "toggle":
            row["set"](not row["get"]())
        elif kind == "slider":
            step = row.get("step", 5)
            row["set"](max(row["min"], min(row["max"], row["get"]() + direction * step)))
        else:
            return
        self._after_change(shell, row)

    def activate(self, shell, _direction: int = 0) -> None:
        row = self.rows[self.index]
        kind = row["type"]
        if kind == "toggle":
            row["set"](not row["get"]())
            self._after_change(shell, row)
        elif kind == "choice":
            self.adjust(shell, 1)
        elif kind == "action":
            row["action"](shell)
        elif kind == "slider":
            self.adjust(shell, 1)

    def _after_change(self, shell, row) -> None:
        shell.on_setting_changed(row.get("key", ""))
        if row.get("after"):
            row["after"](shell)

    # -- drawing -------------------------------------------------------
    def draw_body(self, shell) -> None:
        theme = shell.theme
        c = theme.colors
        rect = self.content_rect(shell)
        x0, y0 = rect.x / theme.scale, rect.y / theme.scale
        width = rect.width / theme.scale
        row_h = 26.0
        visible = max(1, int((rect.height / theme.scale) // row_h))
        start = max(0, min(self.index - visible + 1, len(self.rows) - visible)) \
            if len(self.rows) > visible else 0

        if not self.rows:
            theme.text(shell.surface, "Nothing to configure here.", (x0 + 4, y0 + 6),
                       "body", c.text_muted)
            return

        for offset, index in enumerate(range(start, min(len(self.rows), start + visible))):
            row = self.rows[index]
            selected = index == self.index
            y = y0 + offset * row_h
            box = theme.rect(x0, y, width, row_h - 2)
            if selected:
                pygame.draw.rect(shell.surface, c.card_sel, box, border_radius=theme.px(4))
                pygame.draw.rect(shell.surface, c.primary, box, width=1, border_radius=theme.px(4))
            theme.label(shell.surface, row["label"], (x0 + 6, y + 3),
                        c.text if selected else c.text_muted, key="body")

            value = row.get("value")
            if row["type"] == "slider":
                track = theme.rect(x0 + width * 0.52, y + 6, width * 0.3, 5)
                theme.bar(shell.surface, track, (row["get"]() - row["min"]) /
                          max(1, row["max"] - row["min"]), c.info)
                value = f"{row['get']()}{row.get('suffix', '')}"
            elif row["type"] == "toggle":
                on = bool(row["get"]())
                chip = theme.rect(x0 + width - 30, y + 4, 24, 11)
                pygame.draw.rect(shell.surface, c.success if on else c.border, chip,
                                 border_radius=chip.height // 2)
                knob_r = max(2, chip.height // 2 - theme.px(1.5))
                knob_x = chip.right - knob_r - theme.px(1.5) if on \
                    else chip.x + knob_r + theme.px(1.5)
                pygame.draw.circle(shell.surface, c.text, (knob_x, chip.centery), knob_r)
                value = "On" if on else "Off"
            elif row["type"] == "choice":
                value = row.get("label_for", lambda v: str(v))(row["get"]())
            elif row["type"] == "action":
                value = ">"
            elif row["type"] == "info":
                value = row.get("value", "")

            if value is not None:
                theme.label(shell.surface, truncate(theme.font("small"), str(value),
                                                    theme.px(width * 0.44)),
                            (x0 + width - 6, y + 3),
                            c.info if row["type"] in ("toggle", "choice", "slider") else c.text,
                            align="right")
            hint = row.get("hint", "")
            if hint and selected:
                theme.label(shell.surface, truncate(theme.font("micro"), hint,
                                                    theme.px(width - 14)),
                            (x0 + 6, y + 15), c.highlight if row["type"] != "info" else c.text_dim,
                            key="micro")

        if len(self.rows) > visible:
            theme.label(shell.surface, f"{self.index + 1}/{len(self.rows)}",
                        (x0 + width, y0 + visible * row_h + 2), c.text_dim, align="right")


# =============================================================================
# The shell
# =============================================================================

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

        self.settings = settings_store.load_settings()
        self.library = Library()
        self.games: List[Dict[str, Any]] = []
        self.apps = self.library.get_built_in_apps()
        self.settings_entries = self.library.get_settings_entries()
        self.keybindings = load_keybindings()
        self.history = load_play_history()
        # Favorites are keyed by the stable content id, persisted in DATA/config.
        self.favorites = game_data.Favorites()

        self.current_tab = max(0, min(len(TABS) - 1, initial_tab))
        self.selected = [0] * len(TABS)
        self.scroll = [0] * len(TABS)
        self.filter_text = ""
        self._focus_applied = False
        self.overlays: List[Overlay] = []
        self.toast_text = ""
        self.toast_kind = TOAST_INFO
        self.toast_until = 0.0
        self.running = True
        self.scan_started_at = time.monotonic()
        self.scanner = None
        self._last_tab_before_terminal = TAB_GAMES
        self._screenshot_requested = False

        # Terminal / browser state
        self.terminal_session: Optional[TerminalSession] = None
        self.terminal_screen = TerminalScreen(80, 24)
        self.terminal_view: Optional[TerminalView] = None
        self._terminal_grid = (0, 0)
        self.browser: Optional[Browser] = None
        self._browser_loaded = False

        # Input devices
        self.joysticks: List[Any] = []
        self.axis_gate = {"y": 0.0, "x": 0.0}
        self.axis_dir = {"y": 0, "x": 0}
        self.held_keys: Dict[int, float] = {}

        # Display + theme
        self._init_display()
        self._init_theme()
        self.apply_volume_setting()

        # USB/MTP watcher (background; prompts are raised from update())
        self.usb_watcher: Optional[VolumeWatcher] = None
        self._manual_mtp_manager: Optional[MtpManager] = None
        if self.settings.get("usb_autoscan") and not screenshot_path:
            try:
                self.usb_watcher = VolumeWatcher(
                    known_ids_provider=self._known_library_ids)
                self.usb_watcher.start()
            except Exception as exc:
                logger.warning("USB watcher unavailable: %s", exc)

        if demo:
            self.games = self._demo_entries()
            self._reorder_games(preserve=False)
            self._apply_focus()
        else:
            self.scanner = self.library.start_scan()

        self.clock = pygame.time.Clock()

    # ------------------------------------------------------------ display
    def _init_display(self) -> None:
        """Creates the window and a canvas that matches it exactly."""
        if self.screenshot_path:
            size = (960, 640)
            self.window = pygame.display.set_mode(size)
        elif self.windowed:
            size = (1152, 720)
            self.window = pygame.display.set_mode(size, pygame.RESIZABLE)
        else:
            info = pygame.display.Info()
            width = info.current_w if getattr(info, "current_w", 0) > 0 else 1920
            height = info.current_h if getattr(info, "current_h", 0) > 0 else 1080
            size = (width, height)
            self.window = pygame.display.set_mode(size, pygame.FULLSCREEN)

        if not self.screenshot_path:
            pygame.display.set_caption("GBA OS")
        self.screen_w, self.screen_h = size
        self.pixel_mode = self.settings.get("scaling") == "integer"
        if self.pixel_mode:
            # Retro mode: compose at 480x320 and upscale by a whole factor.
            self.canvas_w, self.canvas_h = 480, 320
        else:
            self.canvas_w, self.canvas_h = size
        self.surface = pygame.Surface((self.canvas_w, self.canvas_h)).convert()
        self.ui_scale = 1.0 if self.pixel_mode else settings_store.ui_scale_for(self.canvas_h)
        self.design_w = self.canvas_w / self.ui_scale
        self.design_h = self.canvas_h / self.ui_scale

    def _init_theme(self) -> None:
        self.theme = Theme(self.settings.get("theme", "indigo"), self.ui_scale)
        if self.terminal_view is not None:
            self.terminal_view.theme = self.theme

    def _rebuild_surface(self) -> None:
        """Used after a resize or a scaling change."""
        self._init_display()
        self._init_theme()
        self.terminal_view = None  # rebuilt lazily with the new font metrics
        if self.browser:
            self.browser.theme = self.theme

    def refresh_joysticks(self) -> None:
        try:
            self.joysticks = [pygame.joystick.Joystick(i)
                              for i in range(pygame.joystick.get_count())]
            for joy in self.joysticks:
                joy.init()
        except Exception as exc:
            logger.warning("Joystick enumeration failed: %s", exc)
            self.joysticks = []

    # --------------------------------------------------------------- data
    def _demo_entries(self) -> List[Dict[str, Any]]:
        entries = []
        for item in DEMO_GAMES:
            entry = dict(item)
            entry.update({
                "filename": f"{item['title'].replace(' ', '_')}.{item['format'].lower()}",
                "type": "game",
                "content_path": os.path.join(get_games_dir(), f"{entry['title'].replace(' ', '_')}.{entry['format'].lower()}"),
                "handler": "demo-preview",
                "args": [],
            })
            entries.append(entry)
        now = time.time()
        self.history = {
            DEMO_GAMES[0]["id"]: {"last_played": now - 7200, "sessions": 5, "seconds": 5400},
            DEMO_GAMES[2]["id"]: {"last_played": now - 3 * 86400, "sessions": 2, "seconds": 900},
            DEMO_GAMES[7]["id"]: {"last_played": now - 40 * 86400, "sessions": 9, "seconds": 7200},
        }
        return entries

    def _reorder_games(self, preserve: bool = True,
                       target_id: Optional[str] = None) -> None:
        """Refresh favorites-first visibility, keeping the selection stable.

        `self.games` stays in canonical library order (scan order); the
        favorites-first ordering is a *stable partition* computed in
        `visible_games()`.  Sorting the canonical list itself would make the
        result depend on the order in which games were favorited and would
        stop an unfavorited game from returning to its original position.
        """
        items = self.visible_games()
        if preserve and target_id is None:
            index = self.selected[TAB_GAMES]
            if 0 <= index < len(items):
                target_id = items[index]["id"]
        if target_id:
            for position, game in enumerate(items):
                if game["id"] == target_id:
                    self.selected[TAB_GAMES] = position
                    return
        self._clamp()

    def toggle_favorite(self, item: Optional[Dict[str, Any]] = None) -> bool:
        """Favorite/unfavorite one game immediately, keeping it selected."""
        current = self.current_item() if self.current_tab == TAB_GAMES else None
        item = item or current
        if not item or item.get("type") != "game" or self.current_tab != TAB_GAMES:
            return False
        selected_id = current.get("id") if current else None
        is_favorite = self.favorites.toggle(item.get("id", ""))
        self._reorder_games(target_id=selected_id)
        self.toast(("★ Favorite: " if is_favorite else "Unfavorited: ") +
                   item.get("title", ""), TOAST_OK if is_favorite else TOAST_INFO)
        return is_favorite

    def visible_games(self) -> List[Dict[str, Any]]:
        games = self.favorites.order(self.games)
        if not self.filter_text:
            return games
        needle = self.filter_text.lower()
        return [g for g in games
                if needle in g.get("title", "").lower() or needle in g.get("filename", "").lower()]

    def items_for_tab(self, tab: Optional[int] = None) -> List[Dict[str, Any]]:
        tab = self.current_tab if tab is None else tab
        if tab == TAB_GAMES:
            return self.visible_games()
        if tab == TAB_APPS:
            return self.apps
        if tab == TAB_SETTINGS:
            return self.settings_entries
        return []

    def current_items(self) -> List[Dict[str, Any]]:
        return self.items_for_tab()

    def current_item(self) -> Optional[Dict[str, Any]]:
        items = self.current_items()
        index = self.selected[self.current_tab]
        return items[index] if 0 <= index < len(items) else None

    def save_exists(self, entry: Dict[str, Any]) -> bool:
        if entry.get("demo_save"):
            return True
        try:
            save_dir = os.path.join(get_data_dir(), "saves", entry["id"])
            return os.path.isdir(save_dir) and bool(os.listdir(save_dir))
        except Exception:
            return False

    def save_summary(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        if entry.get("demo_save"):
            return {"files": 1, "bytes": 8192, "age": 7200,
                    "path": os.path.join(get_data_dir(), "saves", entry.get("id", "demo"))}
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

    # ------------------------------------------------------- notifications
    def toast(self, text: str, kind: str = TOAST_INFO, seconds: float = 2.6) -> None:
        self.toast_text = text
        self.toast_kind = kind
        self.toast_until = time.monotonic() + seconds
        logger.info("Toast: %s", text)

    def _toast_color(self):
        c = self.theme.colors
        return {"ok": c.success, "warn": c.highlight, "error": c.danger}.get(self.toast_kind, c.info)

    # ------------------------------------------------------------ overlays
    def push_overlay(self, overlay: Overlay) -> None:
        self.overlays.append(overlay)

    def pop_overlay(self, overlay: Overlay) -> None:
        if overlay in self.overlays:
            self.overlays.remove(overlay)

    def close_overlays(self) -> None:
        self.overlays.clear()

    @property
    def overlay(self) -> Optional[Overlay]:
        return self.overlays[-1] if self.overlays else None

    def dim_background(self) -> None:
        shade = pygame.Surface(self.surface.get_size(), pygame.SRCALPHA)
        shade.fill((6, 4, 14, 205))
        self.surface.blit(shade, (0, 0))

    # ------------------------------------------------------------- update
    def update(self) -> None:
        if self.scanner is not None and self.scanner.done:
            self.games = self.scanner.entries
            self._reorder_games(preserve=False)
            self.scanner = None
            if not self._focus_applied:
                self._apply_focus()
            self._clamp()
            if self.games:
                logger.info("Library ready: %d entries in %.2fs",
                            len(self.games), self.library.last_scan_seconds)

        self._poll_usb()
        self._poll_terminal()
        self._poll_key_repeat()

        if self.toast_text and time.monotonic() > self.toast_until:
            self.toast_text = ""

    def _apply_focus(self) -> None:
        items = self.visible_games()      # selection indexes the sorted shelf
        if not items:
            return
        self._focus_applied = True
        target = self.focus_id
        if not target and self.history:
            playable = {g["id"] for g in items}
            for entry_id, _record in sorted(self.history.items(),
                                            key=lambda kv: kv[1].get("last_played", 0),
                                            reverse=True):
                if entry_id in playable:
                    target = entry_id
                    break
        if target:
            for index, game in enumerate(items):
                if game["id"] == target:
                    # Bring the shelf back to this entry, but leave an explicit
                    # initial tab (e.g. --tab browser) alone.
                    self.selected[TAB_GAMES] = index
                    return

    def _clamp(self) -> None:
        items = self.current_items()
        if not items:
            self.selected[self.current_tab] = 0
            return
        self.selected[self.current_tab] = max(0, min(len(items) - 1, self.selected[self.current_tab]))

    def _known_library_ids(self) -> set:
        """
        Content ids the library already knows — the durable "already
        imported" set the USB watcher measures new sticks against. Read from
        the on-disk library cache, so it survives game returns, launcher
        restarts and reboots (the launcher process is restarted by the
        supervisor after every game session).
        """
        try:
            return self.library.known_content_ids()
        except Exception as exc:
            logger.warning("Library identity unavailable for USB scan: %s", exc)
            return set()

    def usb_source_index(self):
        """
        The watcher's shared content-id index for removable media, or None
        when no watcher is running (imports then hash sources directly —
        still correct, just uncached).
        """
        getter = getattr(self.usb_watcher, "get_source_index", None)
        if getter is None:
            return None
        try:
            return getter()
        except Exception:
            return None

    def import_sources(self) -> List[Any]:
        """
        Sources for the manual import overlay, without mounting on the UI thread.

        The watcher already polls MTP in the background; use its last snapshot
        when available. Otherwise only already-mounted FUSE paths are listed.
        """
        watcher = self.usb_watcher
        if watcher is not None and watcher.volumes:
            return list(watcher.volumes)
        try:
            block = list_mounted_volumes()
        except Exception as exc:
            logger.warning("Could not list block import sources: %s", exc)
            block = []
        manager = getattr(watcher, "mtp_manager", None) if watcher is not None else None
        mtp = self._cached_mtp_volumes(manager)
        if not mtp:
            try:
                # Already-mounted MTP/GVfs roots only: listing must never run
                # `jmtpfs -l` or a mount attempt on the UI thread (a locked
                # phone would freeze the shell for seconds). Mounting is the
                # watcher's job; `R` triggers an explicit rescan.
                mtp = list_mounted_mtp_volumes()
                if manager is not None and getattr(manager, "cached_volumes", None):
                    merged = {v.key: v for v in mtp}
                    for volume in self._cached_mtp_volumes(manager):
                        merged.setdefault(volume.key, volume)
                    mtp = list(merged.values())
            except Exception as exc:
                logger.warning("Could not list mounted MTP sources: %s", exc)
                mtp = []
        seen = {v.key for v in block}
        paths = {os.path.realpath(v.mountpoint) for v in block}
        for volume in mtp:
            path = os.path.realpath(volume.mountpoint)
            if volume.key in seen or path in paths:
                continue
            block.append(volume)
            seen.add(volume.key)
            paths.add(path)
        return block

    @staticmethod
    def _cached_mtp_volumes(manager) -> List[Any]:
        getter = getattr(manager, "cached_volumes", None)
        if callable(getter):
            try:
                return list(getter())
            except Exception:
                pass
        return []

    @staticmethod
    def _mtp_status(manager) -> str:
        getter = getattr(manager, "status", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                pass
        return ""

    def refresh_import_sources(self, overlay=None) -> None:
        """
        Explicit R/rescan. MTP mounting and libmtp probing run on a worker so
        an unlocked-but-slow phone cannot freeze the shell (Rule L4).
        """
        import threading

        manager = None
        if self.usb_watcher is not None:
            watcher_refresh = getattr(self.usb_watcher, "refresh", None)
            if callable(watcher_refresh):
                watcher_refresh()
            getter = getattr(self.usb_watcher, "get_mtp_manager", None)
            manager = getter() if callable(getter) else getattr(
                self.usb_watcher, "mtp_manager", None)
        if manager is None:
            if self._manual_mtp_manager is None:
                self._manual_mtp_manager = MtpManager(auto_mount=True)
            else:
                self._manual_mtp_manager.refresh()
            manager = self._manual_mtp_manager

        def worker():
            try:
                volumes = list_import_sources(mtp_manager=manager,
                                              auto_mtp_mount=True)
            except Exception as exc:
                logger.warning("Manual MTP rescan failed: %s", exc)
                volumes = list_mounted_volumes() + self._cached_mtp_volumes(manager)
            hint = self._mtp_status(manager)
            message = f"{len(volumes)} source(s) found"
            if not volumes and hint:
                message = hint
            setter = getattr(overlay, "set_volumes", None)
            if callable(setter):
                setter(volumes, message)

        threading.Thread(target=worker, daemon=True, name="gba-import-source-scan").start()

    def _poll_usb(self) -> None:
        if not self.usb_watcher:
            return
        # Never stack import dialogs: one prompt at a time (F2: the user must
        # answer before anything is copied).
        if any(isinstance(o, (UsbPromptOverlay, UsbProgressOverlay)) for o in self.overlays):
            return
        for volume in self.usb_watcher.take_pending():
            new_count = volume.new_rom_count()
            if new_count <= 0:
                # Everything on this stick is already registered in the
                # library: discovery alone must never re-prompt.
                continue
            logger.info("Import source detected: %s %s (%d new of %d ROMs)",
                        "MTP" if getattr(volume, "is_mtp", False) else "USB",
                        volume.mountpoint, new_count, volume.rom_count())
            self.push_overlay(UsbPromptOverlay(volume))
            break

    def usb_status_text(self) -> str:
        if self.usb_watcher and self.usb_watcher.volumes:
            volume = self.usb_watcher.volumes[0]
            prefix = "MTP" if getattr(volume, "is_mtp", False) else "USB"
            return f"{prefix} {volume.label} ({volume.rom_count()} ROMs)"
        return "none"

    def usb_destination(self, volume) -> str:
        folder = "".join(c if (c.isalnum() or c in " -_.") else "_" for c in volume.label).strip()
        folder = folder or "USB"
        if self.settings.get("usb_copy_subfolders", True):
            return os.path.join(get_games_dir(), folder)
        return get_games_dir()

    def start_usb_import(self, source_overlay: Optional[Overlay], volume) -> None:
        """Runs an import on a worker thread with a progress overlay."""
        import threading

        progress = UsbProgressOverlay(volume)
        self.push_overlay(progress)

        def worker():
            try:
                known_ids = self.library.known_content_ids()
                index = self.usb_source_index() if known_ids else None
                result = import_roms(volume, get_games_dir(),
                                     subfolder=self.settings.get("usb_copy_subfolders", True),
                                     progress=progress.progress,
                                     known_ids=known_ids, index=index)
                if result.registered:
                    # Record the imported ids in the library cache right now:
                    # the "already imported" state becomes durable without
                    # waiting for the rescan to re-hash every copy.
                    self.library.register_content(result.registered)
                progress.finish(result)
                logger.info("USB import: %s", result.summary())
                self.rescan_library(quiet=True)
            except Exception as exc:
                progress.fail(str(exc))
                logger.error("USB import failed: %s", exc)

        threading.Thread(target=worker, daemon=True, name="gba-usb-import").start()

    def _poll_terminal(self) -> None:
        if self.terminal_session is None:
            return
        data = self.terminal_session.drain()
        if data:
            self.terminal_screen.feed(data)

    # -------------------------------------------------------------- input
    def navigate(self, delta: int) -> None:
        items = self.current_items()
        if not items:
            return
        self.selected[self.current_tab] = max(0, min(len(items) - 1,
                                                     self.selected[self.current_tab] + delta))

    def move_grid(self, dx: int, dy: int) -> None:
        if self.settings.get("view_mode") != "grid" or self.current_tab == TAB_SETTINGS:
            self.navigate(dy if dy else dx)
            return
        items = self.current_items()
        if not items:
            return
        cols = max(1, self._grid_columns())
        index = self.selected[self.current_tab]
        if dx:
            index += dx
        elif dy:
            index += dy * cols
        self.selected[self.current_tab] = max(0, min(len(items) - 1, index))

    def switch_tab(self, delta: int) -> None:
        if delta < 0:
            self.current_tab = (self.current_tab - 1) % len(TABS)
        else:
            self.current_tab = (self.current_tab + 1) % len(TABS)
        self.on_tab_activated()

    def goto_tab(self, tab: int) -> None:
        self.current_tab = max(0, min(len(TABS) - 1, tab))
        self.on_tab_activated()

    def on_tab_activated(self) -> None:
        self._clamp()
        if self.current_tab == TAB_TERMINAL:
            self.ensure_terminal()
        elif self.current_tab == TAB_BROWSER:
            self.ensure_browser()

    def _handle_global(self, event) -> bool:
        """Shortcuts that work from anywhere, including inside overlays."""
        if event.type == pygame.QUIT:
            if not self.appliance_mode:
                self.quit()
            return True
        if event.type == pygame.JOYDEVICEADDED:
            self.refresh_joysticks()
            self.toast(f"Gamepad connected ({len(self.joysticks)})", TOAST_OK)
            return True
        if event.type == pygame.JOYDEVICEREMOVED:
            self.refresh_joysticks()
            self.toast("Gamepad disconnected", TOAST_WARN)
            return True
        if event.type == pygame.VIDEORESIZE and self.windowed and not self.screenshot_path:
            self._rebuild_surface()
            return True
        if event.type != pygame.KEYDOWN:
            return False

        mod = getattr(event, "mod", 0)
        ctrl = bool(mod & pygame.KMOD_CTRL)
        alt = bool(mod & pygame.KMOD_ALT)
        shift = bool(mod & pygame.KMOD_SHIFT)

        # Ctrl+Alt+T: terminal from anywhere.
        if ctrl and alt and event.key == pygame.K_t:
            self.close_overlays()
            if self.current_tab == TAB_TERMINAL:
                self.goto_tab(self._last_tab_before_terminal)
            else:
                self._last_tab_before_terminal = self.current_tab
                self.goto_tab(TAB_TERMINAL)
            return True

        # Alt+Tab / Alt+Shift+Tab: cycle the main tabs (ISO_Left_Tab is what
        # X11 reports for Shift+Tab, and it is absent on some pygame builds).
        if alt and event.key in (pygame.K_TAB, getattr(pygame, "K_ISO_Left_Tab", -1)):
            self.close_overlays()
            if event.key == pygame.K_TAB and not shift:
                self.switch_tab(1)
            else:
                self.switch_tab(-1)
            return True

        # F1: context help.
        if event.key == pygame.K_F1:
            self.push_overlay(TextOverlay("KEYBOARD & CONTROLS", self.help_lines(),
                                          "F1 / Esc to close"))
            return True

        # F12: screenshot into DATA/screenshots (handy for bug reports).
        if event.key == pygame.K_F12:
            self.save_screenshot()
            return True

        # L on the library after a crash opens the log.
        if (event.key == pygame.K_l and not self.overlays
                and self.current_tab == TAB_SETTINGS
                and self.last_outcome not in (None, "clean")):
            self.show_logs()
            return True
        return False

    def handle_events(self) -> None:
        for event in pygame.event.get():
            if self._handle_global(event):
                continue
            if self.overlay is not None:
                self.overlay.handle(event, self)
                continue
            self._handle_tab_event(event)
        self._poll_analog()

    def _handle_tab_event(self, event) -> None:
        if self.current_tab == TAB_TERMINAL:
            self._terminal_event(event)
            return
        if self.current_tab == TAB_BROWSER:
            self._browser_event(event)
            return

        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            self._handle_click(event.pos)
        elif event.type == pygame.KEYDOWN:
            self._handle_key(event)
        elif event.type == pygame.JOYHATMOTION:
            dx, dy = event.value
            if dy:
                self.move_grid(0, -dy)
            if dx:
                self.move_grid(dx, 0)
        elif event.type == pygame.JOYBUTTONDOWN:
            if event.button == 0:
                self.activate_current()
            elif event.button == 1:
                self.back()
            elif event.button == 2 and self.current_tab == TAB_GAMES:
                self.toggle_favorite()
            elif event.button in (7, 9):
                self.switch_tab(1)
            elif event.button in (6, 8):
                self.toast("SELECT: search is keyboard-only", TOAST_INFO)

    def _handle_key(self, event) -> None:
        key = event.key
        mod = getattr(event, "mod", 0)
        if key == pygame.K_f and (mod & pygame.KMOD_CTRL) and \
                self.current_tab == TAB_GAMES:
            self.toggle_favorite()
            return
        if key == pygame.K_TAB:
            self.switch_tab(-1 if mod & pygame.KMOD_SHIFT else 1)
        elif key == pygame.K_RIGHT:
            self.move_grid(1, 0)
        elif key == pygame.K_LEFT:
            self.move_grid(-1, 0)
        elif key == pygame.K_UP:
            self.move_grid(0, -1)
        elif key == pygame.K_DOWN:
            self.move_grid(0, 1)
        elif key == pygame.K_PAGEUP:
            self.navigate(-5)
        elif key == pygame.K_PAGEDOWN:
            self.navigate(5)
        elif key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_z, pygame.K_SPACE):
            self.activate_current()
        elif key == pygame.K_ESCAPE:
            self.back()
        elif key == pygame.K_BACKSPACE:
            if self.filter_text:
                self.filter_text = self.filter_text[:-1]
                self.selected[TAB_GAMES] = 0
            else:
                self.back()
        elif key == pygame.K_v and (mod & pygame.KMOD_CTRL):
            mode = "list" if self.settings.get("view_mode") == "grid" else "grid"
            self.set_setting("view_mode", mode)
            self.toast(f"{mode.title()} view", TOAST_INFO)
        elif self.current_tab == TAB_GAMES:
            self._search_key(event)

    def _search_key(self, event) -> None:
        try:
            char = pygame.key.name(event.key)
        except Exception:
            return
        if len(char) == 1 and (char.isalnum() or char in "-_."):
            self.filter_text += char.lower()
            self.selected[TAB_GAMES] = 0
            self.scroll[TAB_GAMES] = 0

    def back(self) -> None:
        if self.filter_text:
            self.filter_text = ""
            self.selected[TAB_GAMES] = 0
            return
        if self.current_tab != TAB_GAMES:
            self.goto_tab(TAB_GAMES)
            return
        if not self.appliance_mode:
            self.quit()
        else:
            self.toast("Already on the games shelf", TOAST_INFO)

    def _handle_click(self, pos) -> None:
        theme = self.theme
        for index, rect in enumerate(self.tab_rects()):
            if rect.collidepoint(pos):
                self.goto_tab(index)
                return
        items = self.current_items()
        if not items:
            return
        if self.settings.get("view_mode") == "grid" and self.current_tab != TAB_SETTINGS:
            columns, tile_w, tile_h, gap, origin_x, origin_y = self._grid_metrics()
            for index, item in enumerate(items):
                col, row = index % columns, index // columns
                cell = pygame.Rect(origin_x + col * (tile_w + gap),
                                   origin_y + row * (tile_h + gap), tile_w, tile_h)
                if cell.collidepoint(pos):
                    if self.selected[self.current_tab] == index:
                        self.activate_current()
                    else:
                        self.selected[self.current_tab] = index
                    return
        else:
            rect = self._list_rect()
            row_h = theme.px(13 if self.current_tab == TAB_SETTINGS else 12)
            if rect.collidepoint(pos):
                row = (pos[1] - rect.y) // max(1, row_h)
                if 0 <= row < len(items):
                    if self.selected[self.current_tab] == row:
                        self.activate_current()
                    else:
                        self.selected[self.current_tab] = row

    def _poll_analog(self) -> None:
        if not self.joysticks or self.overlay is not None:
            return
        if self.current_tab in (TAB_TERMINAL, TAB_BROWSER):
            return
        try:
            joy = self.joysticks[0]
            x_axis = joy.get_axis(0) if joy.get_numaxes() > 0 else 0.0
            y_axis = joy.get_axis(1) if joy.get_numaxes() > 1 else 0.0
        except Exception:
            return
        deadzone = 0.65
        now = time.monotonic()
        for axis, value in (("y", y_axis), ("x", x_axis)):
            direction = 0
            if value <= -deadzone:
                direction = -1
            elif value >= deadzone:
                direction = 1
            if direction == 0:
                self.axis_gate[axis] = 0.0
                self.axis_dir[axis] = 0
                continue
            if self.axis_dir[axis] != direction:
                self.axis_dir[axis] = direction
                self.axis_gate[axis] = now + 0.28
                self.move_grid(direction if axis == "x" else 0, direction if axis == "y" else 0)
            elif now >= self.axis_gate[axis]:
                self.axis_gate[axis] = now + 0.10
                self.move_grid(direction if axis == "x" else 0, direction if axis == "y" else 0)

    def _poll_key_repeat(self) -> None:
        if not self.held_keys:
            return
        now = time.monotonic()
        for key, next_time in list(self.held_keys.items()):
            if now < next_time:
                continue
            self.held_keys[key] = now + 0.08
            if key == pygame.K_UP:
                self.move_grid(0, -1)
            elif key == pygame.K_DOWN:
                self.move_grid(0, 1)
            elif key == pygame.K_LEFT:
                self.move_grid(-1, 0)
            elif key == pygame.K_RIGHT:
                self.move_grid(1, 0)

    # ------------------------------------------------------------ terminal
    def ensure_terminal(self) -> None:
        if self.terminal_session is None or not self.terminal_session.alive:
            self.terminal_session = TerminalSession(cols=80, rows=24)
            if self.terminal_session.error:
                self.toast(f"Terminal unavailable: {self.terminal_session.error}", TOAST_ERROR)
        if self.terminal_view is None:
            self.terminal_view = TerminalView(self.terminal_screen, self.theme, 8,
                                              "starting shell…")

    def _terminal_event(self, event) -> None:
        self.ensure_terminal()
        if event.type == pygame.KEYDOWN:
            self.terminal_session.write(key_to_bytes(event))
        elif event.type == pygame.MOUSEBUTTONDOWN:
            if event.button == 4:
                return
            self.terminal_session.write(b"\x0c")
        elif event.type == pygame.JOYBUTTONDOWN and event.button == 1:
            self.goto_tab(TAB_GAMES)

    # ------------------------------------------------------------- browser
    def ensure_browser(self) -> None:
        if self.browser is None:
            width = self.theme.px(self.design_w - 24)
            self.browser = Browser(self.theme, width,
                                   home=self.settings.get("browser_home", "about:welcome"))
        if not self._browser_loaded:
            self._browser_loaded = True
            self.browser.load("about:welcome")

    def _browser_event(self, event) -> None:
        self.ensure_browser()
        browser = self.browser
        if event.type == pygame.MOUSEBUTTONDOWN:
            if event.button == 4:
                browser.scroll_by(-3)
            elif event.button == 5:
                browser.scroll_by(3)
            return
        if event.type == pygame.JOYHATMOTION:
            browser.scroll_by(-event.value[1] * 3)
            return
        if event.type != pygame.KEYDOWN:
            return

        key = event.key
        mod = getattr(event, "mod", 0)
        alt = bool(mod & pygame.KMOD_ALT)
        ctrl = bool(mod & pygame.KMOD_CTRL)

        if browser.editing_url:
            if key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                browser.commit_url_entry()
            elif key == pygame.K_ESCAPE:
                browser.editing_url = False
            elif key == pygame.K_BACKSPACE:
                browser.address_buffer = browser.address_buffer[:-1]
            else:
                char = getattr(event, "unicode", "")
                if char and char.isprintable():
                    browser.address_buffer += char
            return

        if ctrl and key == pygame.K_l:
            browser.begin_url_entry()
        elif alt and key == pygame.K_LEFT:
            browser.back()
        elif alt and key == pygame.K_RIGHT:
            browser.forward()
        elif alt and key == pygame.K_TAB:
            self.switch_tab(-1 if mod & pygame.KMOD_SHIFT else 1)
        elif key == pygame.K_UP:
            browser.scroll_by(-1)
        elif key == pygame.K_DOWN:
            browser.scroll_by(1)
        elif key == pygame.K_PAGEUP:
            browser.scroll_by(-12)
        elif key == pygame.K_PAGEDOWN:
            browser.scroll_by(12)
        elif key == pygame.K_HOME:
            browser.scroll = 0
        elif key == pygame.K_END:
            browser.scroll = browser.max_scroll()
        elif key == pygame.K_r:
            browser.reload()
        elif key == pygame.K_h:
            browser.load(self.settings.get("browser_home", "about:welcome"))
        elif key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            if 0 <= browser.selected_link < len(browser.document.links if browser.document else []):
                browser.follow(browser.document.links[browser.selected_link])
        elif key == pygame.K_TAB:
            total = len(browser.document.links) if browser.document else 0
            if total:
                browser.selected_link = (browser.selected_link +
                                         (-1 if mod & pygame.KMOD_SHIFT else 1)) % total
                self._scroll_to_link()
        elif pygame.K_1 <= key <= pygame.K_9:
            index = key - pygame.K_1
            if browser.document and index < len(browser.document.links):
                browser.follow(browser.document.links[index])
        elif key == pygame.K_ESCAPE:
            self.goto_tab(TAB_GAMES)

    def _scroll_to_link(self) -> None:
        browser = self.browser
        if not browser or not browser.document:
            return
        href = browser.document.links[browser.selected_link] \
            if browser.selected_link < len(browser.document.links) else None
        if not href:
            return
        for index, line in enumerate(browser.document.lines):
            if line.href == href:
                browser.scroll = max(0, index - 2)
                return

    # ------------------------------------------------------------- actions
    def activate_current(self) -> None:
        item = self.current_item()
        if not item:
            return
        handler = item.get("handler", "")
        logger.info("Activate: tab=%s item=%s handler=%s",
                    TABS[self.current_tab], item.get("title"), handler)
        if item.get("type") == "game":
            self.request_launch(item)
            return

        actions = {
            "internal_system_monitor": lambda: self.push_overlay(MonitorOverlay()),
            "internal_controller_tester": lambda: self.push_overlay(InputDiagOverlay(self)),
            "internal_save_manager": lambda: self.push_overlay(SaveManagerOverlay()),
            "internal_usb_import": lambda: self.push_overlay(UsbManagerOverlay(self)),
            "internal_browser": lambda: self.goto_tab(TAB_BROWSER),
            "internal_terminal": lambda: self.goto_tab(TAB_TERMINAL),
            "internal_settings_display": lambda: self.open_settings_page("display"),
            "internal_settings_audio": lambda: self.open_settings_page("audio"),
            "internal_settings_controls": lambda: self.open_settings_page("controls"),
            "internal_settings_library": lambda: self.open_settings_page("library"),
            "internal_settings_games": lambda: self.open_settings_page("games"),
            "internal_settings_storage": lambda: self.open_settings_page("storage"),
            "internal_settings_system": lambda: self.open_settings_page("system"),
            "internal_reboot": lambda: self.confirm_system("Reboot to the GRUB menu?",
                                                           EXIT_CODE_REBOOT),
            "internal_poweroff": lambda: self.confirm_system("Power off the machine?",
                                                             EXIT_CODE_POWEROFF),
        }
        action = actions.get(handler)
        if action:
            action()
        else:
            self.toast(f"No handler for {handler}", TOAST_WARN)

    def request_launch(self, item: Dict[str, Any]) -> None:
        if item.get("handler") == "demo-preview":
            self.toast("Demo preview — no ROM installed", TOAST_WARN)
            return
        if not os.path.exists(item.get("content_path", "")):
            self.toast("ROM file is missing", TOAST_ERROR)
            return
        if self.settings.get("confirm_launch", True):
            self.push_overlay(LaunchOverlay(item))
        else:
            self.launch_game(item)

    def launch_game(self, item: Dict[str, Any]) -> None:
        req_path = os.path.join(get_run_dir(), "launch_request.json")
        payload = {
            "entry_id": item["id"],
            "content_path": item["content_path"],
            "handler": item.get("handler", "mgba-adapter"),
            "title": item.get("title", ""),
        }
        try:
            tmp = req_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, req_path)
        except Exception as exc:
            logger.error("Could not write launch request: %s", exc)
            self.toast("Launch failed: run directory not writable", TOAST_ERROR)
            return

        record_play_session(item["id"])
        logger.info("Launching %s (%s)", item.get("title"), item.get("id"))

        # Hand the display over cleanly: a half-torn-down KMS/DRM context is
        # one of the classic causes of the emulator showing a black screen.
        self.release_display()
        sys.exit(EXIT_CODE_LAUNCH_REQUEST)

    def release_display(self) -> None:
        try:
            if self.terminal_session:
                self.terminal_session.close()
        except Exception:
            pass
        try:
            pygame.display.quit()
            pygame.quit()
        except Exception:
            pass

    def confirm_system(self, message: str, code: int) -> None:
        def do_it():
            self.release_display()
            sys.exit(code)

        self.push_overlay(ConfirmOverlay("SYSTEM", message, "CONFIRM", False, do_it))

    # ------------------------------------------------------------- settings
    def set_setting(self, key: str, value) -> None:
        self.settings[key] = value
        settings_store.save_settings(self.settings)

    def on_setting_changed(self, key: str) -> None:
        if key in ("theme", "scaling"):
            self._rebuild_surface()
        elif key == "volume" or key == "muted":
            self.apply_volume_setting()
        elif key == "usb_autoscan":
            self.configure_usb_watcher()
        elif key == "brightness":
            settings_store.set_brightness(self.settings.get("brightness", 100))

    def apply_volume_setting(self) -> None:
        settings_store.apply_volume(self.settings.get("volume", 85),
                                    self.settings.get("muted", False))

    def configure_usb_watcher(self) -> None:
        enabled = self.settings.get("usb_autoscan", True)
        if enabled and self.usb_watcher is None:
            self.usb_watcher = VolumeWatcher(
                known_ids_provider=self._known_library_ids)
            self.usb_watcher.start()
        elif not enabled and self.usb_watcher is not None:
            self.usb_watcher.stop()
            self.usb_watcher = None

    def open_settings_page(self, section: str) -> None:
        builders = {
            "display": self._rows_display,
            "audio": self._rows_audio,
            "controls": self._rows_controls,
            "library": self._rows_library,
            "games": self._rows_games,
            "storage": self._rows_storage,
            "system": self._rows_system,
        }
        titles = {
            "display": "DISPLAY", "audio": "AUDIO", "controls": "CONTROLS",
            "library": "LIBRARY", "games": "GAME MANAGEMENT",
            "storage": "STORAGE & SAVES", "system": "SYSTEM",
        }
        builder = builders.get(section)
        if builder:
            self.push_overlay(SettingsOverlay(section, titles[section], builder()))

    # -- settings rows -------------------------------------------------
    def _rows_display(self) -> List[dict]:
        s = self.settings
        rows = [
            {"type": "choice", "label": "Scaling",
             "hint": "Fill runs at native resolution; integer keeps chunky 480x320 pixels.",
             "key": "scaling", "values": ["fit", "integer"],
             "get": lambda: s["scaling"], "set": lambda v: self.set_setting("scaling", v),
             "label_for": lambda v: {"fit": "Fill screen", "integer": "Integer (retro)"}[v]},
            {"type": "choice", "label": "Theme",
             "hint": "Shell colour scheme. Changes apply instantly.",
             "key": "theme", "values": list(THEME_ORDER),
             "get": lambda: s["theme"], "set": lambda v: self.set_setting("theme", v),
             "label_for": lambda v: v.title()},
            {"type": "toggle", "label": "Show clock",
             "hint": "Clock in the top bar.",
             "key": "show_clock", "get": lambda: s["show_clock"],
             "set": lambda v: self.set_setting("show_clock", v)},
            {"type": "toggle", "label": "24-hour clock",
             "hint": "Use 24h instead of 12h time.",
             "key": "clock_24h", "get": lambda: s["clock_24h"],
             "set": lambda v: self.set_setting("clock_24h", v)},
        ]
        brightness = settings_store.get_brightness()
        if brightness is not None:
            rows.append({
                "type": "slider", "label": "Brightness", "min": 10, "max": 100, "step": 5,
                "suffix": "%", "hint": "Panel backlight level.",
                "key": "brightness", "get": lambda: s.get("brightness", brightness),
                "set": lambda v: (self.set_setting("brightness", v),
                                  settings_store.set_brightness(v)),
            })
        else:
            rows.append({"type": "info", "label": "Brightness",
                         "value": "not supported",
                         "hint": "No /sys/class/backlight device on this machine."})
        rows.append({"type": "action", "label": "Test screen",
                     "hint": "Draws colour bars for 2 seconds.",
                     "action": lambda shell: shell.test_pattern()})
        return rows

    def _rows_audio(self) -> List[dict]:
        s = self.settings
        return [
            {"type": "slider", "label": "Volume", "min": 0, "max": 100, "step": 5,
             "suffix": "%", "hint": "Master output level (ALSA).",
             "key": "volume", "get": lambda: s["volume"],
             "set": lambda v: self.set_setting("volume", v)},
            {"type": "toggle", "label": "Mute", "hint": "Silence all output.",
             "key": "muted", "get": lambda: s["muted"],
             "set": lambda v: self.set_setting("muted", v)},
            {"type": "info", "label": "Mixer",
             "value": "ALSA ready" if settings_store.audio_available() else "not found",
             "hint": "Install alsa-utils for hardware volume control."},
            {"type": "action", "label": "Re-apply volume",
             "hint": "Push the current level to the hardware mixer.",
             "action": lambda shell: (shell.apply_volume_setting(),
                                      shell.toast("Volume applied", TOAST_OK))},
        ]

    def _rows_controls(self) -> List[dict]:
        s = self.settings
        return [
            {"type": "action", "label": "Remap buttons",
             "hint": "Rebind all ten GBA buttons for shell and emulator.",
             "action": lambda shell: shell.push_overlay(KeyRemapOverlay(shell))},
            {"type": "action", "label": "Input diagnostics",
             "hint": "Check gamepads, axes and the active mapping.",
             "action": lambda shell: shell.push_overlay(InputDiagOverlay(shell))},
            {"type": "info", "label": "Profile",
             "value": f"{len(GBA_BUTTON_ORDER)} buttons",
             "hint": "One profile shared by the shell and mGBA (rule I3)."},
            {"type": "action", "label": "Reset bindings",
             "hint": "Restore the shipped defaults.",
             "action": lambda shell: shell.reset_keybindings()},
            {"type": "action", "label": "In-game shortcuts",
             "hint": "Configure cheats, fast-forward and save-state combos.",
             "action": lambda shell: shell.push_overlay(ShortcutOverlay(shell))},
            {"type": "info", "label": "Cheats overlay",
             "value": settings_store.shortcut_label(s["shortcut_cheats"]),
             "hint": "Open the per-game cheats overlay while playing."},
            {"type": "info", "label": "Fast-forward cycle",
             "value": settings_store.shortcut_label(s["shortcut_fast_forward"]),
             "hint": "1x → 2x → 3x → 1x, one step per press."},
            {"type": "info", "label": "Save states overlay",
             "value": settings_store.shortcut_label(s["shortcut_save_states"]),
             "hint": "Nine per-game save-state slots with thumbnails."},
        ]

    def _rows_library(self) -> List[dict]:
        s = self.settings
        return [
            {"type": "choice", "label": "View", "values": ["grid", "list"],
             "hint": "Cartridge shelf or a compact list.",
             "key": "view_mode", "get": lambda: s["view_mode"],
             "set": lambda v: self.set_setting("view_mode", v),
             "label_for": lambda v: v.title()},
            {"type": "toggle", "label": "USB / MTP auto-detect",
             "hint": "Offer to import ROMs from a pendrive or Android phone.",
             "key": "usb_autoscan", "get": lambda: s["usb_autoscan"],
             "set": lambda v: self.set_setting("usb_autoscan", v)},
            {"type": "toggle", "label": "Import into subfolder",
             "hint": "Group imported ROMs by drive label.",
             "key": "usb_copy_subfolders", "get": lambda: s["usb_copy_subfolders"],
             "set": lambda v: self.set_setting("usb_copy_subfolders", v)},
            {"type": "action", "label": "Rescan library",
             "hint": "Re-index the games folder now.",
             "action": lambda shell: shell.rescan_library()},
            {"type": "action", "label": "Import from USB / MTP",
             "hint": "List drives and Android phone storages; copy their ROMs.",
             "action": lambda shell: shell.push_overlay(UsbManagerOverlay(shell))},
            {"type": "info", "label": "Games folder", "value": get_games_dir(),
             "hint": "Copy .gba/.gbc/.gb here, or import from USB/MTP."},
        ]

    def _deletable_games(self) -> List[Dict[str, Any]]:
        """Only real imported ROMs, never demo cartridges or built-in apps."""
        return [g for g in self.games
                if g.get("type") == "game" and g.get("content_path")
                and os.path.isfile(g["content_path"])]

    def _rows_games(self) -> List[dict]:
        games = self._deletable_games()
        return [
            {"type": "info", "label": "Imported games", "value": str(len(games)),
             "hint": "ROM files found in the games folder."},
            {"type": "info", "label": "Favorites",
             "value": str(len(self.favorites.ids())),
             "hint": "Ctrl+F or controller X toggles a favorite in GAMES."},
            {"type": "action", "label": "Delete Selected Games",
             "hint": "Choose multiple ROMs; each deletion asks for confirmation.",
             "action": lambda shell: shell.push_overlay(GameManagementOverlay(shell))},
            {"type": "action", "label": "Delete All Imported Games",
             "hint": "Hold A for two seconds after a second confirmation.",
             "action": lambda shell: shell.confirm_delete_all()},
        ]

    def confirm_delete_all(self) -> None:
        games = self._deletable_games()
        if not games:
            self.toast("No imported games to delete", TOAST_WARN)
            return
        # Two gates: an explicit yes/no first, then a deliberate two-second
        # hold. A single accidental Enter can never remove the library.
        self.push_overlay(ConfirmOverlay(
            "DELETE ALL IMPORTED GAMES?",
            f"Permanently remove ALL {len(games)} imported ROM(s) and their "
            f"cheats, save states and thumbnails. This cannot be undone.",
            "CONTINUE", True, self._hold_confirm_delete_all))

    def _hold_confirm_delete_all(self) -> None:
        self.push_overlay(StrongConfirmOverlay(
            "LAST CHANCE — DELETE ALL",
            "Hold A / Enter for two seconds to permanently delete every "
            "imported ROM and its per-game data. Press B to cancel.",
            on_confirm=self.delete_all_games,
            confirm_label="HOLD [A] FOR 2 SECONDS TO DELETE ALL"))

    def delete_all_games(self) -> None:
        games = self._deletable_games()
        if not games:
            self.toast("No imported games to delete", TOAST_WARN)
            return
        result = game_data.delete_all_games(
            games, library=self.library, favorites=self.favorites)
        logger.warning("Deleted all imported games: %d ROM(s), errors=%s",
                       result["roms"], result["errors"])
        self.close_overlays()
        self.rescan_library(quiet=True)
        self.toast(f"Deleted {result['roms']} game(s)", TOAST_OK)

    def _rows_storage(self) -> List[dict]:
        info = settings_store.storage_info(get_data_dir()) or {}
        saves = os.path.join(get_data_dir(), "saves")
        total = 0
        count = 0
        try:
            for root, _dirs, files in os.walk(saves):
                for name in files:
                    count += 1
                    total += os.path.getsize(os.path.join(root, name))
        except Exception:
            pass
        return [
            {"type": "info", "label": "DATA free",
             "value": f"{info.get('free_gb', 0):.1f} GB",
             "hint": f"{get_data_dir()} · journaled storage for saves and config"},
            {"type": "info", "label": "Save data",
             "value": f"{count} files · {human_size(total)}",
             "hint": "Every game keeps its own folder keyed by content hash."},
            {"type": "action", "label": "Open Save Manager",
             "hint": "Inspect sizes and delete a save after confirmation.",
             "action": lambda shell: shell.push_overlay(SaveManagerOverlay())},
            {"type": "action", "label": "Export saves",
             "hint": "Write a .tar.gz archive into DATA/exports.",
             "action": lambda shell: shell.export_saves()},
            {"type": "action", "label": "Clean library cache",
             "hint": "Disposable index; rebuilt on the next scan.",
             "action": lambda shell: shell.clean_cache()},
        ]

    def _rows_system(self) -> List[dict]:
        memory = get_memory_usage()
        return [
            {"type": "info", "label": "Contract", "value": f"v{SYS_CONTRACT_VERSION}",
             "hint": "docs/CONTRACT.md definitions this build implements."},
            {"type": "info", "label": "Memory",
             "value": f"{memory['used_mb']}/{memory['total_mb']} MB",
             "hint": "Target: under 500 MB idle, under 1 GB while emulating."},
            {"type": "info", "label": "Uptime", "value": format_duration(get_uptime_seconds()),
             "hint": "Time since boot."},
            {"type": "info", "label": "Display",
             "value": f"{self.surface.get_width()}x{self.surface.get_height()}",
             "hint": f"UI scale {self.theme.scale:.2f}x · driver {pygame.display.get_driver()}."},
            {"type": "info", "label": "Session",
             "value": "appliance" if not self.windowed else "windowed",
             "hint": "Appliance mode takes the full display; windowed is for testing."},
            {"type": "action", "label": "View logs",
             "hint": "Tail of the current session log.",
             "action": lambda shell: shell.show_logs()},
            {"type": "action", "label": "About GBA OS",
             "hint": "Version, shortcuts and credits.",
             "action": lambda shell: shell.show_about()},
            {"type": "action", "label": "Reboot", "hint": "Return to the GRUB menu.",
             "action": lambda shell: shell.confirm_system("Reboot to the GRUB menu?",
                                                          EXIT_CODE_REBOOT)},
            {"type": "action", "label": "Power off", "hint": "Flush saves and stop.",
             "action": lambda shell: shell.confirm_system("Power off the machine?",
                                                          EXIT_CODE_POWEROFF)},
        ]

    # -- settings actions ----------------------------------------------
    def test_pattern(self) -> None:
        colors = [self.theme.colors.danger, self.theme.colors.success,
                  self.theme.colors.info, self.theme.colors.highlight]
        width = self.surface.get_width() // len(colors)
        started = time.monotonic()
        while time.monotonic() - started < 2.0:
            for event in pygame.event.get():
                if event.type in (pygame.KEYDOWN, pygame.MOUSEBUTTONDOWN):
                    return
            for index, color in enumerate(colors):
                pygame.draw.rect(self.surface, color,
                                 (index * width, 0, width, self.surface.get_height()))
            self.present()
            self.clock.tick(30)

    def reset_keybindings(self) -> None:
        self.keybindings = dict(DEFAULT_KEYBINDINGS)
        save_keybindings(self.keybindings)
        self.toast("Key bindings reset", TOAST_OK)

    def export_saves(self) -> None:
        source = os.path.join(get_data_dir(), "saves")
        if not os.path.isdir(source) or not os.listdir(source):
            self.toast("No saves to export", TOAST_WARN)
            return
        target_dir = os.path.join(get_data_dir(), "exports")
        os.makedirs(target_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        try:
            archive = shutil.make_archive(os.path.join(target_dir, f"gba_saves_{stamp}"),
                                          "gztar", source)
            self.toast(f"Exported to {os.path.basename(archive)}", TOAST_OK)
        except Exception as exc:
            self.toast(f"Export failed: {exc}", TOAST_ERROR)

    def clean_cache(self) -> None:
        cache = os.path.join(get_data_dir(), "library")
        shutil.rmtree(cache, ignore_errors=True)
        self.rescan_library(quiet=True)
        self.toast("Library cache cleared", TOAST_OK)

    def show_logs(self) -> None:
        path = os.path.join(get_logs_dir(), "latest.log")
        if not os.path.exists(path):
            import glob
            candidates = sorted(glob.glob(os.path.join(get_logs_dir(), "boot_*.log")),
                                key=os.path.getmtime, reverse=True)
            path = candidates[0] if candidates else ""
        lines = tail_file(path, 400) if path else ["No log file found."]
        self.push_overlay(TextOverlay("SESSION LOG", lines, os.path.basename(path) or "no log"))

    def show_about(self) -> None:
        lines = [
            "GBA OS — a Game Boy Advance inspired distro",
            "",
            f"Contract: v{SYS_CONTRACT_VERSION}",
            f"Python: {sys.version.split()[0]}   pygame: {pygame.version.ver}",
            f"Display: {self.surface.get_width()}x{self.surface.get_height()} "
            f"@ {self.theme.scale:.2f}x   driver: {pygame.display.get_driver()}",
            "",
            "SHORTCUTS",
            "  Ctrl+Alt+T      terminal (toggle)",
            "  Alt+Tab         next tab",
            "  Alt+Shift+Tab   previous tab",
            "  Tab             next tab (library)",
            "  Ctrl+V          toggle cartridge/list view",
            "  F1              help",
            "  F12             screenshot to DATA/screenshots",
            "  Ctrl+F          favorite / unfavorite a game (pad X)",
            "  typing          search the games shelf",
            "",
            "IN-GAME (Settings -> Controls -> In-game shortcuts)",
            "  UP + LEFT       cheats overlay (per game)",
            "  L + R           fast-forward cycle (1x/2x/3x)",
            "  LEFT + RIGHT    save states (9 slots, per game)",
            "",
            "MAIN TABS",
            "  GAMES     cartridge shelf, A to play",
            "  APPS      built-in tools as cartridges",
            "  BROWSER   lightweight web reader (Ctrl+L for a URL)",
            "  TERMINAL  a real shell",
            "  SETTINGS  display, audio, controls, library, storage, system",
            "",
            "No ROMs or BIOS files are included. Game Boy Advance",
            "is a trademark of Nintendo.",
        ]
        self.push_overlay(TextOverlay("ABOUT GBA OS", lines, "F1 for help anywhere"))

    def help_lines(self) -> List[str]:
        return [
            "GBA OS — keyboard & controller",
            "",
            "LIBRARY",
            "  D-Pad / Arrows   move between cartridges",
            "  A / Enter / Z    launch, open or toggle",
            "  B / Esc / X      back (clears search first)",
            "  Tab / Alt+Tab    switch main tabs",
            "  Start            next tab",
            "  type to search   Backspace edits",
            "  Ctrl+F / pad X   favorite (always listed first)",
            "",
            "GAME (shortcuts are configurable in Settings -> Controls)",
            "  Esc              quit the emulator, back to the library",
            "  SELECT+START     same, from a controller",
            "  UP + LEFT        cheats for this game only",
            "  L + R            fast-forward: 1x -> 2x -> 3x -> 1x",
            "  LEFT + RIGHT     nine save-state slots",
            "  B / Esc          close an in-game overlay",
            "",
            "TERMINAL",
            "  Ctrl+Alt+T       toggle the terminal from anywhere",
            "  Ctrl+C / Ctrl+D  interrupt / end the shell",
            "",
            "BROWSER",
            "  Ctrl+L           type an address",
            "  Tab / 1-9        pick a link       Enter  open it",
            "  Alt+Left/Right   history          R      reload",
            "  Up/Down PgUp/Dn  scroll",
            "",
            "SYSTEM",
            "  F1 help   F12 screenshot   Ctrl+V view mode",
        ]

    def save_screenshot(self) -> None:
        folder = os.path.join(get_data_dir(), "screenshots")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, time.strftime("shot_%Y%m%d-%H%M%S.png"))
        try:
            pygame.image.save(self.surface, path)
            self.toast(f"Saved {os.path.basename(path)}", TOAST_OK)
        except Exception as exc:
            self.toast(f"Screenshot failed: {exc}", TOAST_ERROR)

    def rescan_library(self, quiet: bool = False) -> None:
        self.library = Library()
        self.scanner = self.library.start_scan()
        self.scan_started_at = time.monotonic()
        self.filter_text = ""
        if not quiet:
            self.toast("Rescanning games folder...", TOAST_INFO)

    # ------------------------------------------------------------- layout
    def top_bar_h(self) -> float:
        return 26.0

    def bottom_bar_h(self) -> float:
        return 22.0

    def tab_rects(self) -> List[pygame.Rect]:
        """Hit rects produced by the last top-bar paint (same order as TABS)."""
        rects = getattr(self, "_tab_hit_rects", None)
        if rects and len(rects) == len(TABS):
            return rects
        theme = self.theme
        rects, x = [], 78
        for name in TABS:
            width = theme.font("micro").size(name)[0] / theme.scale + 10
            rects.append(theme.rect(x, 5, width, 17))
            x += width + 3
        return rects

    def content_rect(self) -> pygame.Rect:
        theme = self.theme
        top = self.top_bar_h() + 4
        height = self.design_h - top - self.bottom_bar_h() - 6
        return theme.rect(7, top, self.design_w - 14, height)

    def _split_rects(self) -> Tuple[pygame.Rect, pygame.Rect]:
        """Main (shelf) rect and the detail panel rect, in pixels."""
        theme = self.theme
        content = self.content_rect()
        side_design = min(150.0, max(112.0, self.design_w * 0.30))
        side_w = theme.px(side_design)
        gap = theme.px(6)
        main = pygame.Rect(content.x, content.y, content.width - side_w - gap, content.height)
        side = pygame.Rect(main.right + gap, content.y, side_w, content.height)
        return main, side

    def _grid_metrics(self):
        theme = self.theme
        main, _side = self._split_rects()
        gap_units = 4
        gap = theme.px(gap_units)
        min_tile = theme.px(72)
        columns = max(2, int((main.width + gap) // (min_tile + gap)))
        tile_w = (main.width - gap * (columns - 1)) // columns
        tile_h = int(tile_w * 1.22)
        rows = max(1, (main.height) // (tile_h + gap))
        while rows > 1 and rows * (tile_h + gap) > main.height:
            rows -= 1
        # Center the shelf vertically inside the content area, counting only the
        # rows that are actually used so a short library looks deliberate.
        used_rows = min(rows, max(1, (len(self.current_items()) + columns - 1) // columns))
        total_h = used_rows * tile_h + (used_rows - 1) * gap
        origin_y = main.y + max(0, (main.height - total_h) // 2)
        return columns, tile_w, tile_h, gap, main.x, origin_y

    def _grid_columns(self) -> int:
        return self._grid_metrics()[0]

    def _list_rect(self) -> pygame.Rect:
        main, _side = self._split_rects()
        return main

    # ------------------------------------------------------------- render
    def render(self) -> None:
        self.draw_background()
        if self.overlay is not None:
            self.overlay.draw(self)
        self.draw_toast()
        self.present()

    def draw_background(self) -> None:
        self.surface.fill(self.theme.colors.bg)
        self.draw_top_bar()
        if self.current_tab == TAB_TERMINAL:
            self.draw_terminal()
        elif self.current_tab == TAB_BROWSER:
            self.draw_browser()
        elif self.current_tab == TAB_SETTINGS:
            self.draw_shelf(list_mode=True)
        else:
            self.draw_shelf(list_mode=self.settings.get("view_mode") == "list")
        self.draw_bottom_bar()

    # -- chrome --------------------------------------------------------
    def draw_top_bar(self) -> None:
        theme = self.theme
        c = theme.colors
        height = theme.px(self.top_bar_h())
        pygame.draw.rect(self.surface, c.bg_deep, (0, 0, self.surface.get_width(), height))

        # Logo: a tiny cartridge glyph plus the wordmark.
        glyph = theme.rect(8, 7, 12, 13)
        pygame.draw.rect(self.surface, c.cart_body, glyph, border_radius=theme.px(2))
        pygame.draw.rect(self.surface, c.accent_soft,
                         (glyph.x + theme.px(1.5), glyph.y + theme.px(3),
                          glyph.width - theme.px(3), glyph.height - theme.px(4.5)),
                         border_radius=theme.px(1))
        pygame.draw.line(self.surface, c.cart_body_dark,
                         (glyph.x + theme.px(2), glyph.y + theme.px(1.5)),
                         (glyph.right - theme.px(2), glyph.y + theme.px(1.5)),
                         max(1, theme.px(1)))
        theme.text(self.surface, "GBA", (22, 6), "header", c.highlight)
        theme.text(self.surface, "OS", (44, 6), "header", c.accent)

        # --- right cluster, laid out first so the tabs can never run into it.
        right = self.design_w - 7
        battery = get_battery_status()
        if battery:
            level = battery["capacity"]
            body = theme.rect(right - 15, 7, 13, 9)
            pygame.draw.rect(self.surface, c.text_dim, body, width=1, border_radius=theme.px(2))
            pygame.draw.rect(self.surface, c.text_dim,
                             (body.right, body.y + theme.px(2), theme.px(2), theme.px(5)))
            inner = max(0, int((body.width - theme.px(3)) * level / 100.0))
            if inner:
                pygame.draw.rect(self.surface, c.success if level > 30 else c.danger,
                                 (body.x + theme.px(1.5), body.y + theme.px(1.5),
                                  inner, body.height - theme.px(3)))
            right -= 19
        else:
            cx = theme.px(right - 5)
            cy = theme.px(13)
            pygame.draw.circle(self.surface, c.success, (cx, cy), theme.px(4), width=1)
            pygame.draw.line(self.surface, c.success, (cx, cy - theme.px(4)),
                             (cx, cy - theme.px(8)), max(1, theme.px(1)))
            right -= 12

        if self.settings.get("show_clock", True):
            now = datetime.datetime.now()
            clock = now.strftime("%H:%M") if self.settings.get("clock_24h", True) \
                else now.strftime("%I:%M %p").lstrip("0")
            font = theme.font("micro")
            surf = font.render(clock, True, c.text_muted)
            self.surface.blit(surf, (theme.px(right) - surf.get_width(),
                                     theme.px(13) - surf.get_height() // 2))
            right -= surf.get_width() / theme.scale + 5

        status = self.usb_status_text()
        if status != "none":
            font = theme.font("micro")
            text = font.render(status.split(" (")[0], True, c.bg_deep)
            chip = pygame.Rect(0, 0, text.get_width() + theme.px(8), theme.px(12))
            chip.topright = (theme.px(right), theme.px(7))
            pygame.draw.rect(self.surface, c.info, chip, border_radius=theme.px(6))
            self.surface.blit(text, (chip.x + theme.px(4),
                                     chip.centery - text.get_height() // 2))
            right -= chip.width / theme.scale + 5

        if self.filter_text and self.current_tab == TAB_GAMES:
            font = theme.font("micro")
            text = font.render(f"/{self.filter_text}", True, c.bg_deep)
            chip = pygame.Rect(0, 0, text.get_width() + theme.px(8), theme.px(12))
            chip.topright = (theme.px(right), theme.px(7))
            pygame.draw.rect(self.surface, c.highlight, chip, border_radius=theme.px(6))
            self.surface.blit(text, (chip.x + theme.px(4),
                                     chip.centery - text.get_height() // 2))
            right -= chip.width / theme.scale + 5

        # --- tabs, squeezed into whatever is left of the bar.
        for font_key in ("small", "micro"):
            font = theme.font(font_key)
            pad = 7 if font_key == "small" else 5
            widths = [font.size(name)[0] for name in TABS]
            scaled = [w / theme.scale + pad * 2 for w in widths]
            if 78 + sum(scaled) + 3 * len(TABS) < right - 4:
                break
        x = 78.0
        self._tab_hit_rects = []
        for index, name in enumerate(TABS):
            rect = pygame.Rect(theme.px(x), theme.px(5), theme.px(scaled[index]), theme.px(17))
            active = index == self.current_tab
            if active:
                pygame.draw.rect(self.surface, c.card_sel, rect, border_radius=theme.px(4))
                pygame.draw.rect(self.surface, c.primary, rect, width=1,
                                 border_radius=theme.px(4))
            surf = font.render(name, True, c.text if active else c.text_muted)
            self.surface.blit(surf, (rect.centerx - surf.get_width() // 2,
                                     rect.centery - surf.get_height() // 2))
            self._tab_hit_rects.append(rect)
            x += scaled[index] + 3

        pygame.draw.line(self.surface, c.border_soft, (0, height),
                         (self.surface.get_width(), height), 1)

    def draw_bottom_bar(self) -> None:
        theme = self.theme
        c = theme.colors
        height = theme.px(self.bottom_bar_h())
        top = self.surface.get_height() - height
        pygame.draw.rect(self.surface, c.bg_deep, (0, top, self.surface.get_width(), height))
        pygame.draw.line(self.surface, c.border_soft, (0, top), (self.surface.get_width(), top), 1)

        y = (top + theme.px(5)) / 1.0
        hints = self.context_hints()
        x = theme.px(8)
        font = theme.font("micro")
        for key, description, color in hints:
            key_surf = font.render(key, True, color)
            if x + key_surf.get_width() > self.surface.get_width() - theme.px(8):
                break
            self.surface.blit(key_surf, (x, y))
            x += key_surf.get_width() + theme.px(3)
            desc_surf = font.render(description, True, c.text_dim)
            if x + desc_surf.get_width() > self.surface.get_width() - theme.px(8):
                break
            self.surface.blit(desc_surf, (x, y))
            x += desc_surf.get_width() + theme.px(9)

        # Outcome banner takes priority on the right side of the bar.
        if self.last_outcome and self.last_outcome not in ("clean", "unknown", None):
            text = f"previous session: {self.last_outcome} · L for log"
            surf = theme.font("micro").render(text, True, c.highlight)
            self.surface.blit(surf, (self.surface.get_width() - surf.get_width() - theme.px(8), y))

    def context_hints(self) -> List[Tuple[str, str, Any]]:
        c = self.theme.colors
        tab = self.current_tab
        if tab == TAB_TERMINAL:
            return [("Ctrl+Alt+T", "back to games", c.highlight),
                    ("Alt+Tab", "switch tab", c.info),
                    ("Ctrl+C", "interrupt", c.text_dim)]
        if tab == TAB_BROWSER:
            return [("Ctrl+L", "address", c.info),
                    ("Tab / 1-9", "links", c.text_muted),
                    ("Alt+←/→", "history", c.text_muted),
                    ("R", "reload", c.text_dim)]
        if self.filter_text:
            return [("[Esc]", "clear search", c.highlight),
                    ("[Enter]", "launch", c.primary),
                    ("[Arrows]", "move", c.text_muted)]
        if tab == TAB_SETTINGS:
            return [("[A]", "open", c.primary),
                    ("[↑↓]", "select", c.text_muted),
                    ("[Tab]", "next tab", c.info)]
        if tab == TAB_GAMES:
            return [("(A)", "select", c.primary),
                    ("(B)", "back", c.text_muted),
                    ("(D-Pad)", "navigate", c.text_muted),
                    ("Ctrl+F / X", "favorite", c.highlight),
                    ("[Tab]", "tab", c.info),
                    ("type", "search", c.text_dim)]
        return [("(A)", "select", c.primary),
                ("(B)", "back", c.text_muted),
                ("(D-Pad)", "navigate", c.text_muted),
                ("[Tab]", "tab", c.info),
                ("type", "search", c.text_dim)]

    def draw_toast(self) -> None:
        if not self.toast_text:
            return
        theme = self.theme
        font = theme.font("small")
        text = truncate(font, self.toast_text, theme.px(self.design_w - 40))
        surf = font.render(text, True, theme.colors.bg_deep)
        chip = pygame.Rect(0, 0, surf.get_width() + theme.px(16), theme.px(16))
        chip.centerx = self.surface.get_width() // 2
        chip.bottom = self.surface.get_height() - theme.px(self.bottom_bar_h() + 4)
        pygame.draw.rect(self.surface, self._toast_color(), chip, border_radius=theme.px(8))
        self.surface.blit(surf, (chip.x + theme.px(8), chip.centery - surf.get_height() // 2))

    # -- shelf (games / apps / settings) --------------------------------
    def draw_shelf(self, list_mode: bool = False) -> None:
        items = self.current_items()
        self._clamp()
        index = self.selected[self.current_tab]
        main, side = self._split_rects()

        if not items:
            self.draw_empty_state(main)
        elif list_mode or self.current_tab == TAB_SETTINGS:
            self.draw_list(main, items, index)
        else:
            self.draw_grid(main, items, index)

        self.draw_detail(side)

    def draw_grid(self, rect: pygame.Rect, items, index: int) -> None:
        theme = self.theme
        columns, tile_w, tile_h, gap, origin_x, origin_y = self._grid_metrics()
        rows_visible = max(1, rect.height // (tile_h + gap))
        page = columns * rows_visible
        start = (index // page) * page if index >= page else 0
        for position, item_index in enumerate(range(start, min(len(items), start + page))):
            item = items[item_index]
            col, row = position % columns, position // columns
            cell = pygame.Rect(origin_x + col * (tile_w + gap),
                               origin_y + row * (tile_h + gap), tile_w, tile_h)
            selected = item_index == index
            favorite = self.favorites.is_favorite(item.get("id", ""))
            badge = "FAV" if favorite else (
                "NEW" if item.get("id") in self.history and
                time.time() - self.history[item["id"]].get("last_played", 0) < 86400
                else None)
            theme.cartridge(self.surface, cell, item.get("title", ""),
                            item.get("format", "GBA"), item.get("id", ""),
                            selected=selected, badge=badge)
            if selected:
                pygame.draw.rect(self.surface, theme.colors.highlight,
                                 cell.inflate(theme.px(3), theme.px(3)),
                                 width=max(1, theme.px(1)), border_radius=theme.px(7))

    def draw_list(self, rect: pygame.Rect, items, index: int) -> None:
        theme = self.theme
        c = theme.colors
        settings_mode = self.current_tab == TAB_SETTINGS
        row_h = 28.0 if settings_mode else 22.0
        visible = max(1, int(rect.height / theme.px(row_h)))
        start = 0
        if index >= visible:
            start = min(index - visible + 1, max(0, len(items) - visible))
        for offset, item_index in enumerate(range(start, min(len(items), start + visible))):
            item = items[item_index]
            selected = item_index == index
            y = rect.y + theme.px(offset * row_h)
            box = pygame.Rect(rect.x, y, rect.width, theme.px(row_h) - theme.px(2))
            if selected:
                pygame.draw.rect(self.surface, c.card_sel, box, border_radius=theme.px(4))
                pygame.draw.rect(self.surface, c.primary, box, width=1, border_radius=theme.px(4))
            elif offset % 2:
                pygame.draw.rect(self.surface, c.panel_alt, box, border_radius=theme.px(4))

            # Format badge (the "sticker" of a list row).
            badge = theme.rect(box.x / theme.scale + 3, box.y / theme.scale + 3.5, 18,
                               row_h - 9)
            pygame.draw.rect(self.surface, c.bg_deep, badge, border_radius=theme.px(3))
            pygame.draw.rect(self.surface, c.info if selected else c.border_soft, badge,
                             width=1, border_radius=theme.px(3))
            fmt = theme.font("micro").render(item.get("format", "GBA")[:3], True,
                                             c.highlight if selected else c.info)
            self.surface.blit(fmt, (badge.centerx - fmt.get_width() // 2,
                                    badge.centery - fmt.get_height() // 2))

            text_x = (badge.right / theme.scale) + 5
            text_w = (box.right / theme.scale) - text_x - 6
            title = truncate(theme.font("body_bold"), item.get("title", ""), theme.px(text_w))
            theme.text(self.surface, title, (text_x, box.y / theme.scale + 3),
                       "body_bold", c.text if selected else c.text_muted)
            subtitle = item.get("description", "")
            if not settings_mode and item.get("size_bytes"):
                subtitle = f"{human_size(item['size_bytes'])} · {item.get('id', '')[:8]}"
            theme.label(self.surface, truncate(theme.font("micro"), subtitle, theme.px(text_w)),
                        (text_x, box.y / theme.scale + 18.5), c.text_dim, key="micro")
            if self.current_tab == TAB_GAMES and \
                    self.favorites.is_favorite(item.get("id", "")):
                theme.label(self.surface, "FAV",
                            (box.right / theme.scale - 24,
                             box.y / theme.scale + 2), c.highlight, key="micro")
            if selected:
                theme.label(self.surface, ">", (box.right / theme.scale - 7,
                                                box.y / theme.scale + 2), c.highlight)

    def draw_empty_state(self, rect: pygame.Rect) -> None:
        theme = self.theme
        c = theme.colors
        scanning = self.scanner is not None
        x = rect.x / theme.scale + 10
        y = rect.y / theme.scale + 14
        if scanning:
            title, color = "Scanning the games folder...", c.info
            notes = [f"elapsed {time.monotonic() - self.scan_started_at:.1f}s",
                     "first scan hashes every ROM; later boots use the cache"]
        elif self.filter_text:
            title, color = f"No match for '{self.filter_text}'", c.highlight
            notes = ["press Esc to clear the search"]
        else:
            title, color = "No games on the shelf", c.text
            notes = ["copy .gba / .gbc / .gb files to:",
                     get_games_dir(),
                     "plug in a pendrive or MTP phone; choose Import"]
        theme.text(self.surface, title, (x, y), "body_bold", color)
        y += 14
        for note in notes:
            theme.label(self.surface, truncate(theme.font("small"), note, theme.px(self.design_w * 0.5)),
                        (x, y), c.text_muted)
            y += 10
        if not scanning:
            theme.label(self.surface, "F1 shows the full keyboard map", (x, y + 6), c.text_dim)

    def draw_detail(self, rect: pygame.Rect) -> None:
        theme = self.theme
        c = theme.colors
        item = self.current_item()
        theme.panel(self.surface, rect, fill=c.panel_alt, border=c.border_soft, radius=6)
        if not item:
            theme.label(self.surface, "Nothing selected", (rect.x / theme.scale + 8,
                                                           rect.y / theme.scale + 8), c.text_dim)
            return

        x0 = rect.x / theme.scale
        y0 = rect.y / theme.scale
        width = rect.width / theme.scale
        pad = 8.0

        cart_w = min(width - pad * 2, 96.0)
        cart_h = cart_w * 1.16
        cart = theme.rect(x0 + (width - cart_w) / 2, y0 + pad, cart_w, cart_h)
        is_game = item.get("type") == "game"
        theme.cartridge(self.surface, cart, item.get("title", ""),
                        item.get("format", "SYS"), item.get("id", ""), dim=not is_game)

        y = (cart.bottom / theme.scale) + 6
        title_font = theme.font("body_bold")
        for line in wrap_text(title_font, item.get("title", ""), theme.px(width - pad * 2), 2):
            theme.text(self.surface, line, (x0 + pad, y), "body_bold")
            y += 10
        y += 2

        if is_game:
            summary = self.save_summary(item)
            record = self.history.get(item.get("id", ""))
            info = [
                ("Format", item.get("format", "GBA")),
                ("Size", human_size(item.get("size_bytes", 0))),
                ("Save", human_size(summary["bytes"]) if summary["files"] else "none"),
                ("Favorite", "YES" if self.favorites.is_favorite(
                    item.get("id", "")) else "no"),
            ]
            if record:
                info.append(("Played", f"{record.get('sessions', 0)}x"))
                info.append(("Total", format_duration(record.get("seconds", 0))))
        else:
            info = [("Type", item.get("type", "app").title())]
        for label, value in info:
            theme.label(self.surface, label, (x0 + pad, y + 0.5), c.text_dim, key="micro")
            theme.label(self.surface, truncate(theme.font("small"), str(value),
                                              theme.px(width - pad - 26)),
                        (x0 + pad + 26, y), c.text)
            y += 11

        if not is_game and item.get("description"):
            y += 3
            for line in wrap_text(theme.font("micro"), item["description"],
                                  theme.px(width - pad * 2), 3):
                theme.label(self.surface, line, (x0 + pad, y), c.text_muted, key="micro")
                y += 8

        action = {
            "game": "[A] LAUNCH",
            "app": "[A] OPEN",
            "settings": "[A] OPEN",
        }.get(item.get("type"), "[A] OPEN")
        theme.button(self.surface,
                     pygame.Rect(rect.x + theme.px(pad), rect.bottom - theme.px(22),
                                 theme.px(width - pad * 2), theme.px(16)),
                     action, c.highlight if is_game else c.primary)

    # -- terminal tab ---------------------------------------------------
    def draw_terminal(self) -> None:
        theme = self.theme
        c = theme.colors
        content = self.content_rect()
        self.ensure_terminal()

        header = pygame.Rect(content.x, content.y, content.width, theme.px(16))
        pygame.draw.rect(self.surface, c.panel_alt, header, border_radius=theme.px(4))
        title = "TERMINAL" if self.terminal_session and self.terminal_session.alive \
            else "TERMINAL (unavailable)"
        theme.label(self.surface, title, (content.x / theme.scale + 6, content.y / theme.scale + 3),
                    c.info)
        theme.label(self.surface, "Ctrl+Alt+T returns to the shelf · Alt+Tab switches tabs",
                    (content.right / theme.scale - 6, content.y / theme.scale + 3),
                    c.text_dim, align="right")

        body = pygame.Rect(content.x, header.bottom + theme.px(3), content.width,
                           content.bottom - header.bottom - theme.px(3))
        view = self.terminal_view
        if view is None:
            self.terminal_view = view = TerminalView(self.terminal_screen, theme, 8,
                                                    "starting shell…")
        cols, rows, _, _ = 0, 0, 0, 0
        cw, ch, cols, rows = view.metrics(body)
        if (cols, rows) != self._terminal_grid:
            self._terminal_grid = (cols, rows)
            if self.terminal_session:
                self.terminal_session.resize(cols, rows)
            self.terminal_screen.resize(cols, rows)
        view.draw(self.surface, body, focused=True,
                  cursor_on=(time.monotonic() % 1.0) < 0.6)

    # -- browser tab ----------------------------------------------------
    def draw_browser(self) -> None:
        theme = self.theme
        c = theme.colors
        content = self.content_rect()
        self.ensure_browser()
        browser = self.browser
        browser.theme = theme
        browser.set_width(content.width)

        bar = pygame.Rect(content.x, content.y, content.width, theme.px(17))
        pygame.draw.rect(self.surface, c.panel_alt, bar, border_radius=theme.px(4))
        pygame.draw.rect(self.surface, c.border_soft, bar, width=1, border_radius=theme.px(4))
        url_text = browser.address_buffer if browser.editing_url else (browser.url or "about:welcome")
        color = c.highlight if browser.editing_url else c.text_muted
        prompt = "URL> " if browser.editing_url else ""
        theme.label(self.surface, truncate(theme.font("small"), prompt + url_text,
                                          theme.px((content.width / theme.scale) - 12)),
                    (content.x / theme.scale + 6, content.y / theme.scale + 3.5), color)
        if browser.editing_url and (time.monotonic() % 1.0) < 0.6:
            cursor_x = content.x / theme.scale + 6 + \
                theme.font("small").size(prompt + url_text)[0] + 1
            pygame.draw.line(self.surface, c.text,
                             (theme.px(cursor_x), bar.y + theme.px(3)),
                             (theme.px(cursor_x), bar.bottom - theme.px(3)), 1)

        body = pygame.Rect(content.x, bar.bottom + theme.px(3), content.width,
                           content.bottom - bar.bottom - theme.px(3))
        pygame.draw.rect(self.surface, c.screen, body, border_radius=theme.px(4))
        pygame.draw.rect(self.surface, c.border_soft, body, width=1, border_radius=theme.px(4))
        self.draw_browser_document(browser, body)

        # Status strip over the bottom of the page.
        status = browser.status
        if browser.document and browser.document.links:
            status += f" · {browser.selected_link + 1}/{len(browser.document.links)} links"
        surf = theme.font("micro").render(truncate(theme.font("micro"), status,
                                                   theme.px((body.width / theme.scale) - 8)),
                                          True, c.text_dim)
        self.surface.blit(surf, (body.x + theme.px(4), body.bottom - surf.get_height() - theme.px(2)))

    def draw_browser_document(self, browser, rect: pygame.Rect) -> None:
        theme = self.theme
        c = theme.colors
        if not browser.document:
            theme.label(self.surface, "Ctrl+L to type an address", (rect.x / theme.scale + 8,
                                                                     rect.y / theme.scale + 8),
                        c.text_dim)
            return

        fonts = {
            "h1": theme.font("title"), "h2": theme.font("header"), "h3": theme.font("body_bold"),
            "p": theme.font("body"), "li": theme.font("body"), "quote": theme.font("body"),
            "pre": theme.mono(8), "link": theme.font("body"), "meta": theme.font("micro"),
        }
        colors = {
            "h1": c.highlight, "h2": c.accent, "h3": c.accent, "p": c.text,
            "li": c.text, "quote": c.text_muted, "pre": c.success,
            "link": c.info, "meta": c.text_dim,
        }
        x0 = rect.x + theme.px(4)
        y = rect.y + theme.px(3)
        bottom = rect.bottom - theme.px(10)
        selected_href = None
        if browser.document.links and 0 <= browser.selected_link < len(browser.document.links):
            selected_href = browser.document.links[browser.selected_link]

        start = browser.scroll
        for index in range(start, len(browser.document.lines)):
            if y > bottom:
                break
            line = browser.document.lines[index]
            image = browser.document.images.get(index) if hasattr(browser.document, "images") else None
            font = fonts.get(line.style, fonts["p"])

            if line.text and line.text.strip():
                is_selected_link = line.href and line.href == selected_href
                if is_selected_link:
                    highlight = pygame.Rect(x0, y - theme.px(1),
                                            rect.width - theme.px(8),
                                            font.get_linesize() + theme.px(1))
                    pygame.draw.rect(self.surface, theme.mix(c.card_sel, c.info, 0.25), highlight,
                                     border_radius=theme.px(2))
                color = colors.get(line.style, c.text_muted)
                indent = theme.px(6) * line.indent
                surf = font.render(line.text, True, color)
                self.surface.blit(surf, (x0 + indent, y))
                y += font.get_linesize() + theme.px(0.5)

            if image is not None:
                self.draw_browser_image(browser, image, pygame.Rect(
                    x0, y, rect.width - theme.px(8), theme.px(64)))

    def draw_browser_image(self, browser, block, rect: pygame.Rect) -> None:
        theme = self.theme
        c = theme.colors
        surface = browser.image_for(block)
        if surface is None:
            pygame.draw.rect(self.surface, c.panel_alt, rect, border_radius=theme.px(3))
            pygame.draw.rect(self.surface, c.border_soft, rect, width=1, border_radius=theme.px(3))
            label = f"[image unavailable] {block.get('alt', '')}"
            theme.label(self.surface, truncate(theme.font("micro"), label, rect.width),
                        (rect.x / theme.scale + 4, rect.y / theme.scale + 4), c.text_dim, key="micro")
            return
        scaled = pygame.transform.smoothscale(surface, (rect.width, rect.height))
        self.surface.blit(scaled, rect)

    # -- presentation ---------------------------------------------------
    def present(self) -> None:
        if self.pixel_mode and not self.screenshot_path:
            factor = max(1, min(self.screen_w // self.canvas_w, self.screen_h // self.canvas_h))
            scaled = pygame.transform.scale(
                self.surface, (self.canvas_w * factor, self.canvas_h * factor))
            self.window.fill((0, 0, 0))
            self.window.blit(scaled, ((self.screen_w - scaled.get_width()) // 2,
                                      (self.screen_h - scaled.get_height()) // 2))
        elif not self.screenshot_path:
            self.window.blit(self.surface, (0, 0))
        pygame.display.flip()

    # -------------------------------------------------------------- lifecycle
    def run(self) -> None:
        if self.screenshot_path:
            self._wait_for_scan()
            self.on_tab_activated()
            if self.current_tab == TAB_BROWSER:
                self.browser.load("about:welcome")
            self.render()
            pygame.image.save(self.surface, self.screenshot_path)
            print(f"UI preview captured to {self.screenshot_path}")
            return

        while self.running:
            self.update()
            self.handle_events()
            self.render()
            self.clock.tick(60)
        self.shutdown()

    def _wait_for_scan(self, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while self.scanner is not None and not self.scanner.done and time.monotonic() < deadline:
            time.sleep(0.02)
        self.update()

    def quit(self) -> None:
        self.running = False

    def shutdown(self) -> None:
        if self.terminal_session:
            self.terminal_session.close()
        if self.usb_watcher:
            self.usb_watcher.stop()

    # -- misc -----------------------------------------------------------
    def format_key_name(self, code: int) -> str:
        special = {
            pygame.K_RETURN: "RETURN", pygame.K_BACKSPACE: "BACKSPACE",
            pygame.K_SPACE: "SPACE", pygame.K_TAB: "TAB", pygame.K_ESCAPE: "ESC",
            pygame.K_UP: "UP", pygame.K_DOWN: "DOWN", pygame.K_LEFT: "LEFT",
            pygame.K_RIGHT: "RIGHT", pygame.K_LCTRL: "L-CTRL", pygame.K_LSHIFT: "L-SHIFT",
            pygame.K_LALT: "L-ALT", pygame.K_RCTRL: "R-CTRL", pygame.K_RSHIFT: "R-SHIFT",
            pygame.K_RALT: "R-ALT",
        }
        if code in special:
            return special[code]
        try:
            name = pygame.key.name(code).upper()
            return name if len(name) > 1 else f"{name} KEY"
        except Exception:
            return f"KEY {code}"

    def screen_to_canvas(self, mx: int, my: int) -> Tuple[int, int]:
        """Kept for compatibility: the canvas now matches the window 1:1."""
        if not self.pixel_mode or self.screenshot_path:
            return mx, my
        factor = max(1, min(self.screen_w // self.canvas_w, self.screen_h // self.canvas_h))
        offset_x = (self.screen_w - self.canvas_w * factor) // 2
        offset_y = (self.screen_h - self.canvas_h * factor) // 2
        return (mx - offset_x) // factor, (my - offset_y) // factor


# =============================================================================
# Entry point
# =============================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GBA OS Shell")
    parser.add_argument("--focus", type=str, help="entry id to focus")
    parser.add_argument("--outcome", type=str, help="previous session outcome")
    parser.add_argument("--tab", type=str, default="games",
                        help="initial tab (name or index): games, apps, browser, terminal, settings")
    parser.add_argument("--windowed", action="store_true")
    parser.add_argument("--appliance", action="store_true")
    parser.add_argument("--screenshot", type=str)
    parser.add_argument("--demo", action="store_true",
                        help="render a synthetic library (documentation screenshots)")
    return parser


def tab_from_arg(value: str) -> int:
    if value.isdigit():
        return max(0, min(len(TABS) - 1, int(value)))
    upper = value.upper()
    return TABS.index(upper) if upper in TABS else 0


def main() -> None:
    args = build_arg_parser().parse_args()
    settings_store.configure_sdl_video(
        "dummy" if args.screenshot else None)
    settings_store.configure_sdl_audio()
    pygame.init()
    pygame.joystick.init()

    launcher = GBALauncher(
        focus_id=args.focus,
        last_outcome=args.outcome,
        windowed=args.windowed,
        screenshot_path=args.screenshot,
        initial_tab=tab_from_arg(args.tab),
        appliance_mode=args.appliance,
        demo=args.demo,
    )
    try:
        launcher.run()
    finally:
        launcher.shutdown()


if __name__ == "__main__":
    main()
