#!/usr/bin/env python3
"""
Tests for the TERMINAL tab (gba_terminal.py).

Covers the ANSI screen emulator, key translation and a live PTY session.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame  # noqa: E402

import gba_terminal  # noqa: E402
import theme as theme_module  # noqa: E402


class TestTerminalScreen(unittest.TestCase):
    def setUp(self):
        self.screen = gba_terminal.TerminalScreen(cols=20, rows=5)

    def text(self):
        return "\n".join("".join(ch for ch, _fg, _bold in row).rstrip()
                         for row in self.screen.lines())

    def test_plain_text_and_carriage_return(self):
        self.screen.feed(b"hello\r\nworld")
        text = self.text()
        self.assertIn("hello", text)
        self.assertIn("world", text)

    def test_backspace_and_tab(self):
        self.screen.feed(b"abc\bX\tY")
        self.assertIn("abX", self.text())

    def test_scrollback_keeps_history(self):
        for index in range(12):
            self.screen.feed(f"line{index}\r\n".encode())
        history = "\n".join("".join(c[0] for c in row) for row in self.screen.scrollback)
        self.assertIn("line0", history)
        self.assertIn("line11", self.text())

    def test_scrollback_is_bounded(self):
        screen = gba_terminal.TerminalScreen(cols=10, rows=3, scrollback=4)
        for index in range(40):
            screen.feed(f"x{index}\r\n".encode())
        self.assertLessEqual(len(screen.scrollback), 4)

    def test_clear_screen(self):
        self.screen.feed(b"junk\r\n" * 3)
        self.screen.feed(b"\x1b[2J\x1b[H")
        self.assertNotIn("junk", self.text())

    def test_cursor_movement_csi(self):
        self.screen.feed(b"12345\x1b[1;1HX")
        self.assertTrue(self.text().startswith("X2345"))

    def test_sgr_colours_are_parsed(self):
        self.screen.feed(b"\x1b[31mred\x1b[0m plain")
        row = self.screen.lines()[0]
        colours = {fg for ch, fg, _b in row if ch == "r"}
        self.assertTrue(any(fg != 7 for fg in colours))

    def test_256_colour_sequence_is_mapped(self):
        self.screen.feed(b"\x1b[38;5;196mX\x1b[0m")
        row = self.screen.lines()[0]
        self.assertEqual(row[0][0], "X")

    def test_resize_preserves_content(self):
        self.screen.feed(b"keep me")
        self.screen.resize(40, 10)
        self.assertIn("keep me", self.text())

    def test_utf8_split_across_reads(self):
        payload = "héllo".encode("utf-8")
        self.screen.feed(payload[:1])
        self.screen.feed(payload[1:])
        self.assertIn("héllo", self.text())


class TestKeyTranslation(unittest.TestCase):
    def event(self, key, mod=0, unicode_="", **kwargs):
        return pygame.event.Event(pygame.KEYDOWN, key=key, mod=mod,
                                  unicode=unicode_, **kwargs)

    def test_printable_characters_pass_through(self):
        self.assertEqual(gba_terminal.key_to_bytes(self.event(pygame.K_a, unicode_="a")), b"a")

    def test_enter_and_backspace(self):
        self.assertEqual(gba_terminal.key_to_bytes(self.event(pygame.K_RETURN)), b"\r")
        self.assertEqual(gba_terminal.key_to_bytes(self.event(pygame.K_BACKSPACE)), b"\x7f")

    def test_arrows_emit_escape_sequences(self):
        self.assertEqual(gba_terminal.key_to_bytes(self.event(pygame.K_UP)), b"\x1b[A")
        self.assertEqual(gba_terminal.key_to_bytes(self.event(pygame.K_LEFT)), b"\x1b[D")

    def test_ctrl_letter_is_a_control_code(self):
        self.assertEqual(
            gba_terminal.key_to_bytes(self.event(pygame.K_c, mod=pygame.KMOD_CTRL, unicode_="\x03")),
            b"\x03")

    def test_unknown_key_is_ignored(self):
        self.assertEqual(gba_terminal.key_to_bytes(self.event(pygame.K_F13)), b"")


class TestTerminalSession(unittest.TestCase):
    def test_shell_roundtrip(self):
        session = gba_terminal.TerminalSession(cols=80, rows=24,
                                               shell="/bin/sh")
        try:
            if session.error:
                self.skipTest(f"no pty available: {session.error}")
            session.write(b"echo GBA_TERMINAL_OK\n")
            collected = b""
            deadline = time.time() + 5
            while time.time() < deadline and b"GBA_TERMINAL_OK" not in collected:
                collected += session.drain()
                time.sleep(0.05)
            self.assertIn(b"GBA_TERMINAL_OK", collected)
            session.resize(100, 30)
            self.assertTrue(session.alive)
        finally:
            session.close()
        self.assertFalse(session.alive)

    def test_view_metrics_and_draw(self):
        pygame.init()
        pygame.font.init()
        screen = gba_terminal.TerminalScreen(80, 24)
        screen.feed(b"\x1b[32mready\x1b[0m")
        view = gba_terminal.TerminalView(screen, theme_module.Theme("indigo", 2.0), 8)
        surface = pygame.Surface((900, 500))
        rect = pygame.Rect(10, 10, 880, 480)
        cw, ch, cols, rows = view.metrics(rect)
        self.assertGreater(cols, 20)
        self.assertGreater(rows, 5)
        view.draw(surface, rect, focused=True, cursor_on=True)


if __name__ == "__main__":
    unittest.main()
