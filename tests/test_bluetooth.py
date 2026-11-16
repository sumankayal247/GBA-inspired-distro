"""Bluetooth: pairing, connecting, and the errors users actually see.

The bug this file guards against is a specific one. `bluetoothctl connect`
is asynchronous and BlueZ's answer was pasted into the toast verbatim:

    Attempting to connect to 01:02:03:04:D2:A1 | Failed to connect:
    org.bluez.Error.Failed br-connection-profile-unavailable

The tests below pin down the four things that made that happen and that
would bring it back: the raw D-Bus error is never shown, an unpaired device
is paired before it is connected, the device list (not the exit code) decides
whether a connection succeeded, and a missing audio profile is reported as
the cause instead of as a mystery.
"""

import os
import stat
import threading
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame  # noqa: E402

import config  # noqa: E402
import gba_launcher  # noqa: E402
from tests.test_shell import ShellTestCase  # noqa: E402


MAC = "01:02:03:04:D2:A1"


def fake_run(outputs):
    """A subprocess.run stand-in that answers per bluetoothctl arguments."""
    def run(cmd, **kwargs):
        key = " ".join(str(part) for part in cmd[1:])
        text = outputs.get(key, "")
        return SimpleNamespace(stdout=text.encode("utf-8"),
                              stderr=b"", returncode=0)
    return run


class BluetoothErrorMessageTest(unittest.TestCase):
    """Raw BlueZ errors must never reach the screen."""

    def test_profile_unavailable_becomes_an_actionable_sentence(self):
        raw = ("Attempting to connect to %s\n"
               "Failed to connect: org.bluez.Error.Failed "
               "br-connection-profile-unavailable" % MAC)
        message = config.bluetooth_error_message(raw)
        self.assertNotIn("org.bluez", message)
        self.assertNotIn("br-connection", message)
        self.assertTrue(message)
        self.assertIn("profile", message.lower())

    def test_the_attempting_line_is_never_used_as_the_message(self):
        message = config.bluetooth_error_message("Attempting to connect to %s" % MAC)
        self.assertNotIn("Attempting", message)

    def test_known_bluez_errors_are_translated(self):
        cases = {
            "org.bluez.Error.InProgress": "Another connection",
            "org.bluez.Error.NotReady": "not ready",
            "AuthenticationFailed": "PIN",
            "org.bluez.Error.NoSuchDevice": "gone",
            "Device XX not available": "Out of range",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertIn(expected, config.bluetooth_error_message(raw))

    def test_an_unknown_error_keeps_the_last_line_but_not_the_prefix(self):
        message = config.bluetooth_error_message("Failed to connect: something odd")
        self.assertNotIn("Failed to connect:", message)
        self.assertIn("something odd", message)

    def test_empty_output_still_produces_a_message(self):
        self.assertTrue(config.bluetooth_error_message(""))


class BluetoothConnectTest(unittest.TestCase):
    """Pair, trust, connect - and believe the device list, not the exit code."""

    def _patches(self, session_output="Connection successful", connected=True,
                 info=None):
        info = info if info is not None else {
            "paired": True, "trusted": True, "connected": False, "name": "Rockerz"}
        return [
            patch.object(config.shutil, "which",
                         lambda name: "/usr/bin/" + name),
            patch.object(config, "_bt_device_info", lambda mac: dict(info)),
            patch.object(config, "_btctl_session",
                         lambda *a, **k: session_output),
            patch.object(config, "_btctl", lambda *a, **k: ""),
            patch.object(config, "_bt_wait_connected",
                         lambda mac, want, timeout: connected),
        ]

    def test_a_paired_device_is_connected_and_verified(self):
        patches = self._patches()
        for item in patches:
            item.start()
        try:
            ok, message = config.connect_bluetooth(MAC)
        finally:
            for item in patches:
                item.stop()
        self.assertTrue(ok)
        self.assertEqual(message, "Connected")

    def test_an_unpaired_device_is_paired_first(self):
        patches = self._patches(info={"paired": False, "trusted": False,
                                      "connected": False, "name": "Rockerz"})
        for item in patches:
            item.start()
        try:
            with patch.object(config, "pair_bluetooth",
                              return_value=(True, "Paired")) as paired:
                ok, _ = config.connect_bluetooth(MAC)
        finally:
            for item in patches:
                item.stop()
        self.assertTrue(ok)
        paired.assert_called_once_with(MAC)

    def test_a_failed_pairing_is_reported_not_retried(self):
        patches = self._patches(info={"paired": False, "trusted": False,
                                      "connected": False, "name": "Rockerz"})
        for item in patches:
            item.start()
        try:
            with patch.object(config, "pair_bluetooth",
                              return_value=(False, "Pairing failed: the PIN was rejected")):
                ok, message = config.connect_bluetooth(MAC)
        finally:
            for item in patches:
                item.stop()
        self.assertFalse(ok)
        self.assertIn("PIN", message)

    def test_a_bluez_failure_is_explained(self):
        patches = self._patches(
            session_output=("Attempting to connect to %s\nFailed to connect: "
                            "org.bluez.Error.Failed br-connection-profile-unavailable"
                            % MAC),
            connected=False)
        for item in patches:
            item.start()
        try:
            ok, message = config.connect_bluetooth(MAC)
        finally:
            for item in patches:
                item.stop()
        self.assertFalse(ok)
        self.assertNotIn("org.bluez", message)

    def test_a_silent_failure_is_not_reported_as_success(self):
        """The old code believed the exit code: zero meant "connected"."""
        patches = self._patches(session_output="", connected=False)
        for item in patches:
            item.start()
        try:
            ok, message = config.connect_bluetooth(MAC)
        finally:
            for item in patches:
                item.stop()
        self.assertFalse(ok)
        self.assertTrue(message)

    def test_bluetoothctl_missing_is_reported_plainly(self):
        with patch.object(config.shutil, "which", lambda name: None):
            ok, message = config.connect_bluetooth(MAC)
        self.assertFalse(ok)
        self.assertIn("bluez", message.lower())


class BluetoothSessionTest(unittest.TestCase):
    """bluetoothctl needs one process: the pairing agent lives in it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "bluetoothctl")
        script = (
            "#!/bin/sh\n"
            "while IFS= read -r line; do\n"
            "    echo \"$line\"\n"
            "    case \"$line\" in\n"
            "        \"pair \"*) echo \"Pairing successful\" ;;\n"
            "        \"connect \"*) echo \"Connection successful\" ;;\n"
            "        \"disconnect \"*) echo \"Device disconnected\" ;;\n"
            "    esac\n"
            "done\n"
        )
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(script)
        os.chmod(self.path, os.stat(self.path).st_mode | stat.S_IEXEC)
        self._saved_path = os.environ.get("PATH", "")
        os.environ["PATH"] = self.dir + os.pathsep + self._saved_path

    def tearDown(self):
        os.environ["PATH"] = self._saved_path
        for name in os.listdir(self.dir):
            try:
                os.unlink(os.path.join(self.dir, name))
            except OSError:
                pass
        os.rmdir(self.dir)

    def test_every_command_reaches_the_same_process(self):
        out = config._btctl_session(
            ["agent on", "default-agent", "scan on", "pair " + MAC],
            timeout=5.0, terminators=["pairing successful"])
        self.assertIn("agent on", out)
        self.assertIn("default-agent", out)
        self.assertIn("pair " + MAC, out)
        self.assertIn("Pairing successful", out)

    def test_pairing_uses_one_session_and_reports_success(self):
        ok, message = config.pair_bluetooth(MAC)
        self.assertTrue(ok)
        self.assertEqual(message, "Paired")


class BluetoothStatusTest(unittest.TestCase):
    """The connected device has to be identified - by MAC, not by name."""

    def test_the_connected_device_mac_is_parsed(self):
        outputs = {
            "show": "Controller 00:11:22:33:44:55 Rockerz\n\tPowered: yes\n",
            "devices Connected": "Device %s Rockerz Trinity\n" % MAC,
            "info " + MAC: "\tName: Rockerz Trinity\n\tPaired: yes\n",
        }
        with patch.object(config.shutil, "which", lambda name: "/usr/bin/" + name), \
                patch.object(config.subprocess, "run", fake_run(outputs)):
            status = config.get_bluetooth_status()
        self.assertTrue(status["connected"])
        self.assertEqual(status["device_mac"], MAC)
        self.assertEqual(status["device_name"], "Rockerz Trinity")

    def test_no_adapter_means_no_status(self):
        with patch.object(config.shutil, "which", lambda name: None):
            self.assertIsNone(config.get_bluetooth_status())

    def test_devices_report_their_pairing_state(self):
        outputs = {
            "devices": "Device %s Rockerz Trinity\nDevice AA:BB:CC:DD:EE:FF Speaker\n" % MAC,
            "devices Paired": "Device %s Rockerz Trinity\n" % MAC,
        }
        with patch.object(config.shutil, "which", lambda name: "/usr/bin/" + name), \
                patch.object(config.subprocess, "run", fake_run(outputs)):
            devices = config.get_bluetooth_devices()
        self.assertEqual(len(devices), 2)
        self.assertTrue(devices[0]["paired"])
        self.assertFalse(devices[1]["paired"])

    def test_a_scan_does_not_die_on_the_way(self):
        """The scan used to raise NameError: `time` was never imported."""
        outputs = {"devices": "Device %s Rockerz Trinity\n" % MAC,
                   "devices Paired": ""}
        with patch.object(config.shutil, "which", lambda name: "/usr/bin/" + name), \
                patch.object(config.subprocess, "run", fake_run(outputs)), \
                patch.object(config.time, "sleep", lambda seconds: None):
            devices = config.scan_bluetooth_devices()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["mac"], MAC)


class BluetoothDiagnosticsTest(unittest.TestCase):
    """A missing A2DP sink profile is the real cause of a failed connect."""

    def test_a_missing_audio_profile_is_named(self):
        with patch.object(config.shutil, "which", lambda name: None if name == "dbus-send" else "/usr/bin/" + name), \
                patch.object(config, "_btctl", lambda *a, **k: "Controller AA\n\tPowered: yes\n"), \
                patch.object(config, "_bluez_object_paths", lambda: ["/org/bluez/hci0"]), \
                patch.object(config, "get_audio_service", lambda: ""):
            diagnostics = config.get_bluetooth_diagnostics()
        self.assertFalse(diagnostics["audio_profile"])
        self.assertIn("PipeWire", diagnostics["hint"])

    def test_a_present_a2dp_sink_is_reported(self):
        with patch.object(config.shutil, "which", lambda name: None if name == "dbus-send" else "/usr/bin/" + name), \
                patch.object(config, "_btctl", lambda *a, **k: "Controller AA\n\tPowered: yes\n"), \
                patch.object(config, "_bluez_object_paths",
                             lambda: ["/org/bluez/hci0", "/org/bluez/hci0/A2DPSink"]), \
                patch.object(config, "get_audio_service", lambda: "pipewire"):
            diagnostics = config.get_bluetooth_diagnostics()
        self.assertTrue(diagnostics["audio_profile"])
        self.assertIn("a2dp-sink", diagnostics["profiles"])

    def test_bluetoothctl_missing_is_reported(self):
        with patch.object(config.shutil, "which", lambda name: None):
            diagnostics = config.get_bluetooth_diagnostics()
        self.assertFalse(diagnostics["available"])
        self.assertIn("bluez", diagnostics["hint"].lower())


class BluetoothOverlayTest(ShellTestCase):
    """The overlay must never block the shell on a slow bluetoothctl call."""

    def _overlay(self, launcher):
        overlay = gba_launcher.BluetoothOverlay(launcher)
        launcher.push_overlay(overlay)
        return overlay

    def _pump(self, launcher, overlay, timeout=2.0):
        """Drive the overlay until the job produced a toast."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            overlay.update(launcher)
            if launcher.toast_text:
                return True
            time.sleep(0.01)
        return False

    def test_connecting_runs_off_the_ui_thread(self):
        launcher = self.make_launcher()
        release = threading.Event()
        entered = threading.Event()

        def slow_connect(mac):
            entered.set()
            release.wait(2.0)
            return True, "Connected"

        with patch.object(gba_launcher.settings_store, "get_bluetooth_status",
                          lambda: {"powered": True, "connected": False,
                                   "device_mac": "", "device_name": ""}), \
                patch.object(gba_launcher.settings_store, "get_bluetooth_diagnostics",
                             lambda: {"hint": "", "audio_profile": True}), \
                patch.object(gba_launcher.settings_store, "get_bluetooth_devices",
                             lambda: [{"mac": MAC, "name": "Rockerz", "paired": True}]), \
                patch.object(gba_launcher.settings_store, "connect_bluetooth",
                             slow_connect):
            overlay = self._overlay(launcher)
            overlay._connect_device(launcher, {"mac": MAC, "name": "Rockerz",
                                               "paired": True})
            # The UI thread returned while bluetoothctl is still working.
            self.assertTrue(entered.wait(1.0))
            self.assertEqual(overlay.busy, "Connecting to Rockerz")
            self.assertEqual(launcher.toast_text, "")
            release.set()
            self.assertTrue(self._pump(launcher, overlay))
        self.assertEqual(launcher.toast_text, "Connected")

    def test_a_failure_is_shown_as_a_readable_message(self):
        launcher = self.make_launcher()
        with patch.object(gba_launcher.settings_store, "get_bluetooth_status",
                          lambda: {"powered": True, "connected": False,
                                   "device_mac": "", "device_name": ""}), \
                patch.object(gba_launcher.settings_store, "get_bluetooth_diagnostics",
                             lambda: {"hint": "", "audio_profile": True}), \
                patch.object(gba_launcher.settings_store, "get_bluetooth_devices",
                             lambda: [{"mac": MAC, "name": "Rockerz", "paired": True}]), \
                patch.object(gba_launcher.settings_store, "connect_bluetooth",
                             lambda mac: (False, "No usable BT profile: headset busy")):
            overlay = self._overlay(launcher)
            overlay._connect_device(launcher, {"mac": MAC, "name": "Rockerz",
                                               "paired": True})
            self.assertTrue(self._pump(launcher, overlay))
        self.assertIn("headset busy", launcher.toast_text)

    def test_the_reason_stays_in_the_panel_after_the_toast_is_gone(self):
        launcher = self.make_launcher()
        with patch.object(gba_launcher.settings_store, "get_bluetooth_status",
                          lambda: {"powered": True, "connected": False,
                                   "device_mac": "", "device_name": ""}), \
                patch.object(gba_launcher.settings_store, "get_bluetooth_diagnostics",
                             lambda: {"hint": "", "audio_profile": True}), \
                patch.object(gba_launcher.settings_store, "get_bluetooth_devices",
                             lambda: [{"mac": MAC, "name": "Rockerz", "paired": True}]), \
                patch.object(gba_launcher.settings_store, "connect_bluetooth",
                             lambda mac: (False, "No usable BT profile: headset busy")):
            overlay = self._overlay(launcher)
            overlay._connect_device(launcher, {"mac": MAC, "name": "Rockerz",
                                               "paired": True})
            self.assertTrue(self._pump(launcher, overlay))
        # The toast is transient; the panel keeps the reason.
        launcher.toast_until = 0
        launcher.update()
        self.assertEqual(launcher.toast_text, "")
        self.assertIn("headset busy", overlay.last_error)

    def test_a_connected_device_is_disconnected_instead(self):
        launcher = self.make_launcher()
        calls = []
        with patch.object(gba_launcher.settings_store, "get_bluetooth_status",
                          lambda: {"powered": True, "connected": True,
                                   "device_mac": MAC, "device_name": "Rockerz"}), \
                patch.object(gba_launcher.settings_store, "get_bluetooth_diagnostics",
                             lambda: {"hint": "", "audio_profile": True}), \
                patch.object(gba_launcher.settings_store, "get_bluetooth_devices",
                             lambda: [{"mac": MAC, "name": "Rockerz", "paired": True}]), \
                patch.object(gba_launcher.settings_store, "disconnect_bluetooth",
                             lambda mac: (calls.append(mac), (True, "Disconnected"))[1]), \
                patch.object(gba_launcher.settings_store, "connect_bluetooth",
                             lambda mac: (calls.append(mac), (True, "Connected"))[1]):
            overlay = self._overlay(launcher)
            overlay._connect_device(launcher, {"mac": MAC, "name": "Rockerz",
                                               "paired": True})
            self.assertTrue(self._pump(launcher, overlay))
        self.assertEqual(calls, [MAC])
        self.assertEqual(launcher.toast_text, "Disconnected")


if __name__ == "__main__":
    unittest.main()
