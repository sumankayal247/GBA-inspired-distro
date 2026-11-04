"""
In-game feature logic for the mGBA handler (no display dependency).

This module is deliberately pure Python so the behaviour of the three
configurable shortcuts, the fast-forward cycle, and the Cheats / Save States
overlays can be tested headlessly.  Rendering lives in `gba_ingame_ui.py` and
the mGBA-native operations live in the handler (`gba_adapter_mgba.py`).

Design
------
* Shortcuts are *actions* (`cheats`, `fast_forward`, `save_states`) mapped to
  configurable two-button combos by `config.load_shortcuts()`.  No key check
  anywhere in this module is hard-coded to a particular combo.
* `ComboRouter` is edge-triggered: a combo fires once when its last button is
  pressed and stays latched until the buttons are released.  Holding it for
  hundreds of frames cannot cycle speed repeatedly.
* While a combo is latched its buttons are "consumed", so the handler can
  neutralise them instead of letting them leak into gameplay.
* Overlays are models owned by `InGameSession`; opening one performs a
  display handoff (capture a native state + frame, stop mGBA, let the overlay
  own the screen) and closing one restarts mGBA from that captured state.
  Nothing here ever leaves a stopped emulator behind: a handoff that cannot be
  completed resumes gameplay instead.
"""

import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import common
import config as settings_store

ACTION_CHEATS = settings_store.ACTION_CHEATS
ACTION_FAST_FORWARD = settings_store.ACTION_FAST_FORWARD
ACTION_SAVE_STATES = settings_store.ACTION_SAVE_STATES
SHORTCUT_ACTIONS = settings_store.SHORTCUT_ACTIONS
SLOT_COUNT = 9

# Native GBA frame rate; mGBA's `fpsTarget` config option is the speed knob the
# handler uses (60 = 1x, 120 = 2x, 180 = 3x).
BASE_FPS = 60.0

# Typical SDL / evdev gamepad button numbers.  The D-pad usually arrives as a
# hat or as axes; `buttons_from_hat` / `button_from_axis` cover those.
JOY_BUTTON_TO_GBA = {
    0: "keyA", 1: "keyB", 2: "keyA", 3: "keyB",
    4: "keyL", 5: "keyR", 6: "keySelect", 7: "keyStart",
    8: "keySelect", 9: "keyStart",
}


# ---------------------------------------------------------------------------
# Input mapping (controller first, keyboard fallback via the shared profile)
# ---------------------------------------------------------------------------

def button_from_key(keycode: int, bindings: Optional[Dict[str, int]] = None) -> Optional[str]:
    """Maps a pygame/SDL key code onto a GBA button id using profile I3."""
    bindings = bindings or common.load_keybindings()
    for button_id, code in bindings.items():
        if code == keycode:
            return button_id
    return None


def button_from_joy_button(number: int) -> Optional[str]:
    return JOY_BUTTON_TO_GBA.get(number)


def buttons_from_hat(value: Sequence[int]) -> Set[str]:
    """pygame/SDL hat (dx, dy) -> active D-pad buttons."""
    try:
        dx, dy = int(value[0]), int(value[1])
    except (TypeError, ValueError, IndexError):
        return set()
    result = set()
    if dx < 0:
        result.add("keyLeft")
    elif dx > 0:
        result.add("keyRight")
    if dy > 0:
        result.add("keyUp")
    elif dy < 0:
        result.add("keyDown")
    return result


def button_from_axis(axis: int, value: float, deadzone: float = 0.6) -> Optional[str]:
    """Raw joystick axis -> D-pad button (or None inside the deadzone)."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if abs(value) < deadzone:
        return None
    if axis == 0:
        return "keyLeft" if value < 0 else "keyRight"
    if axis == 1:
        return "keyUp" if value < 0 else "keyDown"
    return None


# ---------------------------------------------------------------------------
# Combination detection
# ---------------------------------------------------------------------------

class ComboRouter:
    """Edge-triggered router for configurable two-button actions.

    ``press``/``release`` are fed from either pygame events or the handler's
    raw `/dev/input` watcher.  ``poll`` exists for callers that run a frame
    loop: it always returns no actions, because a held combination must never
    fire again until it has been released.
    """

    def __init__(self, shortcuts: Optional[Dict[str, Iterable[str]]] = None):
        self.set_shortcuts(shortcuts)

    def set_shortcuts(self, shortcuts: Optional[Dict[str, Iterable[str]]] = None) -> None:
        active = settings_store.load_shortcuts() if shortcuts is None else shortcuts
        self.combos: Dict[str, Tuple[str, ...]] = {}
        for action in SHORTCUT_ACTIONS:
            combo = active.get(action) if isinstance(active, dict) else None
            default = settings_store.DEFAULT_SHORTCUTS[
                settings_store.SHORTCUT_SETTING_FOR_ACTION[action]]
            valid = settings_store.normalise_shortcut(combo, default)
            self.combos[action] = tuple(valid)
        self.reset()

    def reset(self) -> None:
        self.held: Set[str] = set()
        self.latched: Set[str] = set()
        self.consumed: Set[str] = set()
        self.fire_count: Dict[str, int] = {action: 0 for action in SHORTCUT_ACTIONS}

    @property
    def shortcuts(self) -> Dict[str, Tuple[str, ...]]:
        return dict(self.combos)

    def press(self, button: Optional[str]) -> List[str]:
        if not button or button in self.held:
            return []
        self.held.add(button)
        # A latched combination owns the press: no second action (and no
        # unrelated gameplay action) can be produced while it is held.
        if self.latched:
            return []
        for action in SHORTCUT_ACTIONS:
            combo = self.combos[action]
            if button in combo and all(part in self.held for part in combo):
                self.latched.add(action)
                self.consumed.update(combo)
                self.fire_count[action] += 1
                return [action]
        return []

    def release(self, button: Optional[str]) -> None:
        if not button:
            return
        self.held.discard(button)
        for action in list(self.latched):
            if not any(part in self.held for part in self.combos[action]):
                self.latched.discard(action)
        self.consumed = {part for action in self.latched
                         for part in self.combos[action]}

    def poll(self) -> List[str]:
        """Frame tick: never re-fires a held combination."""
        return []

    def is_consumed(self, button: str) -> bool:
        return button in self.consumed

    def consumed_buttons(self) -> Set[str]:
        return set(self.consumed)


# ---------------------------------------------------------------------------
# Fast-forward state machine
# ---------------------------------------------------------------------------

class FastForwardController:
    """1x -> 2x -> 3x -> 1x, one step per deliberate activation."""

    SPEEDS = (1, 2, 3)

    def __init__(self, speeds: Sequence[int] = SPEEDS, base_fps: float = BASE_FPS):
        self.speeds = tuple(speeds)
        self.base_fps = base_fps
        self.index = 0

    @property
    def multiplier(self) -> int:
        return self.speeds[self.index]

    @property
    def fps_target(self) -> float:
        """mGBA's native `fpsTarget` value for the current speed."""
        return self.base_fps * self.multiplier

    @property
    def label(self) -> str:
        return "Normal" if self.multiplier == 1 else f"{self.multiplier}x"

    def activate(self) -> int:
        self.index = (self.index + 1) % len(self.speeds)
        return self.multiplier

    def reset(self) -> None:
        self.index = 0


# ---------------------------------------------------------------------------
# Backend protocol + overlay models
# ---------------------------------------------------------------------------

class InGameBackend:
    """mGBA-native operations. The handler subclasses/duck-types this.

    There are two ways an overlay can get exclusive use of the screen:

    * **display handoff** (`supports_display_handoff = True`): the handler
      captures a native state plus a gameplay screenshot *while mGBA is still
      running*, stops mGBA so it releases SDL/KMS/DRM, and only then lets the
      overlay renderer open a window. Closing the overlay restarts mGBA from
      the captured state. This is what the real handler does.
    * **pause/resume** (`pause`/`resume`): the historical SIGSTOP/SIGCONT
      behaviour, kept only as a fallback for backends (and tests) that do not
      own a process. It cannot share a DRM/KMS display, so overlays may fail
      to appear; the session then resumes immediately instead of freezing.

    ``set_speed`` returns True when the handler must apply the new native
    `fpsTarget` with a state-preserving reload of its own. Handoff backends
    apply it during the overlay-driven restart and therefore return False.
    """

    supports_runtime_speed = False
    supports_display_handoff = False

    def pause(self) -> bool:
        return False

    def resume(self) -> bool:
        return False

    def set_speed(self, multiplier: int, fps_target: float) -> bool:
        return False

    def save_state(self, slot: int):
        return None

    def capture_screenshot(self, slot: int):
        return None

    def load_state(self, slot: int, state_path: str) -> bool:
        return False

    # -- display handoff ---------------------------------------------------
    def suspend_for_overlay(self, kind: str = "") -> bool:
        """Frees the display for an overlay. True = the game is suspended."""
        return self.pause()

    def resume_from_handoff(self) -> bool:
        """Restores gameplay at the captured state (or SIGCONT fallback)."""
        return self.resume()

    def overlay_state(self):
        """Native state captured before the handoff, or None."""
        return None

    def overlay_screenshot(self):
        """Gameplay screenshot captured before the handoff, or None."""
        return None

    def reload_cheats(self) -> bool:
        """Called after a cheat was toggled; True if a restart is required."""
        return False


class OverlayModel:
    title = ""
    subtitle = ""
    kind = ""

    def __init__(self, on_close=None):
        self.closed = False
        self.message = ""
        self._on_close = on_close

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self._on_close:
            self._on_close(self)


class IndicatorOverlayModel(OverlayModel):
    """Transient speed badge; never interactive and never a real panel."""

    kind = "indicator"
    title = ""
    subtitle = ""

    def __init__(self, text: str, seconds: float, clock=time.monotonic,
                 on_close=None):
        super().__init__(on_close)
        self.text = text
        self.clock = clock
        self.expires_at = clock() + seconds

    def expired(self) -> bool:
        return self.clock() >= self.expires_at


class CheatsOverlayModel(OverlayModel):
    """Cheats for the currently running game, with enabled/disabled state."""

    kind = ACTION_CHEATS

    def __init__(self, cheat_store, on_close=None):
        super().__init__(on_close)
        self.store = cheat_store
        self.title = "CHEATS"
        self.subtitle = "A enable / disable · B close"
        self.index = 0

    def items(self) -> List[Dict[str, Any]]:
        return self.store.cheats()

    def move(self, delta: int) -> None:
        items = self.items()
        if not items:
            return
        self.index = (self.index + delta) % len(items)

    def toggle(self) -> bool:
        items = self.items()
        if not items:
            self.message = "No cheats configured for this game"
            return False
        item = items[min(self.index, len(items) - 1)]
        ok = self.store.set_enabled(item["id"], not item["enabled"])
        self.message = (("ENABLED" if not item["enabled"] else "DISABLED")
                        + " " + item["name"]) if ok else "Could not update cheat"
        return ok


class SaveStatesOverlayModel(OverlayModel):
    """Exactly nine slots; empty ones save, occupied ones offer two actions."""

    kind = ACTION_SAVE_STATES
    ACTIONS = ("Load Save", "Overwrite Save")
    COLUMNS = 3

    def __init__(self, state_store, backend, on_close=None):
        super().__init__(on_close)
        self.store = state_store
        self.backend = backend
        self.title = "SAVE STATES"
        self.subtitle = "9 slots · A select · B close"
        self.index = 0
        self.mode = "grid"            # grid | actions | confirm
        self.action_index = 0
        self.loaded_slot = 0          # set when a slot was loaded into gameplay

    @property
    def slot(self) -> int:
        return self.index + 1

    def slots(self):
        return self.store.slots()

    def move(self, dx: int = 0, dy: int = 0) -> None:
        if self.mode != "grid":
            if dy or dx:
                self.action_index = (self.action_index + (dy or dx)) % len(self.ACTIONS)
            return
        if dx:
            self.index = max(0, min(SLOT_COUNT - 1, self.index + dx))
        elif dy:
            self.index = max(0, min(SLOT_COUNT - 1,
                                    self.index + dy * self.COLUMNS))

    def activate(self) -> bool:
        if self.mode == "confirm":
            return self.confirm_overwrite()
        if self.mode == "actions":
            return self.choose_action()
        info = self.slots()[self.index]
        if not info["occupied"]:
            return self._save(self.slot)
        # An occupied but corrupt slot still shows both actions: Load fails
        # gracefully, Overwrite repairs it after confirmation.
        self.mode = "actions"
        self.action_index = 0
        return True

    def choose_action(self, action_index: Optional[int] = None) -> bool:
        index = self.action_index if action_index is None else action_index
        if index == 0:
            return self._load(self.slot)
        if index == 1:
            self.mode = "confirm"      # explicit confirmation before overwrite
            return True
        return False

    def confirm_overwrite(self) -> bool:
        self.mode = "grid"
        return self._save(self.slot)

    def cancel(self) -> None:
        if self.mode in ("actions", "confirm"):
            self.mode = "grid"
        else:
            self.close()

    def _captured_state(self, slot: int):
        """State to archive for `slot`.

        With a display handoff the state was captured *before* mGBA was
        stopped, so the emulator does not have to be alive here — this is what
        makes "save to an empty slot" work at all. A backend without a handoff
        still falls back to asking the running emulator for a native state.
        """
        provider = getattr(self.backend, "overlay_state", None)
        if callable(provider):
            try:
                state = provider()
            except Exception:
                state = None
            if state:
                return state
        try:
            return self.backend.save_state(slot)
        except Exception:
            return None

    def _captured_thumbnail(self, slot: int):
        provider = getattr(self.backend, "overlay_screenshot", None)
        if callable(provider):
            try:
                shot = provider()
            except Exception:
                shot = None
            if shot:
                return shot
        try:
            return self.backend.capture_screenshot(slot)
        except Exception:
            return None               # a missing thumbnail is not fatal

    def _save(self, slot: int) -> bool:
        state = self._captured_state(slot)
        if not state:
            self.message = "Could not capture the emulator state"
            return False
        thumbnail = self._captured_thumbnail(slot)
        try:
            ok = self.store.save(slot, state, thumbnail)
        except Exception:
            ok = False
        self.message = f"Slot {slot} saved" if ok else "Save failed"
        self.mode = "grid"
        return bool(ok)

    def _load(self, slot: int) -> bool:
        path = self.store.state_for_load(slot)
        if not path:
            self.message = "State file is missing or corrupt"
            self.mode = "grid"
            return False
        try:
            ok = self.backend.load_state(slot, path)
        except Exception:
            ok = False
        if ok:
            self.message = f"Slot {slot} loaded"
            self.loaded_slot = slot
            self.close()              # return straight to gameplay
        else:
            self.message = "Load failed"
            self.mode = "grid"
        return bool(ok)


# ---------------------------------------------------------------------------
# Session orchestration
# ---------------------------------------------------------------------------

class InGameSession:
    """Owns the shortcuts, overlays and speed indicator for one game session."""

    def __init__(self, game_id: str, shortcuts: Optional[Dict[str, Iterable[str]]],
                 cheat_store, state_store, backend,
                 clock=time.monotonic):
        self.game_id = game_id
        self.backend = backend
        self.cheat_store = cheat_store
        self.state_store = state_store
        self.clock = clock
        self.router = ComboRouter(shortcuts)
        self.fast_forward = FastForwardController()
        self.overlay: Optional[OverlayModel] = None
        self.indicator = ""
        self.indicator_until = 0.0
        self.relaunch_requested = False
        self.emulator_terminated = False       # overlays never terminate mGBA
        # Display handoff bookkeeping. `suspended` means "the handler owns the
        # screen and gameplay must be restarted from its captured state".
        self.suspended = False
        self.resume_pending = False
        self._resumed = False
        self.status_message = ""
        self.last_failure = ""
        self.cheats_changed = False

    @property
    def shortcuts(self) -> Dict[str, Tuple[str, ...]]:
        return self.router.shortcuts

    @property
    def speed(self) -> int:
        return self.fast_forward.multiplier

    def show_indicator(self, text: str, seconds: float = 1.6) -> None:
        self.indicator = text
        self.indicator_until = self.clock() + seconds

    def show_speed_indicator(self, text: str, seconds: float = 1.1) -> None:
        """Show 2x / 3x / Normal over the game even with no overlay open.

        mGBA SDL has no OSD of its own, so the handler briefly takes the
        display for a badge-only overlay (no panel, no interactive controls).
        That uses the same display handoff as the panels: the frame and a
        native state are captured first, mGBA is stopped, and gameplay is
        restarted from the state — now at the new speed.
        """
        self.show_indicator(text, seconds)
        if self.overlay is not None:
            return                        # an open overlay already draws it
        if not self._suspend("indicator"):
            # No display handoff for the badge: fall back to the
            # state-preserving reload, so the new speed still reaches the
            # emulator instead of being announced and never applied.
            self.relaunch_requested = True
            return
        self.overlay = IndicatorOverlayModel(text, seconds, self.clock,
                                             self._overlay_closed)

    def note_speed_reload_failed(self) -> None:
        """The emulator kept its old speed, so the model must not claim 2x/3x."""
        self.fast_forward.reset()
        self.show_indicator(self.fast_forward.label)

    def tick(self) -> None:
        if self.indicator and self.clock() > self.indicator_until:
            self.indicator = ""

    def _suspend(self, kind: str) -> bool:
        """Takes the display for an overlay; never leaves the game frozen.

        A failure here (no state could be captured, no handoff available) is
        reported through `status_message` and the emulator is resumed instead
        of being left stopped behind a window that cannot be drawn.
        """
        try:
            ok = bool(self.backend.suspend_for_overlay(kind))
        except Exception as exc:
            self.status_message = f"Overlay unavailable: {exc}"
            ok = False
        if ok:
            self.suspended = True
            self._resumed = False
            self.last_failure = ""
            return True
        self.status_message = (self.status_message
                               or "Overlay unavailable: cannot capture the game state")
        self.last_failure = self.status_message
        try:
            self.backend.resume()      # a half-applied handoff must not freeze
        except Exception:
            pass
        self.suspended = False
        self._resumed = True
        return False

    def handle_action(self, action: str) -> None:
        # A transient speed badge must never swallow a deliberate shortcut.
        if self.overlay is not None and self.overlay.kind == "indicator":
            self.close_overlay()
        # While an overlay owns the screen, only its own combo closes it.  All
        # other combos are consumed by the router, never applied to gameplay.
        if self.overlay is not None:
            if action in (ACTION_CHEATS, ACTION_SAVE_STATES) and \
                    action == self.overlay.kind:
                self.close_overlay()
            return
        if action == ACTION_FAST_FORWARD:
            multiplier = self.fast_forward.activate()
            try:
                needs_reload = self.backend.set_speed(multiplier,
                                                     self.fast_forward.fps_target)
            except Exception:
                needs_reload = False
            self.relaunch_requested = self.relaunch_requested or bool(needs_reload)
            self.show_speed_indicator(self.fast_forward.label)
        elif action == ACTION_CHEATS:
            self.toggle_overlay(ACTION_CHEATS)
        elif action == ACTION_SAVE_STATES:
            self.toggle_overlay(ACTION_SAVE_STATES)

    def toggle_overlay(self, kind: str) -> bool:
        if self.overlay is not None:
            previous = self.overlay.kind
            self.close_overlay()
            if previous == kind:       # pressing the combo again closes it
                return True
        return self.open_overlay(kind)

    def open_overlay(self, kind: str) -> bool:
        """Suspends the game and opens an overlay. False = nothing opened."""
        if self.overlay is not None:
            self.close_overlay()
        if not self._suspend(kind):
            return False
        if kind == ACTION_CHEATS:
            self.overlay = CheatsOverlayModel(self.cheat_store, on_close=self._overlay_closed)
        else:
            self.overlay = SaveStatesOverlayModel(self.state_store, self.backend,
                                                  on_close=self._overlay_closed)
        return True

    def _overlay_closed(self, overlay: OverlayModel) -> None:
        if self.overlay is overlay:
            self.overlay = None
        # Cheat toggles and loaded save states only take effect in a fresh
        # mGBA process, which is exactly what the handoff restart provides.
        if getattr(overlay, "kind", "") == ACTION_CHEATS:
            self.cheats_changed = True
            try:
                self.backend.reload_cheats()
            except Exception:
                pass
        if self.backend.supports_display_handoff:
            # The overlay window still owns the screen here. Restarting mGBA
            # now would make two processes fight for KMS/DRM, so the restart
            # waits for `finish_overlay()` (called after the display closes).
            self.resume_pending = True
            return
        self.resume_now()

    def resume_now(self) -> bool:
        """Restarts gameplay from the captured state (idempotent)."""
        self.resume_pending = False
        if self._resumed:
            return False              # already playing; nothing to restore
        suspended, self.suspended = self.suspended, False
        self._resumed = True
        if suspended or self.backend.supports_display_handoff:
            try:
                return bool(self.backend.resume_from_handoff())
            except Exception as exc:
                self.status_message = f"Could not resume the game: {exc}"
                return False
        try:
            return bool(self.backend.resume())
        except Exception:
            return False

    def finish_overlay(self) -> bool:
        """Called by the renderer once the overlay has released the display."""
        if not self.resume_pending and not self.suspended:
            return False
        return self.resume_now()

    def release_display(self) -> bool:
        """Safety net: never leave the emulator suspended without an overlay.

        Used by the handler when a session ends (or an overlay loop dies), so it
        also completes a resume that was waiting for the display to close.
        """
        changed = False
        if self.overlay is not None:
            self.close_overlay()
            changed = True
        if self.resume_pending or self.suspended:
            changed = self.resume_now() or changed
        return changed

    def close_overlay(self) -> None:
        if self.overlay is None:
            return
        self.overlay.close()

    def press(self, button: Optional[str]) -> List[str]:
        actions = self.router.press(button)
        for action in actions:
            self.handle_action(action)
        return actions

    def release(self, button: Optional[str]) -> None:
        self.router.release(button)

    def poll(self) -> List[str]:
        """Frame tick: no action from a held combination."""
        self.tick()
        return self.router.poll()

    def take_relaunch(self) -> bool:
        pending = self.relaunch_requested
        self.relaunch_requested = False
        return pending

