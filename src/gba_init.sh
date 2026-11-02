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
    echo "[init] FATAL: gba-supervisor not found; dropping to a shell." >&2
    exec /bin/sh
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
            sync
            reboot -f
            ;;
        101)
            echo "[init] Powering off..."
            sync
            poweroff -f
            ;;
        *)
            echo "[init] Supervisor exited with status $SUPERVISOR_RET; entering recovery..."
            if [ -x /opt/gba-distro/bin/gba-recovery.py ]; then
                python3 /opt/gba-distro/bin/gba-recovery.py
            elif [ -f "$(dirname "$0")/gba_recovery.py" ]; then
                python3 "$(dirname "$0")/gba_recovery.py"
            else
                /bin/sh
            fi
            ;;
    esac
done
