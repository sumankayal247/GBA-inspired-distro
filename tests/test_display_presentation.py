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
import shutil
import subprocess
import sys
import tempfile
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
        # A displayed session: without $DISPLAY the boot-time check correctly
        # reports "not started yet" and proves nothing either way.
        with mock.patch.dict(os.environ, {"DISPLAY": ":0"}):
            return gba_selftest.check_compositor()

    def test_before_the_x_server_starts_nothing_is_reported_as_broken(self):
        import gba_selftest
        env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
        with mock.patch.dict(os.environ, env, clear=True):
            result = gba_selftest.check_compositor()
        self.assertEqual(result.status, "PASS")
        self.assertIn("no X server at this point", result.detail)

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
        runner.windowed = False
        runner.next_presentation_step = lambda: {"label": "x"}
        runner.screen_sampler = lambda: BLACK
        return runner

    def test_no_watchdog_for_a_fullscreen_window_without_a_compositor(self):
        """Direct scanout makes the root window unreadable: do not judge it."""
        runner = self.runner()
        with mock.patch.object(adapter, "compositor_running", return_value=""):
            runner._start_display_watchdog()
        self.assertIsNone(runner.display_watchdog)

    def test_a_window_is_sampled_even_without_a_compositor(self):
        """The X server copies a window into the framebuffer, so it is readable."""
        runner = self.runner()
        runner.windowed = True
        with mock.patch.object(adapter, "compositor_running", return_value=""):
            runner._start_display_watchdog()
        self.assertIsNotNone(runner.display_watchdog)
        runner._stop_display_watchdog()

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


class CompositorSelfHealing(unittest.TestCase):
    """A stale session script must not be able to break a session.

    /usr/local/bin/gba-session is an installed copy: replacing the adapter
    without re-running the installer leaves the old, broken compositor command
    behind. The adapter therefore starts a compositor itself instead of only
    reporting that none is running.
    """

    def test_an_existing_compositor_is_left_alone(self):
        with mock.patch.object(adapter, "compositor_running", return_value="picom"), \
                mock.patch.object(adapter, "start_compositor") as started:
            self.assertEqual(adapter.ensure_compositor({}), "picom")
        started.assert_not_called()

    def test_a_missing_compositor_is_started_and_the_stale_install_named(self):
        with mock.patch.object(adapter, "compositor_running", return_value=""), \
                mock.patch.object(adapter, "start_compositor",
                                  return_value="picom") as started, \
                self.assertLogs(adapter.logger, level="WARNING") as logs:
            self.assertEqual(adapter.ensure_compositor({}), "picom")
        started.assert_called_once_with("xrender", {})
        joined = "\n".join(logs.output)
        self.assertIn("started picom", joined)
        self.assertIn("install.sh --apply --repair", joined)

    def test_the_requested_backend_is_honoured(self):
        with mock.patch.object(adapter, "compositor_running", return_value=""), \
                mock.patch.object(adapter, "start_compositor",
                                  return_value="compton") as started:
            adapter.ensure_compositor({"GBA_COMPOSITOR_BACKEND": "glx"})
        started.assert_called_once_with("glx", {"GBA_COMPOSITOR_BACKEND": "glx"})

    def test_only_an_explicit_request_disables_compositing(self):
        for env in ({"GBA_COMPOSITOR_BACKEND": "none"},
                    {"GBA_COMPOSITOR_DISABLED": "1"}):
            with mock.patch.object(adapter, "compositor_running", return_value=""), \
                    mock.patch.object(adapter, "start_compositor") as started:
                self.assertEqual(adapter.ensure_compositor(dict(env)), "")
            started.assert_not_called()

    def test_a_script_that_reported_none_is_retried_here(self):
        """The boot log that broke this: `compositing disabled by GBA_COMPOSITOR=none`.

        GBA_COMPOSITOR is the session script's *status* ("picom" or nothing),
        not a request. Reading its legacy `none` as a request skipped the only
        retry that exists for a compositor that failed at boot.
        """
        env = {"GBA_COMPOSITOR": "none"}
        with mock.patch.object(adapter, "compositor_running", return_value=""), \
                mock.patch.object(adapter, "start_compositor",
                                  return_value="picom") as started, \
                self.assertLogs(adapter.logger, level="WARNING") as logs:
            self.assertEqual(adapter.ensure_compositor(env), "picom")
        started.assert_called_once_with(adapter.COMPOSITOR_BACKEND_DEFAULT, env)
        self.assertNotIn("disabled on request", "\n".join(logs.output))

    def test_the_sessions_own_reason_is_carried_into_the_adapter_log(self):
        env = {"GBA_COMPOSITOR": "",
               "GBA_COMPOSITOR_REASON": "no compositor could be started after 3 attempt(s)"}
        with mock.patch.object(adapter, "compositor_running", return_value=""), \
                mock.patch.object(adapter, "start_compositor", return_value="picom"), \
                self.assertLogs(adapter.logger, level="WARNING") as logs:
            adapter.ensure_compositor(env)
        self.assertIn("no compositor could be started after 3 attempt(s)",
                      "\n".join(logs.output))

    def test_an_unstartable_compositor_says_what_to_install(self):
        with mock.patch.object(adapter, "compositor_running", return_value=""), \
                mock.patch.object(adapter, "start_compositor", return_value=""), \
                self.assertLogs(adapter.logger, level="ERROR") as logs:
            self.assertEqual(adapter.ensure_compositor({}), "")
        self.assertIn("apt install picom", "\n".join(logs.output))

    def test_the_adapter_starts_it_before_choosing_the_gl_path(self):
        """Wiring check: detection alone was the gap that left a black screen."""
        import inspect
        source = inspect.getsource(adapter.main)
        self.assertIn("ensure_compositor(env)", source)
        self.assertLess(source.index("ensure_compositor(env)"),
                        source.index("wants_software_gl(env, compositor,"))


class CompositorAttempts(unittest.TestCase):
    """Every way a compositor can be started, and what each failure means."""

    def test_the_configured_backend_is_tried_first_and_the_config_last(self):
        with mock.patch.object(adapter.os.path, "exists", return_value=True):
            attempts = adapter.compositor_attempts("xrender", {})
        self.assertEqual(attempts[0], ("xrender", None))
        self.assertIn(("glx", None), attempts)
        self.assertEqual(attempts[-1], ("xrender", adapter.os.devnull))

    def test_glx_is_not_attempted_without_a_gpu(self):
        with mock.patch.object(adapter.os.path, "exists", return_value=False):
            attempts = adapter.compositor_attempts("xrender", {})
        self.assertNotIn(("glx", None), attempts)

    def test_an_unknown_backend_falls_back_to_the_default(self):
        with mock.patch.object(adapter.os.path, "exists", return_value=True):
            attempts = adapter.compositor_attempts("banana", {})
        self.assertEqual(attempts[0][0], adapter.COMPOSITOR_BACKEND_DEFAULT)

    def test_the_reason_is_the_compositors_own_error_line(self):
        path = os.path.join(tempfile.mkdtemp(), "picom.log")
        self.addCleanup(shutil.rmtree, os.path.dirname(path), ignore_errors=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("info: v11.2\n"
                         "FATAL ERROR: Another composite manager is already running\n")
        self.assertIn("Another composite manager",
                      adapter._compositor_reason(path, "picom"))

    def test_a_silent_exit_still_says_something(self):
        path = os.path.join(tempfile.mkdtemp(), "picom.log")
        self.addCleanup(shutil.rmtree, os.path.dirname(path), ignore_errors=True)
        open(path, "w", encoding="utf-8").close()
        self.assertIn("without leaving a message",
                      adapter._compositor_reason(path, "picom"))


class StartingARealCompositor(unittest.TestCase):
    """start_compositor() against stub binaries, not mocks.

    This is the path that runs on a machine whose session script is older than
    the adapter, so it has to survive the same failures the shell does.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        self.addCleanup(mock.patch.stopall)
        patcher = mock.patch.dict(os.environ, {"PATH": self.bin + ":/usr/bin:/bin"})
        patcher.start()
        self.addCleanup(adapter.stop_compositors)

    def stub(self, body):
        path = os.path.join(self.bin, "picom")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/bash\n" + body)
        os.chmod(path, 0o755)

    def test_a_compositor_that_stays_up_is_returned_with_its_pid(self):
        self.stub("exec sleep 20\n")
        with mock.patch.object(adapter, "compositor_running", return_value=""), \
                self.assertLogs(adapter.logger, level="INFO") as logs:
            self.assertEqual(adapter.start_compositor("xrender"), "picom")
        joined = "\n".join(logs.output)
        self.assertIn("picom started (backend xrender, pid ", joined)

    def test_no_daemon_flag_is_passed_to_the_compositor(self):
        """`--daemon` closes picom's stderr and detaches the PID we verify."""
        self.stub('printf "%%s\\n" "$*" > "%s"\nexec sleep 5\n'
                  % os.path.join(self.dir, "argv"))
        with mock.patch.object(adapter, "compositor_running", return_value=""):
            adapter.start_compositor("xrender")
        with open(os.path.join(self.dir, "argv"), encoding="utf-8") as handle:
            argv = handle.read()
        self.assertNotIn("--daemon", argv)
        self.assertIn("--backend xrender", argv)

    def test_a_broken_config_is_retried_with_the_default_one(self):
        calls = os.path.join(self.dir, "calls")
        self.stub('echo "$*" >> %s\ncase "$*" in *--config*) exec sleep 20 ;; '
                  '*) echo "Failed to load config file" >&2; exit 1 ;; esac\n' % calls)
        with mock.patch.object(adapter, "compositor_running", return_value=""), \
                self.assertLogs(adapter.logger, level="WARNING") as logs:
            self.assertEqual(adapter.start_compositor("xrender"), "picom")
        self.assertIn("Failed to load config file", "\n".join(logs.output))
        with open(calls, encoding="utf-8") as handle:
            attempts = [line.strip() for line in handle if line.strip()]
        # xrender with the user's config, then xrender with the default one:
        # a config error is not a reason to try another backend.
        self.assertEqual(len(attempts), 2, attempts)
        self.assertIn("--config", attempts[1])

    def test_an_unfixable_failure_is_reported_with_the_reason(self):
        self.stub('echo "session_init FATAL ERROR: no screens found" >&2\nexit 1\n')
        with mock.patch.object(adapter, "compositor_running", return_value=""), \
                self.assertLogs(adapter.logger, level="WARNING") as logs:
            self.assertEqual(adapter.start_compositor("xrender"), "")
        self.assertIn("no screens found", "\n".join(logs.output))

    def test_another_compositor_is_kept_instead_of_fought(self):
        self.stub('echo "FATAL ERROR: Another composite manager is already running" >&2\n'
                  'exit 1\n')
        with mock.patch.object(adapter, "compositor_running",
                               return_value="compton"), \
                self.assertLogs(adapter.logger, level="INFO") as logs:
            self.assertEqual(adapter.start_compositor("xrender"), "compton")
        self.assertIn("keeping compton", "\n".join(logs.output))

    def test_a_window_without_a_compositor_needs_no_llvmpipe(self):
        self.assertFalse(adapter.wants_software_gl({}, compositor="", windowed=True))
        self.assertTrue(adapter.wants_software_gl({}, compositor="", windowed=False))

    def test_a_picom_config_file_is_named_when_nothing_starts(self):
        home = self.dir
        conf = os.path.join(home, ".config", "picom", "picom.conf")
        os.makedirs(os.path.dirname(conf), exist_ok=True)
        open(conf, "w", encoding="utf-8").close()
        self.stub('echo "Failed to load config file" >&2\nexit 1\n')
        with mock.patch.dict(os.environ, {"HOME": home, "XDG_CONFIG_HOME": ""}), \
                mock.patch.object(adapter, "compositor_running", return_value=""), \
                self.assertLogs(adapter.logger, level="ERROR") as logs:
            self.assertEqual(adapter.start_compositor("xrender"), "")
            adapter.ensure_compositor({})
        self.assertIn(conf, "\n".join(logs.output))

    def test_the_windowed_fallback_is_wired_into_the_launch_path(self):
        import inspect
        source = inspect.getsource(adapter.main)
        self.assertIn("compositor_window_fallback", source)
        # The fallback must change what mGBA is started with, and the frame
        # check must know about it.
        self.assertIn('presentation = {"windowed": effective_windowed', source)
        self.assertIn("windowed=effective_windowed", source)


class SessionScriptFreshness(unittest.TestCase):
    """`gba-selftest` must catch an installed script older than the build."""

    def setUp(self):
        import gba_selftest
        self.selftest = gba_selftest
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.installed = os.path.join(self.dir, "gba-session")
        self.shipped = os.path.join(os.path.dirname(adapter.__file__),
                                    "gba_session.sh")

    def check(self):
        with mock.patch.dict(os.environ, {"GBA_SESSION_RUNNER": self.installed}):
            return self.selftest.check_session_script()

    def write(self, text):
        with open(self.installed, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_the_old_broken_script_fails_with_the_repair_command(self):
        self.write('#!/bin/bash\npicom --daemon --backend glx --vsync none\n')
        result = self.check()
        self.assertEqual(result.status, "FAIL")
        self.assertIn("--vsync", result.detail)
        self.assertIn("--apply --repair", result.hint)

    def test_a_current_script_passes(self):
        with open(self.shipped, encoding="utf-8") as handle:
            self.write(handle.read())
        self.assertEqual(self.check().status, "PASS")

    def test_a_diverged_script_warns(self):
        self.write("#!/bin/bash\n# something else\n")
        result = self.check()
        self.assertEqual(result.status, "WARN")
        self.assertIn("differs", result.detail)

    def test_an_explicit_reference_copy_wins(self):
        self.write("#!/bin/bash\n# installed\n")
        reference = os.path.join(self.dir, "reference.sh")
        with open(reference, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/bash\n# installed\n")
        with mock.patch.dict(os.environ,
                             {"GBA_SESSION_REFERENCE": reference}):
            result = self.check()
        self.assertEqual(result.status, "PASS")

    def test_without_a_reference_copy_the_command_is_still_judged(self):
        self.write("#!/bin/bash\n# looks current\n")
        with mock.patch.object(self.selftest, "_session_reference_copies",
                               return_value=[]):
            result = self.check()
        self.assertEqual(result.status, "PASS")
        self.assertIn("no reference copy", result.detail)

    def test_the_installed_layout_finds_its_reference_copy(self):
        """$INSTALL_DIR/bin/gba-selftest must see $INSTALL_DIR/src/gba_session.sh."""
        root = os.path.join(self.dir, "opt")
        os.makedirs(os.path.join(root, "bin"))
        os.makedirs(os.path.join(root, "src"))
        reference = os.path.join(root, "src", "gba_session.sh")
        shutil.copyfile(self.shipped, reference)
        fake = os.path.join(root, "bin", "gba-selftest.py")
        with mock.patch.object(self.selftest, "__file__", fake):
            found = self.selftest._session_reference_copies()
        self.assertEqual(found[0], reference)
        # ...and the old script then fails against it.
        self.write('#!/bin/bash\npicom --daemon --backend glx --vsync none\n')
        with mock.patch.dict(os.environ,
                             {"GBA_SESSION_REFERENCE": reference}):
            self.assertEqual(self.check().status, "FAIL")

    def test_a_missing_script_warns(self):
        os.unlink(self.installed) if os.path.exists(self.installed) else None
        result = self.check()
        self.assertEqual(result.status, "WARN")
        self.assertIn("missing", result.detail)


SESSION_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src", "gba_session.sh")


def _extract_compositor_functions() -> str:
    """Pulls the compositor functions out of gba_session.sh, verbatim.

    The boot path is shell, so it is tested as shell: the functions that
    actually run at boot are sourced into a throw-away shell and driven with
    stub binaries. Nothing about the order of `if`s is asserted - only what a
    machine would have logged.
    """
    text = open(SESSION_SCRIPT, encoding="utf-8").read()
    chunks = []
    for name in ("wait_for_x_display", "composite_extension",
                 "compositor_reason", "compositor_attempt",
                 "start_session_compositor"):
        start = text.index(f"{name}() {{")
        end = text.index("\n}\n", start) + 3
        chunks.append(text[start:end])
    return "\n".join(chunks)


class CompositorStartup(unittest.TestCase):
    """Drives src/gba_session.sh's compositor block on stub binaries.

    The reported boot log said "picom exited immediately ... the reason is in
    the log above" and then the session ran with no compositor, which is the
    black screen. Every way that can happen now has a defined outcome, and this
    is where those outcomes are pinned down.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        self.env = {
            "PATH": self.bin + ":/usr/bin:/bin",
            "DISPLAY": ":0",
            "HOME": self.dir,
            "GBA_COMPOSITOR_BACKEND": "xrender",
        }
        self.write("xdpyinfo", '#!/bin/bash\necho "  Composite"\nexit 0\n')
        self.write("pgrep", "#!/bin/bash\nexit 1\n")
        self.functions = os.path.join(self.dir, "functions.sh")
        with open(self.functions, "w", encoding="utf-8") as handle:
            handle.write(_extract_compositor_functions())

    def write(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.chmod(path, 0o755)
        return path

    def run_session(self, **env):
        """Runs the block; returns (log output, compositor name)."""
        full = dict(self.env, **env)
        script = ('. "%s"; start_session_compositor; '
                  'echo "RESULT=$GBA_COMPOSITOR"; '
                  'echo "REASON=$GBA_COMPOSITOR_REASON"; '
                  'echo "DISABLED=$GBA_COMPOSITOR_DISABLED"; '
                  'kill "$COMPOSITOR_PID" 2>/dev/null; exit 0' % self.functions)
        proc = subprocess.run(["bash", "-c", script], env=full, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=60)
        output = proc.stdout
        result = ""
        for line in output.splitlines():
            if line.startswith("RESULT="):
                result = line.split("=", 1)[1].strip()
        return output, result

    def exported(self, output):
        """The variables the session script hands to the adapter."""
        values = {}
        for line in output.splitlines():
            key, sep, value = line.partition("=")
            if sep and key in ("RESULT", "REASON", "DISABLED"):
                values[key] = value
        return values

    # -- the ways it can work -------------------------------------------------

    def test_a_compositor_that_stays_up_is_reported_running(self):
        self.write("picom", "#!/bin/bash\nexec sleep 30\n")
        output, result = self.run_session()
        self.assertEqual(result, "picom")
        self.assertIn("compositor: picom running (backend xrender, pid ", output)

    def test_a_broken_config_is_bypassed_with_the_default_one(self):
        """A config error is not a backend error: do not try the other one."""
        calls = os.path.join(self.dir, "calls")
        self.write("picom", """#!/bin/bash
echo "$*" >> %s
case "$*" in
  *--config*) exec sleep 30 ;;
  *) echo "Failed to load config file picom.conf: syntax error" >&2; exit 1 ;;
esac
""" % calls)
        output, result = self.run_session()
        self.assertEqual(result, "picom")
        self.assertIn("syntax error", output)
        self.assertIn("running (backend xrender", output)
        with open(calls, encoding="utf-8") as handle:
            attempts = [line.strip() for line in handle if line.strip()]
        self.assertEqual(len(attempts), 2, attempts)
        self.assertIn("--config /dev/null", attempts[1])
        self.assertNotIn("glx", attempts[1])

    def test_the_other_backend_is_tried(self):
        self.write("picom", """#!/bin/bash
case "$*" in
  *glx*) exec sleep 30 ;;
  *) echo "xrender backend unavailable" >&2; exit 1 ;;
esac
""")
        output, result = self.run_session()
        self.assertEqual(result, "picom")
        self.assertIn("running (backend glx", output)

    def test_the_x_server_is_waited_for(self):
        counter = os.path.join(self.dir, "xdpyinfo.calls")
        self.write("xdpyinfo", """#!/bin/bash
count=0
[ -f "%s" ] && count=$(cat "%s")
count=$((count + 1))
echo "$count" > "%s"
if [ "$count" -lt 3 ]; then exit 1; fi
echo "  Composite"
""" % (counter, counter, counter))
        self.write("picom", "#!/bin/bash\nsleep 30\n")
        output, result = self.run_session()
        self.assertEqual(result, "picom")
        self.assertNotIn("did not accept a connection", output)

    def test_an_existing_compositor_is_reused_not_duplicated(self):
        marker = os.path.join(self.dir, "started")
        self.write("pgrep", "#!/bin/bash\nexit 0\n")
        self.write("picom", "#!/bin/bash\necho started > %s\nexec sleep 30\n" % marker)
        output, result = self.run_session()
        self.assertEqual(result, "picom")
        self.assertIn("already running (reusing it)", output)
        self.assertFalse(os.path.exists(marker),
                         "a second compositor was started over a live one")

    # -- the ways it can fail, each with a defined outcome --------------------

    def test_a_failure_repeats_the_compositors_own_words(self):
        self.write("picom", '#!/bin/bash\n'
                            'echo "session_init FATAL ERROR: Failed to create GLX context" >&2\n'
                            'exit 1\n')
        output, result = self.run_session()
        self.assertEqual(result, "")
        self.assertIn("Failed to create GLX context", output)
        self.assertIn("no compositor could be started",
                      self.exported(output)["REASON"])

    def test_giving_up_says_what_happens_instead(self):
        self.write("picom", '#!/bin/bash\necho "cannot initialise anything" >&2\nexit 1\n')
        output, result = self.run_session()
        self.assertEqual(result, "")
        self.assertIn("attempt(s); continuing without one", output)
        self.assertIn("starts such a session windowed instead", output)

    def test_another_compositor_is_not_retried_three_times(self):
        self.write("picom", '#!/bin/bash\n'
                            'echo "FATAL ERROR: Another composite manager is already running" >&2\n'
                            'exit 1\n')
        output, result = self.run_session()
        self.assertEqual(result, "")
        self.assertEqual(output.count("exited with backend"), 1)
        self.assertIn("another compositor already owns the screen",
                      self.exported(output)["REASON"])

    def test_a_missing_composite_extension_is_named_before_starting_anything(self):
        marker = os.path.join(self.dir, "started")
        self.write("xdpyinfo", '#!/bin/bash\necho "  RANDR"\nexit 0\n')
        self.write("picom", "#!/bin/bash\necho started > %s\nsleep 30\n" % marker)
        output, result = self.run_session()
        self.assertEqual(result, "")
        self.assertIn("the X server has no Composite extension",
                      self.exported(output)["REASON"])
        self.assertFalse(os.path.exists(marker))

    def test_compositing_can_be_switched_off(self):
        output, result = self.run_session(GBA_COMPOSITOR_BACKEND="none")
        self.assertEqual(result, "")
        self.assertIn("disabled (GBA_COMPOSITOR_DISABLED=1)", output)
        self.assertIn("DISABLED=1", output)
        self.assertIn("REASON=disabled on request", output)

    def test_a_caller_can_still_ask_for_no_compositing(self):
        output, result = self.run_session(GBA_COMPOSITOR="none")
        self.assertEqual(result, "")
        self.assertIn("DISABLED=1", output)

    def test_the_adapter_retries_what_the_session_could_not_start(self):
        """End-to-end across the boundary: script failure -> adapter retry.

        This is the machine from boot_2026-11-09_13-45-43.log: the session
        could not start picom, exported its status, and the adapter has to try
        again instead of reading that status as "the user said no".
        """
        self.write("picom", '#!/bin/bash\necho "Failed to create GLX context" >&2\nexit 1\n')
        output, result = self.run_session()
        exported = self.exported(output)
        self.assertEqual(result, "")
        self.assertEqual(exported["DISABLED"], "")
        env = {"GBA_COMPOSITOR": exported["RESULT"],
               "GBA_COMPOSITOR_REASON": exported["REASON"],
               "GBA_COMPOSITOR_DISABLED": exported["DISABLED"]}
        with mock.patch.object(adapter, "compositor_running", return_value=""), \
                mock.patch.object(adapter, "start_compositor",
                                  return_value="picom") as started, \
                self.assertLogs(adapter.logger, level="WARNING") as logs:
            self.assertEqual(adapter.ensure_compositor(env), "picom")
        started.assert_called_once()
        self.assertIn("no compositor could be started", "\n".join(logs.output))

    def test_no_daemon_flag_reaches_the_compositor(self):
        """`--daemon` closes picom's stderr and detaches the PID we verify."""
        with open(SESSION_SCRIPT, encoding="utf-8") as handle:
            code_lines = [line for line in handle
                          if line.lstrip().startswith(("\"$COMPOSITOR_BIN\"", "$COMPOSITOR_BIN"))]
        self.assertTrue(code_lines, "compositor invocation not found")
        for line in code_lines:
            self.assertNotIn("--daemon", line)


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
        self.assertIn('--backend "$backend"', self.text)
        self.assertIn('GBA_COMPOSITOR_BACKEND="${GBA_COMPOSITOR_BACKEND:-xrender}"',
                      self.text)
        # The configured backend is tried first, then the other one.
        self.assertIn('COMPOSITOR_ATTEMPTS=("$GBA_COMPOSITOR_BACKEND|"', self.text)

    def test_compositor_errors_are_not_hidden(self):
        # Its own output goes to a file that is read back when it dies; the
        # old one-liner sent stderr to /dev/null and claimed success.
        for line in self.text.splitlines():
            if line.lstrip().startswith(("\"$COMPOSITOR_BIN\"", "$COMPOSITOR_BIN")):
                self.assertNotIn("2>/dev/null", line)
        self.assertIn('>"$log" 2>&1', self.text)
        self.assertIn('COMPOSITOR_REASON="$(compositor_reason "$log")"', self.text)

    def test_a_dead_compositor_is_reported_and_not_claimed(self):
        self.assertIn("exited with backend", self.text)
        self.assertIn("pgrep", self.text)
        self.assertIn("kill -0 \"$COMPOSITOR_PID\"", self.text)

    def test_the_backend_is_exported_to_the_adapter(self):
        self.assertIn("export GBA_COMPOSITOR", self.text)
        self.assertIn("export GBA_COMPOSITOR_BACKEND", self.text)


if __name__ == "__main__":
    unittest.main()
