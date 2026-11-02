#!/bin/sh
# ==============================================================================
# GBA OS — Bare-Metal Appliance PID 1 Init Script
#
# Boots straight into the GBA console shell with no desktop environment.
# Implements Rules B1-B9, S1-S8, F1-F2 from docs/CONTRACT.md
#
# This script is intentionally POSIX sh + no bashisms: it may be PID 1 on a
# minimal image where /bin/sh is dash or busybox ash.
# ==============================================================================

set -u

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

# ----------------------------------------------------------------- utilities
# Everything below must keep working on a half-broken machine: no /proc, no
# python, no reboot binary. Rule F4 ("never leave a black screen").
have() { command -v "$1" >/dev/null 2>&1; }

# PID 1 must never return, so every exit path ends in an endless shell loop.
rescue_shell() {
    while :; do
        if have setsid; then
            setsid -c /bin/sh <>/dev/tty1 2>&1 || true
        elif [ -c /dev/tty1 ]; then
            /bin/sh <>/dev/tty1 2>&1 || true
        else
            /bin/sh || true
        fi
        echo ""
        echo "[init] Rescue console closed. All data is untouched."
        echo "[init] Type 'reboot -f' / 'poweroff -f' at the next prompt to leave."
        sleep 3
    done
}

do_reboot() {
    sync
    if have reboot; then reboot -f && return 0; fi
    if [ -w /proc/sysrq-trigger ]; then echo b > /proc/sysrq-trigger 2>/dev/null || true; fi
    return 0
}

do_poweroff() {
    sync
    if have poweroff; then poweroff -f && return 0; fi
    if [ -w /proc/sysrq-trigger ]; then echo o > /proc/sysrq-trigger 2>/dev/null || true; fi
    return 0
}
mount_pseudo() {
    [ -d "$2" ] || mkdir -p "$2"
    grep -qs " $2 " /proc/mounts || mount -t "$1" "${3:-$1}" "$2" 2>/dev/null || true
}

mount_pseudo proc /proc
mount_pseudo sysfs /sys
mount_pseudo devpts /dev/pts
mount_pseudo tmpfs /dev/shm
mount_pseudo tmpfs /run

# ------------------------------------------------------------------- devices
if command -v udevd >/dev/null 2>&1; then
    udevd --daemon 2>/dev/null || udevd 2>/dev/null || true
    udevadm trigger 2>/dev/null || true
    udevadm settle 2>/dev/null || true
fi

# ------------------------------------------------------------------- storage
mkdir -p /data/saves /data/library /data/logs /data/config /data/apps /games /run/gba /tmp

# Volume, on a console appliance, is a system property.
if command -v amixer >/dev/null 2>&1; then
    amixer set Master 85% unmute 2>/dev/null || true
    amixer set PCM 85% unmute 2>/dev/null || true
fi

# ------------------------------------------------------------------- session
# Locate the installation without hard-coding a home directory.
if [ -x /opt/gba-distro/bin/gba-supervisor ]; then
    SUPERVISOR=/opt/gba-distro/bin/gba-supervisor
elif [ -f "$(dirname "$0")/gba_supervisor.py" ]; then
    SUPERVISOR="python3 $(dirname "$0")/gba_supervisor.py"
elif [ -f "$(dirname "$0")/../src/gba_supervisor.py" ]; then
    SUPERVISOR="python3 $(dirname "$0")/../src/gba_supervisor.py"
else
    SUPERVISOR=""
fi

if [ -z "$SUPERVISOR" ]; then
    echo "[init] GBA OS is not installed correctly (gba-supervisor missing)." >&2
    echo "[init] Nothing was damaged: this is a rescue console." >&2
    rescue_shell
fi

# A missing interpreter is not a crash: say so once, then give a console.
case "$SUPERVISOR" in
    python3*)
        if ! have python3; then
            echo "[init] python3 is missing, so the GBA OS shell cannot start." >&2
            echo "[init] This is a rescue console; the machine is otherwise fine." >&2
            rescue_shell
        fi
        ;;
esac

# Boot parameters set from the GRUB menu:
#   gba_safe=1  -> skip the graphical shell entirely (safe graphics / recovery)
#   gba_mode=0  -> do not run the appliance at all (plain getty)
if grep -q "gba_mode=0" /proc/cmdline 2>/dev/null; then
    echo "[init] gba_mode=0: the GBA OS shell is disabled on this boot."
    rescue_shell
fi

if grep -q "gba_safe=1" /proc/cmdline 2>/dev/null; then
    echo "[init] gba_safe=1: starting the recovery console instead of the shell."
    recovery=""
    [ -x /opt/gba-distro/bin/gba-recovery.py ] && recovery=/opt/gba-distro/bin/gba-recovery.py
    if [ -n "$recovery" ] && have python3; then
        python3 "$recovery" || true
    fi
    rescue_shell
fi

[ -t 1 ] && clear
echo ""
echo "=========================================================="
echo "                   BOOTING GBA OS V1                     "
echo "=========================================================="
echo ""

export TERM="${TERM:-linux}"
export SYS_CONTRACT=1
export HOME="${HOME:-/root}"
export GBA_LOG_FILE="${GBA_LOG_FILE:-/data/logs/latest.log}"

# PID 1 owns the console; a clean reboot/poweroff request comes back as an
# exit code (100/101) from the supervisor. Anything else means the supervisor
# died, so we re-enter recovery instead of guessing.
while :; do
    # shellcheck disable=SC2086
    $SUPERVISOR --appliance
    SUPERVISOR_RET=$?

    case "$SUPERVISOR_RET" in
        100)
            echo "[init] Rebooting to the GRUB menu..."
            do_reboot
            ;;
        101)
            echo "[init] Powering off..."
            do_poweroff
            ;;
        *)
            echo "[init] Supervisor exited with status $SUPERVISOR_RET; entering recovery..."
            # Recovery first, then a plain shell, then a console that never dies.
            if [ -x /opt/gba-distro/bin/gba-recovery.py ] && have python3; then
                python3 /opt/gba-distro/bin/gba-recovery.py || true
            elif [ -f "$(dirname "$0")/gba_recovery.py" ] && have python3; then
                python3 "$(dirname "$0")/gba_recovery.py" || true
            else
                rescue_shell
            fi
            # A crash loop must not spin the CPU: give the user five seconds to
            # read what happened before trying again.
            sleep 5
            ;;
    esac
done
