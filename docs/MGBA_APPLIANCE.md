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

## Required physical-appliance checks

These have **not** been executed in the development sandbox; automated tests
cannot establish that frames reach a physical display. Apply/repair the install
to refresh both the session wrapper and the installed adapter, then:

1. Boot the GRUB console appliance entry without a WM/compositor. Launch a
   known-good GBA ROM. Confirm visible, updating frames, screen-filling output,
   correct aspect ratio, audio and controller/keyboard input. The log should
   show `Bare X11: screen-sized window ...; SDL fullscreen disabled`, and argv
   should contain `fullscreen=0`, width/height, and no `-f`.
2. At an active 1920x1080 X11 mode, verify the geometry is 1920x1080 (use
   `xrandr --current` / `xwininfo` from a second terminal if available). Check
   integer and fit scaling and one lower-resolution display. Verify normal
   desktop/windowed launches and the KMS fallback independently.
3. Press ESC during normal gameplay, immediately after launch, and during an
   in-game overlay. Confirm the shelf returns and another game launches.
4. On a disposable test session, simulate an unresponsive emulator (or use a
   test stub ignoring SIGTERM/SIGHUP). Confirm logs record escalation, the shelf
   returns, and `pgrep -af mgba` shows no remaining emulator. Repeat launches
   and exits, including exits within five seconds (must not relaunch a game).

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
