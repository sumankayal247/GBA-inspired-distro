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
    export SDL_VIDEODRIVER=KMSDRM
    export SDL_KMSDRM_DEVICE_INDEX=0
    export SYS_CONTRACT=1
    cd /home/suman
    exec su - suman -c "export SDL_VIDEODRIVER=KMSDRM; export SDL_KMSDRM_DEVICE_INDEX=0; /opt/gba-distro/bin/gba-supervisor --appliance"
else
    exec /sbin/getty --noclear 38400 "$@"
fi
EOF_TTY
chmod +x /usr/local/bin/gba-tty1-wrapper

# 2. Configure /etc/inittab for tty1 wrapper
if [ -f /etc/inittab ] && ! grep -q "gba-tty1-wrapper" /etc/inittab; then
    sed -i 's|^1:2345:respawn:/sbin/getty --noclear 38400 tty1|1:2345:respawn:/usr/local/bin/gba-tty1-wrapper tty1|' /etc/inittab
fi

# 3. Configure slimski to bypass display manager when in GBA mode
if [ -f /etc/init.d/slimski ] && ! grep -q "gba_mode=1" /etc/init.d/slimski; then
    sed -i '/start)/a \    if grep -q "gba_mode=1" /proc/cmdline 2>/dev/null; then\n       log_daemon_msg "GBA OS appliance mode active: skipping display manager." "$NAME"\n       exit 0\n    fi' /etc/init.d/slimski
fi

# 4. Configure GRUB 40_custom
GRUB_CUSTOM="/etc/grub.d/40_custom"

# Remove any previous GBA OS entry in 40_custom
sed -i '/### BEGIN GBA OS ###/,/### END GBA OS ###/d' "$GRUB_CUSTOM"

cat <<EOF >> "$GRUB_CUSTOM"
### BEGIN GBA OS ###
menuentry 'GBA OS (V1 Appliance Mode)' --class gba --class gnu-linux --class os {
	load_video
	insmod gzio
	insmod part_gpt
	insmod ext2
	search --no-floppy --fs-uuid --set=root $ROOT_UUID
	echo 'Starting GBA OS Appliance...'
	linux $KERNEL_PATH root=UUID=$ROOT_UUID ro quiet selinux=0 gba_mode=1
	initrd $INITRD_PATH
}
### END GBA OS ###
EOF

echo "Updating GRUB configuration..."
update-grub

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
