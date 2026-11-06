"""Headless session lifecycle tests; no real mounts or services are touched."""
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


class AutomountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.processes = []
        self.addCleanup(self.stop_all)
        self.env = dict(os.environ, PATH=f"{self.directory}:{os.environ['PATH']}",
                        DISPLAY="", EVENTS=str(self.directory / "events"))
        self.script("udiskie", 'echo "start $*" >> "$EVENTS"\n'
                    'trap \'echo stop >> "$EVENTS"; exit 0\' TERM\n'
                    'while :; do sleep 0.1; done\n')
        self.script("supervisor", 'sleep 0.5\nexit 100\n')
        source = (ROOT / "src/gba_session.sh").read_text()
        source = source.replace('/run/lock/', str(self.directory) + '/')
        source = source.replace('/opt/gba-distro/bin/gba-supervisor',
                                str(self.directory / 'supervisor'))
        (self.directory / 'session').write_text(source)

    def script(self, name, body):
        path = self.directory / name
        path.write_text('#!/bin/bash\n' + body)
        path.chmod(0o755)

    def stop_all(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)

    def start(self):
        process = subprocess.Popen(['bash', str(self.directory / 'session')],
                                   env=self.env, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        self.processes.append(process)
        return process

    def events(self):
        path = self.directory / 'events'
        return path.read_text().splitlines() if path.exists() else []

    def wait_started(self):
        deadline = time.monotonic() + 3
        while not self.events() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(self.events())

    def test_exit_status_cleanup_and_restart(self):
        first = self.start()
        self.assertEqual(first.wait(timeout=5), 100)
        self.assertEqual(self.events(), [
            'start --automount --no-notify --no-tray --no-file-manager', 'stop'])
        self.assertEqual(self.start().wait(timeout=5), 100)
        self.assertEqual(len(self.events()), 4)

    def test_concurrent_session_has_one_automounter(self):
        self.script('supervisor', 'sleep 2\n')
        first = self.start()
        self.wait_started()
        second = self.start()
        self.assertEqual(second.wait(timeout=5), 0)
        self.assertEqual(first.wait(timeout=5), 0)
        self.assertEqual(sum(line.startswith('start ') for line in self.events()), 1)
        self.assertIn('stop', self.events())

    def test_unexpected_daemon_exit_is_restarted(self):
        self.script('supervisor', 'exec sleep 30\n')
        self.script('udiskie', 'echo start >> "$EVENTS"\nexit 1\n')
        process = self.start()
        deadline = time.monotonic() + 8
        while len(self.events()) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertGreaterEqual(len(self.events()), 2)
        process.terminate()
        self.assertEqual(process.wait(timeout=5), 143)

    def test_termination_cleans_up(self):
        self.script('supervisor', 'exec sleep 30\n')
        process = self.start()
        self.wait_started()
        process.terminate()
        self.assertEqual(process.wait(timeout=5), 143)
        self.assertIn('stop', self.events())
