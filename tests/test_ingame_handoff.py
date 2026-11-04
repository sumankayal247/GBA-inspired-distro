#!/usr/bin/env python3
"""
Display handoff tests: overlays must own the screen *and* gameplay must come
back.

The historical design sent SIGSTOP to mGBA and then asked a second Pygame
process to take the same SDL/KMS/DRM display.  That cannot work: the stopped
emulator still owns the surface, so the overlay never appeared and the game
looked frozen.  These tests pin the replacement contract:

* capture a frame + native state while mGBA is still running,
* stop mGBA so the overlay renderer can open its own window,
* restart mGBA from the captured state when the overlay closes (new speed and
  freshly written cheat file included),
* never leave the emulator stopped, orphaned or hotkey-driven after a failure.

The process handoff is simulated (no mGBA, no display, no /dev/uinput), so a
pass here proves the wiring, not that a particular DRM driver hands over
cleanly — that last mile is listed as hardware verification in the docs.
"""

import os
import shutil
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import config as settings_store  # noqa: E402
from game_data import CheatStore, SaveStateStore  # noqa: E402
from gba_adapter_mgba import (  # noqa: E402
    KEY_F1, KEY_F12, KEY_LEFTSHIFT, InputMonitor, MgbaNativeController,
    MgbaSessionRunner,
)
from gba_ingame import (  # noqa: E402
    ACTION_CHEATS, ACTION_FAST_FORWARD, ACTION_SAVE_STATES,
    CheatsOverlayModel, InGameSession, SaveStatesOverlayModel,
)

GAME_A = "44636205c3e1a1b2"
DEFAULTS = settings_store.load_shortcuts(dict(settings_store.DEFAULT_SETTINGS))


class DummyProc:
    """A stand-in for Popen that records signals instead of sending them."""

    def __init__(self, alive=True):
        self.signals = []
        self.returncode = None if alive else 0
        self.pid = 4242

    def poll(self):
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)

    def terminate(self):
        self.returncode = 0

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def kill(self):
        self.returncode = -signal.SIGKILL


class FakeInjector:
    """Pretends to be mGBA's native hotkey receiver (F12 / Shift+F<N>)."""

    def __init__(self, runtime_dir, available=True):
        self.runtime_dir = runtime_dir
        self.taps = []
        self.keys = []
        self._png = 0
        self._available = available

    def available(self):
        return self._available

    def key(self, code, value):
        self.keys.append((code, value))
        return True

    def tap(self, codes, hold=0.04):
        codes = list(codes)
        if not self._available:
            return False
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

    def release(self, codes):
        return True

    def close(self):
        pass


class FakeHandoff:
    """Stands in for MgbaSessionRunner's stop/start half of the handoff."""

    def __init__(self, fail_stop=False, fail_start=False):
        self.fail_stop = fail_stop
        self.fail_start = fail_start
        self.stops = 0
        self.starts = []
        self.running = True

    def stop_emulator(self, timeout=None):
        if self.fail_stop:
            return False
        self.stops += 1
        self.running = False
        return True

    def start_emulator(self, state_path=None, fps_target=None):
        if self.fail_start:
            return False
        self.starts.append((state_path, fps_target))
        self.running = True
        return True


class HandoffTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.save_dir = os.path.join(self.root, "saves", GAME_A)
        self.runtime_dir = os.path.join(self.save_dir, "runtime")
        os.makedirs(self.runtime_dir, exist_ok=True)
        self.rom = os.path.join(self.root, "game.gba")
        with open(self.rom, "wb") as f:
            f.write(b"ROM")
        self.cheats = CheatStore(GAME_A, self.rom, data_dir=self.root)
        self.states = SaveStateStore(GAME_A, data_dir=self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def build(self, fail_stop=False, fail_start=False, injector_available=True):
        self.proc = DummyProc()
        self.injector = FakeInjector(self.runtime_dir, available=injector_available)
        self.handoff = FakeHandoff(fail_stop=fail_stop, fail_start=fail_start)
        self.native = MgbaNativeController(self.proc, self.rom, self.save_dir,
                                           injector=self.injector,
                                           handoff=self.handoff)
        self.session = InGameSession(GAME_A, DEFAULTS, self.cheats,
                                     self.states, self.native)
        return self.session, self.native

    @staticmethod
    def combo(session, *buttons):
        fired = []
        for button in buttons:
            fired.extend(session.press(button))
        for button in buttons:
            session.release(button)
        return fired

    def close_overlay(self):
        """What the renderer does: close the model, then release the display."""
        self.session.close_overlay()
        return self.session.finish_overlay()


class TestOverlayHandoff(HandoffTestCase):
    def test_cheats_overlay_captures_state_then_stops_the_emulator(self):
        session, native = self.build()
        self.combo(session, "keyUp", "keyLeft")
        self.assertIsInstance(session.overlay, CheatsOverlayModel)
        # The frame and the native state were captured while mGBA was running.
        self.assertIn([KEY_F12], self.injector.taps)
        self.assertIn([KEY_LEFTSHIFT, KEY_F1 + 8], self.injector.taps)
        self.assertTrue(os.path.isfile(native.resume_state))
        self.assertTrue(native.last_screenshot)
        # ... and only then was the emulator stopped (never SIGSTOPped).
        self.assertEqual(self.handoff.stops, 1)
        self.assertEqual(self.proc.signals, [])
        self.assertTrue(session.suspended)

    def test_closing_the_overlay_restarts_from_the_captured_state(self):
        session, native = self.build()
        self.combo(session, "keyUp", "keyLeft")
        state = native.resume_state
        self.close_overlay()
        self.assertIsNone(session.overlay)
        self.assertEqual(len(self.handoff.starts), 1)
        started_state, fps = self.handoff.starts[0]
        self.assertEqual(started_state, state)
        self.assertEqual(fps, 60.0)
        self.assertTrue(os.path.isfile(started_state))
        self.assertFalse(session.suspended)
        self.assertTrue(self.handoff.running)

    def test_resume_waits_until_the_overlay_released_the_display(self):
        session, _native = self.build()
        self.combo(session, "keyUp", "keyLeft")
        self.session.close_overlay()          # model closed, window still open
        self.assertTrue(session.resume_pending)
        self.assertEqual(self.handoff.starts, [])
        self.assertTrue(session.finish_overlay())
        self.assertEqual(len(self.handoff.starts), 1)
        self.assertFalse(session.resume_pending)
        self.assertFalse(session.finish_overlay())   # idempotent

    def test_save_states_overlay_round_trip(self):
        session, _native = self.build()
        self.combo(session, "keyLeft", "keyRight")
        self.assertIsInstance(session.overlay, SaveStatesOverlayModel)
        self.assertEqual(self.handoff.stops, 1)
        self.close_overlay()
        self.assertEqual(len(self.handoff.starts), 1)

    def test_overlay_failure_leaves_the_game_playable(self):
        """No state captured -> no overlay, but the game must keep running."""
        session, native = self.build(injector_available=False)
        opened = session.open_overlay(ACTION_CHEATS)
        self.assertFalse(opened)
        self.assertIsNone(session.overlay)
        self.assertEqual(self.handoff.stops, 0)
        self.assertFalse(session.suspended)
        self.assertIn("unavailable", session.status_message.lower())
        self.assertEqual(self.proc.signals, [])

    def test_failed_stop_does_not_freeze_the_emulator(self):
        session, native = self.build(fail_stop=True)
        opened = session.open_overlay(ACTION_CHEATS)
        self.assertFalse(opened)
        self.assertIsNone(session.overlay)
        self.assertEqual(self.handoff.starts, [])   # nothing was stopped
        self.assertFalse(session.suspended)
        self.assertIn("mGBA did not stop", native.last_error)

    def test_display_initialisation_failure_restores_gameplay(self):
        import gba_ingame_ui
        session, _native = self.build()
        self.combo(session, "keyUp", "keyLeft")
        self.assertEqual(self.handoff.starts, [])
        renderer = gba_ingame_ui.InGameOverlayRenderer(
            session, (960, 640), screenshot_path=None, windowed=True)
        with patch.object(gba_ingame_ui.pygame.display, "set_mode",
                          side_effect=RuntimeError("no DRM master")):
            renderer.run()
        self.assertIsNone(session.overlay)
        self.assertEqual(len(self.handoff.starts), 1)
        self.assertIn("no DRM master", session.status_message)
        self.assertFalse(session.suspended)

    def test_release_display_recovers_a_open_overlay(self):
        session, _native = self.build()
        self.combo(session, "keyUp", "keyLeft")
        self.assertTrue(session.release_display())
        self.assertIsNone(session.overlay)
        self.assertFalse(session.suspended)


class TestSaveStatesWithHandoff(HandoffTestCase):
    def test_save_to_an_empty_slot_uses_the_captured_state_and_thumbnail(self):
        session, native = self.build()
        self.combo(session, "keyLeft", "keyRight")
        captured = native.resume_state
        model = session.overlay
        self.assertTrue(model.activate())              # slot 1 is empty
        info = self.states.slots()[0]
        self.assertTrue(info["occupied"] and info["valid"])
        self.assertTrue(info["has_thumbnail"])
        with open(info["state_path"], "rb") as f:
            archived = f.read()
        with open(captured, "rb") as f:
            self.assertEqual(archived, f.read())
        self.assertIn("saved", model.message.lower())

    def test_all_nine_slots_save_overwrite_and_load_without_hotkeys(self):
        session, native = self.build()
        self.combo(session, "keyLeft", "keyRight")
        model = session.overlay
        for slot in range(1, 10):
            model.index = slot - 1
            self.assertTrue(model.activate(), f"slot {slot}")
        slots = self.states.slots()
        self.assertEqual([s["slot"] for s in slots], list(range(1, 10)))
        self.assertTrue(all(s["occupied"] and s["valid"] for s in slots))
        sizes = {os.path.getsize(s["state_path"]) for s in slots}
        self.assertEqual(sizes, {len(b"NATIVE_MGBA_STATE_9")})
        # Every slot is a separate file: no slot aliases another.
        paths = {s["state_path"] for s in slots}
        self.assertEqual(len(paths), 9)

        # Overwrite slot 4 after its confirmation step.
        model.index = 3
        model.activate()                                # actions
        model.choose_action(1)                          # overwrite
        self.assertEqual(model.mode, "confirm")
        self.assertTrue(model.activate())
        with open(self.states.state_path(4), "rb") as f:
            self.assertEqual(f.read(), b"NATIVE_MGBA_STATE_9")

        # Load slot 7: the archived state becomes the resume state.
        model.index = 6
        model.activate()
        self.assertTrue(model.choose_action(0))
        staged = native.resume_state
        with open(staged, "rb") as f:
            self.assertEqual(f.read(), b"NATIVE_MGBA_STATE_9")
        with open(self.states.state_path(7), "rb") as f:
            self.assertEqual(f.read(), b"NATIVE_MGBA_STATE_9")
        # The archived slot was copied, never handed to mGBA directly.
        self.assertNotEqual(staged, self.states.state_path(7))

        self.close_overlay()
        self.assertEqual(self.handoff.starts[-1][0], staged)

    def test_failed_save_keeps_the_existing_slot_intact(self):
        session, native = self.build()
        self.combo(session, "keyLeft", "keyRight")
        model = session.overlay
        model.index = 1
        self.assertTrue(model.activate())               # slot 2 saved
        good = self.states.state_path(2)
        with open(good, "rb") as f:
            before = f.read()

        # A capture failure must not touch the slot that is already good.
        native.resume_state = None
        native.capture_runtime_state = lambda *a, **k: None
        native.save_state = lambda slot: None
        model.index = 1
        model.activate()                                # actions
        model.choose_action(1)
        self.assertFalse(model.activate())              # confirm -> fails
        with open(good, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertTrue(self.states.slots()[1]["valid"])

    def test_failed_load_does_not_resume_from_a_broken_file(self):
        session, native = self.build()
        self.combo(session, "keyLeft", "keyRight")
        model = session.overlay
        self.assertTrue(model.activate())
        with open(self.states.state_path(1), "wb") as f:
            f.write(b"TRUNCATED")
        model.activate()
        self.assertEqual(model.mode, "actions")
        self.assertFalse(model.choose_action(0))
        self.assertIn("corrupt", model.message.lower())
        self.assertEqual(native.resume_state, model and native.resume_state)
        self.close_overlay()
        # Gameplay still restarts from the pre-overlay state, not the garbage.
        self.assertEqual(len(self.handoff.starts), 1)
        with open(self.handoff.starts[0][0], "rb") as f:
            self.assertNotEqual(f.read(), b"TRUNCATED")


class TestCheatsWithHandoff(HandoffTestCase):
    def test_only_the_current_games_cheats_are_shown(self):
        other = CheatStore("80a88361e29f4a77", "other.gba", data_dir=self.root)
        other.add_cheat("Other Game", "AAAA BBBB", enabled=True)
        self.cheats.add_cheat("This Game", "CCCC DDDD")
        session, _native = self.build()
        self.combo(session, "keyUp", "keyLeft")
        model = session.overlay
        self.assertEqual([c["name"] for c in model.items()], ["This Game"])

    def test_toggle_persists_and_is_applied_by_the_restart(self):
        self.cheats.add_cheat("Infinite Lives", "0123 4567")
        session, native = self.build()
        self.combo(session, "keyUp", "keyLeft")
        model = session.overlay
        self.assertTrue(model.toggle())
        self.assertTrue(self.cheats.cheats()[0]["enabled"])
        with open(self.cheats.native_path, "r", encoding="utf-8") as f:
            native_file = f.read()
        self.assertNotIn("!disabled", native_file)
        self.assertIn("Infinite Lives", native_file)
        # Reloaded from disk by the restarted emulator through `-c`.
        self.assertEqual(CheatStore(GAME_A, self.rom,
                                    data_dir=self.root).cheats()[0]["enabled"], True)
        self.close_overlay()
        self.assertTrue(session.cheats_changed)
        self.assertEqual(len(self.handoff.starts), 1)

    def test_toggle_back_off_writes_the_disabled_directive(self):
        self.cheats.add_cheat("Moon Jump", "1111 2222", enabled=True)
        session, _native = self.build()
        self.combo(session, "keyUp", "keyLeft")
        self.assertTrue(session.overlay.toggle())
        with open(self.cheats.native_path, "r", encoding="utf-8") as f:
            self.assertIn("!disabled", f.read())


class TestFastForwardWithHandoff(HandoffTestCase):
    def test_cycle_applies_speed_when_gameplay_restarts(self):
        session, native = self.build()
        expected = [(2, 120.0), (3, 180.0), (1, 60.0)]
        for multiplier, fps in expected:
            self.combo(session, "keyL", "keyR")
            self.assertEqual(session.speed, multiplier)
            self.assertIsNotNone(session.overlay)
            self.assertEqual(self.handoff.stops, expected.index((multiplier, fps)) + 1)
            self.close_overlay()
            state, started_fps = self.handoff.starts[-1]
            self.assertEqual(started_fps, fps)
            self.assertTrue(os.path.isfile(state))
            self.assertEqual(native.fps_target, fps)

    def test_holding_the_combo_never_cycles_twice(self):
        session, _native = self.build()
        session.press("keyL")
        session.press("keyR")
        self.assertEqual(session.speed, 2)
        stops = self.handoff.stops
        for _ in range(200):
            self.assertEqual(session.poll(), [])
        self.assertEqual(session.speed, 2)
        self.assertEqual(self.handoff.stops, stops)


class TestInputMonitorLiveness(unittest.TestCase):
    def test_monitor_survives_the_handoff_stop(self):
        """The closing combo must still arrive while mGBA is stopped."""
        proc = DummyProc()
        proc.returncode = 0                    # the emulator was stopped
        alive = {"value": True}
        monitor = InputMonitor(proc, shortcuts=DEFAULTS,
                               alive_check=lambda: alive["value"])
        self.assertTrue(monitor.alive())
        alive["value"] = False
        self.assertFalse(monitor.alive())
        monitor.stop()

    def test_exit_gesture_is_reported_to_the_runner(self):
        proc = DummyProc()
        requested = []
        monitor = InputMonitor(proc, on_exit_requested=lambda: requested.append(True))
        monitor._trigger("test")
        self.assertEqual(requested, [True])
        self.assertEqual(proc.returncode, 0)
        monitor.stop()


class FakeChild:
    """A Popen lookalike used to drive the runner without starting mGBA."""

    instances = []

    def __init__(self, cmd, **kwargs):
        self.cmd = list(cmd)
        self.stdout = None
        self.returncode = None
        self.pid = 1000 + len(FakeChild.instances)
        self.signals = []
        FakeChild.instances.append(self)

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def kill(self):
        self.returncode = -signal.SIGKILL

    def send_signal(self, sig):
        self.signals.append(sig)


class TestRunnerHandoff(unittest.TestCase):
    """The runner is the only process owner; check it never orphans mGBA."""

    def setUp(self):
        FakeChild.instances = []
        self.root = tempfile.mkdtemp()
        self.rom = os.path.join(self.root, "game.gba")
        with open(self.rom, "wb") as f:
            f.write(b"ROM")
        self.commands = []

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def runner(self, **kwargs):
        def build_command(fps_target=None, savestate=None, **_kwargs):
            self.commands.append((fps_target, savestate))
            return ["mgba", self.rom]

        return MgbaSessionRunner(
            ["mgba", self.rom], {}, build_command=build_command, **kwargs)

    def test_stop_and_start_round_trip(self):
        runner = self.runner()
        with patch("gba_adapter_mgba.subprocess.Popen", FakeChild), \
                patch("gba_adapter_mgba.InputMonitor.start", lambda self: None), \
                patch("gba_adapter_mgba.threading.Thread.start", lambda self: None):
            self.assertTrue(runner.start_emulator(None, 60.0))
            first = runner.child
            self.assertTrue(runner.stop_emulator())
            self.assertIsNotNone(first.poll())
            state = os.path.join(self.root, "state.ss")
            with open(state, "wb") as f:
                f.write(b"STATE")
            self.assertTrue(runner.start_emulator(state, 120.0))
            self.assertIsNot(runner.child, first)
            self.assertEqual(self.commands[-1], (120.0, state))
            runner.shutting_down = True
            self.assertFalse(runner.start_emulator(state, 60.0))

    def test_overlay_is_served_between_the_stop_and_the_restart(self):
        """End-to-end ordering: stop mGBA -> render -> restart from state."""
        import queue as queue_module

        import gba_ingame_ui

        self.save_dir = os.path.join(self.root, "saves", GAME_A)
        runtime = os.path.join(self.save_dir, "runtime")
        os.makedirs(runtime, exist_ok=True)
        cheats = CheatStore(GAME_A, self.rom, data_dir=self.root)
        states = SaveStateStore(GAME_A, data_dir=self.root)
        injector = FakeInjector(runtime)
        native = MgbaNativeController(DummyProc(), self.rom, self.save_dir,
                                      injector=injector)
        session = InGameSession(GAME_A, DEFAULTS, cheats, states, native)
        actions = queue_module.Queue()
        runner = self.runner(session=session, action_queue=actions, native=native)
        self.assertIs(native.handoff, runner)

        seen = {}

        class StubRenderer:
            def __init__(self, session_, *args, **kwargs):
                self.session = session_

            def run(self):
                seen["stopped_while_rendering"] = runner.child.poll() is not None
                seen["overlay"] = type(self.session.overlay).__name__
                self.session.overlay.close()
                self.session.finish_overlay()

        with patch("gba_adapter_mgba.subprocess.Popen", FakeChild), \
                patch("gba_adapter_mgba.InputMonitor.start", lambda self: None), \
                patch("gba_adapter_mgba.threading.Thread.start", lambda self: None), \
                patch.object(gba_ingame_ui, "InGameOverlayRenderer", StubRenderer):
            self.assertTrue(runner.start_emulator(None, 60.0))
            first_child = runner.child
            session.handle_action(ACTION_CHEATS)
            runner._serve_overlay()
            second_child = runner.child

        self.assertTrue(seen["stopped_while_rendering"])
        self.assertEqual(seen["overlay"], "CheatsOverlayModel")
        self.assertIsNot(first_child, second_child)
        self.assertIsNotNone(first_child.poll())
        self.assertIsNone(second_child.poll())
        self.assertEqual(self.commands[-1][0], 60.0)
        self.assertTrue(os.path.isfile(self.commands[-1][1]))

    def test_run_terminates_the_child_and_stops_the_monitor(self):
        runner = self.runner()

        class OnceOnly(FakeChild):
            def __init__(self, cmd, **kwargs):
                super().__init__(cmd, **kwargs)
                self.polls = 0

            def poll(self):
                self.polls += 1
                if self.polls > 3:
                    self.returncode = 0
                return self.returncode

        with patch("gba_adapter_mgba.subprocess.Popen", OnceOnly), \
                patch("gba_adapter_mgba.InputMonitor.start", lambda self: None), \
                patch("gba_adapter_mgba.threading.Thread.start", lambda self: None):
            ret = runner.run()
        self.assertEqual(ret, 0)
        self.assertIsNotNone(runner.child.poll())
        self.assertTrue(runner.shutting_down)


if __name__ == "__main__":
    unittest.main()
