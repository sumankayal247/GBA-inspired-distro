#!/bin/bash
# Shared X11 / KMS appliance session. Keep the automounter in the same user
# context as the launcher; udisks2 uses the system bus, not a desktop session.
if [ -n "${DISPLAY:-}" ]; then
    export GBA_BARE_X11=1
    xhost +local: >/dev/null 2>&1 || true
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
