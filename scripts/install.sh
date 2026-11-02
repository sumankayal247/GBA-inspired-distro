#!/bin/bash
# ==============================================================================
# GBA OS — System Installer & GRUB Integrator
#
#   sudo ./scripts/install.sh              install / upgrade
#   sudo ./scripts/install.sh --uninstall  remove the GRUB entry and files
#
# Configures /opt/gba-distro, /data, /games and registers a top-level GRUB
# entry that boots into the appliance. Safe to re-run: every step is idempotent.
# ==============================================================================

set -euo pipefail

INSTALL_DIR="/opt/gba-distro"
GRUB_ENTRY="/etc/grub.d/11_gba_os"
GRUB_DEFAULTS="/etc/default/grub.d/99_gba.cfg"
TTY_WRAPPER="/usr/local/bin/gba-tty1-wrapper"
SESSION_RUNNER="/usr/local/bin/gba-session"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log()  { printf '  \033[1;36m[gba]\033[0m %s\n' "$*"; }
warn() { printf '  \033[1;33m[gba]\033[0m %s\n' "$*"; }
die()  { printf '  \033[1;31m[gba]\033[0m %s\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -ne 0 ]; then
    die "This script must be run as root (or via sudo)."
fi

# The account that owns /games and /data. Never hard-code a name.
SESSION_USER="${GBA_SESSION_USER:-${SUDO_USER:-}}"
if [ -z "$SESSION_USER" ] || [ "$SESSION_USER" = "root" ]; then
    SESSION_USER="$(awk -F: '$3>=1000 && $3<60000 {print $1; exit}' /etc/passwd || true)"
fi
SESSION_HOME="$(getent passwd "$SESSION_USER" 2>/dev/null | cut -d: -f6 || true)"
[ -n "${SESSION_HOME:-}" ] || SESSION_HOME="/root"

# ------------------------------------------------------------------ uninstall
if [ "${1:-}" = "--uninstall" ]; then
    log "Removing GRUB entry and appliance hooks..."
    rm -f "$GRUB_ENTRY" "$GRUB_DEFAULTS" "$TTY_WRAPPER" "$SESSION_RUNNER"
    if [ -f /etc/inittab ]; then
        sed -i 's|^1:2345:respawn:/usr/local/bin/gba-tty1-wrapper tty1|1:2345:respawn:/sbin/getty --noclear 38400 tty1|' /etc/inittab || true
    fi
    if command -v update-grub >/dev/null 2>&1; then
        update-grub || warn "GRUB update reported an error; check /boot/grub/grub.cfg"
    fi
    log "System files removed. /opt/gba-distro, /data and /games were kept."
    log "Delete them manually if you also want to drop saves and ROMs."
    exit 0
fi

log "Repository: $REPO_DIR"
log "Session user: $SESSION_USER ($SESSION_HOME)"

echo "=== 1. Installing files into $INSTALL_DIR ==="
mkdir -p "$INSTALL_DIR/bin" "$INSTALL_DIR/handlers" "$INSTALL_DIR/src" "$INSTALL_DIR/assets/fonts"
mkdir -p /data/saves /data/library /data/logs /data/config /data/apps /games /run/gba

install -m 0644 "$REPO_DIR/src/common.py"          "$INSTALL_DIR/src/common.py"
install -m 0644 "$REPO_DIR/src/library.py"         "$INSTALL_DIR/src/library.py"
install -m 0644 "$REPO_DIR/src/config.py"          "$INSTALL_DIR/src/config.py"
install -m 0644 "$REPO_DIR/src/theme.py"           "$INSTALL_DIR/src/theme.py"
install -m 0644 "$REPO_DIR/src/gba_terminal.py"    "$INSTALL_DIR/src/gba_terminal.py"
install -m 0644 "$REPO_DIR/src/gba_browser.py"     "$INSTALL_DIR/src/gba_browser.py"
install -m 0644 "$REPO_DIR/src/gba_usb.py"         "$INSTALL_DIR/src/gba_usb.py"
install -m 0755 "$REPO_DIR/src/gba_supervisor.py"  "$INSTALL_DIR/bin/gba-supervisor"
install -m 0755 "$REPO_DIR/src/gba_launcher.py"    "$INSTALL_DIR/bin/gba-launcher"
install -m 0755 "$REPO_DIR/src/gba_recovery.py"    "$INSTALL_DIR/bin/gba-recovery.py"
install -m 0755 "$REPO_DIR/src/gba_init.sh"        "$INSTALL_DIR/bin/gba-init"
install -m 0755 "$REPO_DIR/src/gba_adapter_mgba.py" "$INSTALL_DIR/handlers/mgba-adapter"

# Optional bundled font (the shell falls back to a system TTF or its own).
for font in DejaVuSans.ttf DejaVuSans-Bold.ttf; do
    src="$REPO_DIR/assets/fonts/$font"
    [ -f "$src" ] && install -m 0644 "$src" "$INSTALL_DIR/assets/fonts/$font"
done

# Keep the ownership permissive enough that the desktop user can drop ROMs in.
chown -R "$SESSION_USER":"$SESSION_USER" /games /data 2>/dev/null || true
chmod -R 775 /games /data 2>/dev/null || true

echo "=== 2. Checking runtime dependencies ==="
MISSING=()
command -v python3 >/dev/null 2>&1 || MISSING+=("python3")
command -v mgba    >/dev/null 2>&1 || MISSING+=("mgba-sdl")
python3 -c "import pygame" >/dev/null 2>&1 || MISSING+=("python3-pygame")

if [ ${#MISSING[@]} -gt 0 ]; then
    warn "Missing packages: ${MISSING[*]}"
    warn "Install them before booting the appliance, e.g.:"
    warn "    apt install python3 python3-pygame mgba-sdl   (or: pip install pygame)"
else
    log "All runtime dependencies present."
fi

echo "=== 3. Writing the input / display profile ==="
mkdir -p "$SESSION_HOME/.config/mgba"
python3 - "$INSTALL_DIR" "$SESSION_HOME" <<'PYEOF'
import os, sys, configparser
install_dir, home = sys.argv[1], sys.argv[2]
sys.path.insert(0, os.path.join(install_dir, "src"))
try:
    import common
    common.get_data_dir = lambda: "/data"
    bindings = common.load_keybindings()
    common.save_keybindings(bindings)
    print("  [gba] key profile written to /data/config and ~/.config/mgba")
except Exception as exc:
    print(f"  [gba] could not write input profile: {exc}")
PYEOF
chown -R "$SESSION_USER":"$SESSION_USER" "$SESSION_HOME/.config/mgba" 2>/dev/null || true

echo "=== 4. Installing the session runner (X11 compatibility mode) ==="
cat > "$SESSION_RUNNER" << 'EOF_SESSION'
#!/bin/bash
# Minimal X11 session for mGBA's SDL backend. No desktop environment runs here:
# the shell is the launcher, and the launcher hands the display to the emulator.
export DISPLAY="${DISPLAY:-:0}"
xhost +local: >/dev/null 2>&1 || true
exec /opt/gba-distro/bin/gba-supervisor --appliance
EOF_SESSION
chmod 0755 "$SESSION_RUNNER"

cat > "$TTY_WRAPPER" << 'EOF_TTY'
#!/bin/bash
# GBA OS appliance console wrapper: launches the shell on tty1 with persistent,
# size-capped logging. Falls back to a normal login prompt when not in GBA mode.
if grep -q "gba_mode=1" /proc/cmdline 2>/dev/null; then
    exec 0</dev/tty1 1>/dev/tty1 2>&1
    clear

    mkdir -p /data/logs /run/gba /tmp/gba-run /tmp/.X11-unix
    chmod 1777 /tmp/.X11-unix 2>/dev/null || true

    BOOT_TIMESTAMP=$(date +%Y-%m-%d_%H-%M-%S)
    LOG_FILE="/data/logs/boot_${BOOT_TIMESTAMP}.log"
    LATEST_LOG="/data/logs/latest.log"
    rm -f "$LATEST_LOG" 2>/dev/null || true
    ln -sf "$LOG_FILE" "$LATEST_LOG"

    exec > >(tee -a "$LOG_FILE") 2>&1

    echo "=========================================================="
    echo "                   LAUNCHING GBA OS V1                    "
    echo "=========================================================="
    echo "[boot] Timestamp: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "[boot] Session log: $LOG_FILE"
    echo "[boot] Kernel cmdline: $(cat /proc/cmdline 2>/dev/null)"
    echo "[boot] DRI devices:"; ls -l /dev/dri/ 2>/dev/null || echo "  (none)"
    echo "[boot] ROMs in /games:"; ls -lh /games/ 2>/dev/null || echo "  (none)"

    export TERM=linux
    export SYS_CONTRACT=1
    export GBA_LOG_FILE="$LOG_FILE"

    xinit /usr/local/bin/gba-session -- :0 vt1 -nocursor -quiet
    EXIT_CODE=$?
    echo "[boot] GBA OS session exited with status $EXIT_CODE"
    [ "$EXIT_CODE" -ne 0 ] && sleep 5
else
    exec /sbin/getty --noclear 38400 "$@"
fi
EOF_TTY
chmod 0755 "$TTY_WRAPPER"

echo "=== 5. Hooking the appliance into init ==="
if [ -f /etc/inittab ] && ! grep -q "gba-tty1-wrapper" /etc/inittab; then
    cp /etc/inittab /etc/inittab.gba-backup
    sed -i 's|^1:2345:respawn:/sbin/getty --noclear 38400 tty1|1:2345:respawn:/usr/local/bin/gba-tty1-wrapper tty1|' /etc/inittab
    log "inittab patched (backup: /etc/inittab.gba-backup)"
fi

if [ -f /etc/sv/slimski/run ] && ! grep -q "gba_mode=1" /etc/sv/slimski/run; then
    sed -i '/set -e/a \\n# Disable the display manager when booting into GBA OS mode\nif grep -q "gba_mode=1" /proc/cmdline 2>/dev/null; then\n    exit 161\nfi' /etc/sv/slimski/run
fi
if [ -f /etc/sv/getty-tty1/run ] && ! grep -q "gba_mode=1" /etc/sv/getty-tty1/run; then
    sed -i '1a \\n# GBA OS boot hook\nif grep -q "gba_mode=1" /proc/cmdline 2>/dev/null; then\n    exec /usr/local/bin/gba-tty1-wrapper tty1\nfi' /etc/sv/getty-tty1/run
fi

mkdir -p /etc/X11
cat > /etc/X11/Xwrapper.config << 'EOF_XWRAPPER'
allowed_users=anybody
needs_root_rights=auto
EOF_XWRAPPER

echo "=== 6. Registering the GRUB entry ==="
ROOT_UUID="$(findmnt -n -o UUID / 2>/dev/null || blkid -s UUID -o value "$(findmnt -n -o SOURCE /)" 2>/dev/null || true)"
[ -n "$ROOT_UUID" ] || die "Could not determine the root filesystem UUID."

# Pick the newest installed kernel instead of hard-coding a version.
KERNEL_PATH="$(ls -1t /boot/vmlinuz-* 2>/dev/null | head -n1 || true)"
INITRD_PATH="$(ls -1t /boot/initrd.img-* 2>/dev/null | head -n1 || true)"
[ -n "$KERNEL_PATH" ] || die "No kernel found in /boot."
[ -n "$INITRD_PATH" ] || die "No initrd found in /boot."
log "Kernel: $KERNEL_PATH"
log "Initrd: $INITRD_PATH"
log "Root UUID: $ROOT_UUID"

mkdir -p /etc/default/grub.d
cat > "$GRUB_DEFAULTS" << 'EOF_GRUB_D'
# Always show the GRUB menu so the machine can also boot the primary OS.
GRUB_TIMEOUT_STYLE=menu
GRUB_TIMEOUT=10
GRUB_RECORDFAIL_TIMEOUT=10
GRUB_GFXMODE=auto,keep
GRUB_DISABLE_SUBMENU=n
EOF_GRUB_D

cat > "$GRUB_ENTRY" << EOF
#!/bin/sh
exec tail -n +3 \$0
# GBA OS appliance entry (top-level, after the primary OS entry)
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
chmod 0755 "$GRUB_ENTRY"

# Remove any legacy custom entry from older installers.
if [ -f /etc/grub.d/40_custom ]; then
    sed -i "/### BEGIN GBA OS ###/,/### END GBA OS ###/d" /etc/grub.d/40_custom || true
fi

if command -v update-grub >/dev/null 2>&1; then
    update-grub
elif command -v grub-mkconfig >/dev/null 2>&1; then
    grub-mkconfig -o /boot/grub/grub.cfg
else
    warn "GRUB tooling not found; run update-grub manually."
fi

# The primary OS stays the default boot target: GBA OS is an opt-in entry.
if command -v grub-set-default >/dev/null 2>&1; then
    grub-set-default 0 || true
fi

echo "=== 7. Desktop shortcut for windowed testing ==="
if [ -d "$SESSION_HOME/Desktop" ]; then
    cat > "$SESSION_HOME/Desktop/gba-os.desktop" << EOF
[Desktop Entry]
Type=Application
Name=GBA OS (Windowed Test)
Comment=Run the GBA OS console shell in a window
Exec=$REPO_DIR/scripts/launch_desktop.sh
Terminal=false
Icon=input-gaming
Categories=Game;
EOF
    chmod 0755 "$SESSION_HOME/Desktop/gba-os.desktop"
    chown "$SESSION_USER":"$SESSION_USER" "$SESSION_HOME/Desktop/gba-os.desktop" 2>/dev/null || true
fi

echo
echo "=== GBA OS V1 installation complete ==="
echo "  Add games : copy .gba/.gbc/.gb files into /games/"
echo "  Test now  : ./scripts/launch_desktop.sh"
echo "  Appliance : reboot and pick 'GBA OS (V1 Appliance Mode)'"
echo "  Remove    : sudo ./scripts/install.sh --uninstall"
