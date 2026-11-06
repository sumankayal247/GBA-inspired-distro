"""Real subprocess shutdown regressions, independent of graphics/hardware."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from gba_adapter_mgba import MgbaSessionRunner
import config


class ShutdownTests(unittest.TestCase):
    def test_escape_escalates_and_reaps_repeatedly(self):
        for ignore_hup, expected in ((False, signal.SIGHUP),
                                     (True, signal.SIGKILL),
                                     (True, signal.SIGKILL)):
            with self.subTest(ignore_hup=ignore_hup), tempfile.TemporaryDirectory() as directory:
                ready = Path(directory) / 'ready'
                source = ('import signal,time,pathlib\n'
                          'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
                          + ('signal.signal(signal.SIGHUP, signal.SIG_IGN)\n' if ignore_hup else '')
                          + f'pathlib.Path({str(ready)!r}).touch()\n'
                          'while True: time.sleep(.05)\n')
                runner = MgbaSessionRunner([sys.executable, '-c', source],
                                           dict(os.environ), stop_timeout=0.1)
                # Deliver the same callback as the raw ESC monitor, but only
                # once the child has installed its signal handlers.
                def escape(_monitor):
                    deadline = time.monotonic() + 3
                    while not ready.exists():
                        if time.monotonic() > deadline:
                            raise RuntimeError('child did not become ready')
                        time.sleep(.01)
                    runner._request_exit()
                started = time.monotonic()
                with patch('gba_adapter_mgba.InputMonitor.start', escape):
                    try:
                        self.assertEqual(runner.run(), -expected)
                        self.assertIsNotNone(runner.child.returncode)
                        self.assertLess(time.monotonic() - started, 4)
                        # Popen already reaped it: it is neither live nor a zombie.
                        with self.assertRaises(ChildProcessError):
                            os.waitpid(runner.child.pid, os.WNOHANG)
                    finally:
                        if runner.child and runner.child.poll() is None:
                            runner.child.kill()
                            runner.child.wait(timeout=2)

    def test_normal_escape_terminates_without_escalation(self):
        runner = MgbaSessionRunner([sys.executable, '-c', 'import time; time.sleep(30)'],
                                   dict(os.environ))
        with patch('gba_adapter_mgba.InputMonitor.start', lambda monitor: runner._request_exit()):
            self.assertEqual(runner.run(), -signal.SIGTERM)
        self.assertIsNotNone(runner.child.returncode)

    def test_exit_during_overlay_closes_it_without_handling_more_actions(self):
        session = Mock()
        runner = MgbaSessionRunner([], {}, session=session)
        runner._request_exit()
        runner._drain_actions()
        session.close_overlay.assert_called_once()
        session.handle_action.assert_not_called()
        self.assertFalse(runner.start_emulator())

    def test_adapter_signal_requests_exit_as_well_as_forwarding(self):
        runner = MgbaSessionRunner([], {})
        child = Mock(stdout=None)
        child.poll.return_value = -signal.SIGTERM
        handlers = {}

        def register(sig, handler):
            handlers[sig] = handler
            return signal.SIG_DFL

        def deliver(_seconds):
            handlers[signal.SIGTERM](signal.SIGTERM, None)

        with patch.object(runner, '_spawn', lambda cmd: setattr(runner, 'child', child)), \
                patch('gba_adapter_mgba.signal.signal', side_effect=register), \
                patch('gba_adapter_mgba.time.sleep', side_effect=deliver):
            self.assertEqual(runner.run(), -signal.SIGTERM)
        self.assertTrue(runner.exit_requested)
        child.send_signal.assert_called_once_with(signal.SIGTERM)

    def test_normal_exit_preserves_status(self):
        runner = MgbaSessionRunner([sys.executable, '-c', 'raise SystemExit(7)'],
                                   dict(os.environ))
        with patch('gba_adapter_mgba.InputMonitor.start', lambda monitor: None):
            self.assertEqual(runner.run(), 7)

    def test_partial_spawn_failure_still_reaps_child(self):
        runner = MgbaSessionRunner([sys.executable, '-c', 'import time; time.sleep(30)'],
                                   dict(os.environ))
        with patch('gba_adapter_mgba.InputMonitor.start', side_effect=RuntimeError('input unavailable')):
            self.assertEqual(runner.run(), 3)
        self.assertIsNotNone(runner.child.poll())

    def test_stop_resumes_suspended_child(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        runner = MgbaSessionRunner([], dict(os.environ), stop_timeout=.1)
        runner.child = child
        try:
            child.send_signal(signal.SIGSTOP)
            self.assertTrue(runner.stop_emulator())
            self.assertIsNotNone(child.returncode)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)

    def test_x11_active_mode_wins_over_advertised_drm_mode(self):
        with patch.object(config, '_xrandr_mode_size', return_value=(1920, 1080)), \
                patch.object(config.os, 'listdir', side_effect=AssertionError('must not read DRM')):
            self.assertEqual(config.display_mode_size({'DISPLAY': ':0'}), (1920, 1080))
