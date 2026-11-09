#!/bin/bash
# Shared X11 / KMS appliance session. Keep the automounter in the same user
# context as the launcher; udisks2 uses the system bus, not a desktop session.
if [ -n "${DISPLAY:-}" ]; then
    export GBA_BARE_X11=1
    xhost +local: >/dev/null 2>&1 || true

    # Start a minimal window manager. SDL2 fullscreen windows need focus/expose
    # events to render frames. Without a WM, windows never get these events.
    # matchbox-window-manager is lightweight and designed for embedded/appliance use.
    if command -v matchbox-window-manager >/dev/null 2>&1; then
        matchbox-window-manager -use_titlebar no -use_cursor no &
        echo "[session] window manager: matchbox-window-manager started" >&2
    elif command -v openbox >/dev/null 2>&1; then
        openbox --sm-disable &
        echo "[session] window manager: openbox started" >&2
    else
        echo "[session] WARNING: no window manager found; SDL2 windows may not render" >&2
        echo "[session]   Install: sudo apt install matchbox-window-manager" >&2
    fi

    # --- compositor ----------------------------------------------------------
    # Two faults used to hide here, and together they explain "the game runs
    # but the screen is black on one laptop and fine on another":
    #
    # 1. `picom --vsync` takes NO argument. The old command passed `none`,
    #    which picom rejects as a positional argument ("picom doesn't accept
    #    positional arguments") and exits immediately. Its stderr went to
    #    /dev/null and the script printed "compositor: picom started" anyway,
    #    so NO compositor has ever run - on any machine.
    # 2. The GLX backend is CPU-bound whenever software OpenGL (llvmpipe) is
    #    in use, and at 1920x1080 that is a full-screen GLX composite every
    #    frame. The xrender backend composites the same windows without GL,
    #    so it is the default; GBA_COMPOSITOR_BACKEND=glx restores the old
    #    attempt and `none` disables compositing for this session.
    #
    # The compositor is only worth having if it is really running, so its
    # stderr stays in the session log and the result is checked.
    GBA_COMPOSITOR_BACKEND="${GBA_COMPOSITOR_BACKEND:-xrender}"
    export GBA_COMPOSITOR_BACKEND
    GBA_COMPOSITOR="none"
    if [ "$GBA_COMPOSITOR_BACKEND" = "none" ]; then
        echo "[session] compositor: disabled (GBA_COMPOSITOR_BACKEND=none)" >&2
    else
        COMPOSITOR_BIN=""
        if command -v picom >/dev/null 2>&1; then
            COMPOSITOR_BIN="picom"
        elif command -v compton >/dev/null 2>&1; then
            COMPOSITOR_BIN="compton"
        fi
        if [ -n "$COMPOSITOR_BIN" ] && ! command -v pgrep >/dev/null 2>&1; then
            # Without procps we cannot prove a daemon survived, so say that
            # instead of claiming a compositor that may not exist.
            "$COMPOSITOR_BIN" --daemon --backend "$GBA_COMPOSITOR_BACKEND" &
            sleep 0.7
            GBA_COMPOSITOR="$COMPOSITOR_BIN"
            echo "[session] compositor: requested $COMPOSITOR_BIN (backend $GBA_COMPOSITOR_BACKEND); pgrep is missing, so the result is unverified" >&2
        elif [ -n "$COMPOSITOR_BIN" ]; then
            compositor_before="$(pgrep -c -x "$COMPOSITOR_BIN" 2>/dev/null || true)"
            compositor_before="${compositor_before:-0}"
            # No `2>/dev/null`: a rejected option must reach the boot log, and
            # no `--vsync` argument either - it is a flag, not an option.
            "$COMPOSITOR_BIN" --daemon --backend "$GBA_COMPOSITOR_BACKEND" &
            sleep 0.7
            compositor_after="$(pgrep -c -x "$COMPOSITOR_BIN" 2>/dev/null || true)"
            compositor_after="${compositor_after:-0}"
            if [ "$compositor_after" -gt "$compositor_before" ]; then
                GBA_COMPOSITOR="$COMPOSITOR_BIN"
                echo "[session] compositor: $COMPOSITOR_BIN running (backend $GBA_COMPOSITOR_BACKEND, pid $(pgrep -x "$COMPOSITOR_BIN" | head -1))" >&2
            elif [ "$compositor_after" -gt 0 ]; then
                # An older instance from a previous session owns the overlay.
                GBA_COMPOSITOR="$COMPOSITOR_BIN"
                echo "[session] compositor: $COMPOSITOR_BIN already running (backend requested: $GBA_COMPOSITOR_BACKEND)" >&2
            else
                echo "[session] WARNING: $COMPOSITOR_BIN exited immediately with backend $GBA_COMPOSITOR_BACKEND; the reason is in the log above. Continuing without a compositor." >&2
            fi
        else
            echo "[session] WARNING: no compositor found (picom/compton); a fullscreen OpenGL window may not present" >&2
            echo "[session]   Install: sudo apt install picom" >&2
        fi
    fi
    export GBA_COMPOSITOR

    # Give WM and compositor time to initialize before apps start
    sleep 0.5
fi

# --- verbose subsystem diagnostics -------------------------------------------
# "The game opened to a black screen" cannot be diagnosed from the shell alone:
# SDL2, Mesa and mGBA all fail quietly by default. These exports make every
# stage say what it did, and the tty1 wrapper's `exec > >(tee -a "$LOG_FILE")`
# already puts both stdout and stderr into /data/logs/latest.log:
#
#   SDL_LOG_PRIORITY=verbose  SDL2 video/audio/render/input init, the driver it
#                             selected, window creation, fullscreen transitions
#   LIBGL_DEBUG=verbose       whether Mesa gave mGBA hardware acceleration or
#                             fell back to software (and why)
#   MESA_DEBUG=1              GL context, shader and frame-swap warnings
#   GBA_DEBUG=1               the adapter side: mGBA's own `-l` log mask plus the
#                             X11 window-mapping / DRM connector sampling below
#
# Each honours a value already in the environment, so a support session can
# raise or silence one without editing this file. GBA_DEBUG=0 restores the
# quiet, pre-diagnostics behaviour everywhere in the adapter.
export SDL_LOG_PRIORITY="${SDL_LOG_PRIORITY:-verbose}"
export SDL_LOG_PRIORITY_video="${SDL_LOG_PRIORITY_video:-verbose}"
export SDL_LOG_PRIORITY_audio="${SDL_LOG_PRIORITY_audio:-verbose}"
export SDL_LOG_PRIORITY_render="${SDL_LOG_PRIORITY_render:-verbose}"
export LIBGL_DEBUG="${LIBGL_DEBUG:-verbose}"
export MESA_DEBUG="${MESA_DEBUG:-1}"
export GBA_DEBUG="${GBA_DEBUG:-1}"
echo "[session] diagnostics: SDL_LOG_PRIORITY=$SDL_LOG_PRIORITY LIBGL_DEBUG=$LIBGL_DEBUG MESA_DEBUG=$MESA_DEBUG GBA_DEBUG=$GBA_DEBUG" >&2

# The adapter samples the X11 window tree to prove whether the emulator's window
# actually mapped. Both tools are read-only; say so at boot if they are absent
# rather than leaving a gap in the log that looks like a crash.
missing_tools=
for tool in xwininfo xrandr; do
    command -v "$tool" >/dev/null 2>&1 || missing_tools="$missing_tools $tool"
done
if [ -n "$missing_tools" ]; then
    echo "[session] missing X11 diagnostic tool(s):$missing_tools" >&2
    echo "[session]   window-mapping evidence will be absent from the log; everything" >&2
    echo "[session]   else still works. Install with: apt install x11-utils x11-server-utils" >&2
fi

start_automounter() {
    # /run/lock is local, writable on Debian/Ubuntu, and survives session
    # restarts. Do not unlink the file: that would allow two locked inodes.
    umask 077
    exec 9>"/run/lock/gba-udiskie-$(id -u).lock" || exit 1
    flock -n 9 || exit 0
    child=
    stop() {
        trap '' TERM INT HUP
        if [ -n "$child" ]; then
            kill "$child" 2>/dev/null || true
            wait "$child" 2>/dev/null || true
        fi
        exit 0
    }
    trap stop TERM INT HUP
    # Restart after an unexpected exit (e.g. a system bus restart). Keep the
    # lock while backing off, so another session cannot create a second worker.
    while :; do
        udiskie --automount --no-notify --no-tray --no-file-manager &
        child=$!
        wait "$child"
        status=$?
        echo "[usb] udiskie exited ($status); retrying in 5 seconds" >&2
        sleep 5 &
        child=$!
        wait "$child"
    done
}

if command -v udiskie >/dev/null 2>&1 && command -v flock >/dev/null 2>&1; then
    start_automounter &
    automounter=$!
else
    echo "[usb] Automount unavailable; install udiskie udisks2 dbus util-linux" >&2
    automounter=
fi

supervisor=
cleanup() {
    trap '' TERM INT HUP
    if [ -n "$supervisor" ]; then
        kill "$supervisor" 2>/dev/null || true
        wait "$supervisor" 2>/dev/null || true
    fi
    if [ -n "$automounter" ]; then
        kill "$automounter" 2>/dev/null || true
        wait "$automounter" 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
trap 'exit 129' HUP
/opt/gba-distro/bin/gba-supervisor --appliance <&0 &
supervisor=$!
wait "$supervisor"
status=$?
supervisor=
exit "$status"
