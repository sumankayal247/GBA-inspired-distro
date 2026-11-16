#!/usr/bin/env python3
"""
The terminal has to be able to host a full-screen agent CLI.

The Antigravity CLI (`agy`) is such a program: it takes over the screen, paints
in truecolor, asks the terminal where the cursor is, asks for mouse reports and
sets a window title. A terminal that cannot do those things cannot run it -
the program either draws over itself or waits forever for an answer.

This test runs a stand-in for that program through a *real* pseudo-terminal and
checks the whole loop: the program asks, the terminal answers, the program
receives the answer, and the screen shows what the program painted.
"""

import os
import re
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import gba_terminal  # noqa: E402


# The program the terminal has to satisfy. It is deliberately written the way
# a real TUI writes: alternate screen, truecolor, title, mouse reporting and
# the cursor-position report.
AGENT_PROGRAM = r'''
import os, select, sys

out = sys.stdout
out.write("\033[?1049h")                 # take over the screen
out.write("\033[2J\033[H")
out.write("\033[38;2;120;220;255m")      # truecolor headline
out.write("Antigravity CLI")
out.write("\033[0m")
out.write("\r\n")
out.write("\033]0;Antigravity CLI\007") # window title
out.write("\033[?1000h\033[?1006h")      # mouse reports, SGR encoded
out.write("\033[?2004h")                 # bracketed paste
out.write("\033[6n")                     # where is the cursor?
out.flush()

# Wait for the terminal's answer (the cursor-position report).
reply = b""
while b"R" not in reply:
    ready, _, _ = select.select([0], [], [], 5)
    if not ready:
        break
    reply += os.read(0, 64)
with open(MARKER, "w", encoding="utf-8") as handle:
    handle.write(repr(reply))
'''


class TestAgentCliInTheTerminal(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.mkdtemp()
        self.marker = os.path.join(self.temp, "reply.txt")
        self.session = gba_terminal.TerminalSession(cols=100, rows=30, shell="/bin/bash")
        self.screen = gba_terminal.TerminalScreen(cols=100, rows=30)
        if self.session.error:
            self.skipTest(f"no pty available: {self.session.error}")

    def tearDown(self):
        self.session.close()

    def start_agent(self):
        program = os.path.join(self.temp, "agent.py")
        with open(program, "w", encoding="utf-8") as handle:
            handle.write(AGENT_PROGRAM.replace("MARKER", repr(self.marker)))
        # raw mode, the way a program that wants immediate events does it
        self.session.write(f"stty raw -echo; python3 {program}\n".encode())

    def pump(self, seconds=5.0):
        """Reads the program's output until it goes quiet."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            data = self.session.drain()
            if data:
                self.screen.feed(data)
                deadline = time.time() + 1.0
            else:
                time.sleep(0.02)

    def test_the_program_gets_the_screen_the_terminal_can_give(self):
        self.start_agent()
        self.pump()

        # It took over the screen ...
        self.assertTrue(self.screen.use_alternate)
        # ... painted what it wanted ...
        self.assertIn("Antigravity CLI", self.screen.text())
        # ... set a title ...
        self.assertEqual(self.screen.title, "Antigravity CLI")
        # ... asked for mouse reports ...
        self.assertTrue(self.screen.mouse_tracking())
        # ... and asked where the cursor is.
        self.assertTrue(self.screen.responses, "the question was not answered")
        self.assertRegex(self.screen.responses[0].decode("ascii"),
                         r"^\x1b\[\d+;\d+R$")
        # The answer names the place the cursor really is.
        row, col = re.match(r"\x1b\[(\d+);(\d+)R",
                            self.screen.responses[0].decode("ascii")).groups()
        self.assertEqual(int(row), self.screen.cursor_row + 1)
        self.assertEqual(int(col), self.screen.cursor_col + 1)

    def test_the_cursor_position_report_reaches_the_program(self):
        self.start_agent()
        self.pump()

        # The shell answers the question the program asked.
        while self.screen.responses:
            self.session.write(self.screen.responses.pop(0))

        deadline = time.time() + 5
        reply = ""
        while time.time() < deadline:
            self.session.drain()
            if os.path.exists(self.marker):
                with open(self.marker, encoding="utf-8") as handle:
                    reply = handle.read()
                if reply:
                    break
            time.sleep(0.05)
        self.assertRegex(reply, r"\\x1b\[\d+;\d+R")


if __name__ == "__main__":
    unittest.main()
