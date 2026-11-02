"""
GBA OS terminal: a real PTY-backed shell rendered inside the console shell.

Two pieces:

* `TerminalSession` owns a pseudo-terminal and the child shell process, so the
  terminal tab is a genuine interactive session (saves, `ls`, `nano` ... all
  work) rather than a fake command runner.
* `TerminalScreen` is a small ANSI terminal emulator: cursor movement, erase,
  16/256-colour SGR and scrolling, which is the subset real shells and tools
  actually emit.

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

        try:
            master, slave = pty.openpty()
            self._set_winsize(slave, self.rows, self.cols)
            self._proc = subprocess.Popen(
                [shell, "-i"],
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
    """Minimal ANSI terminal emulator (grid of characters + colours)."""

    def __init__(self, cols: int = 80, rows: int = 24, scrollback: int = 500):
        self.cols = cols
        self.rows = rows
        self.scrollback_limit = scrollback
        self._decoder = b""
        self.reset()
        self.scrollback: List[List[Tuple[str, int, bool]]] = []

    # -- state ---------------------------------------------------------
    def reset(self) -> None:
        self.cells = [[(" ", DEFAULT_FG, False) for _ in range(self.cols)]
                      for _ in range(self.rows)]
        self.cursor_row = 0
        self.cursor_col = 0
        self.fg = DEFAULT_FG
        self.bold = False
        self._state = "normal"
        self._params = ""
        self.cursor_visible = True

    def resize(self, cols: int, rows: int) -> None:
        cols, rows = max(8, cols), max(3, rows)
        if (cols, rows) == (self.cols, self.rows):
            return
        for row in self.cells:
            if len(row) < cols:
                row.extend([(" ", DEFAULT_FG, False)] * (cols - len(row)))
            elif len(row) > cols:
                del row[cols:]
        if rows < len(self.cells):
            self.cells = self.cells[:rows]
        while len(self.cells) < rows:
            self.cells.append([(" ", DEFAULT_FG, False) for _ in range(cols)])
        self.cols, self.rows = cols, rows
        self.cursor_row = min(self.cursor_row, rows - 1)
        self.cursor_col = min(self.cursor_col, cols - 1)

    # -- rendering helpers --------------------------------------------
    def lines(self) -> List[List[Tuple[str, int, bool]]]:
        return self.cells

    def text(self) -> str:
        return "\n".join("".join(cell[0] for cell in row).rstrip() for row in self.cells)

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
            elif char == "\n":
                self._linefeed()
            elif char == "\b":
                self.cursor_col = max(0, self.cursor_col - 1)
            elif char == "\t":
                self.cursor_col = min(self.cols - 1, (self.cursor_col // 8 + 1) * 8)
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
            elif char in "()78":
                self._state = "esc_skip"
            else:
                self._state = "normal"
        elif state == "esc_skip":
            self._state = "normal"
        elif state == "osc":
            if char == "\x07":
                self._state = "normal"
            elif char == "\x1b":
                self._state = "osc_esc"
        elif state == "osc_esc":
            self._state = "normal" if char == "\\" else "osc"
        elif state == "csi":
            if "0" <= char <= "9" or char in ";:?<=>":
                self._params += char
            elif char.isalpha() or char in "@`~":
                self._csi(char)
                self._state = "normal"
            elif char == "\x1b":
                self._state = "esc"
            else:
                self._state = "normal"

    def _put(self, char: str) -> None:
        if self.cursor_col >= self.cols:
            self.cursor_col = 0
            self._linefeed()
        self.cells[self.cursor_row][self.cursor_col] = (char, self.fg, self.bold)
        self.cursor_col += 1

    def _linefeed(self) -> None:
        self.cursor_row += 1
        if self.cursor_row >= self.rows:
            self.cursor_row = self.rows - 1
            dropped = self.cells.pop(0)
            self.cells.append([(" ", DEFAULT_FG, False) for _ in range(self.cols)])
            self.scrollback.append(dropped)
            if len(self.scrollback) > self.scrollback_limit:
                del self.scrollback[: len(self.scrollback) - self.scrollback_limit]

    def _csi(self, final: str) -> None:
        raw = self._params.replace("?", "").replace(">", "").replace("<", "")
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

        if final == "m":
            self._sgr(parts)
        elif final == "H" or final == "f":
            self.cursor_row = min(self.rows - 1, max(0, arg(0, 1) - 1))
            self.cursor_col = min(self.cols - 1, max(0, arg(1, 1) - 1))
        elif final == "A":
            self.cursor_row = max(0, self.cursor_row - arg(0, 1))
        elif final == "B":
            self.cursor_row = min(self.rows - 1, self.cursor_row + arg(0, 1))
        elif final == "C":
            self.cursor_col = min(self.cols - 1, self.cursor_col + arg(0, 1))
        elif final == "D":
            self.cursor_col = max(0, self.cursor_col - arg(0, 1))
        elif final == "G":
            self.cursor_col = min(self.cols - 1, max(0, arg(0, 1) - 1))
        elif final == "d":
            self.cursor_row = min(self.rows - 1, max(0, arg(0, 1) - 1))
        elif final == "J":
            mode = arg(0, 0)
            if mode in (2, 3):
                for row in self.cells:
                    for index in range(len(row)):
                        row[index] = (" ", DEFAULT_FG, False)
            elif mode == 0:
                self._erase_line(self.cursor_row, self.cursor_col, self.cols)
                for row_index in range(self.cursor_row + 1, self.rows):
                    self._erase_line(row_index, 0, self.cols)
        elif final == "K":
            mode = arg(0, 0)
            if mode == 0:
                self._erase_line(self.cursor_row, self.cursor_col, self.cols)
            elif mode == 1:
                self._erase_line(self.cursor_row, 0, self.cursor_col + 1)
            else:
                self._erase_line(self.cursor_row, 0, self.cols)
        elif final in ("h", "l") and raw.startswith("25"):
            self.cursor_visible = final == "h"

    def _erase_line(self, row_index: int, start: int, end: int) -> None:
        row = self.cells[row_index]
        for index in range(start, min(end, len(row))):
            row[index] = (" ", DEFAULT_FG, False)

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
                self.fg, self.bold = DEFAULT_FG, False
            elif code == 1:
                self.bold = True
            elif code == 22:
                self.bold = False
            elif 30 <= code <= 37:
                self.fg = code - 30
            elif 90 <= code <= 97:
                self.fg = code - 90 + 8
            elif code == 39:
                self.fg = DEFAULT_FG
            elif code == 38 and index + 2 < len(parts) and parts[index + 1] == "5":
                try:
                    self.fg = _approx_color(_color_256(int(parts[index + 2])))
                    index += 2
                except ValueError:
                    pass
            elif 40 <= code <= 47 or 100 <= code <= 107 or code in (49,):
                pass  # background colours: keep the glass dark for readability
            index += 1


def _approx_color(color: Color) -> int:
    best, best_distance = DEFAULT_FG, 1 << 30
    for index, candidate in enumerate(ANSI_COLORS):
        distance = sum((a - b) ** 2 for a, b in zip(candidate, color))
        if distance < best_distance:
            best, best_distance = index, distance
    return best


def key_to_bytes(event) -> bytes:
    """Translates a pygame KEYDOWN into the byte sequence a terminal expects."""
    key, mod = event.key, getattr(event, "mod", 0)
    ctrl = bool(mod & pygame.KMOD_CTRL)

    specials = {
        pygame.K_RETURN: b"\r",
        pygame.K_KP_ENTER: b"\r",
        pygame.K_BACKSPACE: b"\x7f",
        pygame.K_TAB: b"\t",
        pygame.K_ESCAPE: b"\x1b",
        pygame.K_UP: b"\x1b[A",
        pygame.K_DOWN: b"\x1b[B",
        pygame.K_RIGHT: b"\x1b[C",
        pygame.K_LEFT: b"\x1b[D",
        pygame.K_HOME: b"\x1b[H",
        pygame.K_END: b"\x1b[F",
        pygame.K_INSERT: b"\x1b[2~",
        pygame.K_DELETE: b"\x1b[3~",
        pygame.K_PAGEUP: b"\x1b[5~",
        pygame.K_PAGEDOWN: b"\x1b[6~",
        pygame.K_F1: b"\x1bOP",
        pygame.K_F2: b"\x1bOQ",
        pygame.K_F3: b"\x1bOR",
        pygame.K_F4: b"\x1bOS",
        pygame.K_F5: b"\x1b[15~",
        pygame.K_F6: b"\x1b[17~",
    }
    if key in specials:
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

    if pygame.K_a <= key <= pygame.K_z or pygame.K_0 <= key <= pygame.K_9:
        base = chr(key).lower() if pygame.K_a <= key <= pygame.K_z else chr(key)
        if mod & pygame.KMOD_SHIFT:
            base = base.upper()
        return base.encode("utf-8")

    unicode_char = getattr(event, "unicode", "")
    if unicode_char:
        return unicode_char.encode("utf-8")
    return b""


class TerminalView:
    """Renders a `TerminalScreen` into a rect and paints the cursor."""

    def __init__(self, screen: TerminalScreen, theme, size_units: float = 9):
        self.screen = screen
        self.theme = theme
        self.size_units = size_units
        self.scroll_offset = 0

    def metrics(self, rect: pygame.Rect) -> Tuple[int, int, int, int]:
        cw, ch = self.theme.char_cell(self.size_units)
        cols = max(8, (rect.width - self.theme.px(8)) // cw)
        rows = max(3, (rect.height - self.theme.px(8)) // ch)
        return cw, ch, cols, rows

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
        origin_x = rect.x + theme.px(4)
        origin_y = rect.y + theme.px(4)

        for row_index, row in enumerate(self.screen.lines()):
            y = origin_y + row_index * ch
            if y + ch > rect.bottom:
                break
            run_text, run_color, run_x = "", None, origin_x
            for char, fg, _bold in row:
                color = ANSI_COLORS[fg % len(ANSI_COLORS)]
                if run_color is None:
                    run_color = color
                elif color != run_color:
                    if run_text.strip():
                        surface.blit(font.render(run_text, True, run_color), (run_x, y))
                    run_x += font.size(run_text)[0]
                    run_text, run_color = "", color
                run_text += char
            if run_text.strip():
                surface.blit(font.render(run_text, True, run_color), (run_x, y))

        if focused and cursor_on and self.screen.cursor_visible:
            cx = origin_x + self.screen.cursor_col * cw
            cy = origin_y + self.screen.cursor_row * ch
            pygame.draw.rect(surface, c.highlight, (cx, cy, max(2, cw - 1), ch), width=0)
