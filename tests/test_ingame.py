"""In-game shortcuts, fast-forward cycle, cheats and save-state overlays."""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import config as settings_store
from game_data import CheatStore, SaveStateStore
from gba_ingame import (
    ACTION_CHEATS, ACTION_FAST_FORWARD, ACTION_SAVE_STATES,
    CheatsOverlayModel, ComboRouter, FastForwardController, InGameBackend,
    InGameSession, IndicatorOverlayModel, SaveStatesOverlayModel,
    button_from_key, button_from_joy_button, buttons_from_hat,
)

GAME_A = "44636205c3e1a1b2"
DEFAULTS = settings_store.load_shortcuts(dict(settings_store.DEFAULT_SETTINGS))


class FakeBackend(InGameBackend):
    """Records every native operation without touching mGBA."""

    def __init__(self, states=None, fail_load=False):
        super().__init__()
        self.paused = False
        self.speeds = []
        self.saved = []
        self.loaded = []
        self.states = states or {}
        self.fail_load = fail_load
        self.alive = True
        self.terminated = False

    def pause(self):
        self.paused = True
        return True

    def resume(self):
        self.paused = False
        return True

    def set_speed(self, multiplier, fps_target):
        self.speeds.append((multiplier, fps_target))
        return False

    def save_state(self, slot):
        self.saved.append(slot)
        return self.states.get(slot, f"NATIVE_STATE_{slot}".encode())

    def capture_screenshot(self, slot):
        return f"NATIVE_PNG_{slot}".encode()

    def load_state(self, slot, state_path):
        self.loaded.append((slot, state_path))
        return not self.fail_load


class SessionTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.data_dir = os.path.join(self.root, "data")
        os.makedirs(self.data_dir, exist_ok=True)
        self.cheats = CheatStore(GAME_A, "game.gba", data_dir=self.data_dir)
        self.states = SaveStateStore(GAME_A, data_dir=self.data_dir)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def session(self, shortcuts=None, backend=None, states=None, clock=None):
        backend = backend or FakeBackend(states)
        kwargs = {"clock": clock} if clock else {}
        return InGameSession(GAME_A, shortcuts or DEFAULTS, self.cheats,
                             self.states, backend, **kwargs), backend

    def combo(self, session, *buttons):
        fired = []
        for button in buttons:
            fired.extend(session.press(button))
        return fired


class TestCheatsOverlay(SessionTestCase):
    def test_cheats_belong_only_to_the_running_game(self):
        other = CheatStore("80a88361e29f4a77", "game.gba", data_dir=self.data_dir)
        other.add_cheat("Other Game Cheat", "AAAA BBBB", enabled=True)
        self.cheats.add_cheat("This Game Cheat", "CCCC DDDD")
        model = CheatsOverlayModel(self.cheats)
        self.assertEqual(len(model.items()), 1)
        self.assertEqual(model.items()[0]["name"], "This Game Cheat")
        self.assertNotIn("Other Game Cheat", str(model.items()))

    def test_enable_disable_persists(self):
        cid = self.cheats.add_cheat("Infinite Lives", "0123 4567")
        model = CheatsOverlayModel(self.cheats)
        self.assertFalse(model.items()[0]["enabled"])
        self.assertTrue(model.toggle())
        self.assertTrue(model.items()[0]["enabled"])
        self.assertTrue(CheatStore(GAME_A, "game.gba",
                                   data_dir=self.data_dir).cheats()[0]["enabled"])
        self.assertTrue(model.toggle())
        self.assertFalse(CheatStore(GAME_A, "game.gba",
                                    data_dir=self.data_dir).cheats()[0]["enabled"])
        self.assertIn("!disabled", open(self.cheats.native_path).read())

    def test_opening_and_closing_does_not_terminate_the_emulator(self):
        session, backend = self.session()
        self.combo(session, "keyUp", "keyLeft")
        self.assertIsInstance(session.overlay, CheatsOverlayModel)
        self.assertTrue(backend.paused)
        self.assertTrue(backend.alive)
        self.assertFalse(backend.terminated)
        session.close_overlay()
        self.assertIsNone(session.overlay)
        self.assertFalse(backend.paused)
        self.assertTrue(backend.alive)
        self.assertFalse(backend.terminated)
        self.assertFalse(session.emulator_terminated)

    def test_same_combo_closes_the_overlay(self):
        session, backend = self.session()
        self.combo(session, "keyUp", "keyLeft")
        self.assertIsNotNone(session.overlay)
        for button in ("keyUp", "keyLeft"):
            session.release(button)
        self.combo(session, "keyUp", "keyLeft")
        self.assertIsNone(session.overlay)
        self.assertFalse(backend.paused)

    def test_empty_cheat_list_is_explicit(self):
        model = CheatsOverlayModel(self.cheats)
        self.assertEqual(model.items(), [])
        self.assertFalse(model.toggle())
        self.assertIn("No cheats", model.message)

    def test_add_cheat_from_overlay_is_enabled_and_persisted(self):
        model = CheatsOverlayModel(self.cheats)
        self.assertTrue(model.activate())       # selected Add Cheat on empty list
        self.assertEqual(model.mode, "edit")
        model.insert_text("Infinite Lives")
        model.switch_field(1)
        model.insert_text("01234567 89abcdef\nCAFEBABE 00000001")
        self.assertTrue(model.save_draft())
        self.assertEqual(model.mode, "list")
        item = self.cheats.cheats()[0]
        self.assertEqual(item["name"], "Infinite Lives")
        self.assertEqual(item["code"], "01234567 89ABCDEF\nCAFEBABE 00000001")
        self.assertTrue(item["enabled"])
        with open(self.cheats.native_path, encoding="utf-8") as native:
            payload = native.read()
        self.assertIn("# Infinite Lives", payload)
        self.assertNotIn("!disabled", payload)

    def test_editor_requires_name_and_safe_nonempty_code(self):
        model = CheatsOverlayModel(self.cheats)
        model.begin_add()
        self.assertFalse(model.save_draft())
        self.assertIn("name", model.message.lower())
        model.insert_text("Bad directive")
        model.switch_field(1)
        model.insert_text("!reset")
        self.assertFalse(model.save_draft())
        self.assertEqual(self.cheats.cheats(), [])
        self.assertIn("cannot start", model.message)

    def test_edit_and_delete_flow_preserves_state_and_requires_confirmation(self):
        cid = self.cheats.add_cheat("Moon Jump", "DEADBEEF 00000001", enabled=True)
        model = CheatsOverlayModel(self.cheats)
        model.edit_selected()
        model.draft_name = "Moon Jump High"
        self.assertTrue(model.save_draft())
        self.assertEqual(self.cheats.cheats()[0]["name"], "Moon Jump High")
        self.assertTrue(self.cheats.cheats()[0]["enabled"])
        model.request_delete()
        self.assertEqual(model.mode, "delete")
        model.cancel()                          # B keeps the cheat
        self.assertEqual(len(self.cheats.cheats()), 1)
        model.request_delete()
        self.assertTrue(model.confirm_delete())
        self.assertEqual(self.cheats.cheats(), [])
        self.assertFalse(os.path.exists(self.cheats.native_path))

    def test_add_row_is_included_in_controller_navigation(self):
        self.cheats.add_cheat("One", "A")
        model = CheatsOverlayModel(self.cheats)
        model.move(1)
        self.assertIsNone(model.selected_item())
        self.assertTrue(model.activate())
        self.assertEqual(model.mode, "edit")
        model.cancel()
        model.move(1)                           # wraps from Add to first cheat
        self.assertEqual(model.selected_item()["name"], "One")


class NoOverlayBackend(FakeBackend):
    """A backend that cannot take the display for an overlay (no injection)."""

    def suspend_for_overlay(self, kind=""):
        return False


class TestFastForward(SessionTestCase):
    def test_no_badge_falls_back_to_a_speed_reload(self):
        """A speed change must still reach the game when no overlay can open."""
        session, backend = self.session(backend=NoOverlayBackend())
        self.combo(session, "keyL", "keyR")
        self.assertEqual(session.speed, 2)
        self.assertIsNone(session.overlay)          # nothing was drawn
        self.assertTrue(backend.alive)              # and the game is still up
        self.assertFalse(backend.terminated)
        # The runner is asked to reload the emulator instead, and the model is
        # not claiming a speed the emulator never got.
        self.assertTrue(session.take_relaunch())
        self.assertFalse(session.take_relaunch())
        self.assertEqual(session.indicator, "2x")

    def test_a_failed_reload_returns_the_model_to_normal(self):
        """`_restart_for_speed` failure is reported by resetting the cycle."""
        session, backend = self.session(backend=NoOverlayBackend())
        self.combo(session, "keyL", "keyR")
        session.take_relaunch()
        # This is what the runner calls when the reload cannot capture a state.
        session.note_speed_reload_failed()
        self.assertEqual(session.speed, 1)
        self.assertEqual(session.indicator, "Normal")
        self.assertIsNone(session.overlay)
    def test_cycle_is_exactly_1x_2x_3x_1x(self):
        controller = FastForwardController()
        self.assertEqual(controller.multiplier, 1)
        self.assertEqual(controller.label, "Normal")
        self.assertEqual(controller.fps_target, 60.0)
        self.assertEqual(controller.activate(), 2)
        self.assertEqual(controller.label, "2x")
        self.assertEqual(controller.fps_target, 120.0)
        self.assertEqual(controller.activate(), 3)
        self.assertEqual(controller.label, "3x")
        self.assertEqual(controller.fps_target, 180.0)
        self.assertEqual(controller.activate(), 1)
        self.assertEqual(controller.label, "Normal")
        self.assertEqual(controller.activate(), 2)   # then repeat

    def test_default_combo_cycles_once_per_press(self):
        session, backend = self.session()
        self.assertEqual(session.speed, 1)
        self.combo(session, "keyL", "keyR")
        self.assertEqual(session.speed, 2)
        self.assertEqual(session.indicator, "2x")
        self.assertEqual(backend.speeds, [(2, 120.0)])
        for _ in range(120):                    # held for 120 frames
            self.assertEqual(session.poll(), [])
        self.assertEqual(session.speed, 2)
        for button in ("keyL", "keyR"):
            session.release(button)
        self.combo(session, "keyL", "keyR")
        self.assertEqual(session.speed, 3)
        self.assertEqual(session.indicator, "3x")
        for button in ("keyL", "keyR"):
            session.release(button)
        self.combo(session, "keyL", "keyR")
        self.assertEqual(session.speed, 1)
        self.assertEqual(session.indicator, "Normal")
        self.assertEqual([m for m, _ in backend.speeds], [2, 3, 1])

    def test_speed_indicator_is_a_transient_non_interactive_badge(self):
        clock = [0.0]
        session, backend = self.session(clock=lambda: clock[0])
        self.combo(session, "keyL", "keyR")
        self.assertIsInstance(session.overlay, IndicatorOverlayModel)
        self.assertTrue(backend.paused)       # game paused behind the badge
        self.assertFalse(session.overlay.expired())
        clock[0] = 10.0
        self.assertTrue(session.overlay.expired())
        session.close_overlay()
        self.assertIsNone(session.overlay)
        self.assertFalse(backend.paused)      # same emulator process resumed
        self.assertFalse(session.emulator_terminated)

    def test_a_new_shortcut_is_not_swallowed_by_a_previous_badge(self):
        session, _ = self.session()
        self.combo(session, "keyL", "keyR")
        self.assertIsInstance(session.overlay, IndicatorOverlayModel)
        for button in ("keyL", "keyR"):
            session.release(button)
        self.combo(session, "keyL", "keyR")
        self.assertEqual(session.speed, 3)
        self.assertIsInstance(session.overlay, IndicatorOverlayModel)

    def test_holding_combo_does_not_repeatedly_cycle(self):
        router = ComboRouter(DEFAULTS)
        self.assertEqual(router.press("keyL"), [])
        self.assertEqual(router.press("keyR"), [ACTION_FAST_FORWARD])
        self.assertEqual(router.press("keyL"), [])       # repeat keydown ignored
        for _ in range(1000):
            self.assertEqual(router.poll(), [])
        self.assertEqual(router.fire_count[ACTION_FAST_FORWARD], 1)
        self.assertTrue(router.is_consumed("keyL"))
        self.assertTrue(router.is_consumed("keyR"))
        router.release("keyL")
        self.assertTrue(router.is_consumed("keyR"))
        router.release("keyR")
        self.assertEqual(router.consumed_buttons(), set())

    def test_configured_shortcut_is_respected(self):
        custom = dict(DEFAULTS)
        custom[ACTION_FAST_FORWARD] = ("keyA", "keyB")
        session, backend = self.session(shortcuts=custom)
        self.combo(session, "keyL", "keyR")           # default no longer bound
        self.assertEqual(session.speed, 1)
        for button in ("keyL", "keyR"):
            session.release(button)
        self.combo(session, "keyA", "keyB")
        self.assertEqual(session.speed, 2)
        self.assertEqual(backend.speeds[-1], (2, 120.0))

    def test_configured_overlay_shortcuts_are_respected(self):
        custom = dict(DEFAULTS)
        custom[ACTION_CHEATS] = ("keyStart", "keyA")
        custom[ACTION_SAVE_STATES] = ("keyDown", "keyB")
        session, _ = self.session(shortcuts=custom)
        self.combo(session, "keyUp", "keyLeft")
        self.assertIsNone(session.overlay)            # old default does nothing
        for button in ("keyUp", "keyLeft"):
            session.release(button)
        self.combo(session, "keyStart", "keyA")
        self.assertIsInstance(session.overlay, CheatsOverlayModel)
        session.close_overlay()
        for button in ("keyStart", "keyA"):
            session.release(button)
        self.combo(session, "keyDown", "keyB")
        self.assertIsInstance(session.overlay, SaveStatesOverlayModel)


class TestSaveStatesOverlay(SessionTestCase):
    def test_nine_slots_and_empty_detection(self):
        model = SaveStatesOverlayModel(self.states, FakeBackend())
        self.assertEqual(len(model.slots()), 9)
        self.assertTrue(all(not slot["occupied"] for slot in model.slots()))
        self.assertTrue(all(slot["slot"] == index + 1
                            for index, slot in enumerate(model.slots())))

    def test_empty_slot_directly_saves_state_and_thumbnail(self):
        backend = FakeBackend()
        model = SaveStatesOverlayModel(self.states, backend)
        self.assertTrue(model.activate())
        self.assertEqual(backend.saved, [1])
        info = self.states.slots()[0]
        self.assertTrue(info["occupied"])
        self.assertTrue(info["valid"])
        self.assertTrue(info["has_thumbnail"])
        with open(info["state_path"], "rb") as f:
            self.assertEqual(f.read(), b"NATIVE_STATE_1")
        with open(info["thumbnail_path"], "rb") as f:
            self.assertEqual(f.read(), b"NATIVE_PNG_1")

    def test_occupied_slot_offers_load_and_overwrite(self):
        model = SaveStatesOverlayModel(self.states, FakeBackend())
        model.activate()                              # slot 1 becomes occupied
        model.activate()                              # now show the actions
        self.assertEqual(model.mode, "actions")
        self.assertEqual(model.ACTIONS, ("Load Save", "Overwrite Save"))

    def test_load_restores_the_selected_slot(self):
        backend = FakeBackend()
        model = SaveStatesOverlayModel(self.states, backend)
        model.activate()                              # save slot 1
        self.states.save(4, b"SLOT_FOUR_STATE", b"SLOT_FOUR_PNG")
        model.index = 3
        model.activate()                              # actions for slot 4
        self.assertEqual(model.mode, "actions")
        self.assertTrue(model.choose_action(0))
        self.assertEqual(backend.loaded[0][0], 4)
        with open(backend.loaded[0][1], "rb") as f:
            self.assertEqual(f.read(), b"SLOT_FOUR_STATE")
        self.assertTrue(model.closed)                 # straight back to gameplay

    def test_overwrite_requires_confirmation_and_replaces_both(self):
        class SequenceBackend(FakeBackend):
            def save_state(self, slot):
                self.saved.append(slot)
                return f"STATE_CAPTURE_{len(self.saved)}".encode()

        backend = SequenceBackend()
        model = SaveStatesOverlayModel(self.states, backend)
        model.index = 1
        model.activate()                              # empty slot 2 -> saved
        first_state = self.states.slots()[1]["state_path"]
        model.activate()                              # actions
        self.assertTrue(model.choose_action(1))
        self.assertEqual(model.mode, "confirm")
        self.assertEqual(backend.saved, [2])         # no overwrite yet
        self.assertTrue(model.activate())             # confirm
        info = self.states.slots()[1]
        with open(info["state_path"], "rb") as f:
            self.assertEqual(f.read(), b"STATE_CAPTURE_2")
        with open(info["thumbnail_path"], "rb") as f:
            self.assertEqual(f.read(), b"NATIVE_PNG_2")
        self.assertEqual(info["state_path"], first_state)
        self.assertTrue(info["valid"])
        self.assertTrue(info["has_thumbnail"])

    def test_corrupt_or_missing_state_fails_gracefully(self):
        model = SaveStatesOverlayModel(self.states, FakeBackend())
        model.activate()
        with open(self.states.state_path(1), "wb") as f:
            f.write(b"TRASHED")
        model.activate()
        self.assertEqual(model.mode, "actions")
        self.assertFalse(model.choose_action(0))
        self.assertIn("corrupt", model.message.lower())
        self.assertEqual(model.mode, "grid")
        self.assertFalse(model.closed)
        os.remove(self.states.state_path(1))
        model.activate()                              # now treated as empty
        self.assertEqual(model.mode, "grid")

    def test_failed_native_save_and_load_are_not_fatal(self):
        class Failing(FakeBackend):
            def save_state(self, slot):
                return None

            def load_state(self, slot, state_path):
                return False

        model = SaveStatesOverlayModel(self.states, Failing())
        self.assertFalse(model.activate())
        self.assertIn("could not", model.message.lower())
        self.assertTrue(self.states.is_empty(1))
        self.states.save(1, b"STATE", b"PNG")
        model.mode = "actions"
        model.action_index = 0
        self.assertFalse(model.choose_action())
        self.assertIn("failed", model.message.lower())


class TestInputMapping(SessionTestCase):
    def test_default_combos_fire_once_per_press(self):
        router = ComboRouter(DEFAULTS)
        self.assertEqual(router.press("keyUp"), [])
        self.assertEqual(router.press("keyLeft"), [ACTION_CHEATS])
        for _ in range(10):
            self.assertEqual(router.poll(), [])
        router.release("keyUp")
        router.release("keyLeft")
        self.assertEqual(router.press("keyLeft"), [])
        self.assertEqual(router.press("keyRight"), [ACTION_SAVE_STATES])
        router.release("keyLeft")
        router.release("keyRight")
        self.assertEqual(router.press("keyL"), [])
        self.assertEqual(router.press("keyR"), [ACTION_FAST_FORWARD])
        self.assertEqual({a: router.fire_count[a] for a in
                          settings_store.SHORTCUT_ACTIONS},
                         {ACTION_CHEATS: 1, ACTION_FAST_FORWARD: 1,
                          ACTION_SAVE_STATES: 1})

    def test_default_shortcuts_work_without_configuration(self):
        self.assertEqual(DEFAULTS[ACTION_CHEATS], ("keyUp", "keyLeft"))
        self.assertEqual(DEFAULTS[ACTION_FAST_FORWARD], ("keyL", "keyR"))
        self.assertEqual(DEFAULTS[ACTION_SAVE_STATES], ("keyLeft", "keyRight"))
        self.assertEqual(settings_store.shortcut_label(DEFAULTS[ACTION_CHEATS]),
                         "UP + LEFT")
        self.assertEqual(settings_store.shortcut_label(
            DEFAULTS[ACTION_FAST_FORWARD]), "L + R")

    def test_invalid_configured_combo_falls_back_to_default(self):
        shortcuts = dict(DEFAULTS, fast_forward=("keyL", "keyL"))
        router = ComboRouter(shortcuts)
        self.assertEqual(router.shortcuts[ACTION_FAST_FORWARD], ("keyL", "keyR"))

    def test_default_keyboard_profile_is_z_a_x_b(self):
        """Z is A and X is B in the shipped profile (the old pair was reversed)."""
        import common
        bindings = dict(common.DEFAULT_KEYBINDINGS)
        self.assertEqual(button_from_key(122, bindings), "keyA")   # Z
        self.assertEqual(button_from_key(120, bindings), "keyB")   # X
        self.assertNotEqual(button_from_key(120, bindings), "keyA")

    def test_keyboard_and_controller_map_to_the_same_buttons(self):
        bindings = {"keyUp": 1073741906, "keyL": 97, "keyR": 115}
        self.assertEqual(button_from_key(1073741906, bindings), "keyUp")
        self.assertEqual(button_from_key(97, bindings), "keyL")
        self.assertIsNone(button_from_key(999, bindings))
        self.assertEqual(button_from_joy_button(4), "keyL")
        self.assertEqual(button_from_joy_button(5), "keyR")
        self.assertEqual(buttons_from_hat((-1, 1)), {"keyLeft", "keyUp"})

    def test_combo_buttons_are_consumed_but_normal_input_is_not(self):
        router = ComboRouter(DEFAULTS)
        router.press("keyA")
        self.assertEqual(router.consumed_buttons(), set())
        self.assertFalse(router.is_consumed("keyA"))
        router.press("keyL")
        router.press("keyR")
        self.assertEqual(router.consumed_buttons(), {"keyL", "keyR"})
        self.assertFalse(router.is_consumed("keyA"))
        router.release("keyL")
        router.release("keyR")
        self.assertEqual(router.consumed_buttons(), set())

    def test_latched_combo_cannot_trigger_a_second_action(self):
        router = ComboRouter(DEFAULTS)
        router.press("keyLeft")
        router.press("keyRight")                   # save states latch
        self.assertEqual(router.press("keyUp"), [])  # no cheats action leaks
        self.assertEqual(router.fire_count[ACTION_CHEATS], 0)


class TestShortcutSettings(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self._orig = settings_store.get_data_dir
        settings_store.get_data_dir = lambda: self.root

    def tearDown(self):
        settings_store.get_data_dir = self._orig
        shutil.rmtree(self.root, ignore_errors=True)

    def test_shortcuts_are_part_of_the_settings_store(self):
        settings = settings_store.load_settings()
        self.assertEqual(settings["shortcut_cheats"], ["keyUp", "keyLeft"])
        self.assertEqual(settings["shortcut_fast_forward"], ["keyL", "keyR"])
        self.assertEqual(settings["shortcut_save_states"], ["keyLeft", "keyRight"])

    def test_shortcut_changes_persist_through_settings_json(self):
        updated = settings_store.save_shortcuts(
            {ACTION_FAST_FORWARD: ["keyA", "keyB"]},
            settings_store.load_settings())
        self.assertEqual(updated["shortcut_fast_forward"], ["keyA", "keyB"])
        reloaded = settings_store.load_shortcuts()
        self.assertEqual(reloaded[ACTION_FAST_FORWARD], ("keyA", "keyB"))
        self.assertEqual(reloaded[ACTION_CHEATS], ("keyUp", "keyLeft"))
        with open(settings_store.settings_path()) as f:
            self.assertIn("shortcut_fast_forward", f.read())

    def test_broken_shortcut_values_are_sanitised(self):
        path = settings_store.settings_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        import json
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"shortcut_cheats": ["keyUp"],
                       "shortcut_fast_forward": "keyA+keyB",
                       "shortcut_save_states": 42}, f)
        shortcuts = settings_store.load_shortcuts()
        self.assertEqual(shortcuts[ACTION_CHEATS], ("keyUp", "keyLeft"))
        self.assertEqual(shortcuts[ACTION_FAST_FORWARD], ("keyA", "keyB"))
        self.assertEqual(shortcuts[ACTION_SAVE_STATES], ("keyLeft", "keyRight"))


if __name__ == "__main__":
    unittest.main()
