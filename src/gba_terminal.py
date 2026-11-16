"""
GBA OS terminal: a real PTY-backed shell rendered inside the console shell.

Two pieces:

* `TerminalSession` owns a pseudo-terminal and the child shell process, so the
  terminal tab is a genuine interactive session (saves, `ls`, `nano` ... all
  work) rather than a fake command runner.
* `TerminalScreen` is an xterm-compatible terminal emulator. It understands
  the escape sequences a *modern* terminal program emits - truecolor, the
  alternate screen, scroll regions, insert/delete, bracketed paste, cursor
  styles and mouse reporting - because that is the difference between a
  terminal that can only run `ls` and one that can run a full-screen TUI
  (an editor, a dashboard, the Antigravity CLI).

No third-party dependencies: `pty`, `fcntl` and `select` from the stdlib.
"""

import errno
import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import termios
import threading
from typing import List, Optional, Tuple

import pygame

Color = Tuple[int, int, int]

# Standard xterm colour ramp, tuned toward the GBA palette.
ANSI_COLORS: List[Color] = [
    (24, 20, 38),     # 0 black
    (255, 107, 107),  # 1 red
    (99, 230, 165),   # 2 green
    (255, 200, 87),   # 3 yellow
    (124, 106, 240),  # 4 blue
    (255, 122, 182),  # 5 magenta
    (111, 214, 255),  # 6 cyan
    (222, 218, 240),  # 7 white
    (110, 102, 138),  # 8 bright black
    (255, 145, 145),  # 9
    (150, 245, 195),  # 10
    (255, 226, 150),  # 11
    (160, 150, 255),  # 12
    (255, 165, 205),  # 13
    (160, 232, 255),  # 14
    (250, 248, 255),  # 15
]
DEFAULT_FG = 7
DEFAULT_BG = 0
DEFAULT_COLOR: Color = ANSI_COLORS[DEFAULT_FG]
DEFAULT_BACKGROUND: Color = ANSI_COLORS[DEFAULT_BG]
# The colours of the glass the terminal is drawn on. A program that asks
# "what colour is your background?" (OSC 11) gets these, so it can pick a
# palette that fits.
GLASS_FOREGROUND: Color = (246, 244, 255)
GLASS_BACKGROUND: Color = (12, 10, 24)

# Cell attributes (a bit set, so a cell stays cheap to copy).
ATTR_BOLD = 1 << 0
ATTR_DIM = 1 << 1
ATTR_ITALIC = 1 << 2
ATTR_UNDERLINE = 1 << 3
ATTR_REVERSE = 1 << 4

# Mouse buttons, in the numbering the xterm protocols use.
MOUSE_LEFT = 0
MOUSE_MIDDLE = 1
MOUSE_RIGHT = 2
MOUSE_WHEEL_UP = 64
MOUSE_WHEEL_DOWN = 65

# Modifier bits as xterm reports them inside a mouse report.
MOUSE_MOD_SHIFT = 4
MOUSE_MOD_ALT = 8
MOUSE_MOD_CTRL = 16


def _color_256(index: int) -> Color:
    if index < 16:
        return ANSI_COLORS[index]
    if index < 232:
        index -= 16
        r = index // 36
        g = (index % 36) // 6
        b = index % 6
        scale = [0, 95, 135, 175, 215, 255]
        return (scale[r], scale[g], scale[b])
    level = 8 + (index - 232) * 10
    return (level, level, level)


class Cell:
    """One character cell: a glyph, a foreground, an optional background.

    Colours are RGB triples. A foreground of ``None`` means "the default
    foreground"; the same holds for the background, which keeps the glass
    dark unless a program explicitly paints a cell.
    """

    __slots__ = ("char", "fg", "bg", "attrs")

    def __init__(self, char: str = " ", fg: Optional[Color] = None,
                 bg: Optional[Color] = None, attrs: int = 0):
        self.char = char
        self.fg = fg
        self.bg = bg
        self.attrs = attrs

    def copy(self) -> "Cell":
        return Cell(self.char, self.fg, self.bg, self.attrs)

    @property
    def bold(self) -> bool:
        return bool(self.attrs & ATTR_BOLD)

    def __iter__(self):
        # Kept for compatibility with code that unpacks (char, fg, bold).
        return iter((self.char, self.fg, self.bold))

    def __getitem__(self, index: int):
        # `cell[0]` is the character, `cell[1]` the foreground, `cell[2]` the
        # bold flag - the shape a plain tuple used to have.
        return (self.char, self.fg, self.bold)[index]

    def __eq__(self, other) -> bool:
        if not isinstance(other, Cell):
            return NotImplemented
        return (self.char == other.char and self.fg == other.fg
                and self.bg == other.bg and self.attrs == other.attrs)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Cell({self.char!r}, fg={self.fg}, bg={self.bg}, attrs={self.attrs})"


def _shell_arguments(shell: str) -> List[str]:
    """The flags the shell is started with.

    A *login* shell reads the profile that adds ~/.local/bin to the PATH, so
    a tool that installs itself (the Antigravity CLI) is found without the
    user having to edit anything.
    """
    name = os.path.basename(shell)
    if name in ("bash", "sh", "dash", "zsh", "ksh", "ash"):
        return ["-il"]
    return ["-i"]


class TerminalSession:
    """A shell attached to a pseudo-terminal."""
    def __init__(self, cols: int = 80, rows: int = 24, shell: Optional[str] = None,
                 cwd: Optional[str] = None):
        self.cols = max(20, cols)
        self.rows = max(6, rows)
        self.alive = False
        self.error: Optional[str] = None
        self._master = -1
        self._proc: Optional[subprocess.Popen] = None
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._reader: Optional[threading.Thread] = None

        shell = shell or os.environ.get("SHELL") or "/bin/bash"
        if not os.path.exists(shell):
            shell = "/bin/sh"

        env = os.environ.copy()
        env.setdefault("TERM", "xterm-256color")
        env.setdefault("COLORTERM", "truecolor")
        env["GBA_TERMINAL"] = "1"
        # Tools that install themselves into ~/.local/bin (the Antigravity CLI
        # does) are only found if that directory is on the PATH - and the
        # profile that adds it is not always read.
        local_bin = os.path.join(os.path.expanduser("~"), ".local", "bin")
        if os.path.isdir(local_bin) and local_bin not in env.get("PATH", "").split(":"):
            env["PATH"] = local_bin + ":" + env.get("PATH", "")

        try:
            master, slave = pty.openpty()
            self._set_winsize(slave, self.rows, self.cols)
            self._proc = subprocess.Popen(
                [shell] + _shell_arguments(shell),
                stdin=slave, stdout=slave, stderr=slave,
                preexec_fn=os.setsid,
                env=env,
                cwd=cwd or os.path.expanduser("~"),
                close_fds=True,
            )
            os.close(slave)
            self._master = master
            os.set_blocking(master, False)
            self.alive = True
            self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                            name="gba-terminal-reader")
            self._reader.start()
        except Exception as exc:  # pragma: no cover - depends on host
            self.error = str(exc)
            self.alive = False

    # -- internals -----------------------------------------------------
    @staticmethod
    def _set_winsize(fd: int, rows: int, cols: int) -> None:
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except Exception:
            pass

    def _read_loop(self) -> None:
        while self.alive:
            try:
                ready, _, _ = select.select([self._master], [], [], 0.1)
                if not ready:
                    if self._proc and self._proc.poll() is not None:
                        break
                    continue
                data = os.read(self._master, 65536)
                if not data:
                    break
                with self._lock:
                    self._buffer.extend(data)
                    if len(self._buffer) > 1024 * 1024:
                        del self._buffer[: len(self._buffer) - 512 * 1024]
            except OSError as exc:
                if exc.errno in (errno.EIO, errno.EBADF):
                    break
                continue
            except Exception:
                break
        self.alive = False

    def drain(self) -> bytes:
        """Returns and clears everything the shell has written since last call."""
        with self._lock:
            if not self._buffer:
                return b""
            data = bytes(self._buffer)
            self._buffer.clear()
            return data

    # -- public API ----------------------------------------------------
    def write(self, data: bytes) -> None:
        if not self.alive or self._master < 0:
            return
        try:
            os.write(self._master, data)
        except Exception:
            pass

    def resize(self, cols: int, rows: int) -> None:
        cols, rows = max(20, cols), max(6, rows)
        if (cols, rows) == (self.cols, self.rows):
            return
        self.cols, self.rows = cols, rows
        if self._master >= 0:
            self._set_winsize(self._master, rows, cols)
        if self._proc and self._proc.poll() is None:
            try:
                os.kill(self._proc.pid, signal.SIGWINCH)
            except Exception:
                pass

    def close(self) -> None:
        self.alive = False
        if self._proc and self._proc.poll() is None:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGHUP)
            except Exception:
                try:
                    self._proc.terminate()
                except Exception:
                    pass
        if self._master >= 0:
            try:
                os.close(self._master)
            except Exception:
                pass
            self._master = -1
        # Reap the child so we never leave a zombie shell behind (S9 hygiene).
        if self._proc is not None:
            try:
                self._proc.wait(timeout=0.5)
            except Exception:
                try:
                    self._proc.kill()
                    self._proc.wait(timeout=0.5)
                except Exception:
                    pass


class TerminalScreen:
    """An xterm-compatible terminal emulator (grid of characters + colours).

    The feature set is the one a full-screen program needs: the alternate
    screen, scroll regions, insert/delete, truecolor, cursor styles, bracketed
    paste and the three mouse-reporting protocols. Anything unknown is
    ignored rather than printed - a terminal that shows escape sequences as
    text is worse than one that drops them.
    """

    def __init__(self, cols: int = 80, rows: int = 24, scrollback: int = 500):
        self.cols = cols
        self.rows = rows
        self.scrollback_limit = scrollback
        self._decoder = b""
        self.reset()
        self.scrollback: List[List[Cell]] = []
        # The size of one character cell in pixels. The shell keeps it up to
        # date; a program that asks how big the text area is (CSI 14t) gets
        # an answer in real pixels.
        self.cell_size = (8, 16)
        # Replies the terminal owes the program (cursor position report, device
        # attributes). The session drains them and writes them back.
        self.responses: List[bytes] = []
        self.title = ""
        # Text the program put on the clipboard (OSC 52).
        self.clipboard = ""

    # -- state ---------------------------------------------------------
    def reset(self) -> None:
        self.primary = self._blank_grid()
        self.alternate: Optional[List[List[Cell]]] = None
        self.use_alternate = False
        self.cursor_row = 0
        self.cursor_col = 0
        self.fg: Optional[Color] = None
        self.bg: Optional[Color] = None
        self.attrs = 0
        self.cursor_visible = True
        self.cursor_style = 1          # DECSCUSR: 1 = block
        self.cursor_blink = True
        self.autowrap = True
        self.insert_mode = False
        self.origin_mode = False
        self.bracketed_paste = False
        self.mouse_modes: set = set()
        self.scroll_top = 0
        self.scroll_bottom = max(0, self.rows - 1)
        self.saved_cursor: Optional[dict] = None
        self.tab_stops = set(range(8, max(9, self.cols), 8))
        self._pending_wrap = False
        self._state = "normal"
        self._params = ""

    def _blank_grid(self) -> List[List[Cell]]:
        return [[Cell() for _ in range(self.cols)] for _ in range(self.rows)]

    # -- grids ---------------------------------------------------------
    @property
    def cells(self) -> List[List[Cell]]:
        return self.alternate if (self.use_alternate and self.alternate is not None) \
            else self.primary

    def resize(self, cols: int, rows: int) -> None:
        cols, rows = max(8, cols), max(3, rows)
        if (cols, rows) == (self.cols, self.rows):
            return
        for grid in (self.primary, self.alternate):
            if grid is None:
                continue
            for row in grid:
                if len(row) < cols:
                    row.extend(Cell() for _ in range(cols - len(row)))
                elif len(row) > cols:
                    del row[cols:]
            while len(grid) < rows:
                grid.append([Cell() for _ in range(cols)])
            del grid[rows:]
        self.cols, self.rows = cols, rows
        self.scroll_top = 0
        self.scroll_bottom = rows - 1
        self.cursor_row = min(self.cursor_row, rows - 1)
        self.cursor_col = min(self.cursor_col, cols - 1)
        self.tab_stops = {stop for stop in self.tab_stops if stop < cols}

    # -- rendering helpers --------------------------------------------
    def lines(self) -> List[List[Cell]]:
        return self.cells

    def text(self) -> str:
        return "\n".join("".join(cell.char for cell in row).rstrip() for row in self.cells)

    # -- input ---------------------------------------------------------
    def feed(self, data: bytes) -> None:
        if not data:
            return
        data = self._decoder + data
        self._decoder = b""
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            # Keep the incomplete tail for the next chunk.
            text = data[: exc.start].decode("utf-8", "replace")
            self._decoder = data[exc.start:]
        for char in text:
            self._consume(char)

    def _consume(self, char: str) -> None:
        state = self._state
        if state == "normal":
            if char == "\x1b":
                self._state = "esc"
            elif char == "\r":
                self.cursor_col = 0
                self._pending_wrap = False
            elif char == "\n":
                self._linefeed()
            elif char == "\b":
                self.cursor_col = max(0, self.cursor_col - 1)
                self._pending_wrap = False
            elif char == "\t":
                self._tab()
            elif char == "\x07":
                pass
            elif char >= " ":
                self._put(char)
        elif state == "esc":
            if char == "[":
                self._state = "csi"
                self._params = ""
            elif char == "]":
                self._state = "osc"
                self._osc = ""
            elif char in "()78":
                self._state = "esc_skip"
            elif char == "7":
                self.save_cursor()
                self._state = "normal"
            elif char == "8":
                self.restore_cursor()
                self._state = "normal"
            elif char == "D":
                self._linefeed()
                self._state = "normal"
            elif char == "E":
                self.cursor_col = 0
                self._linefeed()
                self._state = "normal"
            elif char == "M":
                self._reverse_index()
                self._state = "normal"
            elif char == "c":
                self.soft_reset()
                self._state = "normal"
            else:
                self._state = "normal"
        elif state == "esc_skip":
            self._state = "normal"
        elif state == "osc":
            if char == "\x07":
                self._finish_osc()
            elif char == "\x1b":
                self._state = "osc_esc"
            else:
                self._osc += char
        elif state == "osc_esc":
            if char == "\\":
                self._finish_osc()
                self._state = "normal"
            else:
                self._state = "osc"
        elif state == "csi":
            if "0" <= char <= "9" or char in ";:?<=>":
                self._params += char
            elif char.isalpha() or char in "@`~!":
                self._csi(char)
                self._state = "normal"
            elif char == "\x1b":
                self._state = "esc"
            else:
                self._state = "normal"

    def _finish_osc(self) -> None:
        payload = getattr(self, "_osc", "")
        self._state = "normal"
        self._osc = ""
        if payload.startswith("0;") or payload.startswith("2;"):
            title = payload.split(";", 1)[1] if ";" in payload else ""
            self.title = title.strip()
            return
        if payload.startswith("52;"):
            # OSC 52: the program puts text on the clipboard. The shell owns
            # the real clipboard, so the text is kept here and handed over.
            self._read_clipboard(payload.split(";", 2)[-1])
            return
        if payload in ("10;", "11;") or payload.startswith(("10;?", "11;?")):
            # OSC 10/11: "what colour is your background?" A modern TUI asks
            # this to pick a readable palette - and waits for the answer.
            self._report_color(payload.startswith("10"))
            return

    def _report_color(self, foreground: bool) -> None:
        """Answers the OSC 10/11 colour query."""
        red, green, blue = GLASS_FOREGROUND if foreground else GLASS_BACKGROUND
        color = f"rgb:{red:04x}/{green:04x}/{blue:04x}"
        number = 10 if foreground else 11
        self.responses.append(f"\x1b]{number};{color}\x1b\\".encode("ascii"))

    def _read_clipboard(self, blob: str) -> None:
        import base64
        try:
            self.clipboard = base64.b64decode(blob + "===").decode("utf-8", "replace")
        except Exception:
            self.clipboard = ""

    # -- writing -------------------------------------------------------
    def _put(self, char: str) -> None:
        if self._pending_wrap:
            self.cursor_col = 0
            self._linefeed()
            self._pending_wrap = False
        if self.cursor_col >= self.cols:
            if not self.autowrap:
                self.cursor_col = self.cols - 1
            else:
                self.cursor_col = 0
                self._linefeed()
        row = self.cells[self.cursor_row]
        cell = Cell(char, self.fg, self.bg, self.attrs)
        if self.insert_mode:
            row.insert(self.cursor_col, cell)
            del row[self.cols:]
        else:
            row[self.cursor_col] = cell
        self.cursor_col += 1
        if self.cursor_col >= self.cols:
            self.cursor_col = self.cols - 1
            self._pending_wrap = True

    def _linefeed(self) -> None:
        self._pending_wrap = False
        if self.cursor_row == self.scroll_bottom:
            self._scroll_up(1)
        elif self.cursor_row < self.rows - 1:
            self.cursor_row += 1

    def _reverse_index(self) -> None:
        if self.cursor_row == self.scroll_top:
            self._scroll_down(1)
        elif self.cursor_row > 0:
            self.cursor_row -= 1

    def _scroll_up(self, count: int) -> None:
        grid = self.cells
        for _ in range(max(1, count)):
            if not self.use_alternate and self.scroll_top == 0:
                dropped = grid.pop(0)
                grid.append([Cell() for _ in range(self.cols)])
                self.scrollback.append(dropped)
                if len(self.scrollback) > self.scrollback_limit:
                    del self.scrollback[: len(self.scrollback) - self.scrollback_limit]
            else:
                del grid[self.scroll_top]
                grid.insert(self.scroll_bottom, [Cell() for _ in range(self.cols)])

    def _scroll_down(self, count: int) -> None:
        grid = self.cells
        for _ in range(max(1, count)):
            del grid[self.scroll_bottom]
            grid.insert(self.scroll_top, [Cell() for _ in range(self.cols)])

    def _tab(self) -> None:
        col = self.cursor_col + 1
        stops = sorted(stop for stop in self.tab_stops if stop > self.cursor_col)
        self.cursor_col = min(self.cols - 1, stops[0] if stops else col)

    # -- modes ---------------------------------------------------------
    def set_mode(self, mode: int, enabled: bool) -> None:
        if mode in (1000, 1002, 1003, 1004, 1005, 1006, 1015, 1016):
            if enabled:
                self.mouse_modes.add(mode)
            else:
                self.mouse_modes.discard(mode)
            return
        if mode == 25:
            self.cursor_visible = enabled
        elif mode == 7:
            self.autowrap = enabled
        elif mode == 4:
            self.insert_mode = enabled
        elif mode == 6:
            self.origin_mode = enabled
            self.cursor_row = self.scroll_top if enabled else 0
            self.cursor_col = 0
        elif mode == 2004:
            self.bracketed_paste = enabled
        elif mode in (47, 1047, 1049):
            self._switch_screen(enabled, mode == 1049)

    def _switch_screen(self, enabled: bool, with_cursor: bool) -> None:
        if enabled:
            if self.alternate is None:
                self.alternate = self._blank_grid()
            if with_cursor:
                self.save_cursor()
            self.use_alternate = True
            self.alternate = self._blank_grid()
            self.cursor_row = 0
            self.cursor_col = 0
        else:
            if with_cursor:
                self.restore_cursor()
            self.use_alternate = False
            self.alternate = None

    def soft_reset(self) -> None:
        """DECSTR: reset the modes without wiping the screen."""
        self.cursor_row = 0
        self.cursor_col = 0
        self.fg = None
        self.bg = None
        self.attrs = 0
        self.cursor_visible = True
        self.autowrap = True
        self.insert_mode = False
        self.origin_mode = False
        self.bracketed_paste = False
        self.scroll_top = 0
        self.scroll_bottom = self.rows - 1
        self._pending_wrap = False

    def save_cursor(self) -> None:
        self.saved_cursor = {
            "row": self.cursor_row,
            "col": self.cursor_col,
            "fg": self.fg,
            "bg": self.bg,
            "attrs": self.attrs,
        }

    def restore_cursor(self) -> None:
        saved = self.saved_cursor
        if not saved:
            self.cursor_row = 0
            self.cursor_col = 0
            return
        self.cursor_row = min(saved["row"], self.rows - 1)
        self.cursor_col = min(saved["col"], self.cols - 1)
        self.fg = saved["fg"]
        self.bg = saved["bg"]
        self.attrs = saved["attrs"]

    # -- mouse ---------------------------------------------------------
    def mouse_tracking(self) -> bool:
        """True when the program asked for mouse reports."""
        return bool(self.mouse_modes & {1000, 1002, 1003})

    def mouse_motion(self, button_down: bool) -> bool:
        """True when a drag should be reported."""
        if 1003 in self.mouse_modes:
            return True
        if 1002 in self.mouse_modes:
            return button_down
        return False

    def encode_mouse(self, button: int, col: int, row: int, action: str = "press",
                     modifiers: int = 0) -> bytes:
        """Encodes one mouse event the way xterm does.

        ``action`` is ``press``, ``release`` or ``move``. The SGR encoding
        (mode 1006) is used when the program asked for it, urxvt's (1015)
        when it asked for that, and the original X10 encoding otherwise.
        """
        col = max(1, min(self.cols, col))
        row = max(1, min(self.rows, row))
        code = button + modifiers
        if action == "move":
            code += 32
        if 1006 in self.mouse_modes:
            final = "m" if action == "release" else "M"
            return f"\x1b[<{code};{col};{row}{final}".encode("ascii")
        if 1015 in self.mouse_modes:
            return f"\x1b[{code + 32};{col};{row}M".encode("ascii")
        # X10: the release is reported as button 3, and the three bytes after
        # the introducer are offsets by 32.
        if action == "release":
            code = 3
        return bytes([27, 91, 77, 32 + code, 32 + col, 32 + row])

    # -- CSI -----------------------------------------------------------
    def _csi(self, final: str) -> None:
        raw = self._params.replace("<", "").replace("=", "")
        private = raw.startswith("?")
        if private:
            raw = raw[1:]
        raw = raw.split(":")[0]
        parts = [p for p in raw.split(";") if p != ""]
        try:
            nums = [int(p) for p in parts]
        except ValueError:
            nums = []

        def arg(index: int, default: int) -> int:
            if index < len(nums) and nums[index] > 0:
                return nums[index]
            return default

        def arg0(index: int, default: int) -> int:
            if index < len(nums):
                return nums[index]
            return default

        if final == "m" and not private:
            self._sgr(parts)
        elif final in ("H", "f") and not private:
            self._goto(arg(0, 1) - 1, arg(1, 1) - 1)
        elif final == "A" and not private:
            self.cursor_row = max(0, self.cursor_row - arg(0, 1))
        elif final == "B" and not private:
            self.cursor_row = min(self.rows - 1, self.cursor_row + arg(0, 1))
        elif final == "C" and not private:
            self.cursor_col = min(self.cols - 1, self.cursor_col + arg(0, 1))
        elif final == "D" and not private:
            self.cursor_col = max(0, self.cursor_col - arg(0, 1))
        elif final == "E" and not private:
            self.cursor_row = min(self.rows - 1, self.cursor_row + arg(0, 1))
            self.cursor_col = 0
        elif final == "F" and not private:
            self.cursor_row = max(0, self.cursor_row - arg(0, 1))
            self.cursor_col = 0
        elif final == "G" and not private:
            self.cursor_col = max(0, min(self.cols - 1, arg(0, 1) - 1))
        elif final == "d" and not private:
            self._goto(arg(0, 1) - 1, self.cursor_col)
        elif final == "J" and not private:
            self._erase_display(arg0(0, 0))
        elif final == "K" and not private:
            self._erase_line(arg0(0, 0))
        elif final == "L" and not private:
            self._insert_lines(arg(0, 1))
        elif final == "M" and not private:
            self._delete_lines(arg(0, 1))
        elif final == "@" and not private:
            self._insert_chars(arg(0, 1))
        elif final == "P" and not private:
            self._delete_chars(arg(0, 1))
        elif final == "X" and not private:
            self._erase_chars(arg(0, 1))
        elif final == "S" and not private:
            self._scroll_up(arg(0, 1))
        elif final == "T" and not private:
            self._scroll_down(arg(0, 1))
        elif final == "I" and not private:
            for _ in range(arg(0, 1)):
                self._tab()
        elif final == "Z" and not private:
            for _ in range(arg(0, 1)):
                self.cursor_col = max(0, self.cursor_col - 1)
        elif final == "g" and not private:
            if arg0(0, 0) == 3:
                self.tab_stops.clear()
            else:
                self.tab_stops.discard(self.cursor_col)
        elif final == "r" and not private:
            top = arg(0, 1) - 1
            bottom = arg(1, self.rows) - 1
            if 0 <= top < bottom < self.rows:
                self.scroll_top, self.scroll_bottom = top, bottom
                self._goto(0, 0)
        elif final == "s" and not private:
            self.save_cursor()
        elif final == "u" and not private:
            self.restore_cursor()
        elif final == "h" and private:
            for mode in nums:
                self.set_mode(mode, True)
        elif final == "l" and private:
            for mode in nums:
                self.set_mode(mode, False)
        elif final == "h" and not private:
            pass
        elif final == "l" and not private:
            pass
        elif final == "n" and not private:
            self._device_status(arg0(0, 0))
        elif final == "c" and not private:
            self.responses.append(b"\x1b[?1;2c")
        elif final == "u" and private:
            # The kitty keyboard protocol query. This terminal speaks the
            # classic encodings only, so the answer says "no extra keys" and
            # the program falls back to them.
            self.responses.append(b"\x1b[?0u")
        elif final == "q" and not private:
            self.cursor_style = arg(0, 1)
        elif final == "p" and private:
            pass
        elif final == "t" and not private:
            self._report_size(arg0(0, 0))

    def _report_size(self, request: int) -> None:
        """Answers the window-size reports (`CSI 14t`, `18t`, `19t`).

        A program that asks how big the window is wants the answer; a
        program that waits for it never draws anything.
        """
        if request == 14:
            # The size of the text area, in pixels.
            cell_width, cell_height = self.cell_size
            width = self.cols * max(1, cell_width)
            height = self.rows * max(1, cell_height)
            self.responses.append(f"\x1b[4;{height};{width}t".encode("ascii"))
        elif request == 18:
            # The size of the window, in characters.
            self.responses.append(
                f"\x1b[8;{self.rows};{self.cols}t".encode("ascii"))
        elif request == 19:
            # The size of the screen, in characters.
            self.responses.append(
                f"\x1b[9;{self.rows};{self.cols}t".encode("ascii"))

    def _goto(self, row: int, col: int) -> None:
        if self.origin_mode:
            row += self.scroll_top
        self.cursor_row = max(0, min(self.rows - 1, row))
        self.cursor_col = max(0, min(self.cols - 1, col))
        self._pending_wrap = False

    def _device_status(self, request: int) -> None:
        if request == 6:
            self.responses.append(
                f"\x1b[{self.cursor_row + 1};{self.cursor_col + 1}R".encode("ascii"))
        elif request == 5:
            self.responses.append(b"\x1b[0n")

    def _erase_display(self, mode: int) -> None:
        grid = self.cells
        if mode in (2, 3):
            for row in grid:
                for index in range(len(row)):
                    row[index] = Cell()
        elif mode == 0:
            # From the cursor to the end of the screen.
            self._erase_line(0)
            for row_index in range(self.cursor_row + 1, self.rows):
                self._erase_range(row_index, 0, self.cols)
        elif mode == 1:
            # From the start of the screen to the cursor.
            self._erase_line(1)
            for row_index in range(0, self.cursor_row):
                self._erase_range(row_index, 0, self.cols)

    def _erase_line(self, mode: int, start: int = 0, end: Optional[int] = None) -> None:
        if end is None:
            end = self.cols
        if mode == 0:
            self._erase_range(self.cursor_row, self.cursor_col, end)
        elif mode == 1:
            self._erase_range(self.cursor_row, start, self.cursor_col + 1)
        else:
            self._erase_range(self.cursor_row, start, end)

    def _erase_range(self, row_index: int, start: int, end: int) -> None:
        row = self.cells[row_index]
        for index in range(max(0, start), min(end, len(row))):
            row[index] = Cell()

    def _insert_lines(self, count: int) -> None:
        if not (self.scroll_top <= self.cursor_row <= self.scroll_bottom):
            return
        grid = self.cells
        for _ in range(count):
            del grid[self.scroll_bottom]
            grid.insert(self.cursor_row, [Cell() for _ in range(self.cols)])

    def _delete_lines(self, count: int) -> None:
        if not (self.scroll_top <= self.cursor_row <= self.scroll_bottom):
            return
        grid = self.cells
        for _ in range(count):
            del grid[self.cursor_row]
            grid.insert(self.scroll_bottom, [Cell() for _ in range(self.cols)])

    def _insert_chars(self, count: int) -> None:
        row = self.cells[self.cursor_row]
        for _ in range(count):
            row.insert(self.cursor_col, Cell())
        del row[self.cols:]

    def _delete_chars(self, count: int) -> None:
        row = self.cells[self.cursor_row]
        for _ in range(count):
            if self.cursor_col < len(row):
                del row[self.cursor_col]
        row.extend(Cell() for _ in range(self.cols - len(row)))

    def _erase_chars(self, count: int) -> None:
        row = self.cells[self.cursor_row]
        for index in range(self.cursor_col, min(self.cursor_col + count, len(row))):
            row[index] = Cell()

    # -- SGR -----------------------------------------------------------
    def _sgr(self, parts: List[str]) -> None:
        if not parts:
            parts = ["0"]
        index = 0
        while index < len(parts):
            try:
                code = int(parts[index])
            except ValueError:
                index += 1
                continue
            if code == 0:
                self.fg, self.bg, self.attrs = None, None, 0
            elif code == 1:
                self.attrs |= ATTR_BOLD
            elif code == 2:
                self.attrs |= ATTR_DIM
            elif code == 3:
                self.attrs |= ATTR_ITALIC
            elif code == 4:
                self.attrs |= ATTR_UNDERLINE
            elif code == 7:
                self.attrs |= ATTR_REVERSE
            elif code == 22:
                self.attrs &= ~(ATTR_BOLD | ATTR_DIM)
            elif code == 23:
                self.attrs &= ~ATTR_ITALIC
            elif code == 24:
                self.attrs &= ~ATTR_UNDERLINE
            elif code == 27:
                self.attrs &= ~ATTR_REVERSE
            elif 30 <= code <= 37:
                self.fg = ANSI_COLORS[code - 30]
            elif 90 <= code <= 97:
                self.fg = ANSI_COLORS[code - 90 + 8]
            elif code == 39:
                self.fg = None
            elif code == 49:
                self.bg = None
            elif 40 <= code <= 47:
                self.bg = ANSI_COLORS[code - 40]
            elif 100 <= code <= 107:
                self.bg = ANSI_COLORS[code - 100 + 8]
            elif code in (38, 48):
                target = "fg" if code == 38 else "bg"
                if index + 1 < len(parts) and parts[index + 1] == "5":
                    try:
                        color = _color_256(int(parts[index + 2]))
                    except (ValueError, IndexError):
                        color = None
                    if color is not None:
                        setattr(self, target, color)
                    index += 2
                elif index + 1 < len(parts) and parts[index + 1] == "2":
                    try:
                        color = (int(parts[index + 2]), int(parts[index + 3]),
                                 int(parts[index + 4]))
                    except (ValueError, IndexError):
                        color = None
                    if color is not None:
                        setattr(self, target, color)
                    index += 4
            index += 1


def _xterm_modifier(shift: bool, alt: bool, ctrl: bool) -> int:
    """The modifier number xterm puts into a CSI sequence (1 = none)."""
    return 1 + (1 if shift else 0) + (2 if alt else 0) + (4 if ctrl else 0)


def key_to_bytes(event) -> bytes:
    """Translates a pygame KEYDOWN into the byte sequence a terminal expects.

    Modified keys use the xform encoding (``CSI 1;5A`` for Ctrl+Up) because a
    full-screen program reads the modifier from the sequence, not from a
    separate escape.
    """
    key, mod = event.key, getattr(event, "mod", 0)
    ctrl = bool(mod & pygame.KMOD_CTRL)
    alt = bool(mod & pygame.KMOD_ALT)
    shift = bool(mod & pygame.KMOD_SHIFT)
    modifier = _xterm_modifier(shift, alt, ctrl)

    arrow = {
        pygame.K_UP: "A",
        pygame.K_DOWN: "B",
        pygame.K_RIGHT: "C",
        pygame.K_LEFT: "D",
    }.get(key)
    if arrow is not None:
        if modifier == 1:
            return f"\x1b[{arrow}".encode("ascii")
        return f"\x1b[1;{modifier}{arrow}".encode("ascii")

    if key in (pygame.K_HOME, pygame.K_END):
        final = "H" if key == pygame.K_HOME else "F"
        if modifier == 1:
            return f"\x1b[{final}".encode("ascii")
        return f"\x1b[1;{modifier}{final}".encode("ascii")

    tilde = {
        pygame.K_INSERT: 2,
        pygame.K_DELETE: 3,
        pygame.K_PAGEUP: 5,
        pygame.K_PAGEDOWN: 6,
    }.get(key)
    if tilde is not None:
        if modifier == 1:
            return f"\x1b[{tilde}~".encode("ascii")
        return f"\x1b[{tilde};{modifier}~".encode("ascii")

    specials = {
        pygame.K_RETURN: b"\r",
        pygame.K_KP_ENTER: b"\r",
        pygame.K_BACKSPACE: b"\x7f",
        pygame.K_TAB: b"\t",
        pygame.K_ESCAPE: b"\x1b",
        pygame.K_F1: b"\x1bOP",
        pygame.K_F2: b"\x1bOQ",
        pygame.K_F3: b"\x1bOR",
        pygame.K_F4: b"\x1bOS",
        pygame.K_F5: b"\x1b[15~",
        pygame.K_F6: b"\x1b[17~",
    }
    if key in specials:
        if key == pygame.K_TAB and shift:
            # Shift+Tab is how a program says "previous field".
            return b"\x1b[Z"
        return specials[key]

    if ctrl:
        if pygame.K_a <= key <= pygame.K_z:
            return bytes([key - pygame.K_a + 1])
        if key == pygame.K_SPACE:
            return b"\x00"
        if key == pygame.K_LEFT_BRACKET:
            return b"\x1b"
        if key == pygame.K_BACKSLASH:
            return b"\x1c"
        if key == pygame.K_SPACE:
            return b"\x00"

    if pygame.K_a <= key <= pygame.K_z or pygame.K_0 <= key <= pygame.K_9:
        base = chr(key).lower() if pygame.K_a <= pygame.K_z else chr(key)
        if shift:
            base = base.upper()
        if alt:
            return b"\x1b" + base.encode("utf-8")
        return base.encode("utf-8")

    unicode_char = getattr(event, "unicode", "")
    if unicode_char:
        if alt:
            return b"\x1b" + unicode_char.encode("utf-8")
        return unicode_char.encode("utf-8")
    return b""


class TerminalView:
    """Renders a `TerminalScreen` into a rect and paints the cursor."""

    def __init__(self, screen: TerminalScreen, theme, size_units: float = 9,
                 placeholder: str = ""):
        self.screen = screen
        self.theme = theme
        self.size_units = size_units
        self.scroll_offset = 0
        # The text the user selected with the mouse, as a rectangle of cells:
        # (first column, first row, last column, last row).
        self.selection: Optional[Tuple[int, int, int, int]] = None
        # Shown only while the shell has not printed anything yet, so opening
        # the tab never looks like a broken black rectangle.
        self.placeholder = placeholder

    def metrics(self, rect: pygame.Rect) -> Tuple[int, int, int, int]:
        cw, ch = self.theme.char_cell(self.size_units)
        cols = max(8, (rect.width - self.theme.px(8)) // cw)
        rows = max(3, (rect.height - self.theme.px(8)) // ch)
        return cw, ch, cols, rows

    def origin(self, rect: pygame.Rect) -> Tuple[int, int]:
        """Top-left pixel of the character grid inside `rect`."""
        return rect.x + self.theme.px(4), rect.y + self.theme.px(4)

    def cell_at(self, rect: pygame.Rect, x: int, y: int) -> Optional[Tuple[int, int]]:
        """Maps a pixel to a (column, row) of the grid, or None if outside."""
        cw, ch, cols, rows = self.metrics(rect)
        origin_x, origin_y = self.origin(rect)
        if x < origin_x or y < origin_y:
            return None
        col = (x - origin_x) // cw
        row = (y - origin_y) // ch
        if col < 0 or col >= cols or row < 0 or row >= rows:
            return None
        return int(col), int(row)

    def visible_rows(self) -> List[List[Cell]]:
        """The rows to draw, including any scrolled-back history."""
        history = self.screen.scrollback
        offset = max(0, min(len(history), self.scroll_offset))
        if offset <= 0:
            return self.screen.lines()
        past = history[len(history) - offset:]
        combined: List[List[Cell]] = []
        for row in past:
            combined.append(row)
        combined.extend(self.screen.lines())
        return combined

    def row_offset(self) -> int:
        """The index of the screen's first row in the visible list.

        The selection is counted in visible rows, so it stays where the user
        put it while the history is scrolled back.
        """
        history = len(self.screen.scrollback)
        return max(0, min(history, self.scroll_offset))

    def _selection_columns(self, row_index: int) -> Optional[Tuple[int, int]]:
        """The columns of the selection on one visible row."""
        selection = self.selection
        if selection is None:
            return None
        first_col, first_row, last_col, last_row = selection
        if row_index < first_row or row_index > last_row:
            return None
        if first_row == last_row:
            return first_col, last_col
        if row_index == first_row:
            return first_col, 10 ** 6
        if row_index == last_row:
            return 0, last_col
        return 0, 10 ** 6

    def selected_text(self) -> str:
        """The text the user selected, as it is on the screen."""
        selection = self.selection
        if selection is None:
            return ""
        first_col, first_row, last_col, last_row = selection
        rows = self.visible_rows()
        if first_row < 0 or last_row >= len(rows) or first_row > last_row:
            return ""
        pieces = []
        for index in range(first_row, last_row + 1):
            row = rows[index]
            start = first_col if index == first_row else 0
            end = last_col if index == last_row else len(row) - 1
            start = max(0, min(start, len(row) - 1))
            end = max(start, min(end, len(row) - 1))
            pieces.append("".join(cell.char for cell in row[start:end + 1]).rstrip())
        return "\n".join(pieces).strip("\n")

    def draw(self, surface, rect: pygame.Rect, focused: bool = True,
             cursor_on: bool = True) -> None:
        theme = self.theme
        c = theme.colors
        cw, ch, cols, rows = self.metrics(rect)
        self.screen.resize(cols, rows)

        pygame.draw.rect(surface, c.screen, rect, border_radius=theme.px(4))
        pygame.draw.rect(surface, c.border_soft, rect, width=max(1, theme.px(1)),
                         border_radius=theme.px(4))

        font = theme.mono(self.size_units)
        bold_font = theme.mono(self.size_units, bold=True)
        origin_x, origin_y = self.origin(rect)

        rows_to_draw = self.visible_rows()
        if self.scroll_offset > 0:
            rows_to_draw = rows_to_draw[-rows:]
        else:
            rows_to_draw = rows_to_draw[:rows]

        for row_index, row in enumerate(rows_to_draw[:rows]):
            y = origin_y + row_index * ch
            if y + ch > rect.bottom:
                break
            selection = self._selection_columns(row_index)
            run_text = ""
            run_color: Optional[Color] = None
            run_bg: Optional[Color] = None
            run_bold = False
            run_underline = False
            run_x = origin_x

            def flush() -> None:
                nonlocal run_text, run_x
                if not run_text:
                    return
                active = bold_font if run_bold else font
                width = active.size(run_text)[0]
                if run_bg is not None:
                    pygame.draw.rect(surface, run_bg, (run_x, y, width, ch))
                surface.blit(active.render(run_text, True, run_color or DEFAULT_COLOR),
                             (run_x, y))
                if run_underline:
                    pygame.draw.line(surface, run_color or DEFAULT_COLOR,
                                     (run_x, y + ch - 1), (run_x + width, y + ch - 1), 1)
                run_x += width
                run_text = ""

            for column, cell in enumerate(row):
                color = cell.fg if cell.fg is not None else DEFAULT_COLOR
                background = cell.bg
                bold = bool(cell.attrs & ATTR_BOLD)
                underline = bool(cell.attrs & ATTR_UNDERLINE)
                if cell.attrs & ATTR_REVERSE:
                    color = background if background is not None else c.screen
                    background = cell.fg if cell.fg is not None else c.text
                if selection is not None and selection[0] <= column <= selection[1]:
                    background = theme.mix(background or c.screen, c.highlight, 0.45)
                if run_color is None:
                    run_color, run_bg, run_bold, run_underline = \
                        color, background, bold, underline
                elif (color, background, bold, underline) != \
                        (run_color, run_bg, run_bold, run_underline):
                    flush()
                    run_color, run_bg, run_bold, run_underline = \
                        color, background, bold, underline
                run_text += cell.char
            flush()

        if self.placeholder:
            has_text = any(cell.char.strip() for row in self.screen.lines()
                           for cell in row)
            if not has_text:
                hint = theme.font("body").render(self.placeholder, True, c.text_dim)
                surface.blit(hint, (origin_x + theme.px(2),
                                    origin_y + theme.px(2)))

        if (focused and cursor_on and self.screen.cursor_visible
                and self.scroll_offset == 0):
            cx = origin_x + self.screen.cursor_col * cw
            cy = origin_y + self.screen.cursor_row * ch
            if self.screen.cursor_style in (3, 4):
                pygame.draw.rect(surface, c.highlight,
                                 (cx, cy + ch // 3, max(2, cw - 1), max(2, ch // 3)),
                                 width=0)
            elif self.screen.cursor_style in (5, 6):
                pygame.draw.rect(surface, c.highlight,
                                 (cx, cy, max(2, cw - 1), max(2, ch // 3)),
                                 width=0)
            else:
                pygame.draw.rect(surface, c.highlight, (cx, cy, max(2, cw - 1), ch),
                                 width=0)

    def _blit_run(self, surface, font, bold_font, text: str, color, background,
                  x: int, y: int, cw: int, ch: int, cell_attrs: int = 0) -> None:
        """Draws one run of identically styled characters."""
        if not text:
            return
        active = bold_font if cell_attrs & ATTR_BOLD else font
        width = active.size(text)[0]
        if background is not None:
            pygame.draw.rect(surface, background, (x, y, width, ch))
        surface.blit(active.render(text, True, color or DEFAULT_COLOR), (x, y))
