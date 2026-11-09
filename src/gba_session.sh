#!/bin/bash
# Shared X11 / KMS appliance session. Keep the automounter in the same user
# context as the launcher; udisks2 uses the system bus, not a desktop session.
#
# --- compositor helpers ------------------------------------------------------
# A compositor is only useful if it is *proven* to be running, and every reason
# it can fail has to end up in the boot log. These helpers do three things the
# old one-liner did not:
#
#   * they wait for the X server to accept connections before asking another
#     client to talk to it (picom racing xinit is "Can't open display"),
#   * they run the compositor as a tracked child instead of with `--daemon`,
#     because picom's own src/picom.c closes stdout/stderr and calls setsid()
#     once it has daemonized - a daemonized failure is invisible, and the
#     process the shell forked is gone before the compositor is proven,
#   * they retry along the ways a compositor can still succeed (other backend,
#     default config) and print the compositor's own words when every way
#     failed, instead of "the reason is in the log above".
wait_for_x_display() {
    [ -n "${DISPLAY:-}" ] || return 1
    local i
    for i in $(seq 1 30); do
        if command -v xdpyinfo >/dev/null 2>&1; then
            xdpyinfo -display "$DISPLAY" >/dev/null 2>&1 && return 0
        elif command -v xset >/dev/null 2>&1; then
            xset -display "$DISPLAY" q >/dev/null 2>&1 && return 0
        else
            return 0
        fi
        sleep 0.1
    done
    return 1
}

composite_extension() {
    # ok | missing | unknown (no xdpyinfo to ask with)
    command -v xdpyinfo >/dev/null 2>&1 || { echo unknown; return 0; }
    if xdpyinfo -display "${DISPLAY:-}" 2>/dev/null | grep -qw Composite; then
        echo ok
    else
        echo missing
    fi
}

compositor_reason() {
    # One line that explains an exit, taken from the attempt's own output.
    local reason
    reason="$(grep -m1 -iE 'fatal|error|cannot|can.t|failed' "$1" 2>/dev/null || true)"
    if [ -z "$reason" ]; then
        reason="$(tail -n 1 "$1" 2>/dev/null || true)"
    fi
    if [ -z "$reason" ]; then
        reason="exited without leaving a message"
    fi
    printf '%s' "$reason"
}

compositor_attempt() {
    # $1 backend, $2 config path ("" = the user's own config).
    # Returns 0 only when the process is still alive after ~1.2s.
    local backend="$1" config="$2" log i
    log="/run/gba/compositor.log"
    mkdir -p /run/gba 2>/dev/null || log="/tmp/gba-compositor.log"
    : > "$log" 2>/dev/null || log=/dev/null
    if [ -n "$config" ]; then
        "$COMPOSITOR_BIN" --backend "$backend" --config "$config" >"$log" 2>&1 &
    else
        "$COMPOSITOR_BIN" --backend "$backend" >"$log" 2>&1 &
    fi
    COMPOSITOR_PID=$!
    for i in $(seq 1 12); do
        kill -0 "$COMPOSITOR_PID" 2>/dev/null || break
        sleep 0.1
    done
    if kill -0 "$COMPOSITOR_PID" 2>/dev/null; then
        COMPOSITOR_LOG="$log"
        return 0
    fi
    COMPOSITOR_REASON="$(compositor_reason "$log")"
    if [ -n "$config" ]; then
        echo "[session] compositor: $COMPOSITOR_BIN exited with backend $backend and the default config: $COMPOSITOR_REASON" >&2
    else
        echo "[session] compositor: $COMPOSITOR_BIN exited with backend $backend: $COMPOSITOR_REASON" >&2
    fi
    [ "$log" = "/dev/null" ] || rm -f "$log" 2>/dev/null || true
    return 1
}

start_session_compositor() {
# --- compositor ----------------------------------------------------------
# Three faults used to hide here, and together they explain "the game runs
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
# 3. `--daemon` hid every later failure (picom closes its own stderr when
#    it daemonizes) and made the survival check meaningless. See the
#    helpers above: tracked child, real exit, real reason.
#
# The reasons a compositor still fails on a machine that is otherwise
# fine, all handled below without a human reading the log first:
#   * the X server is not accepting connections yet      -> wait for it
#   * another compositor already owns the overlay        -> reuse it
#   * the X server has no Composite extension            -> say so, skip
#   * a broken ~/.config/picom.conf                      -> retry with none
#   * the backend cannot initialise on this GPU          -> try the other
GBA_COMPOSITOR_BACKEND="${GBA_COMPOSITOR_BACKEND:-xrender}"
export GBA_COMPOSITOR_BACKEND
# Status, not intent: empty means "no compositor is running". The reason is
# kept in GBA_COMPOSITOR_REASON, and GBA_COMPOSITOR_DISABLED marks the one case
# where no compositor is *wanted* - otherwise the adapter retries once the
# session is fully up, which is a different moment from this one.
GBA_COMPOSITOR_REQUEST="${GBA_COMPOSITOR:-}"
GBA_COMPOSITOR=""
GBA_COMPOSITOR_REASON=""
GBA_COMPOSITOR_DISABLED=""
# A caller can still ask for no compositing by exporting GBA_COMPOSITOR=none
# before the session; that is the only way that value is a request, and it is
# converted into the explicit marker here so the adapter cannot confuse it with
# the status "nothing came up".
if [ "$GBA_COMPOSITOR_REQUEST" = "none" ] || [ "$GBA_COMPOSITOR_BACKEND" = "none" ]; then
    GBA_COMPOSITOR_DISABLED=1
    GBA_COMPOSITOR=""
    GBA_COMPOSITOR_REASON="disabled on request"
    echo "[session] compositor: disabled (GBA_COMPOSITOR_DISABLED=1)" >&2
else
    COMPOSITOR_BIN=""
    if command -v picom >/dev/null 2>&1; then
        COMPOSITOR_BIN="picom"
    elif command -v compton >/dev/null 2>&1; then
        COMPOSITOR_BIN="compton"
    fi
    COMPOSITOR_EXISTING=""
    if command -v pgrep >/dev/null 2>&1; then
        for candidate in picom compton; do
            if pgrep -x "$candidate" >/dev/null 2>&1; then
                COMPOSITOR_EXISTING="$candidate"
                break
            fi
        done
    fi
    if [ -n "$COMPOSITOR_EXISTING" ]; then
        # Starting a second one is not harmless: picom registers
        # _NET_WM_CM_Sn, finds the overlay taken and exits with "Another
        # composite manager is already running" - losing a compositor that
        # was working.
        GBA_COMPOSITOR="$COMPOSITOR_EXISTING"
        echo "[session] compositor: $COMPOSITOR_EXISTING already running (reusing it)" >&2
    elif [ -z "$COMPOSITOR_BIN" ]; then
        GBA_COMPOSITOR_REASON="no compositor installed (picom/compton)"
        echo "[session] WARNING: no compositor found (picom/compton); a fullscreen OpenGL window may show nothing" >&2
        echo "[session]   Install: sudo apt install picom" >&2
    else
        if ! wait_for_x_display; then
            echo "[session] WARNING: $DISPLAY did not accept a connection within 3s; starting $COMPOSITOR_BIN anyway" >&2
        fi
        if [ "$(composite_extension)" = "missing" ]; then
            GBA_COMPOSITOR_REASON="the X server has no Composite extension"
            echo "[session] compositor: the X server has no Composite extension, so no compositor can ever run; continuing without one" >&2
        else
            COMPOSITOR_OTHER="glx"
            [ "$GBA_COMPOSITOR_BACKEND" = "glx" ] && COMPOSITOR_OTHER="xrender"
            COMPOSITOR_ATTEMPTS=("$GBA_COMPOSITOR_BACKEND|" \
                                 "$COMPOSITOR_OTHER|" \
                                 "$GBA_COMPOSITOR_BACKEND|/dev/null")
            COMPOSITOR_TRIED=0
            COMPOSITOR_VERDICT=""
            COMPOSITOR_ONLY_DEFAULT=0
            for attempt in "${COMPOSITOR_ATTEMPTS[@]}"; do
                if [ "$COMPOSITOR_ONLY_DEFAULT" = "1" ] && [ -z "${attempt#*|}" ]; then
                    # The user's config file is the problem: another backend
                    # would fail exactly the same way, so go straight to the
                    # default-config attempt.
                    continue
                fi
                COMPOSITOR_TRIED=$((COMPOSITOR_TRIED + 1))
                attempt_backend="${attempt%%|*}"
                attempt_config="${attempt#*|}"
                if compositor_attempt "$attempt_backend" "$attempt_config"; then
                    GBA_COMPOSITOR="$COMPOSITOR_BIN"
                    echo "[session] compositor: $COMPOSITOR_BIN running (backend $attempt_backend, pid $COMPOSITOR_PID)" >&2
                    # Keep its output: a compositor that dies later leaves the
                    # reason there instead of nowhere at all.
                    if [ "$COMPOSITOR_LOG" != "/dev/null" ]; then
                        echo "[session] compositor: $COMPOSITOR_BIN output -> $COMPOSITOR_LOG" >&2
                    fi
                    break
                fi
                # Three failures are worth no second try: the overlay belongs to somebody
                # else, the X server refuses connections, or it has no Composite extension.
                COMPOSITOR_LOWER="$(printf '%s' "$COMPOSITOR_REASON" | tr 'A-Z' 'a-z')"
                case "$COMPOSITOR_LOWER" in
                    *config*)
                        echo "[session] compositor: picom could not read a config file; the next attempt ignores it, and you can fix or delete $HOME/.config/picom/picom.conf" >&2
                        COMPOSITOR_ONLY_DEFAULT=1
                        continue
                        ;;
                    *"another composite manager"*)
                        COMPOSITOR_VERDICT="another compositor already owns the screen"
                        break
                        ;;
                    *"open display"*)
                        COMPOSITOR_VERDICT="the X server refused the connection ($COMPOSITOR_REASON)"
                        break
                        ;;
                    *composite*extension*)
                        COMPOSITOR_VERDICT="the X server has no Composite extension ($COMPOSITOR_REASON)"
                        break
                        ;;
                esac
            done
            if [ -z "$GBA_COMPOSITOR" ]; then
                [ -n "$COMPOSITOR_VERDICT" ] || \
                    COMPOSITOR_VERDICT="no compositor could be started after $COMPOSITOR_TRIED attempt(s)"
                GBA_COMPOSITOR_REASON="$COMPOSITOR_VERDICT"
                echo "[session] WARNING: compositor: $COMPOSITOR_VERDICT; continuing without one. On a driver where a fullscreen OpenGL window cannot present by itself this is a black screen - the adapter starts such a session windowed instead, and its own log line says so." >&2
            fi
        fi
    fi
fi
    export GBA_COMPOSITOR GBA_COMPOSITOR_REASON GBA_COMPOSITOR_DISABLED
    if [ -n "$GBA_COMPOSITOR_REASON" ] && [ -z "$GBA_COMPOSITOR_DISABLED" ]; then
        echo "[session] compositor: passing the reason to the adapter: $GBA_COMPOSITOR_REASON" >&2
    fi
}

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
    # The whole decision - reuse, wait for X, check Composite, try the other
    # backend, try the default config, report the compositor's own reason - is
    # one function, so it can be tested without a machine to boot.
    start_session_compositor

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
