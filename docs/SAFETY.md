# Can this brick my PC?

Short answer: no. Read on for why, and for the exact list of things that *can*
change on your machine.

This document exists because that is the first question anyone sensible asks
before booting an unfamiliar image from a USB stick. Everything below is a
property of the code in this repository, not a promise about the future.

---

## 1. Three different ways to run GBA OS

| Way | What it touches | What it can destroy |
|---|---|---|
| **Live USB** (`make iso`, boot the stick) | Only RAM, plus the stick itself | Nothing on your PC |
| **Install with the USB** (`gba-install` / guided setup) | The partition or disk you confirm | Only that partition/disk |
| **Install on a running Linux** (`scripts/install.sh --apply`) | `/opt/gba-distro`, `/data`, `/games`, GRUB drop-ins | Nothing outside those paths |

Nothing in this project ever writes to firmware, NVRAM (`efibootmgr` is never
called with a write), a bootloader of another operating system, or a disk it was
not told to use.

---

## 2. The live USB cannot change your computer

Booting the stick runs a normal Linux live system from a squashfs image. The
image is read-only; the live-boot layer mounts a RAM overlay. Removing the stick
(or powering off) discards everything.

The setup program that starts on `tty1` **asks before it writes**. Until you
pick a target and confirm it, the only operations performed are reads: `lsblk`,
`/proc/mounts`, `/sys/block`, and read-only mounts of candidate partitions to see
whether they contain an operating system.

The boot menu also offers:

- *Install GBA OS (safe graphics)* — `nomodeset`, no GPU driver, plain framebuffer.
- *Start a shell on this medium* — no installer at all.
- *Boot from the internal disk instead* — leave without changing anything.

---

## 3. The installer's refusals

`src/gba_installer.py` will not proceed, at all, when any of these is true. Each
case has a test (`tests/test_installer.py::TestSafetyRefusals`):

| Refusal | Why it exists |
|---|---|
| Target is the disk/partition the installer is running from | You would delete the running system mid-install |
| Target is the live medium | You would destroy the stick you booted from |
| Target partition is mounted | Formatting a mounted filesystem corrupts data |
| Target partition contains an OS (`/etc/os-release`, `/boot/vmlinuz`) | Your existing system must survive |
| Target is smaller than 8 GB | It would fail half way and waste your time |
| Target looks removable and `--allow-removable` was not given | Accidental stick wipes |
| EFI partition is on a different disk than the target | A cross-disk bootloader is unfixable later |
| Root filesystem source missing or unreadable | Nothing would be installed |
| Existing EFI partition | It is never formatted; a foreign `EFI/BOOT` loader is copied to `EFI/BOOT.gba-backup-<timestamp>` first |
| `--mount-root` is already in use | Files would land in the wrong place |

Whole-disk mode (which *does* erase a disk) additionally requires typing
`ERASE /dev/sdX` exactly. `--yes` alone is not enough: `--accept-phrase` must
also match. That is deliberate — it is the one operation in this project that
can destroy data outside its own directories.

---

## 4. Order of operations, and why it matters

```
validate → [partition] → format → mount → copy → verify → bootloader → sync
```

The bootloader is written **last**, after the system has been copied and
verified. A failure before that point leaves your boot menu untouched, so the
machine still starts its usual operating system. The installer unmounts
everything on every exit path, including failure.

Exit codes are part of the interface:

| Code | Meaning | Machine state |
|---|---|---|
| 0 | Success | GBA OS installed, menu entry added |
| 2 | Usage error | Nothing changed |
| 3 | Safety refusal | Nothing changed |
| 4 | A step failed | Boot menu untouched; partial data on the target only |
| 5 | Bootloader failed | System copied, boot menu untouched |

The guided setup prints the same information in words: *"Your boot menu was not
modified, so this computer still starts its usual operating system."*

---

## 5. What the GRUB entry does (and does not) do

`scripts/install.sh` writes `/etc/grub.d/11_gba_os`, which **appends** entries:

- `GBA OS (console appliance)` — the appliance.
- `GBA OS (safe graphics / recovery)` — adds `nomodeset gba_safe=1`; skips the
  GPU entirely and boots the recovery console.
- `Boot the primary operating system` — lets you return to your old system
  without touching the firmware boot order.

Then it regenerates `grub.cfg` and **verifies** the result with
`grep "GBA OS (console appliance)"`. If `grub-mkconfig` fails, or the entry is
missing, the previous `grub.cfg` is restored byte-for-byte and the drop-in files
are removed. `GRUB_DEFAULT` is never changed: the default boot target stays
whatever it was.

Uninstalling restores `/etc/inittab` from `/etc/inittab.gba-backup` and keeps
`/data` and `/games`.

---

## 6. Every component has a fallback

| Missing / broken | What happens |
|---|---|
| pygame | The launcher prints what to install and exits `103`; the supervisor goes straight to the recovery console instead of looping |
| mGBA | The game does not start; the adapter prints "mGBA is not installed" with the install command; saves are untouched |
| mGBA crashes mid-game | Saves are backed up before the session and restored if the session dies (`restore_backup_if_needed`) |
| X11 / Wayland | The shell runs directly on KMS/DRM, then on the dummy driver for headless checks |
| `gba-supervisor` missing | `gba_init.sh` starts a rescue shell, never a black screen |
| python3 missing | The init prints one line saying so, then starts the rescue shell |
| `reboot` / `poweroff` binaries missing | Falls back to `/proc/sysrq-trigger` |
| Supervisor crashes repeatedly | Recovery console, then a shell; PID 1 never exits |
| Installer cannot find room | Explains that this is not a problem with your computer, and changes nothing |

`make selftest` (or `gba-selftest`) checks a machine against this table and
reports pass / warn / fail per row, with the exact fix for each failure.

---

## 7. How the claims are tested

```
make test          # 350 tests: unit, contract, safety
make lint          # python compile + sh/bash syntax of every script
make iso-check     # what is missing to build an ISO on this machine
make selftest      # does *this* machine have everything GBA OS needs
```

`tests/test_safety.py` is specifically about the dangerous parts:

- every script parses in the shell that will run it (and `gba_init.sh` stays
  POSIX sh, so busybox/dash PID 1 works);
- no shell script formats, partitions or writes a disk — disk writes live only in
  `gba_installer.py`, behind the safety gate;
- the installer is dry-run by default;
- the GRUB entry adds a choice, always ships an emergency variant, is verified
  after regeneration, and never sets `GRUB_DEFAULT`;
- the ISO builder refuses to run (listing every missing tool) rather than
  producing a half-built image;
- a launcher without pygame exits 103 with a human-readable message.

What is *not* tested here: real hardware, real firmware, and a real ISO build.
This sandbox has no `xorriso`, no loop devices and no root, so the ISO path is
verified by syntax checks and by reading the generated configuration — not by
booting it. That limitation is stated in `README.md` and `CHANGELOG.md` as well.

---

## 8. If something does go wrong

1. **Machine does not boot at all.** Reboot and pick `Boot the primary operating
   system`, or `GBA OS (safe graphics / recovery)`. If the menu itself is gone,
   boot any Linux live medium and run `scripts/install.sh --apply --uninstall`
   from a checkout, or restore your distribution's `grub.cfg`.
2. **Black screen after the boot menu.** Choose the safe-graphics entry, or add
   `nomodeset` to the kernel line.
3. **The shell starts but games do not.** Run `gba-selftest` (or `Ctrl+Alt+T`
   and `gba-selftest`); it names the missing package.
4. **You want it gone.** `sudo ./scripts/install.sh --apply --uninstall` removes
   the hooks and the boot entries; `/data` and `/games` are kept unless you
   delete them.
