# mGBA appliance display and exit verification

## Display strategy

`gba-session` marks its WM-less X11 environment with `GBA_BARE_X11=1`.
For this environment only, when screen geometry is known, the adapter uses
`fullscreen=0`, omits `-f`, supplies the active width/height, and sets
`SDL_VIDEO_WINDOW_POS=0,0`. With no window manager there are no decorations:
this is a screen-filling unmanaged window, **not SDL fullscreen mode**.
Aspect ratio and integer/fit scaling remain unchanged. Normal desktop launches
and direct KMS/DRM retain their existing SDL fullscreen behavior.

This is based on the SDL2 mGBA implementation: both its GL and software paths
create windows with `SDL_WINDOW_FULLSCREEN_DESKTOP` when fullscreen is enabled.
SDL2's X11 fullscreen implementation sends EWMH requests to the WM. Combining
that path with geometry is not a reliable way to fill a WM-less screen. We
retain explicit geometry and bypass the WM fullscreen request, rather than
simply dropping dimensions and risking a native-sized window.

References inspected:
- https://github.com/mgba-emu/mgba/blob/master/src/platform/sdl/gl-common.c
- https://github.com/mgba-emu/mgba/blob/master/src/platform/sdl/sw-sdl2.c
- https://github.com/libsdl-org/SDL/blob/SDL2/src/video/x11/SDL_x11window.c

X11's active xrandr mode takes precedence over a connector's advertised DRM
preferred mode. An active 1920x1080 screen therefore gets a 1920x1080 window;
the adapter does not switch the monitor to an unsupported resolution. If
xrandr cannot be queried, DRM discovery remains the fallback. If neither can
determine geometry, the existing SDL fullscreen/4x fallback remains; the log
reports `screen=unknown` and this case needs investigation on the target.

## Exit strategy

ESC sets the runner's exit flag; adapter SIGTERM/SIGINT do likewise. The same
bounded stop path is used for exit, overlays and reloads: SIGCONT (in case the
child is suspended), SIGTERM with a 2-second grace, SIGHUP with a 0.5-second
grace, then SIGKILL with a 2-second bounded reap. Every wait is bounded.
Exit requests also close an active overlay, and no relaunch is allowed once
exit is requested. Termination statuses do not trigger the early video retry.

If a process is stuck in kernel uninterruptible I/O, even SIGKILL cannot force
immediate reaping. The adapter logs that exceptional condition and does not
block indefinitely; ordinary userspace SDL hangs are killed and reaped.

## Verbose diagnostics: what the boot log proves

A black screen has four candidate stages and every one of them is silent by default. The appliance
session (`src/gba_session.sh`) exports verbose subsystem logging and the adapter records its own
evidence around the child. All of it lands in `/data/logs/latest.log` through the tty1 wrapper's
`exec > >(tee -a "$LOG_FILE") 2>&1`.

| Stage | Source | Set by | What it proves |
|---|---|---|---|
| SDL2 | `SDL_LOG_PRIORITY=verbose` (+ `_video`, `_audio`, `_render`) | `gba-session` | video driver selected, window creation, fullscreen transitions, audio buffer state |
| Mesa/OpenGL | `LIBGL_DEBUG=verbose`, `MESA_DEBUG=1` | `gba-session` | hardware acceleration vs. software fallback, GL context and frame-swap warnings |
| mGBA core | `-l 31` (`--log-level`) | adapter, from `mgba_log_level()` | ROM/BIOS load, save routing, audio sync, frame pipeline (mgba-sdl defaults `logToStdout` to on) |
| Window mapping | `xwininfo -root -tree`, `xwininfo -id <win>`, `xrandr --current` | adapter, ~1.5 s after each spawn | whether the window exists, `IsViewable` vs. `IsUnmapped`, its geometry against the active mode |

`-l 31` is `FATAL|ERROR|WARN|INFO|DEBUG` from `include/mgba/core/log.h` (`STUB` 0x20 and
`GAME_ERROR` 0x40 stay off; `mLOG_ALL` is 0x7F). mGBA's stderr is merged into stdout at spawn time,
so SDL2's and Mesa's verbose output arrives on the same pumped stream as mGBA's own.

Log prefixes, in the order a healthy session prints them:

```
[load]  ROM path, size and parsed cartridge title; save-file existence; BIOS resolution
Display session=… driver=… windowed=… scaling=… screen=WxH
Subsystem diagnostics: mGBA log mask -l 31 (FATAL|ERROR|WARN|INFO|DEBUG); SDL_LOG_PRIORITY=…
Executing mGBA: …        the exact argv, including -l and every -C override
Spawned mGBA (pid N, spawn #1)
[mgba]  mGBA's own lines plus SDL2/Mesa output on the merged stream
Input:  watched /dev/input devices, escape keys and combos
[diag]  sampling the display: child-alive=yes DISPLAY=:0 …
[x11]   outputs, active mode, window tree, then the emulator-window verdict
Input:  Exit key detected (evdev code 1); ending the session …
Stop sequence: SIGCONT → SIGTERM → SIGHUP → SIGKILL, each with its outcome
[mgba]  output stream closed after N line(s)
Save fsync verified: N file(s) plus the directory …
Teardown: exit status N after X.Xs total
```

Without X11 the `[x11]` block is replaced by `[drm]` connector evidence — status, enabled, DPMS,
preferred mode and how many connectors the pinned card actually drives. That is the console
equivalent of the window check: a card with no connected output produces a black screen with a
perfectly healthy emulator, and it is what the D-047 card ladder exists to escape.

The emulator-window verdict is the line that settles "did it map?":

* `map-state=IsViewable size=1920x1080 position=0,0` followed by `fills the active 1920x1080 mode`
  — output reached the screen, so look at audio or input instead;
* `map-state=IsUnmapped` — SDL created the window and something never mapped it;
* `lies entirely outside the active … mode` — the window is off-screen (a bad
  `SDL_VIDEO_WINDOW_POS`, or a second output that is not the one being watched);
* `does not fill the display` — mapped and visible, but not screen-sized (see D-049);
* `NO emulator window in the X11 tree` — mGBA died before creating one, and the `[mgba]` lines
  immediately above carry the SDL/Mesa reason;
* `[x11] window tree unavailable …` — the tools themselves failed (a dead display, a missing
  package), which is *not* the same evidence as an empty tree and is never reported as one.

Windows are matched by title (`mGBA`, mGBA's own `projectName`) or by SDL's WM_CLASS (`SDL_app`), so
a renamed build is still found. The sample re-runs after every handoff restart, which is what catches
an overlay leaving a stale or misplaced window behind.

`xwininfo` and `xrandr` are read-only and optional. When they are missing the adapter names the
packages (`x11-utils`, `x11-server-utils`) and continues; `gba-selftest` reports the same thing as a
`diagnostics` warning, and `gba-session` says it once at boot. Every diagnostic call is
time-bounded (`DIAG_TOOL_TIMEOUT`) on its own thread, so a hanging tool cannot delay or kill a
session, and the window tree is truncated (`DIAG_MAX_TREE_LINES`) because the log is size-capped
(Rule ST5).

`GBA_DEBUG=0` turns the adapter side off and restores mGBA's default `FATAL|ERROR|WARN` mask. The
SDL/Mesa exports all use `${VAR:-default}`, so any of them can be raised or silenced for one boot
without editing a file. `GBA_MGBA_LOG_LEVEL=127` raises the mask to `mLOG_ALL` for a deeper capture.

## Required physical-appliance checks

These have **not** been executed in the development sandbox; automated tests
cannot establish that frames reach a physical display. Apply/repair the install
to refresh both the session wrapper and the installed adapter, then:

1. Boot the GRUB console appliance entry without a WM/compositor. Launch a
   known-good GBA ROM. Confirm visible, updating frames, screen-filling output,
   correct aspect ratio, audio and controller/keyboard input. The log should
   show `Bare X11: screen-sized window ...; SDL fullscreen disabled`, and argv
   should contain `fullscreen=0`, width/height, and no `-f`.
2. At an active 1920x1080 X11 mode, verify the geometry is 1920x1080. The boot log now records the
   evidence itself — `[x11] active mode: 1920x1080 …`, the window tree, and
   `[x11] emulator window … map-state=IsViewable size=1920x1080 position=0,0` — so a second terminal
   with `xrandr --current` / `xwininfo` is a cross-check rather than the only source (and needs
   `x11-utils` + `x11-server-utils` installed for the log to contain it). Check integer and fit
   scaling and one lower-resolution display. Verify normal desktop/windowed launches and the KMS
   fallback independently — the KMS path logs `[drm]` connector evidence instead of `[x11]`.
3. Press ESC during normal gameplay, immediately after launch, and during an
   in-game overlay. Confirm the shelf returns and another game launches.
4. On a disposable test session, simulate an unresponsive emulator (or use a
   test stub ignoring SIGTERM/SIGHUP). Confirm logs record escalation, the shelf
   returns, and `pgrep -af mgba` shows no remaining emulator. Repeat launches
   and exits, including exits within five seconds (must not relaunch a game).
   The escalation trail is the `Stop sequence:` lines: one per signal actually
   sent, with the wait outcome and the final reaped status, so a session that
   needed SIGKILL says so instead of only reporting an exit code.

Automated coverage uses real subprocesses ignoring SIGTERM and SIGHUP, checks
exit status and reaping across repeated runs, checks stopped-child cleanup and
partial-launch failure, and tests argv and active-mode selection. It does not
claim physical boot/rendering validation.

## Shelf transitions and Alt+Tab

The shelf and in-game overlays now also use native-sized `NOFRAME` windows in
the marked bare-X11 session. This avoids alternating SDL fullscreen requests
with mGBA's unmanaged window during handoffs. Desktop/windowed and KMS paths
keep their existing flags. The shelf uses the actual SDL drawable size, and
windowed resize/scaling rebuilds preserve the chosen window dimensions.

The shelf requests SDL keyboard grabbing only in appliance mode, including
when focus returns. This helps keep Alt+Tab inside the appliance rather than
letting the window system consume it. Windowed desktop testing never grabs the
keyboard; a desktop WM may therefore still reserve Alt+Tab there (plain Tab on
the shelf and clicking tab headers remain available). Keyboard modifier events
are tracked in addition to each event's modifier flags, cleared on focus loss,
and repeated Alt+Tab keydowns do not race through the tabs.

On hardware, repeat Games → another tab → Games, launch/ESC, and overlay
open/close at least ten times. Check that no stale frames, misplaced windows,
or display-mode changes occur. Check Alt+Tab and Alt+Shift+Tab from every main
tab and a popup, both before and after a game exit. These physical checks remain
outstanding; headless tests cover flags, geometry and shortcut dispatch only.
