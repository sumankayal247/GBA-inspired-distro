"""
Pygame renderer for the mGBA handler's in-game overlays.

This is the same UI/input approach the launcher already uses: one event loop,
`Theme` design units scaled to the native display, no new GUI framework.

The handler performs a *display handoff* before this renderer is called: it
captures the last gameplay frame (mGBA's native screenshot action) and a
temporary native save state, then stops mGBA so its SDL/KMS/DRM surface is
released.  This renderer therefore gets the display to itself, draws the
captured frame behind a dimmed panel, and on close releases the window
*before* the handler restarts mGBA from that state.  Keyboard, gamepad
hat/buttons and mouse all navigate.

If a display cannot be opened (a driver that refuses to initialise, or a
KMS/DRM session another process still owns), opening fails gracefully: the
overlay is closed and gameplay is restored immediately instead of crashing or
freezing the handler.
"""

import os
import time
from typing import Optional, Tuple

import pygame

import config as settings_store
from gba_ingame import CheatsOverlayModel, SaveStatesOverlayModel, SLOT_COUNT
from theme import Theme


class InGameOverlayRenderer:
    """Draws and drives the Cheats / Save States overlay for one session."""

    def __init__(self, session, size: Tuple[int, int],
                 theme_name: str = "indigo", screenshot_path: Optional[str] = None,
                 windowed: bool = False, logger=None, poll_actions=None):
        self.session = session
        self.poll_actions = poll_actions
        self.size = (max(320, int(size[0])), max(240, int(size[1])))
        self.theme_name = theme_name
        self.screenshot_path = screenshot_path
        self.windowed = windowed
        self.logger = logger
        self.surface = None
        self.background = None
        self.joysticks = []
        self.clock = None
        self._display_ready = False
        self._apply_size(self.size)

    def _apply_size(self, size: Tuple[int, int]) -> None:
        """Recompute the whole design-unit layout for one physical mode."""
        self.size = (max(320, int(size[0])), max(240, int(size[1])))
        self.scale = settings_store.ui_scale_for(self.size[1])
        self.design_w = self.size[0] / self.scale
        self.design_h = self.size[1] / self.scale
        self.theme = Theme(self.theme_name, self.scale)

    @staticmethod
    def _fullscreen_size(fallback: Tuple[int, int]) -> Tuple[int, int]:
        """Use the real display mode, not a guessed default, for FULLSCREEN.

        The handler normally passes the mode it resolved for mGBA. If that was
        unavailable (fallback 960x640), asking SDL for the desktop size keeps
        the overlay truly fullscreen — otherwise the popup would be drawn in a
        small window and its whole design-unit scale would be wrong.
        """
        try:
            info = pygame.display.Info()
            width, height = int(info.current_w), int(info.current_h)
            if width >= 320 and height >= 240:
                return width, height
        except Exception:
            pass
        return fallback

    def _log(self, message: str, *args) -> None:
        if self.logger:
            try:
                self.logger.warning(message, *args)
            except Exception:
                pass

    # ---------------------------------------------------------------- lifecycle
    def open(self) -> bool:
        """Takes the display for the overlay. False means 'resume gameplay'."""
        try:
            if not pygame.get_init():
                pygame.init()
            if not pygame.display.get_init():
                pygame.display.init()
            # SDL's dummy driver has a fixed 1024x768 mode; asking it for a
            # fullscreen window would silently change the overlay geometry.
            dummy = os.environ.get("SDL_VIDEODRIVER") == "dummy"
            fullscreen = not (self.windowed or dummy)
            if fullscreen:
                self._apply_size(self._fullscreen_size(self.size))
            flags = pygame.FULLSCREEN if fullscreen else 0
            self.surface = pygame.display.set_mode(self.size, flags)
            pygame.display.set_caption("GBA OS")
            self._display_ready = True
        except Exception as exc:
            self._log("In-game overlay display unavailable: %s", exc)
            self._display_ready = False
            try:
                self.session.status_message = f"Overlay display unavailable: {exc}"
            except Exception:
                pass
            return False
        if not self.screenshot_path:
            # The handler captured this frame just before the handoff.
            getter = getattr(self.session.backend, "overlay_screenshot", None)
            if callable(getter):
                try:
                    self.screenshot_path = getter()
                except Exception:
                    self.screenshot_path = None
        self._load_background()
        try:
            pygame.joystick.init()
            self.joysticks = [pygame.joystick.Joystick(i)
                              for i in range(pygame.joystick.get_count())]
            for joy in self.joysticks:
                joy.init()
        except Exception:
            self.joysticks = []
        self.clock = pygame.time.Clock()
        return True

    def _load_background(self) -> None:
        """The last native mGBA frame, so the game stays visible behind."""
        self.background = None
        if not self.screenshot_path or not os.path.isfile(self.screenshot_path):
            return
        try:
            image = pygame.image.load(self.screenshot_path).convert()
            self.background = pygame.transform.scale(image, self.size)
        except Exception:
            self.background = None

    def close(self) -> None:
        if self._display_ready:
            try:
                pygame.display.quit()
            except Exception:
                pass
            self._display_ready = False
        self.surface = None
        self.joysticks = []

    def run(self) -> None:
        """Blocks only while an overlay is open; always releases the display.

        The order matters for the display handoff: the overlay window is
        closed *before* gameplay is restarted, otherwise mGBA would have to
        create its SDL/KMS surface while Pygame still owns the screen.
        """
        if not self.open():
            self.session.close_overlay()
            self.session.finish_overlay()
            return
        try:
            while self.session.overlay is not None:
                for event in pygame.event.get():
                    self.handle(event)
                # The raw input watcher keeps running while the overlay is up;
                # drain its actions so the same combo can close the overlay.
                if self.poll_actions:
                    try:
                        self.poll_actions()
                    except Exception:
                        pass
                self.session.tick()
                model = self.session.overlay
                if model is not None and getattr(model, "expired", lambda: False)():
                    model.close()             # temporary badge times out
                self.draw()
                pygame.display.flip()
                self.clock.tick(60)
        finally:
            self.close()
            try:
                self.session.finish_overlay()   # restart mGBA from the state
            except Exception as exc:
                self._log("Gameplay restart failed: %s", exc)

    # -------------------------------------------------------------------- input
    def handle(self, event) -> None:
        model = self.session.overlay
        if model is None:
            return
        if event.type == pygame.QUIT:
            model.close()
            return
        if getattr(model, "kind", "") == "indicator":
            return                            # badge is not interactive
        if event.type == pygame.KEYDOWN:
            key = event.key
            if key in (pygame.K_ESCAPE, pygame.K_BACKSPACE, pygame.K_x):
                self._cancel(model)
            elif key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_z, pygame.K_SPACE):
                self._activate(model)
            elif key == pygame.K_UP:
                self._move(model, 0, -1)
            elif key == pygame.K_DOWN:
                self._move(model, 0, 1)
            elif key == pygame.K_LEFT:
                self._move(model, -1, 0)
            elif key == pygame.K_RIGHT:
                self._move(model, 1, 0)
            return
        if event.type == pygame.JOYHATMOTION:
            dx, dy = event.value
            if dx or dy:
                self._move(model, dx, -dy)
            return
        if event.type == pygame.JOYBUTTONDOWN:
            if event.button == 0:
                self._activate(model)
            elif event.button == 1:
                self._cancel(model)
            elif event.button in (6, 8, 4):
                self._cancel(model)
            return
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            self._click(model, event.pos)
            return

    def _activate(self, model) -> None:
        try:
            if isinstance(model, CheatsOverlayModel):
                model.toggle()
            else:
                model.activate()
        except Exception as exc:
            self._log("Overlay action failed: %s", exc)
            model.message = "Action failed"

    def _cancel(self, model) -> None:
        if isinstance(model, SaveStatesOverlayModel) and model.mode != "grid":
            model.cancel()
        else:
            model.close()

    def _move(self, model, dx: int, dy: int) -> None:
        if isinstance(model, CheatsOverlayModel):
            model.move(dy or dx)
        else:
            model.move(dx, dy)

    def _click(self, model, pos) -> None:
        """Mouse fallback: click to select, click the selection again to act."""
        try:
            if isinstance(model, CheatsOverlayModel):
                row = self._cheat_row_at(pos)
                if row is not None:
                    if row == model.index:
                        self._activate(model)
                    else:
                        model.index = row
            else:
                cell = self._slot_cell_at(pos)
                if cell is not None:
                    if cell == model.index:
                        self._activate(model)
                    else:
                        model.index = cell
        except Exception:
            pass

    # ------------------------------------------------------------------ geometry
    @staticmethod
    def _fit(text: str, font, max_pixels: int) -> str:
        """Clip one line to its box; at small scales fonts round up a pixel."""
        if max_pixels <= 0:
            return ""
        if font.size(text)[0] <= max_pixels:
            return text
        while text and font.size(text + "...")[0] > max_pixels:
            text = text[:-1]
        return text + "..." if text else ""

    def panel_rect(self) -> pygame.Rect:
        width = min(360.0, self.design_w - 16)
        height = min(272.0, self.design_h - 16)
        x = (self.design_w - width) / 2
        y = (self.design_h - height) / 2
        return self.theme.rect(x, y, width, height)

    def _cheat_row_at(self, pos):
        rect = self.panel_rect()
        theme = self.theme
        top = rect.y / theme.scale + 44
        row_h = 22.0
        row = int((pos[1] / theme.scale - top) // row_h)
        model = self.session.overlay
        return row if 0 <= row < len(model.items()) else None

    def _slot_geometry(self):
        rect = self.panel_rect()
        theme = self.theme
        x0 = rect.x / theme.scale + 8
        y0 = rect.y / theme.scale + 38
        width = rect.width / theme.scale - 16
        height = min(196.0, rect.height / theme.scale - 70)
        gap = 5.0
        cell_w = (width - gap * 2) / 3
        cell_h = (height - gap * 2) / 3
        return x0, y0, cell_w, cell_h, gap

    def _slot_cell_at(self, pos):
        theme = self.theme
        x0, y0, cell_w, cell_h, gap = self._slot_geometry()
        x, y = pos[0] / theme.scale, pos[1] / theme.scale
        if x < x0 or y < y0:
            return None
        col = int((x - x0) // (cell_w + gap))
        row = int((y - y0) // (cell_h + gap))
        if not (0 <= col < 3 and 0 <= row < 3):
            return None
        index = row * 3 + col
        return index if index < SLOT_COUNT else None

    # ------------------------------------------------------------------ drawing
    def draw(self) -> None:
        if not self._display_ready or self.surface is None:
            return
        theme, c = self.theme, self.theme.colors
        surface = self.surface
        if self.background is not None:
            surface.blit(self.background, (0, 0))
        else:
            surface.fill(c.bg_deep)
        model = self.session.overlay
        # A speed change draws only the badge: the gameplay frame stays
        # visible and no panel/modal blocks the player.
        indicator_only = getattr(model, "kind", "") == "indicator"
        shade = pygame.Surface(surface.get_size(), pygame.SRCALPHA)
        shade.fill((6, 4, 14, 60 if indicator_only else 190))
        surface.blit(shade, (0, 0))
        if indicator_only or model is None:
            self._draw_indicator()
            return

        rect = self.panel_rect()
        theme.panel(surface, rect, fill=c.panel, border=c.primary,
                    radius=8, width=1.4)
        theme.text(surface, model.title,
                   (rect.centerx / theme.scale, rect.y / theme.scale + 6),
                   "header", c.highlight, align="center")
        theme.label(surface, model.subtitle,
                    (rect.centerx / theme.scale, rect.y / theme.scale + 19),
                    c.text_dim, align="center")
        pygame.draw.line(surface, c.border_soft,
                         (rect.x + theme.px(8), rect.y + theme.px(27)),
                         (rect.right - theme.px(8), rect.y + theme.px(27)), 1)

        if isinstance(model, CheatsOverlayModel):
            self._draw_cheats(model, rect)
        else:
            self._draw_states(model, rect)

        if model.message:
            theme.label(surface, model.message,
                        (rect.x / theme.scale + 8,
                         rect.bottom / theme.scale - 14), c.highlight, key="small")
        self._draw_indicator()

    def _draw_cheats(self, model, rect) -> None:
        theme, c = self.theme, self.theme.colors
        surface = self.surface
        items = model.items()
        x0 = rect.x / theme.scale + 8
        y0 = rect.y / theme.scale + 32
        width = rect.width / theme.scale - 16
        if not items:
            theme.text(surface, "No cheats for this game", (x0 + 2, y0 + 8),
                       "body", c.text_muted)
            theme.label(surface, "Put a .cheats file in DATA/saves/<id>/cheats/.",
                        (x0 + 2, y0 + 26), c.text_dim)
            return
        visible = min(len(items), 9)
        start = max(0, min(model.index - visible + 1, len(items) - visible))
        for offset, index in enumerate(range(start, start + visible)):
            item = items[index]
            y = y0 + offset * 22
            row = theme.rect(x0, y, width, 20)
            selected = index == model.index
            if selected:
                pygame.draw.rect(surface, c.card_sel, row, border_radius=theme.px(4))
                pygame.draw.rect(surface, c.highlight, row, width=1,
                                 border_radius=theme.px(4))
            name = self._fit(item["name"], theme.font("body"),
                             theme.px(width - 60))
            theme.label(surface, name, (x0 + 5, y + 5),
                        c.text if selected else c.text_muted, key="body")
            state = "ENABLED" if item["enabled"] else "DISABLED"
            color = c.success if item["enabled"] else c.text_dim
            chip = theme.rect(x0 + width - 46, y + 4, 42, 12)
            pygame.draw.rect(surface, c.panel_alt, chip,
                             border_radius=theme.px(3))
            theme.label(surface, state,
                        (x0 + width - 25, y + 5.5), color, key="micro",
                        align="center")

    def _draw_states(self, model, rect) -> None:
        theme, c = self.theme, self.theme.colors
        surface = self.surface
        x0, y0, cell_w, cell_h, gap = self._slot_geometry()
        slots = model.slots()
        for index, info in enumerate(slots):
            col, row = index % 3, index // 3
            cell = theme.rect(x0 + col * (cell_w + gap),
                              y0 + row * (cell_h + gap), cell_w, cell_h)
            selected = index == model.index
            pygame.draw.rect(surface, c.card_sel if selected else c.panel_alt, cell,
                             border_radius=theme.px(4))
            pygame.draw.rect(surface, c.highlight if selected else c.border_soft,
                             cell, width=max(1, theme.px(1)),
                             border_radius=theme.px(4))
            theme.label(surface, f"SLOT {info['slot']}",
                        (cell.x / theme.scale + 3, cell.y / theme.scale + 2),
                        c.highlight if selected else c.info, key="micro")
            thumb_rect = theme.rect(cell.x / theme.scale + 3,
                                    cell.y / theme.scale + 11,
                                    cell_w - 6, cell_h - 26)
            if info["has_thumbnail"]:
                self._draw_thumbnail(info["thumbnail_path"], thumb_rect)
            else:
                pygame.draw.rect(surface, c.bg_deep, thumb_rect,
                                 border_radius=theme.px(2))
                label = "CORRUPT" if info["corrupt"] else \
                    ("EMPTY" if not info["occupied"] else "NO IMAGE")
                theme.label(surface, label,
                            (thumb_rect.centerx / theme.scale,
                             thumb_rect.centery / theme.scale - 4),
                            c.danger if info["corrupt"] else c.text_dim,
                            key="micro", align="center")
            stamp = info["timestamp_label"] if info["occupied"] else "—"
            stamp = self._fit(stamp, theme.font("micro"),
                              cell.width - theme.px(6))
            theme.label(surface, stamp,
                        (cell.x / theme.scale + 3,
                         cell.bottom / theme.scale - 10), c.text_muted,
                        key="micro")
        if model.mode in ("actions", "confirm"):
            self._draw_state_actions(model, rect)

    def _draw_thumbnail(self, path: str, rect: pygame.Rect) -> None:
        try:
            image = pygame.image.load(path).convert()
            fitted = pygame.transform.scale(image, (rect.width, rect.height))
            self.surface.blit(fitted, rect.topleft)
        except Exception:
            pygame.draw.rect(self.surface, self.theme.colors.bg_deep, rect)

    def _draw_state_actions(self, model, rect) -> None:
        theme, c = self.theme, self.theme.colors
        w, h = 190.0, 88.0
        box = theme.rect((self.design_w - w) / 2, (self.design_h - h) / 2, w, h)
        theme.panel(self.surface, box, fill=c.panel, border=c.danger
                    if model.mode == "confirm" else c.primary, radius=6, width=1.4)
        slot = model.slot
        if model.mode == "confirm":
            heading = f"OVERWRITE SLOT {slot}?"
            lines = ["This replaces the state and thumbnail.",
                     "[A] CONFIRM OVERWRITE     [B] CANCEL"]
        else:
            heading = f"SLOT {slot}"
            lines = [f"{model.ACTIONS[0]}  ·  {model.ACTIONS[1]}",
                     "Use ←/↑ or ↓/→, then [A]"]
        theme.text(self.surface, heading,
                   (box.centerx / theme.scale, box.y / theme.scale + 7),
                   "body_bold", c.highlight, align="center")
        for index, line in enumerate(lines):
            line = self._fit(line, theme.font("micro"),
                             box.width - theme.px(16))
            theme.label(self.surface, line,
                        (box.centerx / theme.scale, box.y / theme.scale + 28 + index * 12),
                        c.text, key="small", align="center")
        if model.mode == "actions":
            for index, action in enumerate(model.ACTIONS):
                y = box.bottom / theme.scale - 28 + index * 12
                chip = theme.rect(box.x / theme.scale + 8, y, w - 16, 11)
                if index == model.action_index:
                    pygame.draw.rect(self.surface, c.card_sel, chip,
                                     border_radius=theme.px(3))
                theme.label(self.surface, action,
                            (box.centerx / theme.scale, y + 2),
                            c.highlight if index == model.action_index else c.text_muted,
                            key="micro", align="center")

    def _draw_indicator(self) -> None:
        """Small temporary 2x / 3x / Normal badge."""
        if not self.session.indicator:
            return
        theme, c = self.theme, self.theme.colors
        font = theme.font("body_bold")
        label = self.session.indicator
        surf = font.render(label, True, c.bg_deep)
        chip = pygame.Rect(0, 0, surf.get_width() + theme.px(18), theme.px(18))
        chip.topright = (self.surface.get_width() - theme.px(10), theme.px(10))
        pygame.draw.rect(self.surface, c.highlight, chip,
                         border_radius=theme.px(6))
        self.surface.blit(surf, (chip.x + theme.px(9),
                                 chip.centery - surf.get_height() // 2))
