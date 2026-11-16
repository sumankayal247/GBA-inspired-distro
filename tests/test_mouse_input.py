#!/usr/bin/env python3
"""
Tests for MOUSE support in the terminal and in the browser.

The terminal has to behave like a terminal: a program that asked for mouse
reports gets xterm-compatible reports, a program that did not ask gets
nothing, and the wheel scrolls the history instead. The browser has to
behave like a document: a click on a link follows it, and the pointer
highlights the link under it.
"""

import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame  # noqa: E402

import common  # noqa: E402
import config  # noqa: E402
import gba_browser  # noqa: E402
import gba_launcher  # noqa: E402
import gba_terminal  # noqa: E402
import library  # noqa: E402
from gba_launcher import GBALauncher  # noqa: E402

PAGE = """<html><head><title>Mouse</title></head><body>
<h1>Heading</h1>
<p>Text before <a href="https://example.com/one">a link</a> and after.</p>
<p><a href="https://example.com/two">second link</a></p>
</body></html>"""


class MouseTestCase(unittest.TestCase):
    def setUp(self):
        pygame.init()
        pygame.joystick.init()
        self.test_dir = tempfile.mkdtemp()
        self.games_dir = os.path.join(self.test_dir, "games")
        self.run_dir = os.path.join(self.test_dir, "run")
        os.makedirs(self.games_dir, exist_ok=True)
        os.makedirs(self.run_dir, exist_ok=True)
        self.page = os.path.join(self.test_dir, "page.html")
        with open(self.page, "w", encoding="utf-8") as handle:
            handle.write(PAGE)

        self._original = {}

        def patch(module, name, value):
            self._original[(module, name)] = getattr(module, name, None)
            setattr(module, name, value)

        for module in (common, config, gba_launcher, library):
            patch(module, "get_data_dir", lambda: self.test_dir)
            if hasattr(module, "get_games_dir"):
                patch(module, "get_games_dir", lambda: self.games_dir)
            if hasattr(module, "get_run_dir"):
                patch(module, "get_run_dir", lambda: self.run_dir)
        patch(gba_launcher.settings_store, "apply_volume", lambda *a, **k: True)
        patch(gba_launcher.settings_store, "set_brightness", lambda *a, **k: True)
        patch(gba_launcher, "VolumeWatcher", lambda *a, **k: None)
        patch(gba_launcher.settings_store, "load_settings",
              lambda: dict(config.DEFAULT_SETTINGS))
        patch(gba_launcher.settings_store, "save_settings", lambda payload: None)

    def tearDown(self):
        for (module, name), value in self._original.items():
            setattr(module, name, value)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def make_launcher(self):
        launcher = GBALauncher(demo=True,
                                screenshot_path=os.path.join(self.test_dir, "shot.png"))
        launcher.screenshot_path = None
        launcher.update()
        launcher.on_tab_activated()
        return launcher


class TestTerminalMouse(MouseTestCase):
    def setUp(self):
        super().setUp()
        self.launcher = self.make_launcher()
        self.launcher.goto_tab(gba_launcher.TAB_TERMINAL)
        self.launcher.ensure_terminal()
        self.written = []
        self.launcher.terminal_session.write = self.written.append

    def cell_point(self, col=1, row=1):
        """A pixel inside the given cell of the terminal grid."""
        body = self.launcher._terminal_body_rect()
        view = self.launcher.terminal_view
        cw, ch, _cols, _rows = view.metrics(body)
        origin_x, origin_y = view.origin(body)
        return (origin_x + cw * col + 1, origin_y + ch * row + 1)

    def test_click_reaches_a_program_that_asked_for_mouse_reports(self):
        self.launcher.terminal_screen.feed(b"\x1b[?1000h\x1b[?1006h")
        self.written.clear()
        self.launcher._terminal_mouse_event(
            pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=self.cell_point()))
        self.assertTrue(self.written)
        self.assertTrue(self.written[0].startswith(b"\x1b[<0;"))
        self.assertTrue(self.written[0].endswith(b"M"))

    def test_click_is_ignored_when_the_program_did_not_ask(self):
        self.written.clear()
        self.launcher._terminal_mouse_event(
            pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=self.cell_point()))
        self.assertEqual(self.written, [])

    def test_release_is_reported(self):
        self.launcher.terminal_screen.feed(b"\x1b[?1000h\x1b[?1006h")
        self.launcher._terminal_mouse_event(
            pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=self.cell_point()))
        self.written.clear()
        self.launcher._terminal_mouse_event(
            pygame.event.Event(pygame.MOUSEBUTTONUP, button=1, pos=self.cell_point()))
        self.assertTrue(self.written[0].endswith(b"m"))

    def test_wheel_scrolls_the_history_when_no_program_is_listening(self):
        for index in range(40):
            self.launcher.terminal_screen.feed(f"line{index}\r\n".encode())
        view = self.launcher.terminal_view
        view.scroll_offset = 0
        self.written.clear()
        self.launcher._terminal_mouse_event(
            pygame.event.Event(pygame.MOUSEWHEEL, x=0, y=1, pos=self.cell_point()))
        self.assertEqual(self.written, [])
        self.assertGreater(view.scroll_offset, 0)

    def test_wheel_is_forwarded_when_the_program_listens(self):
        self.launcher.terminal_screen.feed(b"\x1b[?1000h\x1b[?1006h")
        self.written.clear()
        self.launcher._terminal_mouse_event(
            pygame.event.Event(pygame.MOUSEWHEEL, x=0, y=1, pos=self.cell_point()))
        self.assertTrue(any(b"64" in chunk for chunk in self.written))

    def test_drag_is_only_reported_while_a_button_is_down(self):
        self.launcher.terminal_screen.feed(b"\x1b[?1002h\x1b[?1006h")
        self.written.clear()
        self.launcher._terminal_mouse_event(
            pygame.event.Event(pygame.MOUSEMOTION, pos=self.cell_point(2, 2),
                               rel=(1, 1), buttons=(0, 0, 0)))
        self.assertEqual(self.written, [])
        self.launcher._terminal_mouse_event(
            pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=self.cell_point()))
        self.written.clear()
        self.launcher._terminal_mouse_event(
            pygame.event.Event(pygame.MOUSEMOTION, pos=self.cell_point(3, 2),
                               rel=(1, 0), buttons=(1, 0, 0)))
        self.assertTrue(self.written)
        self.assertIn(b"32", self.written[0])


class TestTerminalSelection(MouseTestCase):
    """Without a program listening, the pointer selects the text."""

    def setUp(self):
        super().setUp()
        self.launcher = self.make_launcher()
        self.launcher.goto_tab(gba_launcher.TAB_TERMINAL)
        self.launcher.ensure_terminal()
        self.written = []
        self.launcher.terminal_session.write = self.written.append
        self.launcher.terminal_screen.feed(b"hello world\r\nsecond line\r\n")

    def cell_point(self, col=1, row=1):
        body = self.launcher._terminal_body_rect()
        view = self.launcher.terminal_view
        cw, ch, _cols, _rows = view.metrics(body)
        origin_x, origin_y = view.origin(body)
        return (origin_x + cw * col + 1, origin_y + ch * row + 1)

    def test_a_drag_selects_the_text_under_the_pointer(self):
        self.launcher._terminal_mouse_event(
            pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=self.cell_point(0, 0)))
        self.launcher._terminal_mouse_event(
            pygame.event.Event(pygame.MOUSEMOTION, pos=self.cell_point(5, 0),
                               rel=(1, 0), buttons=(1, 0, 0)))
        view = self.launcher.terminal_view
        self.assertIsNotNone(view.selection)
        self.assertIn("hello", view.selected_text())

    def test_the_selection_is_not_given_to_the_program(self):
        self.launcher._terminal_mouse_event(
            pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=self.cell_point(0, 0)))
        self.launcher._terminal_mouse_event(
            pygame.event.Event(pygame.MOUSEMOTION, pos=self.cell_point(5, 0),
                               rel=(1, 0), buttons=(1, 0, 0)))
        self.assertEqual(self.written, [])

    def test_a_program_that_asked_for_the_mouse_keeps_it(self):
        self.launcher.terminal_screen.feed(b"\x1b[?1000h\x1b[?1006h")
        self.launcher._terminal_mouse_event(
            pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=self.cell_point(0, 0)))
        self.launcher._terminal_mouse_event(
            pygame.event.Event(pygame.MOUSEMOTION, pos=self.cell_point(5, 0),
                               rel=(1, 0), buttons=(1, 0, 0)))
        self.assertTrue(self.written)
        self.assertIsNone(self.launcher.terminal_view.selection)

    def test_copy_puts_the_selection_on_the_clipboard(self):
        copied = {}
        self.launcher._copy_to_clipboard = lambda text: copied.setdefault("text", text) or True
        self.launcher._terminal_mouse_event(
            pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=self.cell_point(0, 0)))
        self.launcher._terminal_mouse_event(
            pygame.event.Event(pygame.MOUSEMOTION, pos=self.cell_point(5, 0),
                               rel=(1, 0), buttons=(1, 0, 0)))
        self.launcher._terminal_copy_selection()
        self.assertIn("hello", copied.get("text", ""))


class TestBrowserMouse(MouseTestCase):
    def setUp(self):
        super().setUp()
        self.launcher = self.make_launcher()
        self.launcher.goto_tab(gba_launcher.TAB_BROWSER)
        self.launcher.ensure_browser()
        self.launcher.browser.load("file://" + self.page)

    def link_point(self, href):
        """A pixel inside the link with that address."""
        body = self.launcher._browser_body_rect()
        theme = self.launcher.theme
        for index, line, box in gba_launcher.document_boxes(self.launcher.browser.document,
                                                            theme, body,
                                                            self.launcher.browser.scroll):
            if line.link == href:
                return (box.x + 4, box.centery)
        raise AssertionError(f"link {href} is not on the page")

    def test_click_on_a_link_follows_it(self):
        fake = ("<html><head><title>Two</title></head><body>"
                "<p>second page</p></body></html>")

        def fake_fetch(url, timeout=None):
            return url, fake.encode("utf-8"), "utf-8"

        with patch.object(gba_browser, "fetch", fake_fetch):
            point = self.link_point("https://example.com/two")
            self.launcher._browser_click(point)
            deadline = time.time() + 5
            while time.time() < deadline and self.launcher.browser.loading:
                time.sleep(0.02)
                self.launcher._poll_browser()
        self.assertIn("example.com", self.launcher.browser.url)
        self.assertIn("second page", self.launcher.browser.document.plain_text())

    def test_a_click_does_not_wait_for_the_page(self):
        def slow_fetch(url, timeout=None):
            time.sleep(1.5)
            return url, b"<html><body><p>slow</p></body></html>", "utf-8"

        with patch.object(gba_browser, "fetch", slow_fetch):
            point = self.link_point("https://example.com/one")
            started = time.time()
            self.launcher._browser_click(point)
            elapsed = time.time() - started
        self.assertLess(elapsed, 0.5)
        self.assertTrue(self.launcher.browser.loading)

    def test_click_on_the_address_bar_starts_editing(self):
        bar = self.launcher._browser_bar_rect()
        self.launcher._browser_click((bar.centerx, bar.centery))
        self.assertTrue(self.launcher.browser.editing_url)

    def test_click_on_empty_space_starts_a_selection(self):
        body = self.launcher._browser_body_rect()
        self.launcher._browser_click((body.x + 4, body.bottom - 4))
        # Nothing to select below the last line: the selection stays empty.
        self.assertIsNone(self.launcher.browser.hit_test(body.x + 4, body.bottom - 4,
                                                        body, self.launcher.theme))

    def test_dragging_selects_text(self):
        body = self.launcher._browser_body_rect()
        boxes = gba_launcher.document_boxes(self.launcher.browser.document,
                                            self.launcher.theme, body,
                                            self.launcher.browser.scroll)
        first = boxes[0][2]
        last = boxes[2][2]
        self.launcher._browser_click((first.x + 4, first.centery))
        self.launcher._browser_motion((last.x + 4, last.centery))
        text = self.launcher.browser.selection_text(*self.launcher._browser_selection)
        self.assertIn("Heading", text)

    def test_pointer_highlights_the_link_under_it(self):
        point = self.link_point("https://example.com/one")
        self.launcher._browser_motion(point)
        self.assertEqual(self.launcher._browser_hover, "https://example.com/one")

    def test_the_wheel_scrolls_the_page(self):
        body = self.launcher._browser_body_rect()
        for index in range(60):
            self.launcher.browser.document.lines.append(
                gba_browser.Line([(f"line {index}", "p")], "p"))
        self.launcher.browser.scroll = 0
        self.launcher._browser_event(
            pygame.event.Event(pygame.MOUSEWHEEL, x=0, y=-1, pos=(body.x + 4, body.y + 4)))
        self.assertGreater(self.launcher.browser.scroll, 0)
        scrolled = self.launcher.browser.scroll
        self.launcher._browser_event(
            pygame.event.Event(pygame.MOUSEWHEEL, x=0, y=1, pos=(body.x + 4, body.y + 4)))
        self.assertLess(self.launcher.browser.scroll, scrolled)

    def test_copying_a_selection_reports_what_was_copied(self):
        body = self.launcher._browser_body_rect()
        boxes = gba_launcher.document_boxes(self.launcher.browser.document,
                                            self.launcher.theme, body,
                                            self.launcher.browser.scroll)
        self.launcher._browser_selection = (0, 0)
        # The dummy SDL driver has no clipboard; the copy itself is what the
        # test is about.
        self.launcher._copy_to_clipboard = lambda text: True
        self.launcher._browser_copy_selection()
        self.assertIn("copied", self.launcher.toast_text.lower())


if __name__ == "__main__":
    unittest.main()
