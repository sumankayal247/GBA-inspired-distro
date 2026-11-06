#!/usr/bin/env python3
"""
Tests for the emulator diagnostics path (docs/MGBA_APPLIANCE.md).

The "game opened to a black screen" report cannot be reproduced headlessly, but
everything the adapter does to make it *legible* can be: the `-l` log mask in
mGBA's argv, the X11 window-mapping sample, the DRM connector fallback, the
load-stage evidence and the merged stderr/stdout pump. `xwininfo`/`xrandr` are
faked with scripts on PATH and `/sys/class/drm` with a temporary directory, so
the assertions are about parsing and verdicts rather than about this machine.
"""

import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import gba_adapter_mgba as adapter  # noqa: E402
from gba_adapter_mgba import (  # noqa: E402
    MgbaSessionRunner, MGBA_LOG_MASK_ALL, MGBA_LOG_MASK_DEFAULT,
    build_mgba_command, describe_log_mask, diagnostics_enabled,
    log_display_diagnostics, log_load_diagnostics, mgba_log_level,
    sync_directory,
)

XRANDR_1080P = """Screen 0: minimum 320 x 200, current 1920 x 1080, maximum 16384 x 16384
HDMI-1 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 527mm x 296mm
   1920x1080     60.00*+  59.94    50.00
   1280x720      60.00    59.94
DP-1 disconnected (normal left inverted right x axis y axis)
"""

TREE_WITH_MGBA = """xwininfo: Window id: 0x1e7 (the root window) (has no defined name)

  Root window id: 0x1e7 (the root window) (has no defined name)
  Parent window id: 0x0 (none)
     2 children:
     0x2c00002 "mGBA": ("SDL_app" "SDL_app")  1920x1080+0+0  +0+0
        0 children:
     0x2c00003 "panel": ("xfdesktop" "Xfdesktop")  1920x1080+0+0  +0+0
        0 children:
"""

TREE_WITHOUT_MGBA = """xwininfo: Window id: 0x1e7 (the root window) (has no defined name)

  Root window id: 0x1e7 (the root window) (has no defined name)
  Parent window id: 0x0 (none)
     1 children:
     0x2c00003 "panel": ("xfdesktop" "Xfdesktop")  1920x1080+0+0  +0+0
        0 children:
"""

# A GBA cartridge title is 12 bytes at 0xA0; mGBA's own title parser and the
# library's are both lenient, and the log only has to prove it was read.
ROM_TITLE = "METROIDFUSIO"


def _detail(width, height, x, y, map_state="IsViewable"):
    return f"""xwininfo: Window id: 0x2c00002 "mGBA"

  Absolute upper-left X:  {x}
  Absolute upper-left Y:  {y}
  Relative upper-left X:  {x}
  Relative upper-left Y:  {y}
  Width: {width}
  Height: {height}
  Depth: 24
  Border width: 0
  Map State: {map_state}
  Override redirection state: yes
"""


def _write_tool(directory, name, script):
    """Installs a fake diagnostic tool on PATH."""
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(script)
    mode = os.stat(path).st_mode
    os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class _LogCapture(logging.Handler):
    """Collects formatted records from the adapter logger."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append((record.levelname, record.getMessage()))

    @property
    def text(self):
        return "\n".join(f"{level}: {message}" for level, message in self.records)

    def levels(self):
        return {level for level, _ in self.records}


class AdapterLogTestCase(unittest.TestCase):
    """Captures every adapter log record for the duration of a test."""

    def capture(self):
        handler = _LogCapture()
        log = logging.getLogger("gba.adapter")
        previous_level = log.level
        log.addHandler(handler)
        log.setLevel(logging.DEBUG)
        self.addCleanup(log.removeHandler, handler)
        self.addCleanup(log.setLevel, previous_level)
        return handler


class DiagnosticsSwitchTests(unittest.TestCase):
    """GBA_DEBUG is the single switch for the whole diagnostic path."""

    def test_enabled_by_default(self):
        saved = os.environ.pop("GBA_DEBUG", None)
        try:
            self.assertTrue(diagnostics_enabled())
            self.assertEqual(mgba_log_level(), MGBA_LOG_MASK_DEFAULT)
        finally:
            if saved is not None:
                os.environ["GBA_DEBUG"] = saved

    def test_explicit_off_silences_everything(self):
        for value in ("0", "false", "no", "off", ""):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {"GBA_DEBUG": value}):
                    self.assertFalse(diagnostics_enabled())
                    self.assertIsNone(mgba_log_level())

    def test_explicit_on(self):
        with mock.patch.dict(os.environ, {"GBA_DEBUG": "1"}):
            self.assertTrue(diagnostics_enabled())
            self.assertEqual(mgba_log_level(), MGBA_LOG_MASK_DEFAULT)

    def test_mask_override_is_parsed_and_clamped(self):
        cases = {"16": 16, "0x7f": MGBA_LOG_MASK_ALL, "127": MGBA_LOG_MASK_ALL,
                 "9999": MGBA_LOG_MASK_ALL, "-5": 0}
        for value, expected in cases.items():
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {"GBA_MGBA_LOG_LEVEL": value}):
                    self.assertEqual(mgba_log_level(), expected)

    def test_garbage_mask_falls_back_instead_of_breaking_argv(self):
        """A typo must never reach getopt_long and abort the whole command."""
        with mock.patch.dict(os.environ, {"GBA_MGBA_LOG_LEVEL": "everything"}):
            self.assertEqual(mgba_log_level(), MGBA_LOG_MASK_DEFAULT)

    def test_describe_log_mask_names_the_bits(self):
        self.assertEqual(describe_log_mask(31), "FATAL|ERROR|WARN|INFO|DEBUG")
        self.assertEqual(describe_log_mask(7), "FATAL|ERROR|WARN")
        self.assertEqual(describe_log_mask(0), "(silent)")


class LogLevelArgvTests(unittest.TestCase):
    def setUp(self):
        self.save_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.save_dir, ignore_errors=True)

    def test_log_level_adds_l_flag_and_keeps_rom_last(self):
        cmd = build_mgba_command("/usr/bin/mgba", "/games/x.gba", self.save_dir,
                                 False, "integer", (1920, 1080), log_level=31)
        self.assertIn("-l", cmd)
        self.assertEqual(cmd[cmd.index("-l") + 1], "31")
        self.assertEqual(cmd[-1], "/games/x.gba")

    def test_without_log_level_the_argv_is_unchanged(self):
        """SV1/D2 default: no diagnostics means the original command line."""
        cmd = build_mgba_command("/usr/bin/mgba", "/games/x.gba", self.save_dir,
                                 False, "integer", (1920, 1080))
        self.assertNotIn("-l", cmd)
        self.assertNotIn("-1", cmd)

    def test_log_level_survives_every_display_mode(self):
        for kwargs in ({"windowed": True}, {"windowed": False},
                       {"bare_x11": True}):
            with self.subTest(**kwargs):
                cmd = build_mgba_command(
                    "mgba", "/game.gba", self.save_dir,
                    kwargs.get("windowed", False), "fit", (1366, 768),
                    bare_x11=kwargs.get("bare_x11", False), log_level=31)
                self.assertIn("-l", cmd)
                self.assertEqual(cmd[-1], "/game.gba")


class FakeX11Mixin:
    """Runs the diagnostics against scripted xwininfo/xrandr output."""

    def make_tools(self, tree, detail=None, xrandr=XRANDR_1080P):
        self.bin_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.bin_dir, ignore_errors=True)
        _write_tool(self.bin_dir, "xrandr",
                    "#!/bin/sh\nprintf '%s' \"$XRANDR_OUT\"\n")
        _write_tool(self.bin_dir, "xwininfo",
                    "#!/bin/sh\nif [ \"$1\" = \"-id\" ]; then\n"
                    "  printf '%s' \"$XWININFO_DETAIL\"\n"
                    "else\n  printf '%s' \"$XWININFO_TREE\"\nfi\n")
        env = dict(os.environ)
        env["PATH"] = self.bin_dir + os.pathsep + env.get("PATH", "")
        env["DISPLAY"] = ":0"
        env["XRANDR_OUT"] = xrandr
        env["XWININFO_TREE"] = tree
        env["XWININFO_DETAIL"] = _detail(1920, 1080, 0, 0) if detail is None else detail
        return env


class DisplayDiagnosticsTests(FakeX11Mixin, AdapterLogTestCase):
    def sample(self, tree, detail=None):
        env = self.make_tools(tree, detail)
        logs = self.capture()
        with mock.patch.dict(os.environ, env):
            log_display_diagnostics(env, settle=0)
        return logs

    def test_mapped_screen_filling_window_is_reported_as_healthy(self):
        logs = self.sample(TREE_WITH_MGBA, _detail(1920, 1080, 0, 0, "IsViewable"))
        self.assertIn("map-state=IsViewable", logs.text)
        self.assertIn("fills the active 1920x1080 mode", logs.text)
        self.assertIn("active mode: 1920x1080", logs.text)
        self.assertIn("HDMI-1 connected", logs.text)
        self.assertIn("0x2c00002", logs.text)
        self.assertIn("2 window(s), 1 look like the emulator", logs.text)
        # xwininfo spells this field "Override redirection state"; the parser
        # lower-cases keys so the value survives that inconsistency.
        self.assertIn("override-redirect=yes", logs.text)
        self.assertNotIn("WARNING", logs.levels())

    def test_xwininfo_field_spelling_variants_are_both_accepted(self):
        """xwininfo is inconsistent with itself; the evidence must survive it."""
        for spelling in ("Override redirection state", "Override Redirect State",
                         "override REDIRECTION state"):
            with self.subTest(spelling=spelling):
                detail = _detail(1920, 1080, 0, 0).replace(
                    "Override redirection state:", f"{spelling}:")
                logs = self.sample(TREE_WITH_MGBA, detail)
                self.assertIn("override-redirect=yes", logs.text)
                self.assertIn("map-state=IsViewable", logs.text)

    def test_unmapped_window_is_called_out(self):
        logs = self.sample(TREE_WITH_MGBA, _detail(1920, 1080, 0, 0, "IsUnmapped"))
        self.assertIn("map-state=IsUnmapped", logs.text)
        self.assertIn("is not on screen", logs.text)
        self.assertIn("WARNING", logs.levels())

    def test_window_entirely_off_screen_is_called_out(self):
        logs = self.sample(TREE_WITH_MGBA, _detail(1920, 1080, 1920, 0))
        self.assertIn("entirely outside", logs.text)
        self.assertIn("WARNING", logs.levels())

    def test_window_smaller_than_the_screen_is_reported(self):
        logs = self.sample(TREE_WITH_MGBA, _detail(960, 540, 0, 0))
        self.assertIn("does not fill the display", logs.text)
        self.assertNotIn("entirely outside", logs.text)
        self.assertNotIn("WARNING", logs.levels())

    def test_small_wm_frame_offset_is_not_reported_as_off_screen(self):
        """A window manager's few pixels of frame must not read as a fault."""
        logs = self.sample(TREE_WITH_MGBA, _detail(1920, 1080, -4, -4))
        self.assertNotIn("entirely outside", logs.text)
        self.assertNotIn("overflows", logs.text)
        self.assertIn("fills the active 1920x1080 mode", logs.text)
        self.assertNotIn("WARNING", logs.levels())

    def test_a_larger_offset_is_reported_as_clipped(self):
        logs = self.sample(TREE_WITH_MGBA, _detail(1920, 1080, -40, 0))
        self.assertIn("overflows the 1920x1080 mode", logs.text)
        self.assertNotIn("entirely outside", logs.text)

    def test_missing_emulator_window_is_the_loud_case(self):
        logs = self.sample(TREE_WITHOUT_MGBA)
        self.assertIn("NO emulator window in the X11 tree", logs.text)
        self.assertIn("0 look like the emulator", logs.text)
        self.assertIn("WARNING", logs.levels())

    def test_sdl_wm_class_alone_identifies_the_window(self):
        """A renamed build still matches on SDL's WM_CLASS."""
        logs = self.sample(TREE_WITH_MGBA.replace('"mGBA"', '"Some Fork"'))
        self.assertIn("1 look like the emulator", logs.text)
        self.assertIn("map-state=IsViewable", logs.text)

    def test_absent_tools_are_reported_not_fatal(self):
        """No x11-utils on the appliance must not stop a game from starting."""
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        env = {"DISPLAY": ":0", "PATH": empty, "HOME": os.environ.get("HOME", "/tmp")}
        logs = self.capture()
        with mock.patch.dict(os.environ, env, clear=True):
            log_display_diagnostics(env, settle=0)
        self.assertIn("xrandr is not installed", logs.text)
        self.assertIn("xwininfo is not installed", logs.text)
        self.assertIn("package: x11-utils", logs.text)

    def test_a_failing_tool_is_not_reported_as_a_missing_window(self):
        """'xwininfo failed' and 'no emulator window' are opposite diagnoses."""
        env = self.make_tools(TREE_WITH_MGBA)
        _write_tool(self.bin_dir, "xwininfo",
                    "#!/bin/sh\necho 'xwininfo: error: unable to open display' >&2\nexit 1\n")
        logs = self.capture()
        with mock.patch.dict(os.environ, env):
            log_display_diagnostics(env, settle=0)
        self.assertIn("exited with status 1", logs.text)
        self.assertIn("unable to open display", logs.text)
        self.assertIn("window tree unavailable", logs.text)
        self.assertNotIn("NO emulator window", logs.text)

    def test_silenced_by_gba_debug_zero(self):
        env = self.make_tools(TREE_WITH_MGBA)
        env["GBA_DEBUG"] = "0"
        logs = self.capture()
        with mock.patch.dict(os.environ, env):
            log_display_diagnostics(env, settle=0)
        self.assertEqual([], logs.records)

    def test_a_hanging_tool_cannot_hang_the_session(self):
        env = self.make_tools(TREE_WITH_MGBA)
        _write_tool(self.bin_dir, "xrandr", "#!/bin/sh\nsleep 30\n")
        logs = self.capture()
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(adapter, "DIAG_TOOL_TIMEOUT", 0.4):
            log_display_diagnostics(env, settle=0)
        self.assertIn("did not answer", logs.text)

    def test_no_display_falls_back_to_connector_evidence(self):
        env = self.make_tools(TREE_WITH_MGBA)
        env.pop("DISPLAY", None)
        drm = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, drm, ignore_errors=True)
        base = os.path.join(drm, "card0-HDMI-A-1")
        os.makedirs(base, exist_ok=True)
        for field, value in (("status", "connected"), ("modes", "1920x1080\n"),
                             ("enabled", "enabled"), ("dpms", "On")):
            with open(os.path.join(base, field), "w", encoding="utf-8") as f:
                f.write(value)
        logs = self.capture()
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(adapter, "_log_drm_connectors",
                                  wraps=adapter._log_drm_connectors) as drm_log:
            log_display_diagnostics(env, settle=0)
        self.assertTrue(drm_log.called, "the KMS path must sample DRM connectors")
        self.assertIn("[drm]", logs.text)
        # There is no window tree without X, so the X11 tools must not be run.
        self.assertNotIn("[x11]", logs.text)


class DrmDiagnosticsTests(AdapterLogTestCase):
    """The console/KMS path has no window tree, so connectors are the evidence."""

    def setUp(self):
        self.drm = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.drm, ignore_errors=True)

    def make_connector(self, name, status, modes="1920x1080\n1280x720\n",
                       enabled="enabled", dpms="On"):
        base = os.path.join(self.drm, name)
        os.makedirs(base, exist_ok=True)
        for field, value in (("status", status), ("modes", modes),
                             ("enabled", enabled), ("dpms", dpms)):
            with open(os.path.join(base, field), "w", encoding="utf-8") as f:
                f.write(value)

    def log_for(self, env):
        logs = self.capture()
        adapter._log_drm_connectors(dict(env), drm_root=self.drm)
        return logs

    def test_connected_connector_is_reported_with_its_preferred_mode(self):
        self.make_connector("card0-HDMI-A-1", "connected")
        self.make_connector("card0-DP-1", "disconnected")
        env = {"SDL_VIDEODRIVER": "kmsdrm", "SDL_VIDEO_KMSDRM_DEVICE": "/dev/dri/card0"}
        logs = self.log_for(env)
        self.assertIn("card0-HDMI-A-1: connected", logs.text)
        self.assertIn("preferred=1920x1080", logs.text)
        self.assertIn("2 mode(s)", logs.text)
        self.assertIn("card0-DP-1: not connected", logs.text)
        self.assertIn("connected-connector(s)=1", logs.text)
        self.assertNotIn("WARNING", logs.levels())

    def test_a_card_with_no_output_is_named_as_the_suspect(self):
        self.make_connector("card1-HDMI-A-1", "disconnected")
        env = {"SDL_VIDEODRIVER": "kmsdrm", "SDL_VIDEO_KMSDRM_DEVICE": "/dev/dri/card1"}
        logs = self.log_for(env)
        self.assertIn("no connected connector on card1", logs.text)
        self.assertIn("black screen", logs.text)
        self.assertIn("WARNING", logs.levels())

    def test_only_the_pinned_card_is_considered(self):
        self.make_connector("card0-HDMI-A-1", "connected")
        self.make_connector("card1-HDMI-A-1", "disconnected")
        env = {"SDL_VIDEODRIVER": "kmsdrm", "SDL_VIDEO_KMSDRM_DEVICE": "/dev/dri/card0"}
        logs = self.log_for(env)
        self.assertNotIn("card1-HDMI-A-1", logs.text)

    def test_an_unreadable_sysfs_is_reported_not_fatal(self):
        logs = self.capture()
        adapter._log_drm_connectors({"SDL_VIDEODRIVER": "kmsdrm"},
                                    drm_root=os.path.join(self.drm, "absent"))
        self.assertIn("is unreadable", logs.text)


class LoadDiagnosticsTests(AdapterLogTestCase):
    """Stage 1: ROM header, save routing and BIOS, before the emulator starts."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.save_dir = os.path.join(self.root, "saves")
        os.makedirs(self.save_dir, exist_ok=True)
        self.rom = os.path.join(self.root, "Metroid Fusion (USA).gba")
        with open(self.rom, "wb") as f:
            f.write(b"\x00" * 0xA0)
            f.write(ROM_TITLE.encode("ascii").ljust(12, b"\x00"))
            f.write(b"\x00" * 4096)
        self.env = {k: v for k, v in os.environ.items() if "BIOS" not in k}

    def test_rom_save_and_bios_are_all_reported(self):
        with open(os.path.join(self.save_dir, "Metroid Fusion (USA).sav"), "wb") as f:
            f.write(b"\x01" * 131072)
        logs = self.capture()
        with mock.patch.dict(os.environ, self.env, clear=True):
            log_load_diagnostics(self.rom, self.save_dir,
                                 "/data/config/handlers/mgba-adapter")
        self.assertIn("[load] ROM:", logs.text)
        self.assertIn("4,268 bytes", logs.text)
        self.assertIn("title=", logs.text)
        self.assertIn("131,072 bytes", logs.text)
        self.assertIn("built-in HLE BIOS", logs.text)
        self.assertIn("mgba/config.ini", logs.text)

    def test_the_cartridge_title_is_parsed_not_just_the_filename(self):
        logs = self.capture()
        with mock.patch.dict(os.environ, self.env, clear=True):
            log_load_diagnostics(self.rom, self.save_dir)
        # extract_gba_title() title-cases an all-caps header title.
        self.assertIn(ROM_TITLE.title(), logs.text)

    def test_a_missing_save_is_expected_not_an_error(self):
        logs = self.capture()
        with mock.patch.dict(os.environ, self.env, clear=True):
            log_load_diagnostics(self.rom, self.save_dir)
        self.assertIn("does not exist yet", logs.text)
        self.assertNotIn("WARNING", logs.levels())

    def test_a_configured_but_absent_bios_is_a_warning(self):
        env = dict(self.env, GBA_BIOS_PATH="/data/bios/gba_bios.bin")
        logs = self.capture()
        with mock.patch.dict(os.environ, env, clear=True):
            log_load_diagnostics(self.rom, self.save_dir)
        self.assertIn("does not exist", logs.text)
        self.assertIn("WARNING", logs.levels())


class FsyncReportingTests(AdapterLogTestCase):
    def setUp(self):
        self.save_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.save_dir, ignore_errors=True)

    def test_sync_reports_how_many_files_reached_the_disk(self):
        for name in ("game.sav", "game.sav.bak"):
            with open(os.path.join(self.save_dir, name), "wb") as f:
                f.write(b"DATA")
        logs = self.capture()
        self.assertEqual(sync_directory(self.save_dir), 2)
        self.assertIn("Save fsync verified: 2 file(s)", logs.text)

    def test_sync_of_a_missing_directory_is_quiet_and_safe(self):
        self.assertEqual(sync_directory(os.path.join(self.save_dir, "nope")), 0)


class SpawnDiagnosticsTests(AdapterLogTestCase):
    """The window sample runs off the main thread, once per spawn."""

    def setUp(self):
        self.calls = []
        self.event = threading.Event()

    def record(self, env, settle=0, alive_check=None):
        self.calls.append((dict(env), alive_check() if alive_check else None))
        self.event.set()

    def make_runner(self, cmd=None):
        cmd = cmd or [sys.executable, "-c", "import time; time.sleep(5)"]
        return MgbaSessionRunner(cmd, dict(os.environ), stop_timeout=0.2)

    def test_display_sample_runs_after_spawn(self):
        runner = self.make_runner()
        try:
            with mock.patch.object(adapter, "log_display_diagnostics", self.record):
                runner._spawn(runner.cmd)
                self.assertTrue(self.event.wait(5.0), "display diagnostics never ran")
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(runner.spawn_count, 1)
            self.assertTrue(self.calls[0][1], "the child should be alive when sampled")
        finally:
            runner.stop_emulator(timeout=0.2)

    def test_a_restart_records_its_argv_but_the_first_spawn_does_not(self):
        """main() already logs `Executing mGBA:`; a handoff restart does not."""
        runner = self.make_runner([sys.executable, "-c", "pass"])
        logs = self.capture()
        children = []
        try:
            with mock.patch.object(adapter, "log_display_diagnostics", lambda *a, **k: None):
                runner._spawn(runner.cmd)
                children.append(runner.child)
                runner._spawn(runner.cmd)
                children.append(runner.child)
        finally:
            runner.stop_emulator(timeout=0.2)
            # A real restart follows a stop that already reaped the child; here
            # both are reaped explicitly so no Popen is left unreaped.
            for child in children:
                if child is not None:
                    try:
                        child.wait(timeout=5)
                    except Exception:
                        child.kill()
        spawned = [message for _, message in logs.records
                   if message.startswith("Spawned mGBA")]
        self.assertEqual(len(spawned), 2)
        self.assertEqual(runner.spawn_count, 2)
        self.assertNotIn("spawn #1):", spawned[0])
        self.assertIn("spawn #2): " + sys.executable, spawned[1])

    def test_gba_debug_zero_skips_the_sample(self):
        runner = self.make_runner()
        try:
            with mock.patch.dict(os.environ, {"GBA_DEBUG": "0"}), \
                    mock.patch.object(adapter, "log_display_diagnostics", self.record):
                runner._spawn(runner.cmd)
                self.assertFalse(self.event.wait(0.5))
            self.assertEqual(self.calls, [])
            self.assertIsNotNone(runner.child)
        finally:
            runner.stop_emulator(timeout=0.2)

    def test_stderr_is_merged_into_the_logged_stream(self):
        """mGBA logs to stdout while SDL2/Mesa use stderr: both must be kept."""
        source = ("import sys;"
                  "sys.stdout.write('ROM loaded\\n'); sys.stdout.flush();"
                  "sys.stderr.write('Mesa: software rasterizer\\n'); sys.stderr.flush()")
        runner = self.make_runner([sys.executable, "-c", source])
        logs = self.capture()
        try:
            runner._spawn(runner.cmd)
            pump = None
            for _ in range(50):
                pump = next((t for t in threading.enumerate()
                             if t.name == "gba-mgba-log"), None)
                if pump is not None:
                    break
            self.assertIsNotNone(pump, "the log pump thread never started")
            pump.join(timeout=5.0)
            self.assertFalse(pump.is_alive(), "log pump did not finish")
        finally:
            runner.stop_emulator(timeout=0.2)
        self.assertIn("[mgba] ROM loaded", logs.text)
        self.assertIn("[mgba] Mesa: software rasterizer", logs.text)
        self.assertIn("output stream closed after 2 line(s)", logs.text)

    def test_stop_sequence_logs_every_escalation_step(self):
        """Closing must show which signals were tried, in order, and the status.

        The child ignores SIGTERM and SIGHUP, so the whole ladder runs; the
        ready file is the same handshake tests/test_adapter_shutdown.py uses,
        because signalling before the handlers exist proves nothing.
        """
        with tempfile.TemporaryDirectory() as directory:
            ready = os.path.join(directory, "ready")
            source = ("import signal, time, pathlib\n"
                      "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                      "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
                      f"pathlib.Path({ready!r}).touch()\n"
                      "while True: time.sleep(0.05)\n")
            runner = self.make_runner([sys.executable, "-c", source])
            logs = self.capture()
            try:
                runner._spawn(runner.cmd)
                deadline = time.monotonic() + 5.0
                while not os.path.exists(ready) and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(os.path.exists(ready),
                                "child never installed its signal handlers")
                self.assertTrue(runner.stop_emulator(timeout=0.3))
            finally:
                if runner.child is not None and runner.child.poll() is None:
                    runner.child.kill()
                    runner.child.wait(timeout=2)
        for step in ("SIGCONT", "SIGTERM", "SIGHUP", "SIGKILL"):
            self.assertIn(f"Stop sequence: {step}", logs.text)
        self.assertIn("did not exit after SIGTERM", logs.text)
        self.assertIn("did not exit after SIGHUP", logs.text)
        self.assertIn("reaped with status", logs.text)

    def test_a_clean_stop_reports_the_signal_that_worked(self):
        runner = self.make_runner()
        logs = self.capture()
        try:
            runner._spawn(runner.cmd)
            self.assertTrue(runner.stop_emulator(timeout=1.0))
        finally:
            if runner.child is not None and runner.child.poll() is None:
                runner.child.kill()
                runner.child.wait(timeout=2)
        self.assertIn("Stop sequence: SIGTERM", logs.text)
        self.assertIn("exited after SIGTERM", logs.text)
        self.assertNotIn("SIGKILL", logs.text)
        self.assertIn("reaped with status -15", logs.text)


class SessionWrapperTests(unittest.TestCase):
    """src/gba_session.sh must export the subsystem debug variables."""

    def setUp(self):
        self.path = Path(__file__).resolve().parents[1] / "src" / "gba_session.sh"
        self.text = self.path.read_text(encoding="utf-8")

    def test_exports_the_sdl_and_mesa_debug_variables(self):
        for export in ("export SDL_LOG_PRIORITY=", "export LIBGL_DEBUG=",
                       "export MESA_DEBUG=", "export GBA_DEBUG="):
            self.assertIn(export, self.text)

    def test_values_can_be_overridden_by_the_caller(self):
        # `${VAR:-default}` keeps an operator-supplied value; a bare assignment
        # would silently discard it.
        for name in ("SDL_LOG_PRIORITY", "LIBGL_DEBUG", "MESA_DEBUG", "GBA_DEBUG"):
            self.assertIn(f"${{{name}:-", self.text)

    def test_missing_x11_tools_are_announced_at_boot(self):
        self.assertIn("xwininfo", self.text)
        self.assertIn("xrandr", self.text)

    def test_script_parses(self):
        result = subprocess.run(["bash", "-n", str(self.path)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr.decode())


if __name__ == "__main__":
    unittest.main()
