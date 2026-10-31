#!/usr/bin/env python3
"""
GBA OS Shell / Launcher.
Implements Rules L1-L8, I1-I4, D1-D4, S6 from CONTRACT.md.
Provides a rich retro console user experience inspired by the Game Boy Advance.
"""

import os
import sys
import time
import json
import argparse
import datetime
import shutil

# Ensure local src directory is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pygame
from typing import List, Dict, Any, Optional

from common import (
    get_run_dir,
    get_data_dir,
    get_games_dir,
    setup_logging,
    SYS_CONTRACT_VERSION
)
from library import Library

logger = setup_logging("gba.launcher")

# Virtual Canvas Dimensions (2x native GBA 240x160 resolution)
CANVAS_WIDTH = 480
CANVAS_HEIGHT = 320

# Authentic GBA Color Palette
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

EXIT_CODE_LAUNCH_REQUEST = 10
EXIT_CODE_REBOOT = 100
EXIT_CODE_POWEROFF = 101
EXIT_CODE_RECOVERY = 102

class GBALauncher:
    def __init__(
        self,
        focus_id: Optional[str] = None,
        last_outcome: Optional[str] = None,
        windowed: bool = False,
        screenshot_path: Optional[str] = None,
        initial_tab: int = 0
    ):
        self.focus_id = focus_id
        self.last_outcome = last_outcome
        self.windowed = windowed
        self.screenshot_path = screenshot_path
        
        self.library = Library()
        self.games = self.library.scan_games()
        self.apps = self.library.get_built_in_apps()
        self.settings = self.library.get_settings_entries()

        # Tabs: 0: GAMES, 1: APPS, 2: SETTINGS
        self.tabs = ["GAMES", "APPS", "SETTINGS"]
        self.current_tab = initial_tab
        self.selected_indices = [0, 0, 0]

        # Pygame setup
        if self.screenshot_path:
            os.environ["SDL_VIDEODRIVER"] = "dummy"
            os.environ["SDL_AUDIODRIVER"] = "dummy"

        pygame.init()
        pygame.joystick.init()
        self.joysticks = [pygame.joystick.Joystick(i) for i in range(pygame.joystick.get_count())]
        for joy in self.joysticks:
            joy.init()

        self.canvas = pygame.Surface((CANVAS_WIDTH, CANVAS_HEIGHT))

        # Fonts
        pygame.font.init()
        self.font_title = pygame.font.Font(None, 24)
        self.font_header = pygame.font.Font(None, 20)
        self.font_body = pygame.font.Font(None, 18)
        self.font_small = pygame.font.Font(None, 14)

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
        else:
            self.screen_w, self.screen_h = CANVAS_WIDTH, CANVAS_HEIGHT
            self.window = self.canvas

        # Focus previous entry if provided
        if self.focus_id:
            for idx, g in enumerate(self.games):
                if g["id"] == self.focus_id:
                    self.current_tab = 0
                    self.selected_indices[0] = idx
                    break

        self.scroll_offsets = [0, 0, 0]
        self.clock = pygame.time.Clock()
        self.running = True

    def get_current_items(self) -> List[Dict[str, Any]]:
        if self.current_tab == 0:
            return self.games
        elif self.current_tab == 1:
            return self.apps
        else:
            return self.settings

    def draw_top_bar(self):
        """Draws the retro console top navigation bar."""
        pygame.draw.rect(self.canvas, (20, 16, 32), (0, 0, CANVAS_WIDTH, 32))
        pygame.draw.line(self.canvas, COLOR_BORDER, (0, 32), (CANVAS_WIDTH, 32), 1)

        # GBA OS Badge
        logo_text = self.font_header.render("GBA", True, COLOR_ACCENT_GOLD)
        os_text = self.font_header.render("OS", True, COLOR_ACCENT_CYAN)
        self.canvas.blit(logo_text, (10, 8))
        self.canvas.blit(os_text, (48, 8))

        # Dot matrix accent
        pygame.draw.circle(self.canvas, (180, 80, 120), (74, 15), 3)
        pygame.draw.circle(self.canvas, (100, 180, 240), (83, 15), 3)

        # Category Tabs (Center)
        tab_start_x = 115
        for i, name in enumerate(self.tabs):
            tab_x = tab_start_x + (i * 85)
            is_active = (i == self.current_tab)
            if is_active:
                pygame.draw.rect(self.canvas, COLOR_BG_CARD_SEL, (tab_x - 4, 6, 75, 20), border_radius=4)
                pygame.draw.rect(self.canvas, COLOR_ACCENT_GOLD, (tab_x - 4, 6, 75, 20), width=1, border_radius=4)
                color = COLOR_TEXT_WHITE
            else:
                color = COLOR_TEXT_MUTED
            text = self.font_small.render(name, True, color)
            self.canvas.blit(text, (tab_x + 8, 10))

        # Status Indicators (Right)
        now_str = datetime.datetime.now().strftime("%H:%M")
        time_surf = self.font_small.render(now_str, True, COLOR_TEXT_MUTED)
        self.canvas.blit(time_surf, (CANVAS_WIDTH - 95, 10))

        # Battery / Power Icon
        pygame.draw.rect(self.canvas, COLOR_TEXT_MUTED, (CANVAS_WIDTH - 42, 10, 22, 12), width=1, border_radius=2)
        pygame.draw.rect(self.canvas, COLOR_TEXT_MUTED, (CANVAS_WIDTH - 20, 13, 2, 6))
        pygame.draw.rect(self.canvas, COLOR_ACCENT_MINT, (CANVAS_WIDTH - 40, 12, 16, 8)) # Full battery

    def draw_cartridge_mockup(self, rect: pygame.Rect, title: str, format_str: str, entry_id: str):
        """Draws a stylized pixel Game Boy Advance cartridge illustration."""
        cx, cy, cw, ch = rect.x, rect.y, rect.width, rect.height
        
        # Drop shadow
        pygame.draw.rect(self.canvas, (15, 12, 22), (cx + 3, cy + 3, cw, ch), border_radius=6)
        # Outer cartridge shell
        pygame.draw.rect(self.canvas, (70, 75, 92), (cx, cy, cw, ch), border_radius=6)
        # Top bevel notch (classic GBA cartridge shape)
        pygame.draw.rect(self.canvas, (52, 56, 70), (cx + 8, cy, cw - 16, 7))
        # Cartridge grip ribs
        for r in range(4):
            pygame.draw.line(self.canvas, (95, 102, 120), (cx + 12 + r*6, cy + 10), (cx + 12 + r*6, cy + 18), 2)

        # Cartridge Sticker Label
        label_x, label_y, label_w, label_h = cx + 8, cy + 22, cw - 16, ch - 30
        pygame.draw.rect(self.canvas, COLOR_CART_LABEL, (label_x, label_y, label_w, label_h), border_radius=4)
        pygame.draw.rect(self.canvas, COLOR_ACCENT_GOLD, (label_x, label_y, label_w, label_h), width=1, border_radius=4)

        # Label Banner
        pygame.draw.rect(self.canvas, (92, 45, 115), (label_x, label_y, label_w, 20), border_top_left_radius=4, border_top_right_radius=4)
        fmt_surf = self.font_small.render(f"GAME BOY {format_str}", True, COLOR_TEXT_WHITE)
        self.canvas.blit(fmt_surf, (label_x + 6, label_y + 4))

        # Game Title on sticker
        wrapped_title = title if len(title) <= 15 else title[:13] + ".."
        title_surf = self.font_header.render(wrapped_title, True, COLOR_ACCENT_GOLD)
        self.canvas.blit(title_surf, (label_x + 6, label_y + 26))

        # Seal of Quality / Chip details
        pygame.draw.circle(self.canvas, COLOR_ACCENT_GOLD, (label_x + label_w - 18, label_y + label_h - 18), 10, width=1)
        seal_text = self.font_small.render("GBA", True, COLOR_ACCENT_GOLD)
        self.canvas.blit(seal_text, (label_x + label_w - 28, label_y + label_h - 22))

        # Entry ID stamp
        id_text = self.font_small.render(f"ID:{entry_id[:8]}", True, COLOR_TEXT_MUTED)
        self.canvas.blit(id_text, (label_x + 6, label_y + label_h - 14))

    def draw_app_preview(self, rect: pygame.Rect, item: Dict[str, Any]):
        """Renders dynamic graphics for built-in apps and diagnostics."""
        handler = item.get("handler")
        px, py, pw, ph = rect.x, rect.y, rect.width, rect.height

        if handler == "internal_system_monitor":
            pygame.draw.rect(self.canvas, (45, 36, 70), (px, py, pw, 24), border_top_left_radius=4, border_top_right_radius=4)
            hdr = self.font_small.render("RESOURCE TELEMETRY", True, COLOR_ACCENT_GOLD)
            self.canvas.blit(hdr, (px + 8, py + 6))

            mem_total, mem_used = 4096, 512
            try:
                with open("/proc/meminfo") as f:
                    lines = f.readlines()
                    for l in lines:
                        if l.startswith("MemTotal:"):
                            mem_total = int(l.split()[1]) // 1024
                        elif l.startswith("MemAvailable:"):
                            mem_avail = int(l.split()[1]) // 1024
                            mem_used = mem_total - mem_avail
            except Exception:
                pass

            mem_pct = min(100, int((mem_used / mem_total) * 100))
            
            lbl1 = self.font_small.render(f"RAM: {mem_used}MB / {mem_total}MB ({mem_pct}%)", True, COLOR_TEXT_WHITE)
            self.canvas.blit(lbl1, (px + 8, py + 32))
            pygame.draw.rect(self.canvas, (20, 16, 32), (px + 8, py + 46, pw - 16, 10), border_radius=3)
            fill_w = max(4, int((pw - 16) * (mem_pct / 100.0)))
            pygame.draw.rect(self.canvas, COLOR_ACCENT_CYAN, (px + 8, py + 46, fill_w, 10), border_radius=3)

            try:
                disk = shutil.disk_usage("/")
                disk_free_gb = disk.free / (1024**3)
                lbl2 = self.font_small.render(f"Storage Free: {disk_free_gb:.1f} GB", True, COLOR_TEXT_WHITE)
            except Exception:
                lbl2 = self.font_small.render("Storage: Ready", True, COLOR_TEXT_WHITE)
            self.canvas.blit(lbl2, (px + 8, py + 64))

            contract_lbl = self.font_small.render("Contract V1 Target: <1GB RAM", True, COLOR_ACCENT_MINT)
            self.canvas.blit(contract_lbl, (px + 8, py + 86))

        elif handler == "internal_controller_tester":
            pygame.draw.rect(self.canvas, (45, 36, 70), (px, py, pw, 24), border_top_left_radius=4, border_top_right_radius=4)
            hdr = self.font_small.render("CONTROLLER DIAGNOSTIC", True, COLOR_ACCENT_GOLD)
            self.canvas.blit(hdr, (px + 8, py + 6))

            dpad_center_x, dpad_center_y = px + 40, py + 65
            pygame.draw.rect(self.canvas, (60, 65, 80), (dpad_center_x - 6, dpad_center_y - 20, 12, 40), border_radius=2)
            pygame.draw.rect(self.canvas, (60, 65, 80), (dpad_center_x - 20, dpad_center_y - 6, 40, 12), border_radius=2)

            btn_x, btn_y = px + 120, py + 65
            pygame.draw.circle(self.canvas, (180, 50, 70), (btn_x + 18, btn_y - 6), 10)
            pygame.draw.circle(self.canvas, (180, 50, 70), (btn_x - 6, btn_y + 8), 10)
            a_lbl = self.font_small.render("A", True, COLOR_TEXT_WHITE)
            b_lbl = self.font_small.render("B", True, COLOR_TEXT_WHITE)
            self.canvas.blit(a_lbl, (btn_x + 14, btn_y - 12))
            self.canvas.blit(b_lbl, (btn_x - 10, btn_y + 2))

            joy_count = pygame.joystick.get_count()
            pad_str = f"Gamepad: {joy_count} Detected" if joy_count > 0 else "Keyboard Mode Active"
            pad_lbl = self.font_small.render(pad_str, True, COLOR_ACCENT_CYAN if joy_count > 0 else COLOR_TEXT_MUTED)
            self.canvas.blit(pad_lbl, (px + 8, py + 92))

        else:
            pygame.draw.rect(self.canvas, (45, 36, 70), (px, py, pw, 24), border_top_left_radius=4, border_top_right_radius=4)
            hdr = self.font_small.render("SYSTEM ACTION", True, COLOR_ACCENT_GOLD)
            self.canvas.blit(hdr, (px + 8, py + 6))

            info1 = self.font_small.render("Direct Hardware Control", True, COLOR_TEXT_WHITE)
            info2 = self.font_small.render(item.get("description", "")[:28], True, COLOR_TEXT_MUTED)
            self.canvas.blit(info1, (px + 8, py + 36))
            self.canvas.blit(info2, (px + 8, py + 54))

            if "reboot" in handler:
                warn = self.font_small.render("Returns to GRUB boot menu", True, COLOR_ACCENT_MINT)
            else:
                warn = self.font_small.render("Flushes pending data & stops", True, COLOR_ACCENT_GOLD)
            self.canvas.blit(warn, (px + 8, py + 74))

    def draw_library_view(self):
        """Draws the list of games and the right-hand inspection card."""
        items = self.get_current_items()
        selected_idx = self.selected_indices[self.current_tab]

        list_rect = pygame.Rect(10, 38, 250, 244)
        pygame.draw.rect(self.canvas, COLOR_BG_PANEL, list_rect, border_radius=6)
        pygame.draw.rect(self.canvas, COLOR_BORDER, list_rect, width=1, border_radius=6)

        if not items:
            msg1 = self.font_body.render("No Games Found", True, COLOR_TEXT_WHITE)
            msg2 = self.font_small.render("Place .gba or .gbc files into:", True, COLOR_TEXT_MUTED)
            msg3 = self.font_small.render("/games/", True, COLOR_ACCENT_GOLD)
            self.canvas.blit(msg1, (list_rect.x + 20, list_rect.y + 40))
            self.canvas.blit(msg2, (list_rect.x + 20, list_rect.y + 70))
            self.canvas.blit(msg3, (list_rect.x + 20, list_rect.y + 90))
        else:
            visible_count = 5
            card_h = 44
            
            if selected_idx < self.scroll_offsets[self.current_tab]:
                self.scroll_offsets[self.current_tab] = selected_idx
            elif selected_idx >= self.scroll_offsets[self.current_tab] + visible_count:
                self.scroll_offsets[self.current_tab] = selected_idx - visible_count + 1

            start_idx = self.scroll_offsets[self.current_tab]
            end_idx = min(len(items), start_idx + visible_count)

            for i in range(start_idx, end_idx):
                item = items[i]
                is_selected = (i == selected_idx)
                card_y = list_rect.y + 6 + (i - start_idx) * (card_h + 3)
                card_rect = pygame.Rect(list_rect.x + 6, card_y, list_rect.width - 12, card_h)

                bg_col = COLOR_BG_CARD_SEL if is_selected else COLOR_BG_CARD
                border_col = COLOR_ACCENT_GOLD if is_selected else COLOR_BORDER
                pygame.draw.rect(self.canvas, bg_col, card_rect, border_radius=4)
                pygame.draw.rect(self.canvas, border_col, card_rect, width=1, border_radius=4)

                if is_selected:
                    arrow = self.font_small.render("▶", True, COLOR_ACCENT_GOLD)
                    self.canvas.blit(arrow, (card_rect.x + 6, card_rect.y + 14))

                badge_x = card_rect.x + (20 if is_selected else 8)
                pygame.draw.rect(self.canvas, (25, 20, 36), (badge_x, card_rect.y + 8, 30, 28), border_radius=3)
                fmt = item.get("format", "GBA")
                fmt_surf = self.font_small.render(fmt, True, COLOR_ACCENT_CYAN)
                self.canvas.blit(fmt_surf, (badge_x + 4, card_rect.y + 16))

                t_str = item["title"]
                if len(t_str) > 18:
                    t_str = t_str[:16] + ".."
                title_surf = self.font_body.render(t_str, True, COLOR_TEXT_WHITE if is_selected else COLOR_TEXT_MUTED)
                self.canvas.blit(title_surf, (badge_x + 36, card_rect.y + 8))

                if item.get("size_bytes"):
                    sz_mb = item["size_bytes"] / (1024 * 1024)
                    sub_str = f"{sz_mb:.1f} MB  ·  {item['id'][:8]}"
                else:
                    sub_str = item.get("description", "")[:24]
                sub_surf = self.font_small.render(sub_str, True, COLOR_TEXT_MUTED)
                self.canvas.blit(sub_surf, (badge_x + 36, card_rect.y + 24))

        # Right Column: Detail & Preview (width: 202px)
        detail_rect = pygame.Rect(268, 38, 202, 244)
        pygame.draw.rect(self.canvas, COLOR_BG_PANEL, detail_rect, border_radius=6)
        pygame.draw.rect(self.canvas, COLOR_BORDER, detail_rect, width=1, border_radius=6)

        if items and selected_idx < len(items):
            cur = items[selected_idx]

            if self.current_tab == 0:
                self.draw_cartridge_mockup(
                    pygame.Rect(detail_rect.x + 16, detail_rect.y + 12, 170, 110),
                    cur["title"],
                    cur.get("format", "GBA"),
                    cur["id"]
                )
            else:
                self.draw_app_preview(
                    pygame.Rect(detail_rect.x + 12, detail_rect.y + 12, 178, 110),
                    cur
                )

            meta_y = detail_rect.y + 130
            dt_title = self.font_header.render(cur["title"][:18], True, COLOR_TEXT_WHITE)
            self.canvas.blit(dt_title, (detail_rect.x + 12, meta_y))

            type_lbl = self.font_small.render(f"Type: {cur['type'].upper()} ({cur.get('format', 'SYS')})", True, COLOR_TEXT_MUTED)
            self.canvas.blit(type_lbl, (detail_rect.x + 12, meta_y + 20))

            if cur["type"] == "game":
                save_dir = os.path.join(get_data_dir(), "saves", cur["id"])
                has_save = os.path.exists(save_dir) and len(os.listdir(save_dir)) > 0
                save_status = "Save Present" if has_save else "Ready to play"
                save_color = COLOR_ACCENT_MINT if has_save else COLOR_TEXT_MUTED
            else:
                save_status = "Built-in Component"
                save_color = COLOR_ACCENT_CYAN

            status_surf = self.font_small.render(f"Status: {save_status}", True, save_color)
            self.canvas.blit(status_surf, (detail_rect.x + 12, meta_y + 36))

            if cur["type"] == "game":
                action_btn = "[A] LAUNCH"
                btn_color = COLOR_ACCENT_GOLD
            elif "reboot" in cur.get("handler", ""):
                action_btn = "[A] REBOOT"
                btn_color = COLOR_ACCENT_CYAN
            elif "poweroff" in cur.get("handler", ""):
                action_btn = "[A] POWER OFF"
                btn_color = COLOR_ACCENT_RED
            else:
                action_btn = "[A] SELECT"
                btn_color = COLOR_ACCENT_GOLD

            btn_surf = self.font_small.render(action_btn, True, btn_color)
            pygame.draw.rect(self.canvas, (40, 30, 60), (detail_rect.x + 12, meta_y + 60, 100, 22), border_radius=4)
            pygame.draw.rect(self.canvas, btn_color, (detail_rect.x + 12, meta_y + 60, 100, 22), width=1, border_radius=4)
            self.canvas.blit(btn_surf, (detail_rect.x + 22, meta_y + 65))

    def draw_bottom_bar(self):
        """Draws the controller / keyboard hints and outcome banner."""
        pygame.draw.rect(self.canvas, (20, 16, 32), (0, 286, CANVAS_WIDTH, 34))
        pygame.draw.line(self.canvas, COLOR_BORDER, (0, 286), (CANVAS_WIDTH, 286), 1)

        if self.last_outcome and self.last_outcome != "clean":
            alert_text = f"Notice: Previous session ended with '{self.last_outcome}'"
            alert_surf = self.font_small.render(alert_text, True, COLOR_ACCENT_GOLD)
            self.canvas.blit(alert_surf, (12, 290))
            hint_y = 304
        else:
            hint_y = 296

        hints = [
            ("(A)", "Select", COLOR_ACCENT_GOLD),
            ("(B)", "Back", COLOR_TEXT_MUTED),
            ("(▲/▼)", "Navigate", COLOR_TEXT_MUTED),
            ("[Tab]", "Category", COLOR_ACCENT_CYAN),
            ("[Esc]", "Quit", COLOR_TEXT_MUTED)
        ]
        hx = 14
        for key, desc, col in hints:
            k_surf = self.font_small.render(key, True, col)
            self.canvas.blit(k_surf, (hx, hint_y))
            hx += k_surf.get_width() + 3
            d_surf = self.font_small.render(desc, True, COLOR_TEXT_MUTED)
            self.canvas.blit(d_surf, (hx, hint_y))
            hx += d_surf.get_width() + 14

    def render(self):
        """Renders the entire canvas and copies to screen with scaling."""
        self.canvas.fill(COLOR_BG_DARK)
        self.draw_top_bar()
        self.draw_library_view()
        self.draw_bottom_bar()

        if not self.screenshot_path:
            scale = min(self.screen_w // CANVAS_WIDTH, self.screen_h // CANVAS_HEIGHT)
            if scale < 1:
                scale = 1
            scaled_w = CANVAS_WIDTH * scale
            scaled_h = CANVAS_HEIGHT * scale
            offset_x = (self.screen_w - scaled_w) // 2
            offset_y = (self.screen_h - scaled_h) // 2

            scaled_canvas = pygame.transform.scale(self.canvas, (scaled_w, scaled_h))
            self.window.fill((0, 0, 0))
            self.window.blit(scaled_canvas, (offset_x, offset_y))
            pygame.display.flip()

    def handle_input(self):
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                logger.info("Window close event received.")
                sys.exit(0)

            elif event.type == pygame.MOUSEBUTTONDOWN:
                if event.button == 1: # Left click
                    mx, my = event.pos
                    # Scale mouse coords into 480x320 virtual canvas
                    scale = min(self.screen_w // CANVAS_WIDTH, self.screen_h // CANVAS_HEIGHT)
                    scale = max(1, scale)
                    scaled_w = CANVAS_WIDTH * scale
                    scaled_h = CANVAS_HEIGHT * scale
                    offset_x = (self.screen_w - scaled_w) // 2
                    offset_y = (self.screen_h - scaled_h) // 2
                    cx = (mx - offset_x) // scale
                    cy = (my - offset_y) // scale

                    # Top tabs click
                    if 8 <= cy <= 32:
                        for i, tx in enumerate([146, 226, 306]):
                            if tx - 30 <= cx <= tx + 30:
                                self.current_tab = i
                                logger.info("Mouse switched to tab %d (%s)", i, self.tabs[i])
                                break
                    # Item list click (left panel)
                    elif 44 <= cx <= 248 and 42 <= cy <= 276:
                        clicked_idx = (cy - 42) // 48
                        items = self.get_current_items()
                        if 0 <= clicked_idx < len(items):
                            if self.selected_indices[self.current_tab] == clicked_idx:
                                self.trigger_action()
                            else:
                                self.selected_indices[self.current_tab] = clicked_idx
                                logger.info("Mouse selected item %d (%s)", clicked_idx, items[clicked_idx].get("title"))
                    # Launch button click (right panel)
                    elif 270 <= cx <= 405 and 215 <= cy <= 245:
                        self.trigger_action()

            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_TAB:
                    self.current_tab = (self.current_tab + 1) % len(self.tabs)
                    logger.info("Tab switched to: %s", self.tabs[self.current_tab])
                elif event.key == pygame.K_RIGHT:
                    self.current_tab = (self.current_tab + 1) % len(self.tabs)
                elif event.key == pygame.K_LEFT:
                    self.current_tab = (self.current_tab - 1) % len(self.tabs)

                elif event.key == pygame.K_UP:
                    items = self.get_current_items()
                    if items:
                        self.selected_indices[self.current_tab] = max(0, self.selected_indices[self.current_tab] - 1)
                elif event.key == pygame.K_DOWN:
                    items = self.get_current_items()
                    if items:
                        self.selected_indices[self.current_tab] = min(len(items) - 1, self.selected_indices[self.current_tab] + 1)

                elif event.key in (pygame.K_RETURN, pygame.K_z, pygame.K_SPACE):
                    self.trigger_action()

                elif event.key in (pygame.K_ESCAPE, pygame.K_x, pygame.K_BACKSPACE):
                    if self.current_tab != 0:
                        self.current_tab = 0
                    else:
                        logger.info("User requested exit from launcher.")
                        sys.exit(0)

            elif event.type == pygame.JOYHATMOTION:
                hat_x, hat_y = event.value
                if hat_y == 1:
                    items = self.get_current_items()
                    if items:
                        self.selected_indices[self.current_tab] = max(0, self.selected_indices[self.current_tab] - 1)
                elif hat_y == -1:
                    items = self.get_current_items()
                    if items:
                        self.selected_indices[self.current_tab] = min(len(items) - 1, self.selected_indices[self.current_tab] + 1)
                elif hat_x == 1:
                    self.current_tab = (self.current_tab + 1) % len(self.tabs)
                elif hat_x == -1:
                    self.current_tab = (self.current_tab - 1) % len(self.tabs)

            elif event.type == pygame.JOYBUTTONDOWN:
                if event.button == 0:
                    self.trigger_action()
                elif event.button == 1:
                    self.current_tab = 0

    def trigger_action(self):
        items = self.get_current_items()
        idx = self.selected_indices[self.current_tab]
        if not items or idx >= len(items):
            return

        item = items[idx]
        handler = item.get("handler")
        logger.info("Trigger action: tab=%d item=%s handler=%s", self.current_tab, item.get("title"), handler)

        if item["type"] == "game":
            run_dir = get_run_dir()
            req_path = os.path.join(run_dir, "launch_request.json")
            req_data = {
                "entry_id": item["id"],
                "content_path": item["content_path"],
                "handler": handler,
                "title": item["title"]
            }
            logger.info("Writing launch request to %s: %s", req_path, req_data)
            with open(req_path, "w", encoding="utf-8") as f:
                json.dump(req_data, f, indent=2)
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

        elif handler == "internal_settings_scale":
            self.cycle_display_scaling()

    def show_system_monitor(self):
        logger.info("Opening System Monitor modal...")
        in_modal = True
        while in_modal:
            for event in pygame.event.get():
                if event.type in (pygame.QUIT,):
                    in_modal = False
                elif event.type == pygame.KEYDOWN and event.key in (pygame.K_ESCAPE, pygame.K_x, pygame.K_b, pygame.K_RETURN, pygame.K_SPACE):
                    in_modal = False
                elif event.type in (pygame.MOUSEBUTTONDOWN, pygame.JOYBUTTONDOWN):
                    in_modal = False

            overlay = pygame.Surface((CANVAS_WIDTH, CANVAS_HEIGHT), pygame.SRCALPHA)
            overlay.fill((0, 0, 0, 180))
            self.canvas.blit(overlay, (0, 0))

            modal_rect = pygame.Rect(40, 30, 400, 260)
            pygame.draw.rect(self.canvas, COLOR_BG_PANEL, modal_rect, border_radius=8)
            pygame.draw.rect(self.canvas, COLOR_ACCENT_GOLD, modal_rect, 2, border_radius=8)

            title_surf = self.font_header.render("SYSTEM DIAGNOSTICS", True, COLOR_ACCENT_GOLD)
            self.canvas.blit(title_surf, (modal_rect.centerx - title_surf.get_width() // 2, 42))

            disk_info = shutil.disk_usage("/")
            disk_free_gb = disk_info.free / (1024**3)
            disk_total_gb = disk_info.total / (1024**3)

            stats = [
                f"OS: GBA OS V1 (antiX appliance)",
                f"Contract: Version {SYS_CONTRACT_VERSION}",
                f"Driver: {pygame.display.get_driver()}",
                f"Resolution: {self.screen_w}x{self.screen_h}",
                f"Storage (/): {disk_free_gb:.1f} GB free of {disk_total_gb:.1f} GB",
                f"ROMs: {len(self.games)} games found in /games",
                f"Gamepads: {pygame.joystick.get_count()} connected",
                f"Log File: latest.log active",
                f"Press (B) or Click to Return"
            ]

            sy = 72
            for s in stats:
                col = COLOR_ACCENT_CYAN if "Press" in s else COLOR_TEXT_LIGHT
                s_surf = self.font_small.render(s, True, col)
                self.canvas.blit(s_surf, (60, sy))
                sy += 18

            self.render()
            self.clock.tick(30)

    def show_controller_tester(self):
        logger.info("Opening Input Diagnostics modal...")
        in_modal = True
        last_key = "None"
        while in_modal:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    in_modal = False
                elif event.type == pygame.KEYDOWN:
                    if event.key in (pygame.K_ESCAPE, pygame.K_BACKSPACE):
                        in_modal = False
                    else:
                        last_key = f"Key: {pygame.key.name(event.key).upper()}"
                elif event.type == pygame.JOYBUTTONDOWN:
                    last_key = f"Button: #{event.button}"
                elif event.type == pygame.JOYHATMOTION:
                    last_key = f"D-Pad: {event.value}"
                elif event.type == pygame.MOUSEBUTTONDOWN and event.pos[1] > 260:
                    in_modal = False

            overlay = pygame.Surface((CANVAS_WIDTH, CANVAS_HEIGHT), pygame.SRCALPHA)
            overlay.fill((0, 0, 0, 180))
            self.canvas.blit(overlay, (0, 0))

            modal_rect = pygame.Rect(40, 30, 400, 260)
            pygame.draw.rect(self.canvas, COLOR_BG_PANEL, modal_rect, border_radius=8)
            pygame.draw.rect(self.canvas, COLOR_ACCENT_CYAN, modal_rect, 2, border_radius=8)

            title_surf = self.font_header.render("INPUT DIAGNOSTICS", True, COLOR_ACCENT_CYAN)
            self.canvas.blit(title_surf, (modal_rect.centerx - title_surf.get_width() // 2, 42))

            num_j = pygame.joystick.get_count()
            j_name = pygame.joystick.Joystick(0).get_name() if num_j > 0 else "Keyboard Only"

            info_lines = [
                f"Connected Gamepads: {num_j}",
                f"Device: {j_name}",
                f"Controls Mapping:",
                f"  D-Pad: Arrow Keys / Controller D-Pad",
                f"  (A) Button: Z / Enter / Button 0",
                f"  (B) Button: X / Backspace / Button 1",
                f"  [Tab]: Switch Category",
                f"Last Detected Input: {last_key}",
                f"Press [Esc] or Click Bottom to Return"
            ]

            sy = 72
            for l in info_lines:
                col = COLOR_ACCENT_GOLD if "Last Detected" in l else COLOR_TEXT_LIGHT
                l_surf = self.font_small.render(l, True, col)
                self.canvas.blit(l_surf, (60, sy))
                sy += 18

            self.render()
            self.clock.tick(30)

    def cycle_display_scaling(self):
        modes = ["Integer Aspect (Crisp)", "Fullscreen Stretched"]
        cur = getattr(self, "scaling_mode", 0)
        self.scaling_mode = (cur + 1) % len(modes)
        logger.info("Switched display scaling mode to: %s", modes[self.scaling_mode])
        self.last_outcome = f"Scale: {modes[self.scaling_mode]}"

    def run(self):
        if self.screenshot_path:
            self.render()
            pygame.image.save(self.canvas, self.screenshot_path)
            print(f"UI preview captured to {self.screenshot_path}")
            return

        while self.running:
            self.handle_input()
            self.render()
            self.clock.tick(60)

def main():
    parser = argparse.ArgumentParser(description="GBA OS Launcher")
    parser.add_argument("--focus", type=str, help="Entry ID to focus")
    parser.add_argument("--outcome", type=str, help="Previous session outcome class")
    parser.add_argument("--tab", type=int, default=0, help="Initial tab index")
    parser.add_argument("--windowed", action="store_true", help="Run in a window")
    parser.add_argument("--screenshot", type=str, help="Save screenshot to file and exit")
    args = parser.parse_args()

    launcher = GBALauncher(
        focus_id=args.focus,
        last_outcome=args.outcome,
        windowed=args.windowed,
        screenshot_path=args.screenshot,
        initial_tab=args.tab
    )
    launcher.run()

if __name__ == "__main__":
    main()
