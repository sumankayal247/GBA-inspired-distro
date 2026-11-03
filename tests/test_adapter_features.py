"""mGBA handler: native cheat/state/speed hooks and input monitoring."""

import os
import queue
import shutil
import signal
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import config as settings_store
from gba_adapter_mgba import (
    KEY_F1, KEY_F12, KEY_LEFTSHIFT, ComboRouter, InputMonitor,
    MgbaNativeController, build_mgba_command, evdev_button_map,
)
from gba_ingame import ACTION_FAST_FORWARD, InGameSession
from game_data import CheatStore, SaveStateStore

GAME_A = "44636205c3e1a1b2"
DEFAULTS = settings_store.load_shortcuts(dict(settings_store.DEFAULT_SETTINGS))


class DummyProc:
    def __init__(self):
        self.signals = []
        self.returncode = None

    def poll(self):
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)

    def terminate(self):
        self.returncode = 0


class FakeInjector:
    """Pretends to be mGBA's native hotkey receiver."""

    def __init__(self, runtime_dir):
        self.runtime_dir = runtime_dir
        self.taps = []
        self.keys = []
        self._png = 0

    def available(self):
        return True

    def key(self, code, value):
        self.keys.append((code, value))
        return True

    def tap(self, codes, hold=0.04):
        codes = list(codes)
        self.taps.append(codes)
        for code in codes:
            if KEY_F1 <= code < KEY_F1 + 9 and KEY_LEFTSHIFT in codes:
                slot = code - KEY_F1 + 1
                path = os.path.join(self.runtime_dir, f"game.ss{slot}")
                with open(path, "wb") as f:
                    f.write(f"NATIVE_MGBA_STATE_{slot}".encode())
                future = time.time() + 2
                os.utime(path, (future, future))
            elif code == KEY_F12:
                self._png += 1
                path = os.path.join(self.runtime_dir, f"shot_{self._png}.png")
                with open(path, "wb") as f:
                    f.write(b"\x89PNG\r\n")
        return True

    def close(self):
        pass


class AdapterFeatureTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.save_dir = os.path.join(self.root, "saves", GAME_A)
        self.runtime_dir = os.path.join(self.save_dir, "runtime")
        os.makedirs(self.runtime_dir, exist_ok=True)
        self.rom = os.path.join(self.root, "game.gba")
        with open(self.rom, "wb") as f:
            f.write(b"ROM")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def controller(self):
        injector = FakeInjector(self.runtime_dir)
        proc = DummyProc()
        native = MgbaNativeController(proc, self.rom, self.save_dir,
                                      injector=injector)
        return native, injector, proc


class TestNativeCommandFlags(AdapterFeatureTestCase):
    def cmd(self, **kwargs):
        return build_mgba_command("/usr/bin/mgba", self.rom, self.save_dir,
                                  windowed=False, display_scaling="fit", **kwargs)

    def test_original_command_is_unchanged_without_features(self):
        cmd = self.cmd()
        joined = " ".join(cmd)
        self.assertIn(f"savestatePath={self.save_dir}", joined)
        self.assertIn(f"screenshotPath={self.save_dir}", joined)
        self.assertEqual(cmd[-1], self.rom)

    def test_cheats_are_loaded_from_the_per_game_native_file(self):
        cheat_file = os.path.join(self.save_dir, "cheats", "game.cheats")
        cmd = self.cmd(cheat_file=cheat_file,
                       cheats_dir=os.path.dirname(cheat_file))
        self.assertIn("-c", cmd)
        self.assertIn(cheat_file, cmd)
        self.assertIn(f"cheatsPath={os.path.dirname(cheat_file)}", " ".join(cmd))
        self.assertEqual(cmd[-1], self.rom)

    def test_no_global_cheat_autoload_when_game_has_no_cheats(self):
        joined = " ".join(self.cmd(cheats_dir=os.path.join(self.save_dir, "cheats")))
        self.assertIn("cheatAutoload=0", joined)
        self.assertNotIn("-c", joined.split())

    def test_speed_uses_mgba_fps_target_not_custom_timing(self):
        for multiplier, expected in ((1, "fpsTarget=60"),
                                     (2, "fpsTarget=120"),
                                     (3, "fpsTarget=180")):
            joined = " ".join(self.cmd(fps_target=60.0 * multiplier))
            self.assertIn(expected, joined)
            self.assertIn("rewindEnable=0", joined)

    def test_native_state_resume_and_runtime_slot_routing(self):
        state = os.path.join(self.runtime_dir, "speed_state.ss")
        cmd = self.cmd(savestate=state, runtime_dir=self.runtime_dir)
        self.assertIn("-t", cmd)
        self.assertIn(state, cmd)
        self.assertIn(f"savestatePath={self.runtime_dir}", " ".join(cmd))
        self.assertIn(f"screenshotPath={self.runtime_dir}", " ".join(cmd))
        self.assertEqual(cmd[-1], self.rom)


class TestNativeOperations(AdapterFeatureTestCase):
    def test_save_state_uses_shift_f_slot_and_stages_a_real_state(self):
        native, injector, _ = self.controller()
        path = native.save_state(3)
        self.assertIsNotNone(path)
        self.assertIn([KEY_LEFTSHIFT, KEY_F1 + 2], injector.taps)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"NATIVE_MGBA_STATE_3")
        self.assertTrue(path.startswith(native.capture_dir))
        # The runtime slot is restored, so it cannot confuse the next load.
        self.assertFalse(os.path.exists(native.native_state_path(3)))

    def test_screenshot_is_a_separate_visual_file(self):
        native, injector, _ = self.controller()
        shot = native.capture_screenshot(1)
        self.assertIsNotNone(shot)
        self.assertIn([KEY_F12], injector.taps)
        self.assertTrue(shot.endswith(".png"))
        self.assertNotEqual(shot, native.native_state_path(1))

    def test_load_state_copies_real_state_then_uses_f_slot(self):
        native, injector, _ = self.controller()
        archive_state = os.path.join(self.root, "archive.ss")
        with open(archive_state, "wb") as f:
            f.write(b"ARCHIVED_MGBA_STATE")
        self.assertTrue(native.load_state(2, archive_state))
        self.assertIn([KEY_F1 + 1], injector.taps)
        with open(native.native_state_path(2), "rb") as f:
            self.assertEqual(f.read(), b"ARCHIVED_MGBA_STATE")
        self.assertFalse(native.load_state(2, os.path.join(self.root, "missing.ss")))
        self.assertFalse(native.load_state(0, archive_state))
        self.assertFalse(native.load_state(10, archive_state))

    def test_speed_change_uses_native_fps_target_and_state_resume(self):
        native, _, _ = self.controller()
        self.assertTrue(native.set_speed(2, 120.0))
        self.assertEqual(native.fps_target, 120.0)
        state = native.capture_ephemeral_state()
        self.assertIsNotNone(state)
        self.assertTrue(os.path.isfile(state))

    def test_pause_resume_does_not_terminate_the_emulator(self):
        native, _, proc = self.controller()
        self.assertTrue(native.pause())
        self.assertTrue(native.resume())
        self.assertEqual(proc.returncode, None)
        self.assertIn(signal.SIGSTOP, proc.signals)
        self.assertIn(signal.SIGCONT, proc.signals)

    def test_consumed_combo_keys_are_released(self):
        native, injector, _ = self.controller()
        native.suppress_combo({"keyL", "keyR"})
        self.assertTrue(injector.keys)
        self.assertTrue(all(value == 0 for _, value in injector.keys))

    def test_failed_hotkey_is_graceful(self):
        native, injector, _ = self.controller()
        injector.tap = lambda *a, **k: False
        self.assertIsNone(native.save_state(1))
        self.assertIsNone(native.capture_screenshot())
        self.assertIsNone(native.capture_ephemeral_state())


class TestInputMonitorCombos(AdapterFeatureTestCase):
    def test_default_keyboard_profile_maps_to_buttons(self):
        mapping = evdev_button_map({"keyUp": 1073741906, "keyLeft": 1073741904,
                                    "keyL": 97, "keyR": 115})
        self.assertEqual(mapping[103], "keyUp")
        self.assertEqual(mapping[105], "keyLeft")
        self.assertEqual(mapping[30], "keyL")
        self.assertEqual(mapping[31], "keyR")

    def test_combos_are_queued_once_and_consumed_buttons_reported(self):
        actions = queue.Queue()
        consumed = []
        monitor = InputMonitor(DummyProc(), shortcuts=DEFAULTS,
                               action_queue=actions,
                               on_consumed=lambda buttons: consumed.append(set(buttons)))
        router = ComboRouter(DEFAULTS)
        monitor._press(router, "keyL")
        monitor._press(router, "keyR")
        self.assertEqual(actions.get_nowait(), ACTION_FAST_FORWARD)
        self.assertTrue(actions.empty())
        self.assertEqual(consumed[-1], {"keyL", "keyR"})
        monitor._press(router, "keyL")           # key repeat while held
        self.assertTrue(actions.empty())
        monitor._release(router, "keyL")
        monitor._release(router, "keyR")
        self.assertEqual(consumed[-1], set())

    def test_hat_axes_map_to_dpad_buttons(self):
        self.assertEqual(InputMonitor._hat_button(16, -1), "keyLeft")
        self.assertEqual(InputMonitor._hat_button(17, -1), "keyUp")
        self.assertIsNone(InputMonitor._hat_button(16, 0))

    def test_in_game_session_can_drive_the_native_controller(self):
        native, injector, proc = self.controller()
        cheat_store = CheatStore(GAME_A, self.rom, data_dir=self.root)
        state_store = SaveStateStore(GAME_A, data_dir=self.root)
        session = InGameSession(GAME_A, DEFAULTS, cheat_store, state_store, native)
        session.press("keyL")
        session.press("keyR")
        self.assertEqual(session.speed, 2)
        self.assertEqual(native.fps_target, 120.0)
        self.assertTrue(session.relaunch_requested)
        self.assertTrue(session.take_relaunch())
        self.assertFalse(session.take_relaunch())
        self.assertIsNone(proc.returncode)


if __name__ == "__main__":
    unittest.main()
