#!/bin/bash
# ==============================================================================
# GBA OS — on-system installer (adds the appliance to an existing Linux)
#
#   sudo ./scripts/install.sh                 dry-run: show exactly what would change
#   sudo ./scripts/install.sh --apply         perform the install (asks once)
#   sudo ./scripts/install.sh --apply --yes   no prompts (CI / scripted)
#   sudo ./scripts/install.sh --repair        re-apply files after a failed update
#   sudo ./scripts/install.sh --uninstall     remove hooks, keep saves and ROMs
#   sudo ./scripts/install.sh --dry-run       same as no arguments (explicit)
#   sudo ./scripts/install.sh --apply --no-boot   install files, leave GRUB alone
#   sudo ./scripts/install.sh --with-x11      force the X11 session (not appliance)
#
# Safety contract (see docs/SAFETY.md):
#   * Nothing is written unless --apply is given.
#   * Every system file touched is backed up, and is restored automatically if a
#     later step fails.
#   * The boot menu is regenerated LAST and verified; if our entry is missing or
#     grub-mkconfig fails, the previous grub.cfg is put back.
#   * The default boot target is never changed: GBA OS is an extra menu entry.
#   * Firmware/NVRAM is never written.
# ==============================================================================

set -Eeuo pipefail
export PATH="$PATH:/usr/games:/usr/local/games"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/common.sh
. "$REPO_DIR/scripts/lib/common.sh"

INSTALL_DIR="${GBA_INSTALL_DIR:-/opt/gba-distro}"
DATA_DIR="${GBA_DATA_DIR:-/data}"
GAMES_DIR="${GBA_GAMES_DIR:-/games}"
GRUB_ENTRY="/etc/grub.d/11_gba_os"
GRUB_DEFAULTS="/etc/default/grub.d/99_gba.cfg"
TTY_WRAPPER="/usr/local/bin/gba-tty1-wrapper"
SESSION_RUNNER="/usr/local/bin/gba-session"
SELFTEST="/usr/local/bin/gba-selftest"
SETUP_WRAPPER="/usr/local/bin/gba-setup"

# The account that owns /games and /data. Never hard-code a name.
SESSION_USER="${GBA_SESSION_USER:-${SUDO_USER:-}}"
if [ -z "$SESSION_USER" ] || [ "$SESSION_USER" = "root" ]; then
    SESSION_USER="$(awk -F: '$3>=1000 && $3<60000 {print $1; exit}' /etc/passwd || true)"
fi
SESSION_HOME="$(getent passwd "$SESSION_USER" 2>/dev/null | cut -d: -f6 || true)"
[ -n "${SESSION_HOME:-}" ] || SESSION_HOME="/root"
SESSION_USER="${SESSION_USER:-root}"

APPLY=0
UNINSTALL=0
REPAIR=0
WRITE_BOOT=1
WANT_X11="auto"

usage() {
    # Print the header comment only: stop at the first line that is not a comment.
    awk 'NR > 1 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --apply) APPLY=1 ;;
        --dry-run) APPLY=0 ;;
        --yes|-y) GB_ASSUME_YES=1 ;;
        --uninstall) UNINSTALL=1 ;;
        --repair) REPAIR=1 ;;
        --no-boot) WRITE_BOOT=0 ;;
        --with-x11) WANT_X11=1 ;;
        --no-x11) WANT_X11=0 ;;
        -h|--help) usage; exit 0 ;;
        *) gb_err "Unknown option: $1"; usage; exit 2 ;;
    esac
    shift
done

[ "$APPLY" = "1" ] && GB_DRY_RUN=0 || GB_DRY_RUN=1

# ----------------------------------------------------------------- uninstall --
do_uninstall() {
    gb_step "Removing the GBA OS boot entry and hooks"
    if [ "$APPLY" != "1" ]; then
        gb_dim "  (dry-run) would remove $GRUB_ENTRY, $GRUB_DEFAULTS, $TTY_WRAPPER, $SESSION_RUNNER"
        gb_dim "  (dry-run) would restore /etc/inittab from /etc/inittab.gba-backup if present"
        gb_dim "  (dry-run) would re-run update-grub"
        gb_info "Re-run with --apply --uninstall to do it."
        return 0
    fi
    gb_require_root
    gb_backup_file /etc/inittab
    gb_install_trap

    rm -f "$GRUB_ENTRY" "$GRUB_DEFAULTS" "$TTY_WRAPPER" "$SESSION_RUNNER" "$SELFTEST"
    if [ -f /etc/inittab.gba-backup ]; then
        cp -a /etc/inittab.gba-backup /etc/inittab
        gb_ok "restored /etc/inittab from the installer backup"
    fi
    # Reverse the runit/s6 hooks we added, if any.
    for hook in /etc/sv/slimski/run /etc/sv/getty-tty1/run; do
        [ -f "$hook" ] || continue
        if grep -q "GBA OS boot hook" "$hook" 2>/dev/null; then
            gb_backup_file "$hook"
            python3 - "$hook" <<'PY'
import sys
path = sys.argv[1]
lines = open(path, encoding="utf-8").read().splitlines(keepends=True)
out, skip = [], 0
for line in lines:
    if "# GBA OS boot hook" in line:
        skip = 3
        continue
    if skip:
        skip -= 1
        continue
    out.append(line)
open(path, "w", encoding="utf-8").writelines(out)
PY
        fi
    done

    gb_step "Rebuilding the boot menu without GBA OS"
    if GBA_GRUB_CFG=$(grub_cfg_path); then
        gb_backup_file "$GBA_GRUB_CFG"
    fi
    run_grub_update || gb_warn "Boot menu regeneration failed; restore from $GB_RUN_BACKUP_DIR if needed."
    gb_disarm_trap
    gb_ok "GBA OS removed. $INSTALL_DIR, $DATA_DIR and $GAMES_DIR were kept."
    gb_info "Delete them manually if you also want to drop saves and ROMs."
}

do_repair() {
    gb_step "Repairing the existing GBA OS install"
    if [ "$APPLY" != "1" ]; then
        gb_info "(dry-run) repair would re-copy every GBA OS file, restore missing"
        gb_info "wrappers and re-add the boot menu entry if it went missing."
        gb_info "Your saves, ROMs and settings are never touched."
    fi
    if [ ! -d "$INSTALL_DIR" ]; then
        gb_die "There is no GBA OS install at $INSTALL_DIR to repair.
Run '$0 --apply' to install it for the first time."
    fi

    install_automount_dependencies
    install_files
    install_ownership
    install_wrappers
    install_desktop_shortcut

    local cfg
    cfg="$(grub_cfg_path || true)"
    if [ -n "$cfg" ] && grep -q "GBA OS (console appliance)" "$cfg" 2>/dev/null; then
        gb_ok "Boot menu entry is present."
    elif [ "$WRITE_BOOT" = "1" ]; then
        gb_warn "Boot menu entry missing; re-adding it."
        install_boot_entry || gb_die "Could not restore the boot entry. Your existing
boot menu was left as it was; run '$0 --apply --repair' again or check
/etc/grub.d/11_gba_os."
    fi

    if [ "$APPLY" = "1" ]; then
        run_selftest || gb_warn "Self-test reported problems (see above)."
        gb_ok "Repair finished. Reboot and pick 'GBA OS (console appliance)'."
    else
        gb_ok "(dry-run) repair plan printed; nothing was changed."
    fi
}

# ---------------------------------------------------------------- preflight --
check_dependencies() {
    gb_step "Checking runtime dependencies"
    local missing=()
    command -v python3 >/dev/null 2>&1 || missing+=("python3")
    command -v python3 >/dev/null 2>&1 && ! python3 -c "import pygame" 2>/dev/null \
        && missing+=("python3-pygame")
    command -v mgba >/dev/null 2>&1 || missing+=("mgba-sdl")

    # Optional: Android phones in File Transfer/MTP mode are not block disks.
    # Normal USB stick import and game launching work without this package.
    if ! command -v jmtpfs >/dev/null 2>&1 && ! command -v gio >/dev/null 2>&1; then
        gb_warn "Android MTP import needs jmtpfs (or GVfs): sudo apt install jmtpfs mtp-tools"
        gb_warn "USB pendrive import is unaffected."
    elif [ ! -e /dev/fuse ]; then
        # jmtpfs can be installed and still be unable to mount anything.
        gb_warn "jmtpfs is installed but /dev/fuse is missing: sudo modprobe fuse"
        gb_warn "Android MTP import will not work until FUSE is available."
    elif ! command -v fusermount3 >/dev/null 2>&1 && ! command -v fusermount >/dev/null 2>&1; then
        gb_warn "No fusermount found; install the FUSE userland: sudo apt install fuse3"
    fi

    if [ ${#missing[@]} -gt 0 ]; then
        gb_warn "Missing packages: ${missing[*]}"
        gb_warn "Install them first (the appliance can still be installed, but games"
        gb_warn "will not start until mGBA and pygame are present):"
        gb_warn "    sudo apt install python3 python3-pygame mgba-sdl"
        if [ "$APPLY" = "1" ] && [ "${GBA_ALLOW_MISSING_DEPS:-0}" != "1" ]; then
            gb_die "Refusing to touch the boot configuration with incomplete runtime deps.
Set GBA_ALLOW_MISSING_DEPS=1 to override (the installer will write a working
recovery-first setup, but games will not launch until you install them)."
        fi
        return 1
    fi
    gb_ok "python3, pygame and mGBA are present."
    return 0
}

# Install only the headless automount stack, never desktop recommendations.
install_automount_dependencies() {
    gb_step "Installing lightweight USB automount support"
    if gb_is_dry_run; then
        gb_dim "  (dry-run) would install udiskie udisks2 dbus util-linux without recommends"
        return 0
    fi
    if ! command -v apt-get >/dev/null 2>&1; then
        gb_die "Install udiskie, udisks2, dbus and util-linux with your package manager,
then retry. Automatic dependency installation currently supports Debian/Ubuntu."
    fi
    apt-get install -y --no-install-recommends udiskie udisks2 dbus util-linux
    # In an image/chroot there is no running service manager. The installed
    # system starts dbus at boot and activates udisks2 on the system bus.
    if [ -d /run/systemd/system ]; then
        systemctl start dbus.service udisks2.service
        systemctl is-active --quiet dbus.service udisks2.service
    else
        gb_info "No running systemd: ensure the system D-Bus is started by init; udisks2 is D-Bus activated."
    fi
}

check_platform() {
    gb_step "Checking the target system"
    [ -d /boot ] || gb_die "/boot is missing: this does not look like a Linux system."
    [ -d /etc ] || gb_die "/etc is missing: this does not look like a Linux system."

    local init="unknown"
    if [ -d /run/systemd/system ]; then init="systemd"
    elif [ -d /etc/runit ]; then init="runit"
    elif [ -f /etc/inittab ]; then init="sysvinit"
    fi
    gb_info "init system: $init"

    if command -v findmnt >/dev/null 2>&1; then
        gb_info "root filesystem: $(findmnt -no FSTYPE,SOURCE / 2>/dev/null || echo unknown)"
    fi
    if ! command -v update-grub >/dev/null 2>&1 && ! command -v grub-mkconfig >/dev/null 2>&1; then
        gb_warn "GRUB tooling not found: the boot entry will not be registered."
        if [ "$WRITE_BOOT" = "1" ]; then
            gb_warn "Use --no-boot to install without touching the boot loader."
        fi
    fi
    gb_ok "Platform looks supported."
}

# ------------------------------------------------------------------- install --
install_files() {
    gb_step "Installing files into $INSTALL_DIR"
    local dirs=("$INSTALL_DIR/bin" "$INSTALL_DIR/handlers" "$INSTALL_DIR/src" \
                "$INSTALL_DIR/assets/fonts")
    local d
    for d in "${dirs[@]}"; do gb_run mkdir -p "$d"; done
    for d in "$DATA_DIR"/saves "$DATA_DIR"/library "$DATA_DIR"/logs "$DATA_DIR"/config \
             "$DATA_DIR"/apps "$GAMES_DIR" /run/gba; do
        gb_run mkdir -p "$d"
    done

    local mod
    for mod in common.py library.py config.py theme.py gba_terminal.py gba_browser.py gba_usb.py \
               gba_mtp.py game_data.py gba_ingame.py gba_ingame_ui.py gba_updater.py; do
        [ -f "$REPO_DIR/src/$mod" ] || gb_die "missing source file: src/$mod"
        gb_run install -m 0644 "$REPO_DIR/src/$mod" "$INSTALL_DIR/src/$mod"
    done
    gb_run install -m 0755 "$REPO_DIR/src/gba_supervisor.py"  "$INSTALL_DIR/bin/gba-supervisor"
    gb_run install -m 0755 "$REPO_DIR/src/gba_launcher.py"    "$INSTALL_DIR/bin/gba-launcher"
    gb_run install -m 0755 "$REPO_DIR/src/gba_recovery.py"    "$INSTALL_DIR/bin/gba-recovery.py"
    gb_run install -m 0755 "$REPO_DIR/src/gba_selftest.py"    "$INSTALL_DIR/bin/gba-selftest.py"
    gb_run install -m 0755 "$REPO_DIR/src/gba_installer.py"   "$INSTALL_DIR/src/gba_installer.py"
    gb_run install -m 0755 "$REPO_DIR/src/gba_setup.py"      "$INSTALL_DIR/bin/gba-setup"
    gb_run install -m 0755 "$REPO_DIR/src/gba_init.sh"        "$INSTALL_DIR/bin/gba-init"
    gb_run install -m 0755 "$REPO_DIR/src/gba_adapter_mgba.py" "$INSTALL_DIR/handlers/mgba-adapter"

    local font
    for font in DejaVuSans.ttf DejaVuSans-Bold.ttf; do
        [ -f "$REPO_DIR/assets/fonts/$font" ] || continue
        gb_run install -m 0644 "$REPO_DIR/assets/fonts/$font" "$INSTALL_DIR/assets/fonts/$font"
    done
    # Version stamp: the in-shell updater (SETTINGS -> Check for updates)
    # compares this commit against origin/main before offering an update.
    local src_sha="unknown"
    if command -v git >/dev/null 2>&1; then
        src_sha="$(git -C "$REPO_DIR" rev-parse --short=12 HEAD 2>/dev/null || echo unknown)"
    fi
    if { echo "GBA OS"
         echo "commit $src_sha"
         echo "installed $(date -u +%Y-%m-%dT%H:%M:%SZ)"
       } > "$INSTALL_DIR/VERSION" 2>/dev/null; then
        gb_ok "files copied (existing install upgraded in place, commit $src_sha)"
    else
        gb_warn "Could not write $INSTALL_DIR/VERSION (non-fatal)."
        gb_ok "files copied (existing install upgraded in place)"
    fi
}

install_ownership() {
    gb_step "Setting ownership for $SESSION_USER"
    if [ -z "$SESSION_USER" ] || [ "$SESSION_USER" = "root" ]; then
        gb_warn "No unprivileged user found; keeping root ownership for $DATA_DIR and $GAMES_DIR."
        return 0
    fi
    gb_run chown -R "$SESSION_USER":"$SESSION_USER" "$GAMES_DIR" "$DATA_DIR"
    gb_run chmod -R 775 "$GAMES_DIR" "$DATA_DIR"
}

install_profile() {
    gb_step "Writing the shared input profile"
    if gb_is_dry_run; then
        gb_dim "  (dry-run) would write $DATA_DIR/config/keybindings.json and ~/.config/mgba/config.ini"
        return 0
    fi
    python3 - "$INSTALL_DIR" "$SESSION_HOME" <<'PYEOF' || gb_warn "Input profile could not be written (non-fatal)."
import os, sys
install_dir, home = sys.argv[1], sys.argv[2]
sys.path.insert(0, os.path.join(install_dir, "src"))
try:
    import common
    common.get_data_dir = lambda: os.environ.get("GBA_DATA_DIR", "/data")
    common.save_keybindings(common.load_keybindings())
    print("  [gba] key profile written to /data/config")
except Exception as exc:
    print(f"  [gba] could not write input profile: {exc}")
    raise SystemExit(0)
PYEOF
    gb_run mkdir -p "$SESSION_HOME/.config/mgba"
    if [ -d "$SESSION_HOME/.config/mgba" ]; then
        gb_run chown -R "$SESSION_USER":"$SESSION_USER" "$SESSION_HOME/.config/mgba" 2>/dev/null || true
    fi
}

install_wrappers() {
    gb_step "Installing the session wrappers"
    if gb_is_dry_run; then
        gb_dim "  (dry-run) would write $TTY_WRAPPER and $SESSION_RUNNER"
        return 0
    fi

    install -m 0755 "$REPO_DIR/src/gba_session.sh" "$SESSION_RUNNER"
    chmod 0755 "$SESSION_RUNNER"

    cat > "$TTY_WRAPPER" << 'EOF_TTY'
#!/bin/bash
# GBA OS appliance console wrapper.
#
# Falls back at every step: without gba_mode it is a normal getty, without X11
# the supervisor runs directly on the KMS/DRM console, and if the supervisor
# dies the recovery console takes over (handled by gba_init.sh).
# Not an appliance boot: hand the console to a normal login prompt.
if ! grep -q "gba_mode=1" /proc/cmdline 2>/dev/null; then
    TTY_NAME="${1:-tty1}"
    if command -v agetty >/dev/null 2>&1; then
        exec agetty --noclear "$TTY_NAME" 38400
    elif [ -x /sbin/getty ]; then
        exec /sbin/getty --noclear 38400 "$TTY_NAME"
    fi
    exec /bin/sh
fi

exec 0</dev/tty1 1>/dev/tty1 2>&1
clear

mkdir -p /data/logs /run/gba /tmp/gba-run
chmod 1777 /tmp/.X11-unix 2>/dev/null || true

LOG_FILE="/data/logs/boot_$(date +%Y-%m-%d_%H-%M-%S).log"
rm -f /data/logs/latest.log 2>/dev/null || true
ln -sf "$LOG_FILE" /data/logs/latest.log
exec > >(tee -a "$LOG_FILE") 2>&1

echo "=========================================================="
echo "                   LAUNCHING GBA OS                       "
echo "=========================================================="
echo "[boot] $(date '+%Y-%m-%d %H:%M:%S')"
echo "[boot] log: $LOG_FILE"
echo "[boot] cmdline: $(cat /proc/cmdline 2>/dev/null)"
echo "[boot] DRI:"; ls -l /dev/dri/ 2>/dev/null || echo "  (none)"
echo "[boot] ROMs:"; ls -lh /games/ 2>/dev/null || echo "  (none)"

# Non-fatal self test: log problems, never block the boot.
if [ -x /usr/local/bin/gba-selftest ]; then
    /usr/local/bin/gba-selftest --quiet || echo "[boot] self-test reported problems (see above)"
fi

export TERM=linux SYS_CONTRACT=1 GBA_LOG_FILE="$LOG_FILE"

# Preferred path: a tiny X server for the SDL backend. If xinit or Xorg is
# missing we do not fail the boot - the shell itself works on KMS/DRM.
if command -v xinit >/dev/null 2>&1 && command -v Xorg >/dev/null 2>&1; then
    xinit /usr/local/bin/gba-session -- :0 vt1 -nocursor -quiet
    EXIT_CODE=$?
else
    echo "[boot] X11 not available; starting the shell directly on KMS/DRM"
    /usr/local/bin/gba-session
    EXIT_CODE=$?
fi

echo "[boot] session exited with status $EXIT_CODE"
if [ "$EXIT_CODE" -eq 100 ]; then
    sync; reboot -f 2>/dev/null || echo b > /proc/sysrq-trigger 2>/dev/null || true
    exec /bin/sh
fi
if [ "$EXIT_CODE" -eq 101 ]; then
    sync; poweroff -f 2>/dev/null || echo o > /proc/sysrq-trigger 2>/dev/null || true
    exec /bin/sh
fi
if [ "$EXIT_CODE" -ne 100 ] && [ "$EXIT_CODE" -ne 101 ]; then
    # Anything unexpected: give the user the recovery console instead of a
    # dead console, then reboot into the normal boot menu.
    echo "[boot] entering recovery in 5 seconds (Ctrl+C to stay at a shell)"
    sleep 5
    if [ -x /opt/gba-distro/bin/gba-recovery.py ] && command -v python3 >/dev/null 2>&1; then
        python3 /opt/gba-distro/bin/gba-recovery.py || true
    fi
    # Last resort: a login shell on the console. Never a black screen.
    exec /bin/sh
fi
EOF_TTY
    chmod 0755 "$TTY_WRAPPER"

    cat > "$SELFTEST" << 'EOF_SELFTEST'
#!/bin/sh
# Convenience wrapper so a user can run the installer's checks at any time.
exec python3 /opt/gba-distro/bin/gba-selftest.py "$@"
EOF_SELFTEST
    chmod 0755 "$SELFTEST"

    cat > "$SETUP_WRAPPER" << 'EOF_SETUP'
#!/bin/sh
# Guided "install GBA OS onto a disk" setup.
#
# This is the same program the installation stick runs. It never writes until
# a target has been chosen and confirmed, it refuses the disk it is running
# from, and it refuses any partition that already contains an operating
# system. If python or pygame are missing it still works: it is plain text.
if [ ! -r /opt/gba-distro/src/gba_installer.py ]; then
    echo "gba-setup: GBA OS is not installed in /opt/gba-distro." >&2
    exit 2
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo "gba-setup: python3 is required. Install it and try again." >&2
    echo "           Nothing was changed." >&2
    exit 2
fi
exec python3 /opt/gba-distro/bin/gba-setup "$@"
EOF_SETUP
    chmod 0755 "$SETUP_WRAPPER"
    gb_ok "wrappers installed with X11 and recovery fallbacks"
}

run_selftest() {
    gb_step "Running the post-install self-test"
    if gb_is_dry_run; then
        gb_dim "  (dry-run) would run: python3 $INSTALL_DIR/bin/gba-selftest.py --installed"
        return 0
    fi
    if ! GBA_INSTALL_DIR="$INSTALL_DIR" GBA_DATA_DIR="$DATA_DIR" GBA_GAMES_DIR="$GAMES_DIR" \
            python3 "$INSTALL_DIR/bin/gba-selftest.py" --installed; then
        gb_warn "Self-test reported problems. The boot entry will still be added,"
        gb_warn "but games may not start until the listed packages are installed."
        return 1
    fi
    gb_ok "self-test passed"
    return 0
}

# ------------------------------------------------------------------ boot ----
grub_cfg_path() {
    local path
    path=$(readlink -f /boot/grub/grub.cfg 2>/dev/null || true)
    [ -n "$path" ] || path=$(readlink -f /boot/grub2/grub.cfg 2>/dev/null || true)
    [ -n "$path" ] && printf '%s\n' "$path"
}

run_grub_update() {
    if command -v update-grub >/dev/null 2>&1; then
        update-grub
    elif command -v grub-mkconfig >/dev/null 2>&1; then
        grub-mkconfig -o "$(grub_cfg_path)"
    elif command -v grub2-mkconfig >/dev/null 2>&1; then
        grub2-mkconfig -o "$(grub_cfg_path)"
    else
        return 1
    fi
}

grub_fs_modules() {
    # GRUB needs the module for the root filesystem before it can read /boot.
    local fstype
    fstype=$(findmnt -no FSTYPE / 2>/dev/null || echo ext4)
    case "$fstype" in
        ext2|ext3|ext4) echo "insmod ext2" ;;
        btrfs)          echo "insmod btrfs" ;;
        xfs)            echo "insmod xfs" ;;
        f2fs)           echo "insmod f2fs" ;;
        zfs)            echo "insmod zfs" ;;
        *)              echo "insmod ext2" ;;
    esac
}

install_boot_entry() {
    if [ "$WRITE_BOOT" != "1" ]; then
        gb_step "Skipping the boot entry (--no-boot)"
        gb_info "Start it manually with: $SESSION_RUNNER"
        return 0
    fi
    gb_step "Registering the GRUB boot entry"

    if ! command -v update-grub >/dev/null 2>&1 && ! command -v grub-mkconfig >/dev/null 2>&1; then
        gb_warn "GRUB tooling missing: cannot register the entry. Nothing was changed."
        gb_info "Add this to /etc/grub.d/40_custom manually if you want it:"
        gb_dim "  menuentry 'GBA OS' { linux /boot/vmlinuz-<ver> root=UUID=<uuid> ro quiet gba_mode=1; initrd /boot/initrd.img-<ver> }"
        return 1
    fi

    local root_uuid kernel initrd fstype extra_modules
    root_uuid=$(findmnt -n -o UUID / 2>/dev/null || true)
    [ -n "$root_uuid" ] || root_uuid=$(blkid -s UUID -o value "$(findmnt -n -o SOURCE /)" 2>/dev/null || true)
    [ -n "$root_uuid" ] || gb_die "Could not determine the root filesystem UUID; refusing to write a bad boot entry."

    kernel=$(ls -1t /boot/vmlinuz-* 2>/dev/null | head -n1 || true)
    initrd=$(ls -1t /boot/initrd.img-* /boot/initramfs-*.img 2>/dev/null | head -n1 || true)
    [ -n "$kernel" ] || gb_die "No kernel found in /boot; refusing to write a boot entry."
    [ -n "$initrd" ] || gb_warn "No initrd found; the entry will rely on the built-in kernel initramfs."

    fstype=$(findmnt -no FSTYPE / 2>/dev/null || echo ext4)
    extra_modules=""
    case "$fstype" in
        lvm|dm*) extra_modules="insmod lvm" ;;
    esac
    [ -d /sys/module/md_mod ] && extra_modules="$extra_modules
	insmod mdraid1x"

    gb_info "kernel: $kernel"
    gb_info "initrd: ${initrd:-none}"
    gb_info "root:   UUID=$root_uuid ($fstype)"

    if gb_is_dry_run; then
        gb_dim "  (dry-run) would back up /etc/inittab, /etc/default/grub.d/*, $(grub_cfg_path 2>/dev/null || echo /boot/grub/grub.cfg)"
        gb_dim "  (dry-run) would write $GRUB_DEFAULTS, $GRUB_ENTRY and re-run update-grub"
        gb_dim "  (dry-run) would verify the new menu contains 'GBA OS' and restore the old one otherwise"
        return 0
    fi

    gb_require_root
    gb_install_trap

    # --- backups first -------------------------------------------------------
    gb_backup_file /etc/inittab
    gb_backup_file "$GRUB_ENTRY"
    gb_backup_file "$GRUB_DEFAULTS"
    GB_GRUB_CFG=$(grub_cfg_path || true)
    if [ -n "${GB_GRUB_CFG:-}" ]; then
        gb_backup_file "$GB_GRUB_CFG"
    fi

    mkdir -p /etc/default/grub.d
    cat > "$GRUB_DEFAULTS" << 'EOF_GRUB_D'
# Always show the boot menu so the machine can also boot its primary OS.
# GBA OS never changes the default boot target.
GRUB_TIMEOUT_STYLE=menu
GRUB_TIMEOUT=10
GRUB_RECORDFAIL_TIMEOUT=10
GRUB_GFXMODE=auto,keep
GRUB_DISABLE_SUBMENU=n
EOF_GRUB_D

    cat > "$GRUB_ENTRY" << EOF
#!/bin/sh
cat << 'EOF_GBA_GRUB'
# GBA OS appliance entries. Appended, never replacing the existing OS entries.
menuentry "GBA OS (console appliance)" --class gba --class gnu-linux --class os {
	load_video
	insmod gzio
	insmod part_gpt
	insmod part_msdos
	$(grub_fs_modules)
	${extra_modules:-}
	search --no-floppy --fs-uuid --set=root $root_uuid
	echo "Starting GBA OS..."
	linux $kernel root=UUID=$root_uuid ro quiet gba_mode=1
	${initrd:+initrd $initrd}
}

menuentry "GBA OS (safe graphics / recovery)" --class gba --class gnu-linux --class os {
	load_video
	insmod gzio
	insmod part_gpt
	insmod part_msdos
	$(grub_fs_modules)
	${extra_modules:-}
	search --no-floppy --fs-uuid --set=root $root_uuid
	echo "Starting GBA OS in safe graphics mode..."
	linux $kernel root=UUID=$root_uuid ro quiet gba_mode=1 nomodeset gba_safe=1
	${initrd:+initrd $initrd}
}
EOF_GBA_GRUB
EOF
    chmod 0755 "$GRUB_ENTRY"

    # Validate the generated script before letting GRUB run it.
    if ! sh -n "$GRUB_ENTRY"; then
        gb_die "Generated GRUB entry has a syntax error; aborting before touching the boot menu."
    fi

    # --- legacy cleanup ------------------------------------------------------
    if [ -f /etc/grub.d/40_custom ]; then
        gb_backup_file /etc/grub.d/40_custom
        sed -i "/### BEGIN GBA OS ###/,/### END GBA OS ###/d" /etc/grub.d/40_custom || true
    fi

    # --- init hook -----------------------------------------------------------
    if [ -f /etc/inittab ] && ! grep -q "gba-tty1-wrapper" /etc/inittab; then
        cp -a /etc/inittab /etc/inittab.gba-backup
        sed -i 's|^1:2345:respawn:/sbin/getty --noclear 38400 tty1|1:2345:respawn:/usr/local/bin/gba-tty1-wrapper tty1|' /etc/inittab
        gb_ok "inittab patched (backup: /etc/inittab.gba-backup)"
    fi

    # --- regenerate and VERIFY ----------------------------------------------
    gb_step "Regenerating the boot menu"
    local previous_cfg=""
    [ -n "${GB_GRUB_CFG:-}" ] && [ -f "$GB_GRUB_CFG" ] && previous_cfg=$(cat "$GB_GRUB_CFG")

    if ! run_grub_update; then
        gb_err "update-grub failed. Restoring the previous boot menu."
        if [ -n "$previous_cfg" ] && [ -n "${GB_GRUB_CFG:-}" ]; then
            printf '%s\n' "$previous_cfg" > "$GB_GRUB_CFG"
        fi
        rm -f "$GRUB_ENTRY" "$GRUB_DEFAULTS"
        gb_die "Boot menu left unchanged. Your machine still boots normally."
    fi

    if [ -n "${GB_GRUB_CFG:-}" ] && ! grep -q "GBA OS (console appliance)" "$GB_GRUB_CFG"; then
        gb_err "The new menu does not contain the GBA OS entry. Restoring the previous menu."
        [ -n "$previous_cfg" ] && printf '%s\n' "$previous_cfg" > "$GB_GRUB_CFG"
        rm -f "$GRUB_ENTRY" "$GRUB_DEFAULTS"
        gb_die "Boot menu restored; nothing to boot into. Check /etc/grub.d/11_gba_os."
    fi

    gb_ok "Boot menu updated and verified: the primary OS is still the default."
}

install_desktop_shortcut() {
    [ -d "$SESSION_HOME/Desktop" ] || return 0
    gb_step "Adding a desktop shortcut for windowed testing"
    if gb_is_dry_run; then
        gb_dim "  (dry-run) would write $SESSION_HOME/Desktop/gba-os.desktop"
        return 0
    fi
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
}

summary() {
    echo
    if [ "$APPLY" = "1" ]; then
        gb_ok "GBA OS installation complete."
    else
        gb_info "Dry-run complete: nothing was changed."
        gb_info "Re-run with --apply to install."
    fi
    echo "  Games     : copy .gba/.gbc/.gb files into $GAMES_DIR"
    echo "  Test now  : ./scripts/launch_desktop.sh"
    echo "  Self-test : $SELFTEST"
    echo "  Appliance : reboot and pick 'GBA OS (console appliance)'"
    echo "  Emergency : pick 'GBA OS (safe graphics / recovery)' or add gba_mode=0"
    echo "  Remove    : sudo ./scripts/install.sh --apply --uninstall"
    gb_print_backup_note
}

main() {
    gb_info "GBA OS installer — repo: $REPO_DIR"
    gb_info "mode: $([ "$APPLY" = "1" ] && echo APPLY || echo 'dry-run (add --apply to install)')"
    [ "$APPLY" = "1" ] && gb_require_root

    if [ "$UNINSTALL" = "1" ]; then
        do_uninstall
        summary
        return 0
    fi

    if [ "$REPAIR" = "1" ]; then
        do_repair
        return 0
    fi

    check_platform
    check_dependencies || true
    if [ "$APPLY" = "1" ]; then
        gb_require_free_space_mb "$DATA_DIR" 512 || gb_die "Free some space in $DATA_DIR first."
    fi

    if [ "$APPLY" = "1" ] && [ "$GB_ASSUME_YES" != "1" ]; then
        echo
        gb_warn "This will write to $INSTALL_DIR, $DATA_DIR and $GAMES_DIR, and add a GRUB entry."
        gb_warn "Every modified system file is backed up and rolled back automatically on failure."
        gb_confirm "Proceed with the install?" || { gb_info "Cancelled. Nothing was changed."; exit 0; }
    fi

    install_automount_dependencies
    install_files
    install_ownership
    install_profile
    install_wrappers
    run_selftest || true
    install_boot_entry || gb_warn "Boot entry not registered; the shell can still be started manually."
    install_desktop_shortcut

    [ "$APPLY" = "1" ] && gb_disarm_trap
    summary
}

main "$@"
