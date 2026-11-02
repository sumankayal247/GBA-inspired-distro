#!/usr/bin/env python3
"""
Tests for the lightweight BROWSER tab (gba_browser.py).

Covers HTML extraction, word-wrap layout, history and the URL bar. No network
access: everything is served from `file://` temp fixtures or `about:welcome`.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame  # noqa: E402

import gba_browser  # noqa: E402
import theme as theme_module  # noqa: E402

PAGE = """<html><head><title>Test Page</title>
<script>var bad = 1;</script><style>p{color:red}</style></head>
<body>
<h1>Hello GBA</h1>
<p>Some text with a <a href="next.html">link here</a> and more words that wrap
across the panel width because it is not very wide at all.</p>
<ul><li>one</li><li>two</li></ul>
<pre>code  block</pre>
<blockquote>quoted</blockquote>
<img src="missing.png" alt="A picture">
<hr>
</body></html>"""


class BrowserTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.init()
        pygame.font.init()

    def setUp(self):
        self.temp = tempfile.mkdtemp()
        self.page = os.path.join(self.temp, "page.html")
        with open(self.page, "w", encoding="utf-8") as f:
            f.write(PAGE)
        self.theme = theme_module.Theme("indigo", 2.0)
        self.browser = gba_browser.Browser(self.theme, 800)


class TestExtractor(BrowserTestCase):
    def parse(self, html):
        parser = gba_browser._HTMLExtractor()
        parser.feed(html)
        parser.close()
        return parser

    def test_skips_script_style_and_head(self):
        parser = self.parse("<html><head><title>T</title></head><body>"
                            "<script>bad()</script><style>x{}</style><p>good</p></body></html>")
        text = " ".join(b["text"] for b in parser.blocks if b["type"] == "text")
        self.assertIn("good", text)
        self.assertNotIn("bad()", text)
        self.assertNotIn("color", text)

    def test_title_is_captured(self):
        self.assertEqual(self.parse("<title>My Title</title><p>x</p>").title, "My Title")

    def test_inline_links_do_not_split_the_paragraph(self):
        parser = self.parse("<p>before <a href='/x'>link</a> after</p>")
        text = " ".join(b["text"] for b in parser.blocks if b["type"] == "text")
        self.assertIn("before link after", text.replace("  ", " "))

    def test_images_and_rules_are_recorded(self):
        parser = self.parse('<img src="a.png" alt="A"><hr>')
        kinds = [b["type"] for b in parser.blocks]
        self.assertIn("image", kinds)
        self.assertIn("rule", kinds)

    def test_lists_are_indented(self):
        parser = self.parse("<ul><li>item</li></ul>")
        items = [b for b in parser.blocks if b["type"] == "text"]
        self.assertTrue(items)
        self.assertGreaterEqual(items[0]["indent"], 1)


class TestLayout(BrowserTestCase):
    def test_document_loads_from_file_url(self):
        self.browser.load("file://" + self.page)
        self.assertEqual(self.browser.document.title, "Test Page")
        text = self.browser.document.plain_text()
        self.assertIn("Hello GBA", text)
        self.assertIn("link here", text)
        self.assertIn("A picture", text)
        self.assertNotIn("bad()", text)

    def test_lines_fit_the_layout_width(self):
        self.browser.load("file://" + self.page)
        font = self.theme.font("body")
        for line in self.browser.document.lines:
            if line.style == "pre":
                continue
            self.assertLessEqual(font.size(line.text)[0], 860, line.text)

    def test_links_are_addressable(self):
        self.browser.load("file://" + self.page)
        self.assertIn("next.html", self.browser.document.links)

    def test_history_back_and_forward(self):
        other = os.path.join(self.temp, "next.html")
        with open(other, "w") as f:
            f.write("<title>Next</title><p>second page</p>")
        self.browser.load("file://" + self.page)
        self.browser.follow("next.html")
        self.assertEqual(self.browser.document.title, "Next")
        self.browser.back()
        self.assertEqual(self.browser.document.title, "Test Page")
        self.browser.forward()
        self.assertEqual(self.browser.document.title, "Next")

    def test_scroll_is_clamped(self):
        self.browser.load("file://" + self.page)
        self.browser.scroll_by(9999)
        self.assertLessEqual(self.browser.scroll, self.browser.max_scroll())
        self.browser.scroll_by(-9999)
        self.assertEqual(self.browser.scroll, 0)

    def test_missing_file_sets_an_error_not_an_exception(self):
        self.browser.load("file://" + os.path.join(self.temp, "nope.html"))
        self.assertTrue(self.browser.document.last_error or "fail" in self.browser.status.lower())


class TestChrome(BrowserTestCase):
    def test_welcome_page_has_links_and_survives_relayout(self):
        self.browser.load("about:welcome")
        self.assertGreaterEqual(len(self.browser.document.links), 3)
        before = len(self.browser.document.lines)
        self.browser.set_width(1200)
        self.assertEqual(len(self.browser.document.lines), before)

    def test_url_entry_commit(self):
        self.browser.begin_url_entry()
        self.assertTrue(self.browser.editing_url)
        self.browser.address_buffer = "file://" + self.page
        self.browser.commit_url_entry()
        self.assertFalse(self.browser.editing_url)
        self.assertIn("page.html", self.browser.url)

    def test_reload_keeps_the_document(self):
        self.browser.load("about:welcome")
        self.browser.reload()
        self.assertTrue(self.browser.document)


if __name__ == "__main__":
    unittest.main()
