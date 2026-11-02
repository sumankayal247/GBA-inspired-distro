"""
Visual language for GBA OS.

Palette and primitives inspired by the Game Boy Advance family: the indigo
shell, the purple logo, the magenta swoosh, the amber power LED and the grey
cartridge plastic with its dark sticker.

All geometry is expressed in "design units" against a 320-unit reference
height; `Theme.px()` converts to real pixels so the same layout code renders
crisply at 768p, 1080p, 1440p and on a 480x320 panel alike.
"""

import os
from typing import Dict, List, Optional, Tuple

import pygame

DESIGN_HEIGHT = 320

Color = Tuple[int, int, int]


class Palette:
    """A named colour set. Themes swap the primary/accent hues only."""

    def __init__(self, **values):
        self.__dict__.update(values)

    def as_dict(self) -> Dict[str, Color]:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}


# Shared neutrals (the "GBA shell" greys and the dark screen glass).
_BASE = dict(
    bg=(19, 15, 38),
    bg_deep=(13, 10, 27),
    panel=(38, 29, 66),
    panel_alt=(30, 23, 54),
    card=(52, 40, 88),
    card_sel=(78, 60, 132),
    border=(74, 59, 118),
    border_soft=(55, 44, 88),
    text=(246, 244, 255),
    text_muted=(168, 158, 205),
    text_dim=(120, 111, 152),
    cart_body=(166, 171, 186),
    cart_body_dark=(120, 126, 146),
    cart_label=(44, 28, 78),
    cart_rib=(190, 195, 208),
    metal=(206, 211, 224),
    shadow=(9, 7, 20),
    screen=(12, 10, 24),
)

THEMES: Dict[str, Palette] = {
    # The classic: indigo shell, purple logo, magenta swoosh.
    "indigo": Palette(
        **_BASE,
        primary=(124, 106, 240),
        primary_dark=(78, 64, 172),
        accent=(255, 122, 182),      # GBA logo magenta
        accent_soft=(196, 84, 148),
        highlight=(255, 200, 87),    # amber power LED / start button
        success=(99, 230, 165),
        danger=(255, 107, 107),
        info=(111, 214, 255),
        glow=(150, 132, 255),
    ),
    # Game Boy Color "Berry" vibes.
    "violet": Palette(
        **_BASE,
        primary=(168, 98, 232),
        primary_dark=(104, 56, 158),
        accent=(255, 154, 214),
        accent_soft=(198, 106, 168),
        highlight=(255, 214, 122),
        success=(126, 232, 176),
        danger=(255, 118, 118),
        info=(126, 206, 255),
        glow=(200, 140, 255),
    ),
    # "Dandelion" shell: warm amber with indigo ink.
    "amber": Palette(
        **{**_BASE,
           "bg": (26, 18, 30), "bg_deep": (18, 12, 22),
           "panel": (48, 33, 48), "panel_alt": (38, 26, 40),
           "card": (66, 45, 64), "card_sel": (102, 70, 88),
           "border": (96, 70, 84), "border_soft": (70, 51, 62),
           "primary": (255, 190, 92), "primary_dark": (190, 132, 56),
           "accent": (255, 128, 128), "accent_soft": (198, 98, 98),
           "highlight": (255, 236, 160), "success": (168, 226, 138),
           "danger": (255, 110, 96), "info": (140, 214, 246),
           "glow": (255, 210, 130)},
    ),
    # Game Boy Pocket "mint" with deep green glass.
    "mint": Palette(
        **{**_BASE,
           "bg": (12, 26, 24), "bg_deep": (8, 18, 17),
           "panel": (22, 44, 40), "panel_alt": (17, 35, 32),
           "card": (30, 58, 52), "card_sel": (46, 84, 74),
           "border": (46, 82, 74), "border_soft": (33, 60, 54),
           "primary": (108, 224, 178), "primary_dark": (64, 158, 124),
           "accent": (255, 196, 112), "accent_soft": (196, 148, 84),
           "highlight": (236, 255, 214), "success": (140, 236, 170),
           "danger": (255, 120, 120), "info": (120, 214, 240),
           "glow": (150, 240, 200)},
    ),
}

THEME_ORDER = ["indigo", "violet", "amber", "mint"]

# Optional bundled fonts, then common system paths.
_FONT_DIRS = [
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "fonts"),
    "/opt/gba-distro/assets/fonts",
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/dejavu",
    "/usr/share/fonts/TTF",
    "/usr/share/fonts/truetype/liberation",
]
_FONT_FILES = {
    "sans": ["DejaVuSans.ttf", "LiberationSans-Regular.ttf"],
    "sans_bold": ["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"],
    "mono": ["DejaVuSansMono.ttf", "LiberationMono-Regular.ttf"],
    "mono_bold": ["DejaVuSansMono-Bold.ttf", "LiberationMono-Bold.ttf"],
}


def _find_font(kind: str) -> Optional[str]:
    for directory in _FONT_DIRS:
        for name in _FONT_FILES.get(kind, []):
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                return path
    return None


class Theme:
    """Palette + fonts + unit conversion for one rendering surface."""

    # Type ramp in design units (multiplied by the UI scale at load time).
    TYPE = {
        "micro": 7,
        "small": 9,
        "body": 10,
        "body_bold": 10,
        "header": 12,
        "title": 15,
        "hero": 20,
    }

    def __init__(self, name: str = "indigo", scale: float = 1.0):
        self.name = name if name in THEMES else "indigo"
        self.colors = Palette(**THEMES[self.name].as_dict())
        self.scale = scale
        self._fonts: Dict[str, pygame.font.Font] = {}
        self._mono_fonts: Dict[int, pygame.font.Font] = {}

    # -- units ---------------------------------------------------------
    def px(self, value: float) -> int:
        """Design units -> pixels (at least 1)."""
        return max(1, int(round(value * self.scale)))

    def pt(self, value: float) -> int:
        """Design units -> pixels without rounding up to a minimum."""
        return int(round(value * self.scale))

    def rect(self, x: float, y: float, w: float, h: float) -> pygame.Rect:
        return pygame.Rect(self.px(x), self.px(y), max(1, self.px(w)), max(1, self.px(h)))

    # -- fonts ---------------------------------------------------------
    def font(self, key: str = "body") -> pygame.font.Font:
        if key not in self._fonts:
            size = self.px(self.TYPE.get(key, 10))
            bold = key.endswith("_bold")
            path = _find_font("sans_bold" if bold else "sans")
            self._fonts[key] = self._load(path, size)
        return self._fonts[key]

    def mono(self, size_units: float = 9) -> pygame.font.Font:
        pixels = self.px(size_units)
        if pixels not in self._mono_fonts:
            self._mono_fonts[pixels] = self._load(_find_font("mono"), pixels)
        return self._mono_fonts[pixels]

    @staticmethod
    def _load(path: Optional[str], size: int) -> pygame.font.Font:
        if path:
            try:
                return pygame.font.Font(path, size)
            except Exception:
                pass
        return pygame.font.Font(None, max(8, int(size * 1.25)))

    def char_cell(self, size_units: float = 9) -> Tuple[int, int]:
        font = self.mono(size_units)
        return max(4, font.size("M")[0]), max(6, font.get_linesize())

    # -- colour helpers ------------------------------------------------
    def mix(self, a: Color, b: Color, t: float) -> Color:
        t = max(0.0, min(1.0, t))
        return (
            int(a[0] + (b[0] - a[0]) * t),
            int(a[1] + (b[1] - a[1]) * t),
            int(a[2] + (b[2] - a[2]) * t),
        )

    # -- primitives ----------------------------------------------------
    def panel(self, surface, rect: pygame.Rect, fill: Optional[Color] = None,
              border: Optional[Color] = None, radius: float = 6, width: float = 1) -> None:
        c = self.colors
        pygame.draw.rect(surface, fill or c.panel, rect, border_radius=self.px(radius))
        if width:
            pygame.draw.rect(surface, border or c.border, rect,
                             width=max(1, self.px(width)),
                             border_radius=self.px(radius))

    def bar(self, surface, rect: pygame.Rect, fraction: float, color: Color,
            track: Optional[Color] = None) -> None:
        c = self.colors
        pygame.draw.rect(surface, track or c.panel_alt, rect, border_radius=rect.height // 2)
        width = int(rect.width * max(0.0, min(1.0, fraction)))
        if 0 < width < 3:
            width = 3
        if width > 1:
            pygame.draw.rect(surface, color,
                             pygame.Rect(rect.x, rect.y, width, rect.height),
                             border_radius=rect.height // 2)

    def text(self, surface, value: str, pos: Tuple[float, float], key: str = "body",
             color: Optional[Color] = None, align: str = "left") -> pygame.Rect:
        font = self.font(key)
        surf = font.render(value, True, color or self.colors.text)
        x, y = self.px(pos[0]), self.px(pos[1])
        if align == "center":
            x -= surf.get_width() // 2
        elif align == "right":
            x -= surf.get_width()
        surface.blit(surf, (x, y))
        return pygame.Rect(x, y, surf.get_width(), surf.get_height())

    def label(self, surface, value: str, pos: Tuple[float, float], color: Optional[Color] = None,
              key: str = "small", align: str = "left") -> pygame.Rect:
        return self.text(surface, value, pos, key, color, align)

    def button(self, surface, rect: pygame.Rect, label: str, accent: Optional[Color] = None,
               filled: bool = True, text_color: Optional[Color] = None,
               key: str = "small") -> None:
        c = self.colors
        accent = accent or c.primary
        fill = self.mix(c.panel, accent, 0.22) if filled else c.panel_alt
        pygame.draw.rect(surface, fill, rect, border_radius=self.px(4))
        pygame.draw.rect(surface, accent, rect, width=max(1, self.px(1.2)),
                         border_radius=self.px(4))
        font = self.font(key)
        surf = font.render(label, True, text_color or (c.text if filled else c.text_muted))
        surface.blit(surf, (rect.centerx - surf.get_width() // 2,
                            rect.centery - surf.get_height() // 2))

    # -- the signature element: a GBA cartridge ------------------------
    def cartridge(self, surface, rect: pygame.Rect, title: str, fmt: str,
                  ident: str = "", selected: bool = False, dim: bool = False,
                  badge: Optional[str] = None) -> None:
        """
        Draws a Game Boy Advance cartridge: notched top, grip ribs, the
        magazine-style sticker with a format banner, and the "seal" badge.
        """
        c = self.colors
        s = self.scale
        x, y, w, h = rect

        def u(v: float) -> int:
            return int(round(v * s))

        body_top = c.cart_body if not dim else self.mix(c.cart_body, c.panel, 0.55)
        body_bottom = c.cart_body_dark if not dim else self.mix(c.cart_body_dark, c.panel, 0.55)

        # Drop shadow + shell with a vertical gradient for a plastic feel.
        pygame.draw.rect(surface, c.shadow, (x + u(2), y + u(3), w, h), border_radius=u(6))
        steps = max(4, h // max(1, u(3)))
        for i in range(steps):
            t = i / float(steps - 1)
            band = pygame.Rect(x, y + int(h * i / steps), w, int(h / steps) + 1)
            pygame.draw.rect(surface, self.mix(body_top, body_bottom, t), band)
        pygame.draw.rect(surface, (0, 0, 0), (x, y, w, h), width=1, border_radius=u(6))

        # Notch + grip ribs along the top edge.
        pygame.draw.rect(surface, self.mix(body_bottom, (0, 0, 0), 0.18),
                         (x + u(6), y, w - u(12), u(5)))
        for r in range(3):
            rib_x = x + u(9) + r * u(4)
            pygame.draw.line(surface, c.cart_rib, (rib_x, y + u(7)),
                             (rib_x, y + u(11)), max(1, u(1.2)))

        # Sticker.
        pad = u(5)
        label = pygame.Rect(x + pad, y + u(14), w - pad * 2, h - u(20))
        pygame.draw.rect(surface, c.cart_label, label, border_radius=u(3))
        pygame.draw.rect(surface, c.primary if not dim else c.border_soft, label,
                         width=max(1, u(0.8)), border_radius=u(3))

        # Magenta banner with the platform name.
        banner = pygame.Rect(label.x, label.y, label.width, u(12))
        pygame.draw.rect(surface, self.mix(c.accent_soft, c.primary_dark, 0.35), banner,
                         border_top_left_radius=u(3), border_top_right_radius=u(3))
        banner_font = self.font("micro")
        platform = f"GAME BOY {fmt.upper()}" if fmt.upper() in ("GBA", "GBC", "GB") else fmt.upper()
        surf = banner_font.render(platform, True, c.text)
        if surf.get_width() > banner.width - u(4):
            surf = banner_font.render(fmt.upper(), True, c.text)
        surface.blit(surf, (banner.x + u(3), banner.y + (banner.height - surf.get_height()) // 2))

        # Title, wrapped inside the space left between banner and footer so the
        # seal and the id chip can never sit on top of it.
        footer = u(12) if h > u(52) else 0
        title_area = label.height - banner.height - footer - u(5)
        title_font = self.font("body_bold" if label.width > u(42) else "small")
        line_h = max(1, title_font.get_linesize())
        max_lines = max(1, title_area // line_h)
        _blit_wrapped(surface, title_font, title, label.x + u(3),
                      banner.bottom + u(2), label.width - u(6),
                      c.highlight, max_lines=max_lines)

        # Bottom row: id chip + seal.
        if h > u(52):
            id_font = self.font("micro")
            seal_r = u(5.5)
            id_width = label.width - u(6) - int(seal_r * 2) - u(3)
            short = ident[:8]
            while short and id_font.size(short)[0] > id_width:
                short = short[:-1]
            id_surf = id_font.render(short, True, self.mix(c.text_muted, c.cart_label, 0.25))
            surface.blit(id_surf, (label.x + u(3),
                                   label.bottom - u(9) - id_surf.get_height() // 2))

            seal_center = (label.right - seal_r - u(3), label.bottom - u(6))
            pygame.draw.circle(surface, c.highlight, seal_center, seal_r, width=max(1, u(0.9)))
            seal_font = self.font("micro")
            seal = seal_font.render("GBA", True, c.highlight)
            surface.blit(seal, (seal_center[0] - seal.get_width() // 2,
                                seal_center[1] - seal.get_height() // 2))

        if badge:
            badge_font = self.font("micro")
            b_surf = badge_font.render(badge, True, c.bg_deep)
            chip = pygame.Rect(0, 0, b_surf.get_width() + u(6), u(10))
            chip.topright = (rect.right - u(4), rect.top + u(4))
            pygame.draw.rect(surface, c.success, chip, border_radius=u(5))
            surface.blit(b_surf, (chip.x + u(3), chip.y + (chip.height - b_surf.get_height()) // 2))

        if selected:
            pygame.draw.rect(surface, c.highlight, rect,
                             width=max(2, u(1.6)), border_radius=u(6))


def _blit_wrapped(surface, font, text: str, x: int, y: int, width: int,
                  color: Color, max_lines: int = 2) -> int:
    """
    Word-wraps `text` into at most `max_lines` lines.

    A word wider than the sticker is elided rather than allowed to bleed over
    the plastic (long titles are common: "Pocket Rally Advance"). Returns the
    next y.
    """
    words = text.split()
    lines: List[str] = []
    current = ""
    hidden = False
    for word in words:
        candidate = f"{current} {word}".strip()
        if font.size(candidate)[0] <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
            if len(lines) >= max_lines:
                hidden = True
                current = ""
                break
        current = word
    if current:
        if len(lines) < max_lines:
            lines.append(current)
        else:
            hidden = True

    line_h = font.get_linesize()
    for index, line in enumerate(lines[:max_lines]):
        elide = hidden or font.size(line)[0] > width
        if elide:
            suffix = ".."
            while line and font.size(line + suffix)[0] > width:
                line = line[:-1]
            line = line.rstrip() + suffix if line else line
        surface.blit(font.render(line, True, color), (x, y + index * line_h))
    return y + len(lines) * line_h
