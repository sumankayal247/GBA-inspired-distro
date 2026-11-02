#!/bin/bash
# ==============================================================================
# GBA OS — build a bootable installation ISO
#
#   ./scripts/build_iso.sh --check      list what is missing, change nothing
#   sudo ./scripts/build_iso.sh         build build/iso/gba-os-<version>-amd64.iso
#   sudo ./scripts/build_iso.sh --clean  throw away the build directory first
#
# The ISO is a live image: it boots on BIOS and UEFI machines, runs the guided
# setup, and can be written to a USB stick with dd, Rufus or balenaEtcher.
# It never writes to the machine's disks until the guided setup is confirmed.
#
# Requirements (Debian/Ubuntu host):
#   debootstrap  xorriso  squashfs-tools  grub-pc-bin  grub-efi-amd64-bin  mtools
# ==============================================================================

set -Eeuo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/common.sh
. "$REPO_DIR/scripts/lib/common.sh"

WORK_DIR="${GBA_ISO_WORK:-$REPO_DIR/build/iso}"
OUT_DIR="${GBA_ISO_OUT:-$REPO_DIR/build}"
ROOTFS_DIR="$WORK_DIR/rootfs"
ISODIR="$WORK_DIR/isodir"
HOSTNAME="gba-os"
VOLUME_ID="GBAOS"
CHECK_ONLY=0
CLEAN=0

# Prefer an explicit release tag; fall back to the short commit for dev builds.
VERSION="$(git -C "$REPO_DIR" describe --tags --always 2>/dev/null || echo dev)"
VERSION="${VERSION#v}"
ISO_NAME="gba-os-${VERSION}-amd64.iso"

usage() {
    sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --check|--check-only) CHECK_ONLY=1 ;;
        --clean) CLEAN=1 ;;
        --work-dir) WORK_DIR="$2"; ROOTFS_DIR="$WORK_DIR/rootfs"; ISODIR="$WORK_DIR/isodir"; shift ;;
        --out) OUT_DIR="$2"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "build_iso.sh: unknown argument '$1'" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

# ------------------------------------------------------------------- tools ----
REQUIRED_TOOLS=(debootstrap mksquashfs xorriso grub-mkrescue mcopy mount chroot)

missing_tools() {
    local tool
    for tool in "$@"; do
        command -v "$tool" >/dev/null 2>&1 || echo "$tool"
    done
}

check_prerequisites() {
    local missing
    missing="$(missing_tools "${REQUIRED_TOOLS[@]}" | tr '\n' ' ')"
    if [ -n "${missing// /}" ]; then
        {
            echo
            echo "build_iso.sh: cannot build the ISO on this machine. Missing:"
            for tool in $missing; do
                echo "    $tool"
            done
            echo
            echo "Install them with:"
            echo "    sudo apt install debootstrap xorriso squashfs-tools mtools \\"
            echo "                     grub-pc-bin grub-efi-amd64-bin isolinux syslinux-common"
            echo
            echo "Nothing was changed."
        } >&2
        return 1
    fi

    if [ "$(id -u)" -ne 0 ]; then
        echo "build_iso.sh: building needs root (debootstrap and loop-free chroot)." >&2
        echo "              Re-run with sudo. Nothing was changed." >&2
        return 1
    fi
    return 0
}

if [ "$CHECK_ONLY" = "1" ]; then
    echo "GBA OS ISO build check"
    echo "  repo    : $REPO_DIR"
    echo "  version : $VERSION"
    echo "  output  : $OUT_DIR/$ISO_NAME"
    if check_prerequisites; then
        echo "  status  : ready to build"
        exit 0
    fi
    exit 1
fi

check_prerequisites || exit 1

rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR" "$OUT_DIR"
[ "$CLEAN" = "1" ] && rm -rf "$ROOTFS_DIR" "$ISODIR"

# --------------------------------------------------------------- rootfs ------
gb_step "Creating the base system in $ROOTFS_DIR"
debootstrap \
    --variant=minbase \
    --arch=amd64 \
    --include=systemd,systemd-sysv,udev,kmod,live-boot,live-config,grub2-common,grub-pc-bin,grub-efi-amd64-bin,efibootmgr,parted,sgdisk,e2fsprogs,dosfstools,squashfs-tools,python3,python3-pygame,fonts-dejavu-core,alsa-utils,console-setup \
    bookworm "$ROOTFS_DIR" "${GBA_ISO_MIRROR:-http://deb.debian.org/debian}"

gb_step "Installing the emulator (optional package)"
if ! chroot "$ROOTFS_DIR" /usr/bin/env DEBIAN_FRONTEND=noninteractive \
        apt-get install -y --no-install-recommends mgba-sdl >/dev/null 2>&1; then
    gb_warn "mgba-sdl is not available from this mirror."
    gb_warn "The ISO still boots and installs; the self-test will report the"
    gb_warn "missing emulator and the setup screen shows how to add it later."
fi

gb_step "Copying GBA OS into the image"
install -d "$ROOTFS_DIR/opt/gba-distro"
tar -C "$REPO_DIR" \
    --exclude='./.git' --exclude='./build' --exclude='./data' \
    --exclude='./__pycache__' --exclude='*/__pycache__' --exclude='./.venv' \
    -cf - . | tar -C "$ROOTFS_DIR/opt/gba-distro" -xf -

gb_step "Installing the GBA OS runtime inside the image"
# The ISO is both the installer and (after a copy) the installed system, so the
# runtime is laid down here exactly as it would be on a running machine. --no-boot
# keeps this step away from GRUB: the image boots through its own menu.
chroot "$ROOTFS_DIR" /usr/bin/env \
    GBA_ALLOW_MISSING_DEPS=1 GBA_DATA_DIR=/data GBA_GAMES_DIR=/games \
    /bin/bash /opt/gba-distro/scripts/install.sh --apply --yes --no-boot \
    || gb_die "install.sh failed inside the image; the ISO would boot to a shell only."

gb_step "Adding the live session"
install -d "$ROOTFS_DIR/usr/local/bin"
cat > "$ROOTFS_DIR/usr/local/bin/gba-live-setup" << 'EOF_LIVE'
#!/bin/sh
# Runs the guided setup on the live medium. Falls back to a console, never to a
# black screen.
echo
echo "  GBA OS installation medium"
echo "  Press Ctrl+C at any question to cancel; nothing is written until you confirm."
echo
if command -v python3 >/dev/null 2>&1; then
    python3 /opt/gba-distro/src/gba_setup.py --source /run/live/medium/live/filesystem.squashfs
else
    echo "  python3 is missing from this medium. This is not a problem with your PC."
fi
echo
echo "  Setup finished. Press Enter for a shell, or reboot to remove the medium."
exec /bin/sh
EOF_LIVE
chmod 0755 "$ROOTFS_DIR/usr/local/bin/gba-live-setup"

install -d "$ROOTFS_DIR/etc/systemd/system"
cat > "$ROOTFS_DIR/etc/systemd/system/gba-setup.service" << 'EOF_UNIT'
[Unit]
Description=GBA OS guided setup
After=systemd-user-sessions.service
Wants=network.target

[Service]
Type=idle
ExecStart=/usr/local/bin/gba-live-setup
StandardInput=tty
StandardOutput=tty
StandardError=tty
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes
Restart=no

[Install]
WantedBy=multi-user.target
EOF_UNIT

ln -sf /etc/systemd/system/gba-setup.service \
    "$ROOTFS_DIR/etc/systemd/system/multi-user.target.wants/gba-setup.service"
ln -sf /dev/null "$ROOTFS_DIR/etc/systemd/system/getty@tty1.service"

gb_step "Writing live boot files"
install -d "$ROOTFS_DIR/etc/live/config.conf.d"
cat > "$ROOTFS_DIR/etc/live/config.conf.d/gba.conf" << 'EOF_LIVE_CONF'
# The GBA OS medium is an installer, not a desktop.
LIVE_HOSTNAME="gba-os"
EOF_LIVE_CONF

rm -rf "$ROOTFS_DIR/var/cache/apt/archives/"*.deb "$ROOTFS_DIR/tmp/"* 2>/dev/null || true
mkdir -p "$ROOTFS_DIR/var/cache/apt/archives/partial" "$ROOTFS_DIR/tmp"
chmod 1777 "$ROOTFS_DIR/tmp"

# ------------------------------------------------------------- squashfs ------
gb_step "Compressing the live filesystem (this is the slow part)"
install -d "$ISODIR/live"
mksquashfs "$ROOTFS_DIR" "$ISODIR/live/filesystem.squashfs" \
    -noappend -comp xz -e boot -wildcards -quiet

KERNEL="$(ls "$ROOTFS_DIR"/boot/vmlinuz-* 2>/dev/null | head -n1 || true)"
INITRD="$(ls "$ROOTFS_DIR"/boot/initrd.img-* 2>/dev/null | head -n1 || true)"
if [ -z "$KERNEL" ] || [ -z "$INITRD" ]; then
    gb_die "No kernel/initrd found in the image; cannot build a bootable ISO.
Install a kernel in the rootfs (apt install linux-image-amd64) and retry.
Nothing was published."
fi
cp "$KERNEL" "$ISODIR/live/vmlinuz"
cp "$INITRD" "$ISODIR/live/initrd.img"

# --------------------------------------------------------------- loader ------
gb_step "Writing the ISO boot menu"
install -d "$ISODIR/boot/grub"
cat > "$ISODIR/boot/grub/grub.cfg" << 'EOF_GRUB'
set default=0
set timeout=10
set gfxpayload=keep

menuentry "Install GBA OS (recommended)" {
    linux /live/vmlinuz boot=live components quiet gba_live=1
    initrd /live/initrd.img
}

menuentry "Install GBA OS (safe graphics)" {
    linux /live/vmlinuz boot=live components nomodeset gba_safe=1 gba_live=1
    initrd /live/initrd.img
}

menuentry "Start a shell on this medium (nothing is installed)" {
    linux /live/vmlinuz boot=live components single
    initrd /live/initrd.img
}

menuentry "Boot from the internal disk instead" {
    exit
}
EOF_GRUB

gb_step "Building the hybrid BIOS+UEFI image"
grub-mkrescue -o "$OUT_DIR/$ISO_NAME" "$ISODIR" \
    --volid "$VOLUME_ID" \
    -- -volid "$VOLUME_ID"

gb_step "Recording checksums"
( cd "$OUT_DIR" && sha256sum "$ISO_NAME" > "$ISO_NAME.sha256" )
gb_ok "Built $OUT_DIR/$ISO_NAME"
gb_info "Size: $(du -h "$OUT_DIR/$ISO_NAME" | cut -f1)"
cat << EOF

  Write it to a USB stick (the stick is the ONLY thing that is erased):

      sudo dd if=$OUT_DIR/$ISO_NAME of=/dev/sdX bs=4M status=progress oflag=sync

  ...or use Rufus / balenaEtcher / the GNOME "Restore Disk Image" tool.

  Boot the stick, pick "Install GBA OS", and answer two questions.
  Your PC's existing system is not modified until you confirm a target.

EOF
