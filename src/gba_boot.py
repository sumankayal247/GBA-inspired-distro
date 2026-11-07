"""GBA OS's original, controller-friendly boot animation.

The animation is deliberately asset-free: a cartridge drops into an indigo
handheld, its screen lights up, and the two-tone GBA OS wordmark fades in.
Timing policy is separate from drawing so startup can remain bounded and
skippable without a display-dependent test.
"""

import math

import pygame


class BootAnimation:
    MIN_DURATION = 2.4
    MAX_DURATION = 6.0
    SKIP_DELAY = 0.45

    def __init__(self, min_duration: float = MIN_DURATION,
                 max_duration: float = MAX_DURATION):
        self.min_duration = max(0.0, float(min_duration))
        self.max_duration = max(self.min_duration, float(max_duration))

    def can_skip(self, elapsed: float) -> bool:
        return elapsed >= self.SKIP_DELAY

    def should_finish(self, elapsed: float, ready: bool) -> bool:
        """Never adds a long delay, and never holds a huge scan hostage.

        A normal boot gets the complete short sequence. A first-boot ROM hash
        can extend it only to MAX_DURATION; after that the responsive shelf
        takes over and continues showing its existing scanning state.
        """
        if elapsed >= self.max_duration:
            return True
        return elapsed >= self.min_duration and bool(ready)

    @staticmethod
    def _clamp(value: float) -> float:
        return max(0.0, min(1.0, value))

    @staticmethod
    def _ease_out(value: float) -> float:
        value = max(0.0, min(1.0, value))
        return 1.0 - (1.0 - value) ** 3

    def draw(self, surface: pygame.Surface, theme, elapsed: float,
             loading: bool = True) -> None:
        c = theme.colors
        scale = theme.scale
        width = surface.get_width() / scale
        height = surface.get_height() / scale
        cx = width / 2

        # Deep indigo glass with a subtle vertical glow and deterministic
        # pixel-stars (no random flicker between frames).
        surface.fill(c.bg_deep)
        bands = 32
        for index in range(bands):
            distance = abs(index - bands * 0.46) / bands
            color = theme.mix(c.bg, c.bg_deep, min(1.0, distance * 1.8))
            y = index * surface.get_height() // bands
            next_y = (index + 1) * surface.get_height() // bands
            pygame.draw.rect(surface, color,
                             (0, y, surface.get_width(), next_y - y))
        for index in range(30):
            x = (index * 83 + 29) % max(1, surface.get_width())
            y = (index * 47 + 17) % max(1, surface.get_height())
            pulse = 0.35 + 0.25 * math.sin(elapsed * 1.7 + index)
            color = theme.mix(c.border_soft, c.glow, pulse)
            size = max(1, theme.px(0.65 if index % 3 else 1.0))
            surface.fill(color, (x, y, size, size))

        body_w, body_h = 190.0, 92.0
        body_x = cx - body_w / 2
        body_y = height / 2 - 43.0
        body = theme.rect(body_x, body_y, body_w, body_h)

        # The cartridge settles behind the handheld during the first 0.7 s.
        drop = self._ease_out(elapsed / 0.7)
        cart_w, cart_h = 52.0, 43.0
        cart_x = cx - cart_w / 2
        cart_y = body_y - 72.0 + drop * 46.0
        cart = theme.rect(cart_x, cart_y, cart_w, cart_h)
        pygame.draw.rect(surface, c.shadow, cart.move(theme.px(2), theme.px(2)),
                         border_radius=theme.px(4))
        pygame.draw.rect(surface, c.cart_body, cart, border_radius=theme.px(4))
        pygame.draw.rect(surface, c.cart_body_dark,
                         theme.rect(cart_x + 4, cart_y + 3, cart_w - 8, 3),
                         border_radius=theme.px(1))
        label = theme.rect(cart_x + 3, cart_y + 11, cart_w - 6, 24)
        pygame.draw.rect(surface, c.cart_label, label, border_radius=theme.px(2))
        pygame.draw.rect(surface, c.accent_soft,
                         theme.rect(cart_x + 3, cart_y + 11, cart_w - 6, 5),
                         border_radius=theme.px(2))
        theme.label(surface, "GBA OS", (cx, cart_y + 18), c.text,
                    key="micro", align="center")

        # Indigo handheld shell, slot and start/select details.
        pygame.draw.rect(surface, c.shadow, body.move(theme.px(3), theme.px(4)),
                         border_radius=theme.px(12))
        pygame.draw.rect(surface, c.primary_dark, body,
                         border_radius=theme.px(12))
        pygame.draw.rect(surface, theme.mix(c.primary_dark, c.primary, 0.45),
                         body, width=max(1, theme.px(1.2)),
                         border_radius=theme.px(12))
        pygame.draw.rect(surface, c.shadow,
                         theme.rect(cx - 30, body_y - 1, 60, 5),
                         border_radius=theme.px(2))

        screen = theme.rect(cx - 53, body_y + 12, 106, 62)
        pygame.draw.rect(surface, c.shadow, screen, border_radius=theme.px(6))
        power = self._clamp((elapsed - 0.42) / 0.8)
        screen_color = theme.mix(c.screen, c.primary_dark, power * 0.42)
        inner = screen.inflate(-theme.px(5), -theme.px(5))
        pygame.draw.rect(surface, screen_color, inner,
                         border_radius=theme.px(3))
        for y in range(inner.y + theme.px(2), inner.bottom, max(2, theme.px(3))):
            pygame.draw.line(surface, theme.mix(screen_color, c.primary, 0.08),
                             (inner.x + 2, y), (inner.right - 2, y), 1)

        # The original two-tone wordmark fades up inside the screen.
        fade = self._clamp((elapsed - 0.72) / 0.65)
        if fade > 0:
            gba = theme.font("hero").render("GBA", True, c.highlight)
            os_text = theme.font("hero").render("OS", True, c.accent)
            gap = theme.px(4)
            total = gba.get_width() + gap + os_text.get_width()
            x = inner.centerx - total // 2
            y = inner.centery - max(gba.get_height(), os_text.get_height()) // 2
            y += theme.px((1.0 - fade) * 4)
            gba.set_alpha(int(255 * fade))
            os_text.set_alpha(int(255 * fade))
            surface.blit(gba, (x, y))
            surface.blit(os_text, (x + gba.get_width() + gap, y))

        # D-pad and face buttons make the silhouette unmistakably GBA-like.
        pad_x, pad_y = body_x + 20, body_y + 37
        pygame.draw.rect(surface, c.bg_deep, theme.rect(pad_x - 3, pad_y - 10, 6, 20),
                         border_radius=theme.px(1))
        pygame.draw.rect(surface, c.bg_deep, theme.rect(pad_x - 10, pad_y - 3, 20, 6),
                         border_radius=theme.px(1))
        for offset_x, offset_y, radius in ((158, 34, 6), (169, 47, 6)):
            pygame.draw.circle(surface, c.accent_soft,
                               (theme.px(body_x + offset_x), theme.px(body_y + offset_y)),
                               theme.px(radius))
        for offset in (0, 10):
            pygame.draw.line(surface, c.cart_body_dark,
                             (theme.px(body_x + 145 + offset), theme.px(body_y + 81)),
                             (theme.px(body_x + 151 + offset), theme.px(body_y + 81)),
                             max(1, theme.px(2)))

        led_color = c.success if power >= 1 else c.highlight
        pygame.draw.circle(surface, led_color,
                           (theme.px(body_x + 13), theme.px(body_y + 15)),
                           theme.px(1.7))

        # Honest, bounded loading feedback rather than a fake percentage.
        status = "READING YOUR GAME LIBRARY" if loading else "OPENING THE CARTRIDGE SHELF"
        if loading and elapsed > self.min_duration:
            status = "PREPARING GAMES  ·  YOU CAN SKIP"
        theme.label(surface, status, (cx, body_y + body_h + 17), c.text_muted,
                    key="small", align="center")
        track = theme.rect(cx - 72, body_y + body_h + 31, 144, 3)
        pygame.draw.rect(surface, c.panel_alt, track, border_radius=track.height // 2)
        if loading:
            # A small travelling block communicates activity without claiming
            # to know how many bytes a filesystem scan still has to hash.
            travel = (elapsed * 0.7) % 1.0
            block_w = theme.px(34)
            x = track.x + int((track.width - block_w) * travel)
            block = pygame.Rect(x, track.y, block_w, track.height)
        else:
            block = track
        pygame.draw.rect(surface, c.primary, block, border_radius=track.height // 2)

        theme.label(surface, "GBA OS  /  POCKET EDITION", (cx, height - 24),
                    c.text_dim, key="micro", align="center")
        if self.can_skip(elapsed):
            theme.label(surface, "PRESS ANY BUTTON TO SKIP", (width - 10, height - 24),
                        c.text_dim, key="micro", align="right")
