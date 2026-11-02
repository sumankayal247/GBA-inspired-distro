#!/bin/bash
# ==============================================================================
# GBA OS — shared shell safety helpers
#
# Every script that can change a running system sources this file. The rules it
# enforces are deliberately blunt:
#
#   1. Nothing destructive happens without GB_DRY_RUN=0 AND an explicit flag.
#   2. Every file we modify is backed up first, and the backup path is printed.
#   3. A failed run rolls the boot-critical files back (see gb_install_trap).
#   4. We never write to firmware/NVRAM and never touch a disk that is not the
#      exact device the user named.
#   5. Refuse to operate on the medium we are running from.
# ==============================================================================

# Re-source guard (scripts source this from several places).
if [ -n "${GBA_SAFETY_SH:-}" ]; then
    return 0 2>/dev/null || exit 0
fi
GBA_SAFETY_SH=1

GB_DRY_RUN="${GB_DRY_RUN:-1}"          # 1 = print the plan, change nothing
GB_ASSUME_YES="${GB_ASSUME_YES:-0}"    # never default to yes in a script
GB_BACKUP_DIR="${GB_BACKUP_DIR:-/var/backups/gba-os}"

if [ -t 1 ]; then
    GB_C_RESET=$'\033[0m'; GB_C_INFO=$'\033[1;36m'; GB_C_WARN=$'\033[1;33m'
    GB_C_ERR=$'\033[1;31m'; GB_C_OK=$'\033[1;32m'; GB_C_DIM=$'\033[2m'
else
    GB_C_RESET=""; GB_C_INFO=""; GB_C_WARN=""; GB_C_ERR=""; GB_C_OK=""; GB_C_DIM=""
fi

gb_info()  { printf '%s[gba]%s %s\n' "$GB_C_INFO" "$GB_C_RESET" "$*"; }
gb_ok()    { printf '%s[ ok ]%s %s\n' "$GB_C_OK" "$GB_C_RESET" "$*"; }
gb_warn()  { printf '%s[warn]%s %s\n' "$GB_C_WARN" "$GB_C_RESET" "$*" >&2; }
gb_err()   { printf '%s[fail]%s %s\n' "$GB_C_ERR" "$GB_C_RESET" "$*" >&2; }
gb_step()  { printf '\n%s==>%s %s\n' "$GB_C_INFO" "$GB_C_RESET" "$*"; }
gb_dim()   { printf '%s%s%s\n' "$GB_C_DIM" "$*" "$GB_C_RESET"; }

gb_is_dry_run() { [ "$GB_DRY_RUN" = "1" ]; }

gb_die() {
    gb_err "$*"
    exit 1
}

# Runs a command, echoing it exactly as a user would type it.
gb_run() {
    if gb_is_dry_run; then
        printf '  %s+ %s%s\n' "$GB_C_DIM" "$*" "$GB_C_RESET"
        return 0
    fi
    "$@"
}

gb_run_shell() {
    if gb_is_dry_run; then
        printf '  %s+ %s%s\n' "$GB_C_DIM" "$*" "$GB_C_RESET"
        return 0
    fi
    bash -o pipefail -c "$*"
}

gb_require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        gb_die "This step needs root. Re-run with sudo (dry-run works without root)."
    fi
}

gb_require_cmd() {
    local missing=() c
    for c in "$@"; do
        command -v "$c" >/dev/null 2>&1 || missing+=("$c")
    done
    if [ ${#missing[@]} -gt 0 ]; then
        gb_err "Missing required tools: ${missing[*]}"
        return 1
    fi
    return 0
}

gb_require_free_space_mb() {
    local path=$1 need_mb=$2 available
    mkdir -p "$path" 2>/dev/null || true
    available=$(df -Pm "$path" 2>/dev/null | awk 'NR==2 {print $4}')
    [ -n "${available:-}" ] || return 0
    if [ "$available" -lt "$need_mb" ]; then
        gb_err "Not enough free space in $path: ${available} MB available, ${need_mb} MB required."
        return 1
    fi
    return 0
}

# ---------------------------------------------------------------- confirmations
# gb_confirm "<question>" — y/N prompt; required for anything irreversible.
gb_confirm() {
    local question=$1 answer
    if [ "$GB_ASSUME_YES" = "1" ]; then
        gb_warn "auto-confirmed (--yes): $question"
        return 0
    fi
    if [ ! -t 0 ]; then
        gb_err "Refusing to continue without a confirmation. Re-run with --yes if you are sure."
        return 1
    fi
    printf '%s?%s %s [y/N] ' "$GB_C_WARN" "$GB_C_RESET" "$question"
    read -r answer
    case "$answer" in
        y|Y|yes|YES) return 0 ;;
        *) return 1 ;;
    esac
}

# gb_confirm_typed "<exact text>" "<question>" — for irreversible disk actions.
gb_confirm_typed() {
    local expected=$1 question=$2 answer
    if [ "$GB_ASSUME_YES" = "1" ]; then
        gb_warn "auto-confirmed (--yes): $question"
        return 0
    fi
    if [ ! -t 0 ]; then
        gb_err "Refusing irreversible action without a typed confirmation."
        return 1
    fi
    printf '%s!!%s %s\n' "$GB_C_ERR" "$GB_C_RESET" "$question"
    printf '   Type exactly "%s" to continue: ' "$expected"
    read -r answer
    [ "$answer" = "$expected" ] && return 0
    gb_warn "Input did not match; nothing was changed."
    return 1
}

# ------------------------------------------------------------------- backups
# Every modified file is copied to GB_BACKUP_DIR/<timestamp>/ and recorded in
# GB_CHANGES, so gb_install_trap can put it back if a later step fails.
GB_CHANGES=()
GB_STAMP="$(date +%Y%m%d-%H%M%S)"
GB_RUN_BACKUP_DIR="$GB_BACKUP_DIR/$GB_STAMP"
GB_TRAP_ARMED=0
GB_ROLLBACK_DONE=0

gb_backup_file() {
    local file=$1
    [ -e "$file" ] || return 0
    if gb_is_dry_run; then
        gb_dim "  (dry-run) would back up $file"
        return 0
    fi
    mkdir -p "$GB_RUN_BACKUP_DIR" || return 1
    local dest="$GB_RUN_BACKUP_DIR/$(echo "$file" | sed 's|/|_|g')"
    cp -a "$file" "$dest" || { gb_err "Could not back up $file"; return 1; }
    GB_CHANGES+=("$file|$dest")
    gb_dim "  backup: $dest"
    return 0
}

gb_written_files() {
    local entry
    for entry in "${GB_CHANGES[@]:-}"; do
        [ -n "$entry" ] || continue
        printf '%s\n' "${entry%%|*}"
    done
}

# Restores every backed-up file. Called from the EXIT trap on failure.
gb_rollback() {
    [ "$GB_ROLLBACK_DONE" = "1" ] && return 0
    GB_ROLLBACK_DONE=1
    local entry file backup
    if [ ${#GB_CHANGES[@]} -eq 0 ]; then
        return 0
    fi
    gb_warn "Rolling back system file changes..."
    for entry in "${GB_CHANGES[@]}"; do
        file=${entry%%|*}
        backup=${entry#*|}
        if [ -f "$backup" ]; then
            cp -a "$backup" "$file" && gb_dim "  restored $file"
        fi
    done
    if [ -n "${GB_GRUB_CFG:-}" ] && command -v update-grub >/dev/null 2>&1; then
        gb_warn "Regenerating the boot menu from the restored configuration..."
        update-grub >/dev/null 2>&1 || grub-mkconfig -o "$GB_GRUB_CFG" >/dev/null 2>&1 || true
    fi
    gb_ok "Rollback complete. Your machine's boot configuration is unchanged."
}

gb_install_trap() {
    GB_TRAP_ARMED=1
    # shellcheck disable=SC2064
    trap 'gb_on_exit $?' EXIT INT TERM
}

gb_on_exit() {
    local status=$?
    if [ "$status" -ne 0 ] && [ "$GB_TRAP_ARMED" = "1" ]; then
        gb_rollback
    fi
    exit "$status"
}

gb_disarm_trap() {
    GB_TRAP_ARMED=0
    trap - EXIT INT TERM
}

# ------------------------------------------------------------ block devices
gb_unescape_mount() {
    # /proc/mounts escapes spaces and friends as octal.
    printf '%b' "$(printf '%s' "$1" | sed 's/\\\([0-7][0-7][0-7]\)/\\0\1/g')"
}

# Prints the block device backing a path ("" when unknown).
gb_device_of_path() {
    local path=$1
    [ -e "$path" ] || { echo ""; return 0; }
    findmnt -no SOURCE --target "$path" 2>/dev/null || true
}

gb_root_device() {
    gb_device_of_path /
}

gb_real_device() {
    # /dev/mapper/x -> /dev/dm-0, /dev/disk/by-uuid/.. -> /dev/sda1
    readlink -f "$1" 2>/dev/null || printf '%s' "$1"
}

# True when $1 is the disk (or a partition of the disk) holding / or /run/live.
gb_is_live_or_root_device() {
    local dev real target
    dev=$(gb_real_device "$1")
    [ -n "$dev" ] || return 1
    for target in "$(gb_root_device)" "/run/live/medium" "/run/initramfs/live" "/cdrom" "/media/cdrom"; do
        [ -n "$target" ] || continue
        local tdev
        tdev=$(gb_real_device "$(gb_device_of_path "$target")")
        [ -n "$tdev" ] || continue
        # Same device, or the same parent disk (sda1 vs sda).
        if [ "$dev" = "$tdev" ] || [ "${dev%[0-9]}" = "${tdev%[0-9]}" ]; then
            return 0
        fi
    done
    return 1
}

gb_is_block_device() {
    [ -b "$1" ]
}

gb_is_mounted() {
    awk -v dev="$(gb_real_device "$1")" '
        { if ($1 == dev) { found = 1 } }
        END { exit found ? 0 : 1 }' /proc/mounts 2>/dev/null
}

gb_human_size() {
    numfmt --to=iec --suffix=B "$1" 2>/dev/null || printf '%s bytes' "$1"
}

# ------------------------------------------------------------------- summary
gb_print_backup_note() {
    if [ -d "$GB_RUN_BACKUP_DIR" ] && [ -n "$(ls -A "$GB_RUN_BACKUP_DIR" 2>/dev/null)" ]; then
        gb_info "Originals of every modified file are in $GB_RUN_BACKUP_DIR"
    fi
}
