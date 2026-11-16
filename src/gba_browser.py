"""
GBA OS web browser — deliberately small.

Scope, by design:

* Fetches one page at a time with the standard library (`urllib`), with a
  hard size cap and timeout so a slow page can never wedge the console.
* Renders a readable text layout of the document (headings, paragraphs, lists,
  pre-formatted blocks, links, images) instead of trying to be a layout engine.
* Follows links, keeps a small back/forward history, and can load local files
  (`file://`) which makes it usable as an offline manual viewer too.

It is what a 480x320 appliance can afford: no JavaScript, no CSS layout, no
external dependencies. Images are decoded only when they fit the size budget.
"""

import io
import os
import re
import threading
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple

import pygame

MAX_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BYTES = 1024 * 1024
USER_AGENT = "GBA-OS-Browser/1.0 (+lightweight text browser)"
TIMEOUT = 8.0

# Styles: (font key, colour role, indent, extra line spacing)
STYLES = {
    "h1": ("title", "text", 0),
    "h2": ("header", "primary", 0),
    "h3": ("body_bold", "text", 0),
    "p": ("body", "text_muted", 0),
    "li": ("body", "text_muted", 1),
    "quote": ("body", "text_dim", 1),
    "pre": ("mono", "success", 1),
    "link": ("body", "info", 0),
    "meta": ("micro", "text_dim", 0),
}

_SKIP_TAGS = {"script", "style", "noscript", "svg", "head", "template"}
# Inline elements continue the current paragraph instead of starting a new one.
_INLINE_TAGS = {"a", "b", "i", "em", "strong", "span", "code", "small", "sup",
                "sub", "u", "mark", "abbr", "cite", "label", "time"}
_BLOCK_TAGS = {"p", "div", "section", "article", "header", "footer", "main", "br",
               "tr", "table", "form", "nav"}
_HEADING_TAGS = {"h1": "h1", "h2": "h2", "h3": "h3", "h4": "h3", "h5": "h3", "h6": "h3"}


class _HTMLExtractor(HTMLParser):
    """Turns HTML into a flat list of blocks with enough structure to render."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: List[dict] = []
        self.title = ""
        self._buffer: List[str] = []
        self._style = "p"
        self._href: Optional[str] = None
        self._skip_depth = 0
        self._in_pre = False
        self._in_title = False
        self._list_depth = 0

    # -- helpers -------------------------------------------------------
    def _flush(self, inline: bool = False) -> None:
        text = " ".join("".join(self._buffer).split())
        if text:
            self.blocks.append({"type": "text", "text": text,
                                "style": self._style, "href": self._href,
                                "indent": min(2, self._list_depth),
                                "inline": inline})
        self._buffer = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs = dict(attrs)
        # <title> lives inside <head>, which we otherwise drop, so grab it
        # before the skip check or every page would be titled by its URL.
        if tag == "title" and not self.title:
            self._in_title = True
            return
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return

        if tag in ("ul", "ol"):
            self._flush()
            self._list_depth += 1
            return
        if tag == "li":
            self._flush()
            self._style = "li"
            self._buffer.append("• ")
            return
        if tag in _HEADING_TAGS:
            self._flush()
            self._style = _HEADING_TAGS[tag]
            return
        if tag == "pre":
            self._flush()
            self._in_pre = True
            self._style = "pre"
            return
        if tag == "blockquote":
            self._flush()
            self._style = "quote"
            return
        if tag in _INLINE_TAGS:
            self._flush(inline=True)
            if tag == "a":
                self._href = attrs.get("href")
                self._style = "link"
            return
        if tag == "img":
            self._flush()
            src = attrs.get("src")
            if src:
                self.blocks.append({"type": "image", "src": src,
                                    "alt": attrs.get("alt", "")})
            return
        if tag == "hr":
            self._flush()
            self.blocks.append({"type": "rule"})
            return
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "title" and self._in_title:
            self._in_title = False
            self.title = " ".join(self.title.split())
            self._buffer = []
            return
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
            return
        if tag in ("ul", "ol"):
            self._flush()
            self._list_depth = max(0, self._list_depth - 1)
            return
        if tag == "li":
            self._flush()
            self._style = "p"
            return
        if tag in _HEADING_TAGS or tag == "blockquote" or tag == "pre":
            self._flush()
            self._style = "p"
            self._in_pre = False
            return
        if tag in _INLINE_TAGS:
            self._flush(inline=True)
            if tag == "a":
                self._href = None
                self._style = "p"
            return
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        if self._in_title:
            self.title += data.strip() + " "
            return
        if self._skip_depth:
            return
        if self._in_pre:
            for line in data.split("\n"):
                if line.strip():
                    self.blocks.append({"type": "text", "text": line.rstrip(),
                                        "style": "pre", "href": None,
                                        "indent": self._list_depth})
            return
        self._buffer.append(data)

    def close(self):
        super().close()
        self._flush()


class Line:
    """One laid-out line of the document.

    A line is a list of spans, and each span knows the link it belongs to.
    A sentence with a link in the middle therefore keeps that link clickable
    instead of being flattened into one unclickable run.
    """

    __slots__ = ("spans", "style", "indent", "href")

    def __init__(self, spans, style: str, indent: int = 0, href: Optional[str] = None):
        # spans: [(text, style)] or [(text, style, href)]
        normalized = []
        for span in spans:
            if len(span) == 3:
                normalized.append((span[0], span[1], span[2]))
            else:
                normalized.append((span[0], span[1], href))
        self.spans = normalized
        self.style = style
        self.indent = indent
        self.href = href

    @property
    def text(self) -> str:
        return "".join(s[0] for s in self.spans)

    @property
    def link(self) -> Optional[str]:
        """The first link on the line, if it has one."""
        for span in self.spans:
            if span[2]:
                return span[2]
        return self.href


class Document:
    def __init__(self, url: str, title: str, blocks: List[dict]):
        self.url = url
        self.title = title
        self.blocks = blocks
        self.lines: List[Line] = []
        self.images: Dict[int, dict] = {}      # line index -> image block
        self.links: List[str] = []
        self.image_rows: Dict[int, int] = {}
        self.image_height = 0
        # Pixel height of every line, so the renderer and the mouse hit test
        # agree on where a line is. Filled in by layout_document().
        self.line_heights: List[int] = []
        self.image_heights: Dict[int, int] = {}
        self.last_error: Optional[str] = None

    def plain_text(self) -> str:
        return "\n".join(line.text for line in self.lines)


def fetch(url: str, timeout: float = TIMEOUT) -> Tuple[str, bytes, str]:
    """Fetches `url` and returns (final_url, body, charset)."""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        if url.startswith("/") or os.path.exists(url):
            url = "file://" + os.path.abspath(url)
        else:
            url = "https://" + url

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "file":
        path = urllib.parse.unquote(parsed.path)
        with open(path, "rb") as f:
            body = f.read(MAX_BYTES)
        return url, body, "utf-8"

    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
        "Accept-Language": "en",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(MAX_BYTES)
        final = response.geturl()
        charset = "utf-8"
        content_type = response.headers.get("Content-Type", "")
        match = re.search(r"charset=([\w-]+)", content_type, re.I)
        if match:
            charset = match.group(1)
    return final, body, charset


class _Measure:
    """Wraps a pygame font so layout code can stay font-agnostic."""

    def __init__(self, font):
        self.font = font
        self._cache: Dict[str, int] = {}

    def width(self, text: str) -> int:
        if text not in self._cache:
            self._cache[text] = self.font.size(text)[0]
            if len(self._cache) > 4000:
                self._cache.clear()
        return self._cache[text]


def layout_document(document: Document, theme, width_px: int) -> Document:
    """Wraps the document's blocks into lines that fit `width_px`."""
    document.lines = []
    document.images = {}
    document.links = []
    theme_fonts = {
        "h1": theme.font("title"),
        "h2": theme.font("header"),
        "h3": theme.font("body_bold"),
        "p": theme.font("body"),
        "li": theme.font("body"),
        "quote": theme.font("body"),
        "pre": theme.mono(8),
        "link": theme.font("body"),
        "meta": theme.font("micro"),
    }
    measures = {key: _Measure(font) for key, font in theme_fonts.items()}
    indent_unit = theme.px(8)
    line_height = {key: max(font.get_linesize(), theme.px(9))
                   for key, font in theme_fonts.items()}

    current: List[Tuple[str, str, Optional[str]]] = []
    current_style = "p"
    current_href: Optional[str] = None
    current_indent = 0
    used = 0
    limit = max(120, width_px)

    def flush_line():
        nonlocal current, used
        if current:
            line_href = next((span[2] for span in current if span[2]), current_href)
            document.lines.append(Line(current, current_style, current_indent, line_href))
            current = []
            used = 0

    for block in document.blocks:
        kind = block["type"]
        if kind == "rule":
            flush_line()
            document.lines.append(Line([("─" * 40, "meta")], "meta", 0, None))
            continue
        if kind == "image":
            flush_line()
            alt = block.get("alt") or os.path.basename(urllib.parse.urlparse(block["src"]).path)
            document.lines.append(Line([(f"[image] {alt}", "meta")], "meta", 1, None))
            index = len(document.lines) - 1
            document.images[index] = block
            # The picture is drawn under its caption; the line reserves the
            # room so the text below never overlaps it.
            image_height = theme.px(64)
            document.image_rows[index] = image_height
            document.image_heights[index] = image_height
            continue

        style = block.get("style", "p")
        indent = block.get("indent", 0) * indent_unit
        href = block.get("href")
        # Normalise: relative links become absolute at follow time.
        span_style = "link" if href else style
        measure = measures.get(span_style) or measures["p"]
        text = block.get("text", "")

        if style in ("h1", "h2", "h3") and document.lines and document.lines[-1].text:
            document.lines.append(Line([("", style)], style))

        for word in text.split():
            token = word if not current else " " + word
            width = measure.width(token)
            if used + width > limit - indent and current:
                flush_line()
                current_style, current_href, current_indent = span_style, href, block.get("indent", 0)
                token = word
                width = measure.width(token)
            if not current:
                current_style = span_style
                current_href = href
                current_indent = block.get("indent", 0)
            current.append((token, span_style, href))
            used += width

        if not block.get("inline"):
            flush_line()
            # Paragraph spacing makes long pages readable on a small glass.
            if style in ("h1", "h2", "h3", "p", "quote"):
                document.lines.append(Line([("", "p")], "p"))
        if href and href not in document.links:
            document.links.append(href)

    # The last block may be inline (a link at the end of a paragraph), in
    # which case its line is still pending.
    flush_line()

    # Collapse duplicate blank lines and record line heights for the renderer.
    cleaned: List[Line] = []
    for line in document.lines:
        if not line.text and cleaned and not cleaned[-1].text:
            continue
        cleaned.append(line)
    document.lines = cleaned
    document._line_heights = line_height  # type: ignore[attr-defined]
    document.line_heights = line_advances(document, theme)
    return document


def line_advances(document: Document, theme) -> List[int]:
    """The vertical space every line takes, in pixels.

    The renderer and the mouse hit test both use this list, so a click
    always lands on the line the user sees under the pointer.
    """
    fonts = {
        "h1": theme.font("title"), "h2": theme.font("header"), "h3": theme.font("body_bold"),
        "p": theme.font("body"), "li": theme.font("body"), "quote": theme.font("body"),
        "pre": theme.mono(8), "link": theme.font("body"), "meta": theme.font("micro"),
    }
    advances: List[int] = []
    for index, line in enumerate(document.lines):
        font = fonts.get(line.style, fonts["p"])
        height = font.get_linesize() + theme.px(0.5)
        if index in document.image_heights:
            height += document.image_heights[index]
        advances.append(height)
    return advances


def document_fonts(theme) -> Dict[str, pygame.font.Font]:
    """The fonts the document is drawn with, per line style."""
    return {
        "h1": theme.font("title"), "h2": theme.font("header"), "h3": theme.font("body_bold"),
        "p": theme.font("body"), "li": theme.font("body"), "quote": theme.font("body"),
        "pre": theme.mono(8), "link": theme.font("body"), "meta": theme.font("micro"),
    }


def document_boxes(document: Document, theme, rect: pygame.Rect,
                   scroll: int = 0) -> List[Tuple[int, Line, pygame.Rect]]:
    """The pixel boxes of the visible lines, exactly as they are drawn."""
    if not document.line_heights:
        document.line_heights = line_advances(document, theme)
    boxes: List[Tuple[int, Line, pygame.Rect]] = []
    x0 = rect.x + theme.px(4)
    y = rect.y + theme.px(3)
    width = max(1, rect.width - theme.px(8))
    for index in range(max(0, scroll), len(document.lines)):
        line = document.lines[index]
        height = document.line_heights[index] if index < len(document.line_heights) \
            else theme.px(12)
        boxes.append((index, line, pygame.Rect(x0, y, width, height)))
        y += height
    return boxes


def link_at_x(document: Document, index: int, x: int, box: pygame.Rect,
              theme) -> Optional[str]:
    """The link under a horizontal position inside a laid-out line."""
    if index >= len(document.lines):
        return None
    line = document.lines[index]
    fonts = document_fonts(theme)
    font = fonts.get(line.style, fonts["p"])
    cursor = box.x + theme.px(6) * line.indent
    for span in line.spans:
        text, style, href = span
        span_font = fonts.get(style, font)
        width = span_font.size(text)[0]
        if x < cursor + width:
            return href
        cursor += width
    return None


class Browser:
    """Fetches, lays out and scrolls documents; owns history and the URL bar."""

    def __init__(self, theme, width_px: int, home: str = "about:welcome"):
        self.theme = theme
        self.width_px = width_px
        self.home = home
        self.history: List[str] = []
        self.future: List[str] = []
        self.document: Optional[Document] = None
        self.url = ""
        self.scroll = 0
        self.status = "Ready"
        self.loading = False
        self.progress = ""
        self.editing_url = False
        self.address_buffer = ""
        self.selected_link = -1
        self._image_cache: Dict[str, Optional[pygame.Surface]] = {}
        # A background fetch waiting to be applied (see load_async/poll).
        self._pending = None

    # -- navigation ----------------------------------------------------
    def load(self, url: str, remember: bool = True) -> None:
        url = (url or "").strip()
        if not url:
            return
        if url == "about:welcome":
            self._load_welcome()
            return

        self.status = f"Loading {url}"
        self.loading = True
        try:
            final_url, body, charset = fetch(url)
            text = body.decode(charset, "replace")
            document = self._parse(final_url, text)
        except Exception as exc:
            self._apply_error(url, f"{type(exc).__name__}: {exc}")
            return
        finally:
            self.loading = False
        self._apply(final_url, document, remember)

    def load_async(self, url: str, remember: bool = True) -> None:
        """Fetches a page in the background.

        A page that takes eight seconds to time out must not take the shell
        with it - a click on a link may not freeze the console. Only the
        network part runs in the thread: laying the page out touches pygame's
        fonts, and those belong to the thread that draws them.
        """
        url = (url or "").strip()
        if not url:
            return
        if url == "about:welcome":
            self._load_welcome()
            return
        self.status = f"Loading {url}"
        self.loading = True
        self._pending = None

        def worker():
            try:
                final_url, body, charset = fetch(url)
                self._pending = (final_url, body, charset, remember, None)
            except Exception as exc:
                self._pending = (url, b"", "utf-8", remember,
                                 f"{type(exc).__name__}: {exc}")

        threading.Thread(target=worker, daemon=True, name="gba-browser-fetch").start()

    def poll(self) -> None:
        """Applies a finished background fetch."""
        pending = self._pending
        if pending is None:
            return
        self._pending = None
        final_url, body, charset, remember, error = pending
        self.loading = False
        if error is not None:
            self._apply_error(final_url, error)
            return
        try:
            text = body.decode(charset, "replace")
            document = self._parse(final_url, text)
        except Exception as exc:
            self._apply_error(final_url, f"{type(exc).__name__}: {exc}")
            return
        self._apply(final_url, document, remember)

    def _apply(self, final_url: str, document: Document, remember: bool) -> None:
        if remember and self.url and self.url != final_url:
            self.history.append(self.url)
            self.future.clear()
        self.document = document
        self.url = final_url
        self.address_buffer = final_url
        self.scroll = 0
        self.status = f"{len(document.lines)} lines · {len(document.links)} links"

    def _apply_error(self, url: str, message: str) -> None:
        self.document = Document(url, url, [])
        self.document.last_error = message
        self.document.lines = [Line([(f"Could not load {url}", "h2")], "h2"),
                               Line([(message, "p")], "p"),
                               Line([("", "p")], "p"),
                               Line([("Try another address with Ctrl+L.", "p")], "p")]
        self.url = url
        self.status = "Load failed"

    def _parse(self, url: str, text: str) -> Document:
        parser = _HTMLExtractor()
        try:
            parser.feed(text)
        except Exception:
            pass
        parser.close()
        blocks = parser.blocks
        if not blocks:
            blocks = [{"type": "text", "text": line, "style": "pre",
                       "href": None, "indent": 0}
                      for line in text.splitlines() if line.strip()]
        document = Document(url, parser.title or url, blocks)
        return layout_document(document, self.theme, self.width_px)

    def _load_welcome(self) -> None:
        """Start page, built from blocks so it can re-flow on a resize."""
        blocks = [
            {"type": "text", "text": "GBA OS Browser", "style": "h1", "href": None, "indent": 0},
            {"type": "text", "text": "A deliberately small browser for the console shell.",
             "style": "p", "href": None, "indent": 0},
            {"type": "text", "text": "Ctrl+L   —   type an address", "style": "h3",
             "href": None, "indent": 0},
            {"type": "text", "text": "Click   —   open the link under the pointer", "style": "h3",
             "href": None, "indent": 0},
            {"type": "text", "text": "Enter   —   open the highlighted link", "style": "h3",
             "href": None, "indent": 0},
            {"type": "text", "text": "Tab / Shift+Tab   —   cycle links", "style": "h3",
             "href": None, "indent": 0},
            {"type": "text", "text": "Drag   —   select text · Ctrl+C copies it", "style": "h3",
             "href": None, "indent": 0},
            {"type": "text", "text": "Alt+Left / Alt+Right   —   history", "style": "h3",
             "href": None, "indent": 0},
            {"type": "text", "text": "Up / Down / PgUp / PgDn   —   scroll", "style": "h3",
             "href": None, "indent": 0},
            {"type": "text", "text": "Try one of these:", "style": "h2",
             "href": None, "indent": 0},
            {"type": "link", "text": "Project page",
             "href": "https://github.com/sumankayal247/GBA-inspired-distro",
             "indent": 0},
            {"type": "link", "text": "Hacker News", "href": "https://news.ycombinator.com",
             "indent": 0},
            {"type": "link", "text": "Wikipedia (text-heavy, works well)",
             "href": "https://en.wikipedia.org/wiki/Game_Boy_Advance", "indent": 0},
            {"type": "text", "text": "No JavaScript, no CSS layout, no trackers — just words "
                                     "and pictures. Anything unreadable is dropped rather "
                                     "than rendered badly.", "style": "quote",
             "href": None, "indent": 0},
        ]
        document = Document("about:welcome", "GBA OS Browser", blocks)
        self.document = layout_document(document, self.theme, self.width_px)
        self.url = "about:welcome"
        self.status = "Ready"
        self.scroll = 0
        self.selected_link = 0 if self.document.links else -1

    def follow(self, href: str, background: bool = False) -> None:
        target = self._resolve(href)
        if not target:
            return
        if background:
            self.load_async(target)
        else:
            self.load(target)

    def _resolve(self, href: str) -> str:
        """The absolute address a link points at."""
        if not href:
            return ""
        base = self.url if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", self.url) else None
        target = urllib.parse.urljoin(base or "", href)
        if target.startswith("#"):
            return ""
        return target

    def back(self, background: bool = False) -> None:
        if self.history:
            self.future.append(self.url)
            target = self.history.pop()
            if background:
                self.load_async(target, remember=False)
            else:
                self.load(target, remember=False)

    def forward(self, background: bool = False) -> None:
        if self.future:
            self.history.append(self.url)
            target = self.future.pop()
            if background:
                self.load_async(target, remember=False)
            else:
                self.load(target, remember=False)

    def reload(self, background: bool = False) -> None:
        if self.url and self.url != "about:welcome":
            if background:
                self.load_async(self.url, remember=False)
            else:
                self.load(self.url, remember=False)

    def begin_url_entry(self) -> None:
        self.editing_url = True
        self.address_buffer = "" if self.url == "about:welcome" else self.url

    def commit_url_entry(self, background: bool = False) -> None:
        if not self.editing_url:
            return
        target = self.address_buffer.strip() or self.home
        self.editing_url = False
        if background:
            self.load_async(target, remember=False)
        else:
            self.load(target, remember=False)

    # -- scrolling -----------------------------------------------------
    def scroll_by(self, lines: int) -> None:
        self.scroll = max(0, min(self.max_scroll(), self.scroll + lines))

    def max_scroll(self) -> int:
        return max(0, len(self.lines) - 1)

    @property
    def lines(self) -> List[Line]:
        return self.document.lines if self.document else []

    def set_width(self, width_px: int) -> None:
        if abs(width_px - self.width_px) < 8 or not self.document:
            return
        self.width_px = width_px
        layout_document(self.document, self.theme, width_px)

    # -- mouse ---------------------------------------------------------
    def hit_test(self, x: int, y: int, rect: pygame.Rect, theme=None) -> Optional[Tuple[int, str]]:
        """The line under a pixel and its link, or None.

        A click on a link follows it; the same geometry is used to draw the
        page, so the link under the pointer is the one the user sees.
        """
        theme = theme or self.theme
        document = self.document
        if not document:
            return None
        for index, line, box in document_boxes(document, theme, rect, self.scroll):
            if box.top <= y < box.bottom:
                href = link_at_x(document, index, x, box, theme)
                return index, (href or line.link or "")
        return None

    def selection_text(self, start: Optional[int], end: Optional[int]) -> str:
        """The text between two line indices (inclusive)."""
        if start is None or end is None or not self.document:
            return ""
        first, last = sorted((start, end))
        lines = self.document.lines
        if first < 0 or last >= len(lines):
            return ""
        return "\n".join(line.text for line in lines[first:last + 1]).strip()

    # -- images --------------------------------------------------------
    def image_for(self, block: dict) -> Optional[pygame.Surface]:
        src = block.get("src", "")
        if not src:
            return None
        target = urllib.parse.urljoin(self.url, src)
        if target in self._image_cache:
            return self._image_cache[target]
        surface = None
        try:
            if target.startswith("data:"):
                header, _, payload = target.partition(",")
                import base64
                raw = base64.b64decode(payload)
            else:
                _, raw, _ = fetch(target)
            if len(raw) <= MAX_IMAGE_BYTES:
                surface = pygame.image.load(io.BytesIO(raw)).convert()
        except Exception:
            surface = None
        if len(self._image_cache) > 32:
            self._image_cache.clear()
        self._image_cache[target] = surface
        return surface
