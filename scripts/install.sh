#!/bin/bash
# ==============================================================================
# GBA OS — System Installer & GRUB Integrator
# Configures /opt/gba-distro, /data, /games, and registers GBA OS into GRUB.
# ==============================================================================

set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "This script must be run as root (or via sudo)."
    exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="/opt/gba-distro"

echo "=== 1. Setting up /opt/gba-distro ==="
mkdir -p "$INSTALL_DIR/bin" "$INSTALL_DIR/handlers" "$INSTALL_DIR/src" "$INSTALL_DIR/assets"
mkdir -p /data/saves /data/library /data/logs /data/apps /games

cp "$REPO_DIR/src/common.py" "$INSTALL_DIR/src/"
cp "$REPO_DIR/src/library.py" "$INSTALL_DIR/src/"
cp "$REPO_DIR/src/gba_supervisor.py" "$INSTALL_DIR/bin/gba-supervisor"
cp "$REPO_DIR/src/gba_launcher.py" "$INSTALL_DIR/bin/gba-launcher"
cp "$REPO_DIR/src/gba_recovery.py" "$INSTALL_DIR/bin/gba-recovery.py"
cp "$REPO_DIR/src/gba_init.sh" "$INSTALL_DIR/bin/gba-init"
cp "$REPO_DIR/src/gba_adapter_mgba.py" "$INSTALL_DIR/handlers/mgba-adapter"

chmod +x "$INSTALL_DIR/bin/"*
chmod +x "$INSTALL_DIR/handlers/"*

# Ensure regular user can add games easily
chown -R suman:suman /games /data 2>/dev/null || true
chmod -R 775 /games /data 2>/dev/null || true

echo "=== 2. Configuring GRUB Menu Entry & TTY1 Wrapper ==="
# Find root filesystem UUID
ROOT_UUID=$(findmnt -n -o UUID / || blkid -s UUID -o value /dev/sdb4)
KERNEL_PATH="/boot/vmlinuz-5.10.240-antix.1-amd64-smp"
INITRD_PATH="/boot/initrd.img-5.10.240-antix.1-amd64-smp"

if [ ! -f "$KERNEL_PATH" ]; then
    KERNEL_PATH="/boot/vmlinuz-6.6.119-antix.1-amd64-smp"
    INITRD_PATH="/boot/initrd.img-6.6.119-antix.1-amd64-smp"
fi

# 1. Install TTY1 wrapper
cat << "EOF_TTY" > /usr/local/bin/gba-tty1-wrapper
#!/bin/bash
if grep -q "gba_mode=1" /proc/cmdline 2>/dev/null; then
    exec 0</dev/tty1 1>/dev/tty1 2>&1
    clear
    echo "=========================================================="
    echo "                   LAUNCHING GBA OS V1                    "
    echo "=========================================================="
    export HOME=/home/suman
    export USER=suman
    export TERM=linux
    export SYS_CONTRACT=1
    cd /home/suman

    # Try KMSDRM directly first
    if su - suman -c "export SDL_VIDEODRIVER=KMSDRM; export SDL_KMSDRM_DEVICE_INDEX=0; python3 -c \"import sys, pygame; pygame.display.init(); sys.exit(0)\"" >/dev/null 2>&1; then
        echo "Starting GBA OS appliance via KMSDRM..."
        exec su - suman -c "export SDL_VIDEODRIVER=KMSDRM; export SDL_KMSDRM_DEVICE_INDEX=0; /opt/gba-distro/bin/gba-supervisor --appliance"
    else
        echo "Starting dedicated GBA OS appliance session..."
        exec su - suman -c "xinit /opt/gba-distro/bin/gba-supervisor --appliance -- :0 vt1 -nocursor -quiet"
    fi
else
    exec /sbin/getty --noclear 38400 "$@"
fi
EOF_TTY
chmod +x /usr/local/bin/gba-tty1-wrapper

# 2. Configure /etc/inittab (SysVinit systems)
if [ -f /etc/inittab ] && ! grep -q "gba-tty1-wrapper" /etc/inittab; then
    sed -i 's|^1:2345:respawn:/sbin/getty --noclear 38400 tty1|1:2345:respawn:/usr/local/bin/gba-tty1-wrapper tty1|' /etc/inittab
fi

# 3. Configure runit services (antiX runit systems)
if [ -f /etc/sv/slimski/run ] && ! grep -q "gba_mode=1" /etc/sv/slimski/run; then
    sed -i '/set -e/a \\n# Disable slimski display manager if booting into GBA OS mode\nif grep -q "gba_mode=1" /proc/cmdline 2>/dev/null; then\n    exit 161\nfi' /etc/sv/slimski/run
fi

if [ -f /etc/sv/getty-tty1/run ] && ! grep -q "gba_mode=1" /etc/sv/getty-tty1/run; then
    sed -i '1a \\n# GBA OS boot hook\nif grep -q "gba_mode=1" /proc/cmdline 2>/dev/null; then\n    exec /usr/local/bin/gba-tty1-wrapper tty1\nfi' /etc/sv/getty-tty1/run
fi

# Configure Xwrapper for unprivileged xinit fallback if needed
cat << "EOF_XWRAPPER" > /etc/X11/Xwrapper.config
allowed_users=anybody
needs_root_rights=auto
EOF_XWRAPPER

# 4. Configure GRUB menu display override
mkdir -p /etc/default/grub.d
cat << "EOF_GRUB_D" > /etc/default/grub.d/99_gba.cfg
# Force GRUB menu to ALWAYS show with a 10-second countdown
GRUB_TIMEOUT_STYLE=menu
GRUB_TIMEOUT=10
GRUB_RECORDFAIL_TIMEOUT=10
GRUB_GFXMODE=auto,keep
GRUB_DISABLE_SUBMENU=n
EOF_GRUB_D

# 5. Configure GRUB 11_gba_os entry
cat <<EOF > /etc/grub.d/11_gba_os
#!/bin/sh
exec tail -n +3 \$0
# GBA OS Top-Level Entry (Placed directly after primary antiX entry)
menuentry "GBA OS (V1 Appliance Mode)" --class gba --class gnu-linux --class os {
	load_video
	insmod gzio
	insmod part_gpt
	insmod ext2
	search --no-floppy --fs-uuid --set=root $ROOT_UUID
	echo "Starting GBA OS Appliance..."
	linux $KERNEL_PATH root=UUID=$ROOT_UUID ro quiet selinux=0 gba_mode=1
	initrd $INITRD_PATH
}
EOF
chmod +x /etc/grub.d/11_gba_os

# Clean any old entry in 40_custom
if [ -f /etc/grub.d/40_custom ]; then
    sed -i "/### BEGIN GBA OS ###/,/### END GBA OS ###/d" /etc/grub.d/40_custom
fi

echo "Updating GRUB configuration..."
update-grub
grub-set-default 0

echo "=== 3. Creating Desktop Shortcut ==="
cat <<EOF > /home/suman/Desktop/gba-os.desktop
[Desktop Entry]
Name=GBA OS (Windowed Test)
Comment=Play Game Boy Advance Console Shell
Exec=/home/suman/Desktop/GBA-inspired-distro/scripts/launch_desktop.sh
Terminal=false
Type=Application
Icon=input-gaming
Categories=Game;
EOF
chmod +x /home/suman/Desktop/gba-os.desktop
chown suman:suman /home/suman/Desktop/gba-os.desktop 2>/dev/null || true

echo "=== GBA OS V1 Installation Complete! ==="
