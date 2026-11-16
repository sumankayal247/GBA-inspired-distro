#!/usr/bin/env python3
"""
Tests for the full-browser handler (the NetSurf / Dillo app).

The built-in browser reads pages; this one runs a real browser in the
session's X display, where the mouse is a mouse. What is tested here is
the part that can be tested without a browser: which program is chosen,
what it is told, where it keeps its profile, and that the shell hands the
display over to it in a way the supervisor understands.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame  # noqa: E402

import gba_adapter_web  # noqa: E402
import gba_supervisor  # noqa: E402


class TestBrowserDiscovery(unittest.TestCase):
    def which(self, mapping):
        return lambda name: mapping.get(name)

    def test_netsurf_is_preferred(self):
        found = {"netsurf": "/usr/bin/netsurf", "dillo": "/usr/bin/dillo"}
        with patch.object(gba_adapter_web.shutil, "which", self.which(found)):
            browser = gba_adapter_web.find_browser()
        self.assertEqual(browser[0], "/usr/bin/netsurf")

    def test_dillo_is_used_when_netsurf_is_missing(self):
        found = {"dillo": "/usr/bin/dillo"}
        with patch.object(gba_adapter_web.shutil, "which", self.which(found)):
            browser = gba_adapter_web.find_browser()
        self.assertEqual(browser[0], "/usr/bin/dillo")

    def test_nothing_installed_is_reported(self):
        with patch.object(gba_adapter_web.shutil, "which", self.which({})):
            self.assertIsNone(gba_adapter_web.find_browser())

    def test_a_chosen_browser_that_is_missing_is_not_replaced(self):
        with patch.object(gba_adapter_web.shutil, "which", self.which({"dillo": "/usr/bin/dillo"})):
            self.assertIsNone(gba_adapter_web.find_browser("netsurf"))

    def test_the_chosen_browser_is_used_when_it_is_installed(self):
        found = {"netsurf": "/usr/bin/netsurf"}
        with patch.object(gba_adapter_web.shutil, "which", self.which(found)):
            browser = gba_adapter_web.find_browser("netsurf")
        self.assertEqual(browser[0], "/usr/bin/netsurf")

    def test_auto_means_whichever_browser_is_there(self):
        found = {"dillo": "/usr/bin/dillo"}
        with patch.object(gba_adapter_web.shutil, "which", self.which(found)):
            browser = gba_adapter_web.find_browser("auto")
        self.assertEqual(browser[0], "/usr/bin/dillo")


class TestCommandAndProfile(unittest.TestCase):
    def test_the_url_is_the_last_argument(self):
        command = gba_adapter_web.build_command(("/usr/bin/netsurf", []),
                                                "https://example.com")
        self.assertEqual(command[0], "/usr/bin/netsurf")
        self.assertEqual(command[-1], "https://example.com")

    def test_the_profile_lives_in_the_handler_directory(self):
        env = gba_adapter_web.build_environment("/data/config/handlers/web-adapter")
        self.assertEqual(env["HOME"], "/data/config/handlers/web-adapter")
        self.assertTrue(env["XDG_CONFIG_HOME"].startswith(
            "/data/config/handlers/web-adapter"))

    def test_the_x_authority_is_kept(self):
        # A session whose X server asks for a cookie refuses a client that
        # does not present it.
        os.environ["XAUTHORITY"] = "/run/gba/xauth"
        try:
            env = gba_adapter_web.build_environment("/tmp/profile")
        finally:
            os.environ.pop("XAUTHORITY", None)
        self.assertEqual(env.get("XAUTHORITY"), "/run/gba/xauth")

    def test_the_advertised_browsers_are_the_ones_we_know(self):
        names = [name for name, _ in gba_adapter_web.BROWSERS]
        self.assertIn("netsurf", names)
        self.assertIn("dillo", names)


class TestSupervisorRouting(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.mkdtemp()
        self.supervisor = gba_supervisor.Supervisor.__new__(gba_supervisor.Supervisor)
        self.supervisor.base_dir = self.temp
        self.supervisor.data_dir = self.temp
        self.supervisor.games_dir = self.temp

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_the_web_handler_needs_no_content_file(self):
        _path, needs_content = self.supervisor._handler_binary("web-adapter")
        self.assertFalse(needs_content)

    def test_the_emulator_still_needs_a_rom(self):
        _path, needs_content = self.supervisor._handler_binary("mgba-adapter")
        self.assertTrue(needs_content)

    def test_the_web_adapter_is_found_next_to_the_sources(self):
        path, _needs = self.supervisor._handler_binary("web-adapter")
        self.assertTrue(path and path.endswith("gba_adapter_web.py"))


class TestAdapterEndToEnd(unittest.TestCase):
    """The handler really starts a browser - with the right arguments.

    NetSurf and Dillo are not installed on a build machine, so the browser
    is a stub that writes down what it was told. Everything else is the real
    handler: the same program the appliance runs.
    """

    def setUp(self):
        self.temp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        self.bindir = os.path.join(self.temp, "bin")
        os.makedirs(self.bindir, exist_ok=True)
        self.marker = os.path.join(self.temp, "marker")
        self.profile = os.path.join(self.temp, "profile")

    def install_stub(self, name="netsurf"):
        stub = os.path.join(self.bindir, name)
        with open(stub, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\n"
                         f'printf "%s\\n%s\\n" "$1" "$HOME" > "{self.marker}"\n'
                         "exit 0\n")
        os.chmod(stub, 0o755)
        return stub

    def run_adapter(self, **extra):
        env = dict(os.environ)
        env["PATH"] = self.bindir + os.pathsep + env.get("PATH", "")
        env["DISPLAY"] = ":0"
        env["SYS_CONFIG_DIR"] = self.profile
        env["SYS_DATA_DIR"] = os.path.join(self.temp, "data")
        env.update(extra)
        return subprocess.run([sys.executable, gba_adapter_web.__file__],
                              env=env, capture_output=True, text=True, timeout=60)

    def test_the_browser_opens_the_url_with_an_isolated_profile(self):
        self.install_stub()
        result = self.run_adapter(GBA_WEB_URL="https://example.com/page")
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(self.marker, encoding="utf-8") as handle:
            url, home = handle.read().split("\n")[:2]
        self.assertEqual(url, "https://example.com/page")
        self.assertEqual(home, self.profile)
        self.assertTrue(os.path.isdir(self.profile))

    def test_the_chosen_browser_is_the_one_that_is_started(self):
        self.install_stub("dillo")
        result = self.run_adapter(GBA_WEB_BROWSER="dillo",
                                  GBA_WEB_URL="https://example.com/dillo")
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(self.marker, encoding="utf-8") as handle:
            url, _home = handle.read().split("\n")[:2]
        self.assertEqual(url, "https://example.com/dillo")

    def test_without_a_browser_the_handler_says_so(self):
        result = self.run_adapter(PATH=self.temp)
        self.assertEqual(result.returncode, 4)
        self.assertIn("netsurf-3 dillo", result.stdout)

    def test_without_a_display_the_handler_refuses(self):
        self.install_stub()
        env = dict(os.environ)
        env.pop("DISPLAY", None)
        result = subprocess.run([sys.executable, gba_adapter_web.__file__],
                                env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 3)
        self.assertIn("DISPLAY", result.stdout)


class TestSupervisorEndToEnd(unittest.TestCase):
    """The whole chain: launch request -> supervisor -> handler -> browser."""

    def setUp(self):
        import common
        self.temp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        self.run_dir = os.path.join(self.temp, "run")
        os.makedirs(self.run_dir, exist_ok=True)
        self._original = (common.get_data_dir, common.get_run_dir)
        common.get_data_dir = lambda: self.temp
        common.get_run_dir = lambda: self.run_dir
        self.addCleanup(self._restore)

        self.bindir = os.path.join(self.temp, "bin")
        os.makedirs(self.bindir, exist_ok=True)
        self.marker = os.path.join(self.temp, "marker")
        stub = os.path.join(self.bindir, "netsurf")
        with open(stub, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\n"
                         f'printf "%s\\n%s\\n" "$1" "$HOME" > "{self.marker}"\n'
                         "exit 0\n")
        os.chmod(stub, 0o755)
        self._old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = self.bindir + os.pathsep + self._old_path
        self.addCleanup(self._restore_path)
        # The handler refuses to start without a display; the stub browser
        # does not need one, but the check is part of what is tested.
        self._old_display = os.environ.get("DISPLAY")
        os.environ["DISPLAY"] = ":99"
        self.addCleanup(self._restore_display)

    def _restore(self):
        import common
        common.get_data_dir, common.get_run_dir = self._original

    def _restore_path(self):
        os.environ["PATH"] = self._old_path

    def _restore_display(self):
        if self._old_display is None:
            os.environ.pop("DISPLAY", None)
        else:
            os.environ["DISPLAY"] = self._old_display

    def test_the_supervisor_starts_the_browser_with_the_requested_page(self):
        import gba_supervisor
        supervisor = gba_supervisor.Supervisor(is_desktop_mode=True)
        outcome = supervisor.run_handler({
            "entry_id": "app_web_browser",
            "content_path": "",
            "handler": "web-adapter",
            "url": "https://example.com/from-the-shell",
            "browser": "auto",
        })
        self.assertEqual(outcome, "clean")
        with open(self.marker, encoding="utf-8") as handle:
            url, home = handle.read().split("\n")[:2]
        self.assertEqual(url, "https://example.com/from-the-shell")
        # The profile stays in the handler's own directory, not in the home.
        self.assertTrue(home.endswith(os.path.join("config", "handlers", "web-adapter")),
                        home)
        self.assertNotEqual(home, os.path.expanduser("~"))


class TestLaunchRequest(unittest.TestCase):
    """The shell hands the display over with a launch request."""

    def setUp(self):
        import common
        import config
        import gba_launcher
        import library
        self.test_dir = tempfile.mkdtemp()
        self.run_dir = os.path.join(self.test_dir, "run")
        os.makedirs(self.run_dir, exist_ok=True)
        self._original = {}
        for module in (common, config, gba_launcher, library):
            for name, value in (("get_data_dir", lambda: self.test_dir),
                                ("get_games_dir", lambda: self.test_dir),
                                ("get_run_dir", lambda: self.run_dir)):
                if hasattr(module, name):
                    self._original[(module, name)] = getattr(module, name)
                    setattr(module, name, value)

    def tearDown(self):
        for (module, name), value in self._original.items():
            setattr(module, name, value)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_the_request_names_the_handler_and_the_start_page(self):
        import gba_launcher
        from gba_launcher import GBALauncher
        launcher = GBALauncher(demo=True,
                                screenshot_path=os.path.join(self.test_dir, "shot.png"))
        launcher.screenshot_path = None
        launcher.update()
        launcher.release_display = lambda: None
        with self.assertRaises(SystemExit):
            launcher.request_app_launch({
                "id": "app_web_browser", "title": "Web Browser",
                "handler": "web-adapter", "type": "app",
            })
        with open(os.path.join(self.run_dir, "launch_request.json"),
                  encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["handler"], "web-adapter")
        self.assertEqual(payload["entry_id"], "app_web_browser")
        self.assertTrue(payload["url"].startswith("http"))


if __name__ == "__main__":
    unittest.main()
