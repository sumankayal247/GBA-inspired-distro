#!/usr/bin/env python3
"""
Tests for the terminal's MOUSE support and its full-screen-program features.

A modern terminal program (an editor, a dashboard, the Antigravity CLI) needs
more than a grid of characters: it switches to the alternate screen, paints
in truecolor, asks where the cursor is and - if it wants a pointer - asks
for mouse reports. These tests cover that contract, because a terminal that
gets any of it wrong does not run such a program at all.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame  # noqa: E402

import gba_terminal  # noqa: E402
import theme as theme_module  # noqa: E402


class TestAlternateScreen(unittest.TestCase):
    def setUp(self):
        self.screen = gba_terminal.TerminalScreen(cols=20, rows=5)

    def test_alternate_screen_is_a_separate_grid(self):
        self.screen.feed(b"primary")
        self.screen.feed(b"\x1b[?1049h")
        self.assertTrue(self.screen.use_alternate)
        self.screen.feed(b"full screen program")
        self.assertIn("full screen program", self.screen.text())
        self.assertNotIn("primary", self.screen.text())

    def test_leaving_the_alternate_screen_restores_the_primary_one(self):
        self.screen.feed(b"primary")
        self.screen.feed(b"\x1b[?1049h\x1b[2J\x1b[Hdrawing")
        self.screen.feed(b"\x1b[?1049l")
        self.assertFalse(self.screen.use_alternate)
        self.assertIn("primary", self.screen.text())
        self.assertNotIn("drawing", self.screen.text())

    def test_alternate_screen_starts_empty(self):
        self.screen.feed(b"\x1b[?1049h")
        self.assertEqual(self.screen.text().strip(), "")


class TestMouseReporting(unittest.TestCase):
    def setUp(self):
        self.screen = gba_terminal.TerminalScreen(cols=80, rows=24)

    def test_mouse_is_not_tracked_until_the_program_asks(self):
        self.assertFalse(self.screen.mouse_tracking())
        self.screen.feed(b"\x1b[?1000h")
        self.assertTrue(self.screen.mouse_tracking())

    def test_sgr_report_when_the_program_asks_for_it(self):
        self.screen.feed(b"\x1b[?1000h\x1b[?1006h")
        report = self.screen.encode_mouse(gba_terminal.MOUSE_LEFT, 5, 3)
        self.assertEqual(report, b"\x1b[<0;5;3M")

    def test_release_uses_the_lowercase_final_byte(self):
        self.screen.feed(b"\x1b[?1000h\x1b[?1006h")
        report = self.screen.encode_mouse(gba_terminal.MOUSE_LEFT, 5, 3, "release")
        self.assertEqual(report, b"\x1b[<0;5;3m")

    def test_x10_is_the_fallback(self):
        self.screen.feed(b"\x1b[?1000h")
        report = self.screen.encode_mouse(gba_terminal.MOUSE_LEFT, 5, 3)
        self.assertEqual(report, b"\x1b[M %#")

    def test_drag_is_only_reported_when_asked_for(self):
        self.screen.feed(b"\x1b[?1000h")
        self.assertFalse(self.screen.mouse_motion(button_down=True))
        self.screen.feed(b"\x1b[?1002h")
        self.assertTrue(self.screen.mouse_motion(button_down=True))
        self.screen.feed(b"\x1b[?1003h")
        self.assertTrue(self.screen.mouse_motion(button_down=False))

    def test_coordinates_are_one_based_and_clamped(self):
        self.screen.feed(b"\x1b[?1000h\x1b[?1006h")
        report = self.screen.encode_mouse(gba_terminal.MOUSE_LEFT, 0, 0)
        self.assertEqual(report, b"\x1b[<0;1;1M")
        report = self.screen.encode_mouse(gba_terminal.MOUSE_LEFT, 500, 500)
        self.assertEqual(report, b"\x1b[<0;80;24M")

    def test_mouse_modes_are_forgotten_when_switched_off(self):
        self.screen.feed(b"\x1b[?1000h")
        self.screen.feed(b"\x1b[?1000l")
        self.assertFalse(self.screen.mouse_tracking())


class TestProgramQueries(unittest.TestCase):
    def setUp(self):
        self.screen = gba_terminal.TerminalScreen(cols=80, rows=24)

    def test_cursor_position_report_is_queued(self):
        self.screen.feed(b"\x1b[3;7H")
        self.screen.feed(b"\x1b[6n")
        self.assertEqual(self.screen.responses, [b"\x1b[3;7R"])

    def test_device_attributes_are_answered(self):
        self.screen.feed(b"\x1b[c")
        self.assertEqual(self.screen.responses, [b"\x1b[?1;2c"])

    def test_kitty_keyboard_query_falls_back_to_the_classic_keys(self):
        self.screen.feed(b"\x1b[?u")
        self.assertEqual(self.screen.responses, [b"\x1b[?0u"])

    def test_window_title_is_captured(self):
        self.screen.feed(b"\x1b]0;Antigravity CLI\x07")
        self.assertEqual(self.screen.title, "Antigravity CLI")

    def test_clipboard_write_is_kept(self):
        import base64
        blob = base64.b64encode(b"copied text").decode()
        self.screen.feed(f"\x1b]52;c;{blob}\x07".encode())
        self.assertEqual(self.screen.clipboard, "copied text")


class TestScreenFeatures(unittest.TestCase):
    def setUp(self):
        self.screen = gba_terminal.TerminalScreen(cols=20, rows=6)

    def test_truecolor_is_parsed(self):
        self.screen.feed(b"\x1b[38;2;10;20;30mX")
        self.assertEqual(self.screen.lines()[0][0].fg, (10, 20, 30))

    def test_background_colour_is_kept(self):
        self.screen.feed(b"\x1b[41mX")
        self.assertEqual(self.screen.lines()[0][0].bg,
                         gba_terminal.ANSI_COLORS[1])

    def test_scroll_region_keeps_the_text_above_it(self):
        self.screen.feed(b"top\r\nsecond\r\nthird\r\nfourth")
        self.screen.feed(b"\x1b[2;4r")
        self.screen.feed(b"\x1b[4;1H\r\nX")
        text = self.screen.text()
        self.assertIn("top", text)
        self.assertIn("X", text)

    def test_insert_and_delete_characters(self):
        self.screen.feed(b"abcd")
        self.screen.feed(b"\x1b[1;1H\x1b[2@")
        self.assertTrue(self.screen.text().startswith("  abcd"))
        self.screen.feed(b"\x1b[1;1H\x1b[2P")
        self.assertTrue(self.screen.text().startswith("abcd"))

    def test_erase_characters(self):
        self.screen.feed(b"abcdef")
        self.screen.feed(b"\x1b[1;2H\x1b[2X")
        self.assertTrue(self.screen.text().startswith("a  def"))

    def test_the_background_colour_query_is_answered(self):
        # A modern TUI asks what colour the background is before it picks a
        # palette, and waits for the answer.
        self.screen.feed(b"\x1b]11;?\x1b\\")
        self.assertTrue(self.screen.responses)
        answer = self.screen.responses[0].decode("ascii")
        self.assertTrue(answer.startswith("\x1b]11;rgb:"))
        self.assertTrue(answer.endswith("\x1b\\"))

    def test_the_foreground_colour_query_is_answered(self):
        self.screen.feed(b"\x1b]10;?\x07")
        self.assertTrue(self.screen.responses[0].decode("ascii").startswith("\x1b]10;"))

    def test_the_window_size_is_reported_in_characters(self):
        self.screen.feed(b"\x1b[18t")
        self.assertEqual(self.screen.responses[0], b"\x1b[8;6;20t")
        self.screen.feed(b"\x1b[19t")
        self.assertEqual(self.screen.responses[1], b"\x1b[9;6;20t")

    def test_the_text_area_is_reported_in_pixels(self):
        self.screen.cell_size = (10, 20)
        self.screen.feed(b"\x1b[14t")
        self.assertEqual(self.screen.responses[0], b"\x1b[4;120;200t")

    def test_erase_to_the_end_of_the_screen_clears_the_rows_below(self):
        self.screen.feed(b"one\r\ntwo\r\nthree")
        self.screen.feed(b"\x1b[2;2H\x1b[J")
        lines = self.screen.text().split("\n")
        self.assertEqual(lines[0], "one")
        self.assertEqual(lines[1], "t")
        self.assertEqual(lines[2].strip(), "")

    def test_erase_to_the_start_of_the_screen_clears_the_rows_above(self):
        self.screen.feed(b"one\r\ntwo\r\nthree")
        self.screen.feed(b"\x1b[3;2H\x1b[1J")
        lines = self.screen.text().split("\n")
        self.assertEqual(lines[0].strip(), "")
        self.assertEqual(lines[1].strip(), "")
        # The cursor cell belongs to the erase, exactly as in xterm.
        self.assertEqual(lines[2], "  ree")

    def test_bracketed_paste_mode_is_remembered(self):
        self.screen.feed(b"\x1b[?2004h")
        self.assertTrue(self.screen.bracketed_paste)
        self.screen.feed(b"\x1b[?2004l")
        self.assertFalse(self.screen.bracketed_paste)

    def test_autowrap_can_be_switched_off(self):
        self.screen.feed(b"\x1b[?7l")
        self.assertFalse(self.screen.autowrap)
        self.screen.feed(b"\x1b[?7h")
        self.assertTrue(self.screen.autowrap)

    def test_cursor_visibility_follows_the_program(self):
        self.screen.feed(b"\x1b[?25l")
        self.assertFalse(self.screen.cursor_visible)
        self.screen.feed(b"\x1b[?25h")
        self.assertTrue(self.screen.cursor_visible)


class TestKeyTranslation(unittest.TestCase):
    def event(self, key, mod=0, unicode_=""):
        return pygame.event.Event(pygame.KEYDOWN, key=key, mod=mod, unicode=unicode_)

    def test_plain_arrow_is_the_classic_sequence(self):
        self.assertEqual(gba_terminal.key_to_bytes(self.event(pygame.K_UP)), b"\x1b[A")

    def test_ctrl_arrow_carries_the_modifier(self):
        self.assertEqual(
            gba_terminal.key_to_bytes(self.event(pygame.K_UP, mod=pygame.KMOD_CTRL)),
            b"\x1b[1;5A")

    def test_shift_arrow_carries_the_modifier(self):
        self.assertEqual(
            gba_terminal.key_to_bytes(self.event(pygame.K_RIGHT, mod=pygame.KMOD_SHIFT)),
            b"\x1b[1;2C")

    def test_ctrl_shift_arrow(self):
        self.assertEqual(
            gba_terminal.key_to_bytes(self.event(pygame.K_LEFT,
                                                 mod=pygame.KMOD_CTRL | pygame.KMOD_SHIFT)),
            b"\x1b[1;6D")

    def test_shift_tab_is_the_backwards_tab(self):
        self.assertEqual(
            gba_terminal.key_to_bytes(self.event(pygame.K_TAB, mod=pygame.KMOD_SHIFT)),
            b"\x1b[Z")

    def test_alt_letter_is_escaped(self):
        self.assertEqual(
            gba_terminal.key_to_bytes(self.event(pygame.K_b, mod=pygame.KMOD_ALT,
                                                 unicode_="b")),
            b"\x1bb")

    def test_modified_page_key(self):
        self.assertEqual(
            gba_terminal.key_to_bytes(self.event(pygame.K_PAGEUP,
                                                 mod=pygame.KMOD_CTRL)),
            b"\x1b[5;5~")

    def test_home_and_end(self):
        self.assertEqual(gba_terminal.key_to_bytes(self.event(pygame.K_HOME)), b"\x1b[H")
        self.assertEqual(gba_terminal.key_to_bytes(self.event(pygame.K_END)), b"\x1b[F")


class TestTerminalView(unittest.TestCase):
    def setUp(self):
        pygame.init()
        pygame.font.init()
        self.screen = gba_terminal.TerminalScreen(cols=40, rows=10)
        self.view = gba_terminal.TerminalView(self.screen, theme_module.Theme("indigo", 2.0), 8)
        self.rect = pygame.Rect(0, 0, 800, 400)

    def test_pointer_is_mapped_to_a_cell(self):
        cw, ch, cols, rows = self.view.metrics(self.rect)
        origin_x, origin_y = self.view.origin(self.rect)
        cell = self.view.cell_at(self.rect, origin_x + cw * 2 + 1, origin_y + ch * 3 + 1)
        self.assertEqual(cell, (2, 3))

    def test_pointer_outside_the_grid_has_no_cell(self):
        self.assertIsNone(self.view.cell_at(self.rect, 2, 2))

    def test_scrolling_back_shows_the_history(self):
        for index in range(20):
            self.screen.feed(f"line{index}\r\n".encode())
        self.view.scroll_offset = 3
        rows = self.view.visible_rows()
        self.assertTrue(any("line16" in "".join(c.char for c in row) for row in rows))

    def test_drawing_does_not_raise(self):
        surface = pygame.Surface((800, 400))
        self.screen.feed(b"\x1b[31mred\x1b[0m \x1b[48;2;10;20;30m bg")
        self.view.draw(surface, self.rect, focused=True, cursor_on=True)


class TestMouseOverTheWire(unittest.TestCase):
    """The whole loop: program asks, terminal encodes, program receives."""

    def test_a_report_reaches_the_program_through_the_pty(self):
        import os
        import tempfile
        import time

        marker = os.path.join(tempfile.mkdtemp(), "probe.txt")
        program = (
            "import sys, os; "
            "sys.stdout.write('\\033[?1000h\\033[?1006h'); sys.stdout.flush(); "
            "d = os.read(0, 9); "
            f"open({marker!r},'w').write(repr(d))"
        )
        session = gba_terminal.TerminalSession(cols=80, rows=20, shell="/bin/bash")
        screen = gba_terminal.TerminalScreen(cols=80, rows=20)
        try:
            if session.error:
                self.skipTest(f"no pty available: {session.error}")
            # raw mode, the way a program that wants immediate events does it
            session.write(f"stty raw -echo; python3 -c \"{program}\"\n".encode())
            deadline = time.time() + 5
            while time.time() < deadline and not os.path.exists(marker):
                data = session.drain()
                if data:
                    screen.feed(data)
                time.sleep(0.05)

            self.assertTrue(screen.mouse_tracking(), "the program did not ask for mouse")
            report = screen.encode_mouse(gba_terminal.MOUSE_LEFT, 5, 3, "press")
            session.write(report)

            deadline = time.time() + 5
            received = ""
            while time.time() < deadline:
                session.drain()
                if os.path.exists(marker):
                    with open(marker, encoding="utf-8") as handle:
                        received = handle.read()
                    if received:
                        break
                time.sleep(0.05)
            self.assertEqual(received, repr(report))
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
