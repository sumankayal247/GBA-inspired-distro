"""
Presentation tests: does the emulated frame actually reach the screen?

The appliance ran mGBA successfully and showed a black screen: the emulator
was fine, the presentation path was not.  These tests cover the three things
that determine whether a frame arrives -

  * the compositor command (a `--vsync` *argument* makes picom exit, so no
    compositor ever ran),
  * the OpenGL policy (forcing llvmpipe is a no-compositor workaround, not a
    default for a 1080p machine with a working GPU),
  * the pixels themselves (the only witness that a frame was never shown),
    plus the recovery that relaunches through a different path.
"""

import ctypes
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))
import gba_adapter_mgba as adapter
from gba_adapter_mgba import (
    DISPLAY_MAX_RECOVERIES,
    DisplayWatchdog,
    MgbaSessionRunner,
    compositor_running,
    presentation_steps,
    screen_is_flat,
    wants_software_gl,
    x11_framebuffer_sample,
)

BLACK = [(0, 0, 0)] * 64
WHITE = [(255, 255, 255)] * 64


class TestScreenSampling(unittest.TestCase):
    def test_a_uniform_screen_is_flat(self):
        self.assertTrue(screen_is_flat(BLACK))
        self.assertTrue(screen_is_flat(WHITE))

    def test_a_rendered_frame_is_not_flat(self):
        frame = [(0, 0, 0)] * 63 + [(255, 255, 255)]
        self.assertFalse(screen_is_flat(frame))

    def test_near_identical_pixels_count_as_flat(self):
        # Compression/dithering noise must not look like a rendered frame.
        noisy = [(0, 0, 0)] * 63 + [(2, 1, 2)]
        self.assertTrue(screen_is_flat(noisy, tolerance=3))
        self.assertFalse(screen_is_flat(noisy, tolerance=0))

    def test_unreadable_screen_is_not_evidence(self):
        # None/empty means "could not sample"; that must never trigger a
        # restart of a session that may be perfectly healthy.
        self.assertFalse(screen_is_flat(None))
        self.assertFalse(screen_is_flat([]))

    def test_sampling_without_a_display_is_harmless(self):
        self.assertIsNone(x11_framebuffer_sample({"DISPLAY": ""}))
        with mock.patch.dict(os.environ, {"DISPLAY": ":99"}):
            # No X server on :99 in the test environment: fail open, no raise.
            self.assertIsNone(x11_framebuffer_sample({"DISPLAY": ":99"}))


class TestPixelParsing(unittest.TestCase):
    """Exercises the real XImage parsing with a known framebuffer.

    The struct field order and offsets match Xlib's XImage on LP64
    (data @16, byte_order @24, depth @40, bytes_per_line @44,
    bits_per_pixel @48, masks @56/64/72), so this covers the code path that
    runs against a live X server without needing one in CI.
    """

    def fake_display(self, pixels, width, height):
        buffer = ctypes.create_string_buffer(len(pixels) * 4)
        for index, (r, g, b) in enumerate(pixels):
            buffer[index * 4:index * 4 + 4] = bytes((b, g, r, 0))

        image = adapter._XImageStruct()
        image.width, image.height = width, height
        image.format = 2                      # ZPixmap
        image.data = ctypes.cast(buffer, ctypes.c_void_p)
        image.byte_order = 0                  # LSBFirst
        image.depth = 24
        image.bytes_per_line = width * 4
        image.bits_per_pixel = 32
        image.red_mask, image.green_mask, image.blue_mask = 0xff0000, 0xff00, 0xff

        holder = {"image": image, "buffer": buffer}

        class FakeLib:
            def XOpenDisplay(self, _name):
                return 1

            def XCloseDisplay(self, _dpy):
                return 1

            def XDefaultRootWindow(self, _dpy):
                return 1

            def XDisplayWidth(self, _dpy, _screen):
                return width

            def XDisplayHeight(self, _dpy, _screen):
                return height

            def XGetImage(self, *_args):
                return ctypes.pointer(holder["image"])

            def XFree(self, _ptr):
                return 1

        return FakeLib(), holder

    def test_pixels_are_read_with_the_right_channel_order(self):
        lib, _holder = self.fake_display([(255, 0, 0), (0, 255, 0),
                                          (0, 0, 255), (7, 8, 9)], 4, 1)
        with mock.patch.object(adapter, "_x11", return_value=lib):
            samples = x11_framebuffer_sample({"DISPLAY": ":0"}, grid=(4, 1))
        self.assertEqual(samples, [(255, 0, 0), (0, 255, 0), (0, 0, 255), (7, 8, 9)])

    def test_a_black_screen_reads_as_flat(self):
        lib, _holder = self.fake_display([(0, 0, 0)] * 4, 4, 1)
        with mock.patch.object(adapter, "_x11", return_value=lib):
            samples = x11_framebuffer_sample({"DISPLAY": ":0"}, grid=(4, 1))
        self.assertTrue(screen_is_flat(samples))

    def test_a_rendered_screen_reads_as_not_flat(self):
        lib, _holder = self.fake_display([(0, 0, 0), (0, 0, 0),
                                          (0, 0, 0), (200, 40, 10)], 4, 1)
        with mock.patch.object(adapter, "_x11", return_value=lib):
            samples = x11_framebuffer_sample({"DISPLAY": ":0"}, grid=(4, 1))
        self.assertFalse(screen_is_flat(samples))

    def test_a_broken_server_returns_none_instead_of_raising(self):
        class ExplodingLib:
            def XOpenDisplay(self, _name):
                raise RuntimeError("X server gone")

        with mock.patch.object(adapter, "_x11", return_value=ExplodingLib()):
            self.assertIsNone(x11_framebuffer_sample({"DISPLAY": ":0"}))


class TestOpenGLPolicy(unittest.TestCase):
    def test_a_compositor_means_hardware_opengl(self):
        """The compositor redirects fullscreen, so no llvmpipe is needed."""
        self.assertFalse(wants_software_gl({}, compositor="picom"))

    def test_no_compositor_keeps_the_historic_workaround(self):
        self.assertTrue(wants_software_gl({}, compositor=""))

    def test_explicit_overrides_win(self):
        self.assertTrue(wants_software_gl({"GBA_SOFTWARE_GL": "1"},
                                          compositor="picom"))
        self.assertTrue(wants_software_gl({"GBA_HARDWARE_GL": "0"},
                                          compositor="picom"))
        self.assertFalse(wants_software_gl({"GBA_HARDWARE_GL": "1"},
                                           compositor=""))


class TestPresentationSteps(unittest.TestCase):
    def test_first_retry_leaves_llvmpipe_for_the_gpu(self):
        steps = presentation_steps({}, windowed=False, compositor="picom")
        self.assertEqual(steps[0]["label"], "software OpenGL (llvmpipe)")
        self.assertEqual(steps[0]["env"], {"LIBGL_ALWAYS_SOFTWARE": "1"})

    def test_a_compositor_is_dropped_before_falling_back_to_a_window(self):
        labels = [step["label"] for step in
                  presentation_steps({}, windowed=False, compositor="picom")]
        self.assertEqual(labels, ["software OpenGL (llvmpipe)", "no compositor",
                                  "windowed"])

    def test_paths_already_in_use_are_not_offered(self):
        steps = presentation_steps({"LIBGL_ALWAYS_SOFTWARE": "1"},
                                   windowed=True, compositor="")
        self.assertEqual(steps, [])

    def test_windowed_is_not_a_step_when_already_windowed(self):
        labels = [step["label"] for step in
                  presentation_steps({"LIBGL_ALWAYS_SOFTWARE": "1"},
                                     windowed=True, compositor="picom")]
        self.assertEqual(labels, ["no compositor"])


class TestDisplayWatchdog(unittest.TestCase):
    def watchdog(self, sampler, samples=2, alive=lambda: True):
        return DisplayWatchdog({}, alive_check=alive, delay=0,
                               interval=0.01, samples=samples, sampler=sampler)

    def test_a_black_screen_is_reported(self):
        dog = self.watchdog(lambda: BLACK)
        dog.start()
        dog.join(timeout=5)
        self.assertIn("single colour", dog.take_blank_reason())
        self.assertEqual(dog.take_blank_reason(), "")   # reported once

    def test_a_moving_picture_is_never_reported(self):
        frames = iter([BLACK, [(0, 0, 0)] * 63 + [(9, 9, 9)]] * 10)
        dog = self.watchdog(lambda: next(frames, [(0, 0, 0)] * 63 + [(9, 9, 9)]))
        dog.start()
        dog.join(timeout=5)
        self.assertEqual(dog.take_blank_reason(), "")

    def test_an_unsampleable_screen_is_not_reported(self):
        dog = self.watchdog(lambda: None)
        dog.start()
        dog.join(timeout=5)
        self.assertEqual(dog.take_blank_reason(), "")

    def test_the_watchdog_stops_with_the_child(self):
        alive = {"value": False}
        dog = self.watchdog(lambda: BLACK, samples=100, alive=lambda: alive["value"])
        dog.start()
        dog.join(timeout=5)
        self.assertFalse(dog.is_alive())
        self.assertEqual(dog.take_blank_reason(), "")


class TestDisplayRecovery(unittest.TestCase):
    def runner(self):
        runner = MgbaSessionRunner.__new__(MgbaSessionRunner)
        runner.env = {"DISPLAY": ":0"}
        runner.windowed = False
        runner.child = None
        runner.shutting_down = False
        runner.display_recoveries = 0
        runner.display_watchdog = None
        runner.cmd = ["mgba", "game.gba"]
        runner.native = mock.Mock()
        runner.native.capture_runtime_state.return_value = "/tmp/state.ss"
        runner.stop_emulator = mock.Mock(return_value=True)
        runner._spawn = mock.Mock()
        runner.build_command = mock.Mock(return_value=["mgba", "-t", "/tmp/state.ss"])
        runner.next_presentation_step = mock.Mock(return_value={
            "label": "software OpenGL (llvmpipe)", "windowed": False,
            "env": {"LIBGL_ALWAYS_SOFTWARE": "1"}, "stop_compositor": False})
        return runner

    def test_recovery_relaunches_with_the_next_path_and_resumes_the_state(self):
        runner = self.runner()
        runner._recover_display("screen stayed black")
        self.assertEqual(runner.env["LIBGL_ALWAYS_SOFTWARE"], "1")
        runner.build_command.assert_called_once_with(savestate="/tmp/state.ss")
        runner._spawn.assert_called_once_with(["mgba", "-t", "/tmp/state.ss"])
        self.assertEqual(runner.display_recoveries, 1)

    def test_recovery_can_drop_the_compositor(self):
        runner = self.runner()
        runner.next_presentation_step = mock.Mock(return_value={
            "label": "no compositor", "windowed": False, "env": {},
            "stop_compositor": True})
        with mock.patch.object(adapter, "stop_compositors",
                               return_value=["picom"]) as stopped:
            runner._recover_display("screen stayed black")
        stopped.assert_called_once()
        runner._spawn.assert_called_once()

    def test_recovery_is_bounded(self):
        runner = self.runner()
        runner.display_recoveries = DISPLAY_MAX_RECOVERIES
        with self.assertLogs(adapter.logger, level="ERROR") as logs:
            runner._recover_display("screen stayed black")
        runner._spawn.assert_not_called()
        self.assertIn("giving up", "\n".join(logs.output))

    def test_no_further_path_keeps_the_session_running(self):
        runner = self.runner()
        runner.next_presentation_step = mock.Mock(return_value=None)
        with self.assertLogs(adapter.logger, level="ERROR"):
            runner._recover_display("screen stayed black")
        runner.stop_emulator.assert_not_called()
        runner._spawn.assert_not_called()


class TestCompositorDetection(unittest.TestCase):
    def test_detection_uses_procps_and_never_raises(self):
        with mock.patch.object(adapter.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=1)
            self.assertEqual(compositor_running(), "")
        with mock.patch.object(adapter.subprocess, "run") as run:
            run.side_effect = FileNotFoundError("pgrep")
            self.assertEqual(compositor_running(), "")

    def test_proc_fallback_when_procps_is_missing(self):
        """No pgrep must not mean "no compositor" (that would force llvmpipe)."""
        real_open = open

        def fake_open(path, *args, **kwargs):
            if str(path).endswith("/comm"):
                import io
                return io.StringIO("picom\n")
            return real_open(path, *args, **kwargs)

        with mock.patch.object(adapter.subprocess, "run",
                               side_effect=FileNotFoundError("pgrep")), \
                mock.patch("builtins.open", side_effect=fake_open):
            self.assertTrue(adapter._process_running("picom"))

    def test_a_running_compositor_is_detected(self):
        def fake_run(argv, **kwargs):
            return mock.Mock(returncode=0 if argv[:2] == ["pgrep", "-x"] and argv[2] == "picom" else 1)
        with mock.patch.object(adapter.subprocess, "run", side_effect=fake_run):
            self.assertEqual(compositor_running(), "picom")


class SelfTestReporting(unittest.TestCase):
    """`gba-selftest` must report a compositor that runs, not one that exists."""

    def check(self):
        import gba_selftest
        return gba_selftest.check_compositor()

    def test_a_running_compositor_passes(self):
        with mock.patch.object(adapter, "compositor_running", return_value="picom"):
            result = self.check()
        self.assertEqual(result.status, "PASS")
        self.assertIn("picom running", result.detail)

    def test_an_installed_but_dead_compositor_warns(self):
        import gba_selftest
        with mock.patch.object(adapter, "compositor_running", return_value=""), \
                mock.patch.object(gba_selftest.shutil, "which",
                                  side_effect=lambda name: "/usr/bin/picom"
                                  if name == "picom" else None):
            result = self.check()
        self.assertEqual(result.status, "WARN")
        self.assertIn("not running", result.detail)
        self.assertIn("--vsync", result.hint)

    def test_no_compositor_at_all_explains_the_install(self):
        import gba_selftest
        with mock.patch.object(adapter, "compositor_running", return_value=""), \
                mock.patch.object(gba_selftest.shutil, "which", return_value=None):
            result = self.check()
        self.assertEqual(result.status, "WARN")
        self.assertIn("picom", result.hint)


class WatchdogActivation(unittest.TestCase):
    """The frame check only runs where its evidence is trustworthy."""

    def runner(self):
        runner = MgbaSessionRunner.__new__(MgbaSessionRunner)
        runner.env = {"DISPLAY": ":0"}
        runner.child = None
        runner.shutting_down = False
        runner.display_watchdog = None
        runner.next_presentation_step = lambda: {"label": "x"}
        runner.screen_sampler = lambda: BLACK
        return runner

    def test_no_watchdog_without_a_compositor(self):
        runner = self.runner()
        with mock.patch.object(adapter, "compositor_running", return_value=""):
            runner._start_display_watchdog()
        self.assertIsNone(runner.display_watchdog)

    def test_a_compositor_enables_the_check(self):
        runner = self.runner()
        with mock.patch.object(adapter, "compositor_running", return_value="picom"):
            runner._start_display_watchdog()
        self.assertIsNotNone(runner.display_watchdog)
        runner._stop_display_watchdog()

    def test_verification_can_be_switched_off(self):
        runner = self.runner()
        runner.env = {"DISPLAY": ":0", "GBA_VERIFY_DISPLAY": "0"}
        with mock.patch.object(adapter, "compositor_running", return_value="picom"):
            runner._start_display_watchdog()
        self.assertIsNone(runner.display_watchdog)


class SessionScriptTests(unittest.TestCase):
    """src/gba_session.sh must start a compositor that can actually run."""

    def setUp(self):
        from pathlib import Path
        self.text = (Path(__file__).resolve().parents[1] / "src"
                     / "gba_session.sh").read_text(encoding="utf-8")

    def test_no_positional_argument_is_passed_to_picom(self):
        # `picom --vsync none` exits with "picom doesn't accept positional
        # arguments", which silently removed the compositor from every boot.
        self.assertNotIn("--vsync none", self.text)
        for line in self.text.splitlines():
            code = line.split("#", 1)[0]
            if "$COMPOSITOR_BIN" in code:
                self.assertNotIn("--vsync", code)

    def test_the_compositor_runs_with_a_valid_backend(self):
        self.assertIn('--backend "$GBA_COMPOSITOR_BACKEND"', self.text)
        self.assertIn('GBA_COMPOSITOR_BACKEND="${GBA_COMPOSITOR_BACKEND:-xrender}"',
                      self.text)

    def test_compositor_errors_are_not_hidden(self):
        for line in self.text.splitlines():
            if "COMPOSITOR_BIN\" --daemon" in line:
                self.assertNotIn("2>/dev/null", line)

    def test_a_dead_compositor_is_reported_and_not_claimed(self):
        self.assertIn("exited immediately", self.text)
        self.assertIn("pgrep", self.text)

    def test_the_backend_is_exported_to_the_adapter(self):
        self.assertIn("export GBA_COMPOSITOR", self.text)
        self.assertIn("export GBA_COMPOSITOR_BACKEND", self.text)


if __name__ == "__main__":
    unittest.main()
