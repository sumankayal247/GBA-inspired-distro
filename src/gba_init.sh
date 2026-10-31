#!/bin/bash
# ==============================================================================
# GBA OS — Bare-Metal Appliance PID 1 Init Script
# Boots directly from GRUB into the GBA console shell with no desktop overhead.
# Implements Rules B1-B9, S1-S8, F1-F2 from docs/CONTRACT.md
# ==============================================================================

set -e

# Mount pseudo filesystems if not mounted
[ -d /proc/1 ] || mount -t proc proc /proc
[ -d /sys/kernel ] || mount -t sysfs sysfs /sys
[ -d /dev/pts ] || mount -t devpts devpts /dev/pts
[ -d /dev/shm ] || mount -t tmpfs tmpfs /dev/shm
[ -d /run ] || mount -t tmpfs tmpfs /run

# Trigger device recognition for GPU and Gamepads
if command -v udevd >/dev/null 2>&1; then
    udevd --daemon || true
    udevadm trigger || true
    udevadm settle || true
fi

# Ensure storage directories exist
mkdir -p /data/saves /data/library /data/logs /games /run/gba /tmp

# Configure sound volume (unmute master/PCM if amixer is present)
if command -v amixer >/dev/null 2>&1; then
    amixer set Master 85% unmute 2>/dev/null || true
    amixer set PCM 85% unmute 2>/dev/null || true
fi

# Clear console
clear
echo ""
echo "=========================================================="
echo "                   BOOTING GBA OS V1                      "
echo "=========================================================="
echo ""

export HOME=/root
export TERM=linux
export SDL_VIDEODRIVER=kmsdrm
export SYS_CONTRACT=1

# Main Supervisor Execution Loop
while true; do
    if [ -x /opt/gba-distro/bin/gba-supervisor ]; then
        /opt/gba-distro/bin/gba-supervisor --appliance || SUPERVISOR_RET=$?
    else
        python3 /home/suman/Desktop/GBA-inspired-distro/src/gba_supervisor.py --appliance || SUPERVISOR_RET=$?
    fi

    # Check termination signal / request
    if [ "$SUPERVISOR_RET" = "100" ]; then
        echo "Rebooting system to GRUB..."
        sync
        reboot -f
    elif [ "$SUPERVISOR_RET" = "101" ]; then
        echo "Shutting down system..."
        sync
        poweroff -f
    else
        echo "Launching emergency recovery..."
        if [ -x /opt/gba-distro/bin/gba-recovery.py ]; then
            python3 /opt/gba-distro/bin/gba-recovery.py
        else
            /bin/bash
        fi
    fi
done
