# Decisions

This log records *why*. The normative rules are in [`CONTRACT.md`](CONTRACT.md).

Status values: **Accepted** (agreed in design review, 2026-10-30), **Provisional** (accepted for
Phase 1 only, to be revisited with evidence), **Open** (undecided).

When a decision is superseded, add a new entry and mark the old one `Superseded by D-NNN`. Do not
rewrite history.

---

## D-001 The shell is the desktop
**Status:** Accepted

The system is a console appliance, not a Linux desktop with a theme. The launcher is a replaceable,
supervised process, not a desktop environment. There is no window manager, panel, tray or desktop
icons unless a specific entry proves it needs one.

Consequence: the library is the primary interface, and everything else is an entry in it.

## D-002 Buildroot is the product base; Tiny Core is prototype-only
**Status:** Accepted

- Buildroot with a `br2-external` tree produces a reproducible image from one command, which suits
a repository-based release process.
- Remastering Tiny Core is awkward to version-control, and its RAM-loaded model and extension
  persistence were designed for a general tinkerer's system rather than versioned releases.
- Tiny Core's main benefit (prebuilt extensions) shrinks because mGBA and SDL2 would likely need
  building either way.
- Cost accepted: we own kernel configuration and firmware selection, which is where hardware
  compatibility is won or lost.

## D-003 Process model: supervisor -> launcher -> handler
**Status:** Accepted

```text
PID 1 (init)
  +-- supervisor   headless; owns the system button; classifies outcomes
        +-- launcher   owns the display while browsing
        +-- handler    owns the display while running an entry
```

Separate processes give fault isolation (a crashed emulator cannot take down the library) and avoid
two processes competing for display ownership. We rejected one monolithic executable.

## D-004 The launcher exits on launch
**Status:** Accepted

The launcher terminates with a launch request. The supervisor starts the handler, waits, and
restarts the launcher at the previous entry. This avoids display-master handoff races (contract
S2, S3) and keeps the launcher nearly stateless.

## D-005 Save boundary: handler adapter, not supervisor
**Status:** Accepted

The supervisor is completely ignorant of save semantics (S5). Durability is the responsibility of
the handler, optionally through an adapter. A supervisor that snapshots files risks copying an
inconsistent file and turns into a save-management daemon.

The adapter may keep a clean-exit backup, but this is **not** assumed sufficient. A copy taken at
launch could capture a file already torn by a previous power cut, so any backup is taken only after
a clean exit, and the final strategy is chosen from the evidence of T-SAVE-1.

`sync` is a final safeguard, not the mechanism. The mechanism is atomic replacement, `fsync` of the
file and directory, a journaling filesystem, correct emulator flushing, and clean shutdown.

## D-006 No browser in the base image
**Status:** Accepted

No small browser handles the modern web well, and a capable one needs a windowing system plus
keyboard and mouse. A browser also invites in-system ROM downloading. Content arrives by USB, by the
user-visible GAMES partition, and later by a LAN upload screen. A browser may return as an optional
package.

## D-007 mGBA as a separate process
**Status:** Accepted (embedding deferred)

mGBA is accurate, actively maintained, MPL-2.0, and its built-in HLE BIOS means no proprietary BIOS
is shipped. It also covers GB and GBC. It runs as a separate process with per-launch configuration
overrides pointing saves and states at our layout.

An own thin frontend over a libretro core or libmgba remains a later option, for an in-game overlay
or to drop a graphics-stack dependency.

## D-008 SDL2 with the KMS/DRM backend; graphics stack cost to be measured
**Status:** Provisional

Avoiding X11 and Wayland does not avoid Mesa, because SDL2's KMS/DRM backend relies on GBM and
EGL. As far as we know, this brings in tens of MB of libraries and an awkward software-rendering
fallback. T-MEAS-1 measures it.

Alternative if the cost is unacceptable: CPU-rendered UI on DRM dumb buffers, ALSA audio and evdev
input, with an own frontend.

## D-009 Storage layout
**Status:** Accepted for DATA and GAMES. **Provisional** for where the system image physically lives.

BOOT (FAT32/ESP), DATA (ext4), optional GAMES (exFAT/FAT32). The contract requires only that the
system image is immutable and boot-selectable (B3, B4). Placing a large image on the FAT32 ESP is
convenient for the prototype but couples image storage to FAT. The product may instead use dedicated
system partitions (`SYSTEM-A`, `SYSTEM-B`) beside DATA and GAMES. ext4 for DATA because its tooling
and journaling behavior are mature and debuggable on unknown hardware. GAMES is separate so the user
can manage content from any operating system, and everything the system needs for correctness stays
off FAT-family filesystems, which are not power-loss safe.

First boot creates DATA (and GAMES where applicable), which keeps the release image small.

## D-010 Initramfs as rootfs for Phase 1
**Status:** Provisional

The whole immutable rootfs is an initramfs. This gives the simplest boot chain, inherent immutability,
no loop-mounted root, and easy recovery. It costs the image size in RAM, which we will measure rather
than guess.

Later option: initramfs -> mount squashfs -> `switch_root`, with tmpfs overlays, if the RAM/image
trade-off justifies it. The contract only requires an immutable image (B3), so this change would not
affect any contract clause.

## D-011 Single slot in Phase 1, slot-shaped; A/B in Phase 9
**Status:** Accepted

Dual-slot machinery before a working OS is unnecessary complexity. What matters is the interface:
the runtime never assumes one slot (B4). The active slot comes from the bootloader (for example a
kernel parameter), not a symlink, because the BOOT filesystem is FAT and has no symlinks.

Phase 9 target: A/B images, boot counting, automatic rollback, and single-file updates delivered on
USB with signature verification. Network updates are deferred until the signing story exists.

## D-012 Entry identity is a content hash
**Status:** Accepted

Entry ids for games are derived from a hash of the content, so renaming a file does not orphan its
saves. Hashes are cached by (path, size, modification time) to avoid hashing large files on every
scan.

## D-013 GAMES is mounted read-only
**Status:** Accepted

It removes dirty-flag and unclean-unplug corruption, and makes removal during play harmless because
saves live on DATA. On-device content management, if added later, will need a deliberate remount
policy.

## D-014 Text-mode recovery independent of the GPU userland
**Status:** Accepted

If the failure being recovered from is "the graphics stack does not work on this GPU", a graphical
recovery fails with it. Recovery is a text interface on the kernel console through generic framebuffer
support. The kernel configuration must therefore include that support.

## D-015 Neutral interface names
**Status:** Accepted

Environment variables and paths use a neutral prefix (`SYS_`), not `GBA_`. GB/GBC support is nearly
free with mGBA and further systems are plausible, so the interface must not assume one system.

## D-016 Metrics instead of RAM targets
**Status:** Accepted

RAM is not the hard constraint: mGBA needs little, and the stated 2 GB limit is easily met. The
metrics that matter are boot-to-launcher time, input latency, image size, and power-loss behavior.
Phase 1 records measurements (T-MEAS-1) and sets targets afterward from data.

## D-017 Parameter defaults
**Status:** Provisional

`GRACE` = 3 s. Crash-loop threshold N = 3 crashes within M = 30 s. Both are starting points to be
tuned from Phase 1 evidence.

## D-018 Contract versioning is minimal
**Status:** Accepted

Manifests carry `contract = 1`. Components ignore or reject unsupported contracts without crashing.
A richer compatibility scheme (for example `supported_contracts`) waits until an application
ecosystem needs it.

## D-019 Name and assets
**Status:** Accepted

"GBA-inspired" describes a look and feel. All art, names and UI assets are original, with no Nintendo
marks. Check the project name before any public release.

## D-020 Root in Phase 1 is a development simplification
**Status:** Accepted

Phase 1 may run every process as root, because privilege separation would distract from proving the
architecture. Root is not an application contract. Controlled paths (H1) and declared needs (L8)
must behave identically when privilege separation arrives, so applications never change.

We do not claim enforcement we cannot test: while everything is root, nothing prevents a handler
from violating H5. Those clauses are verified by review of the bundled handlers, and enforcement is
deferred (SEC7).

## D-021 Verification classes instead of a test per clause
**Status:** Accepted

Each MUST carries `TEST`, `REVIEW` or `DEFERRED`. Architectural invariants (for example "the runtime
does not hardcode a slot") are verified by inspection, not by contrived runtime tests. Phase 1 adds
tests only where a hardware or behavioral claim could otherwise be silently false. Traceability is
checked mechanically: every tagged test appears in the Verifies column and vice versa.

## D-022 Outcome classification is lifecycle state, and adapters must be faithful
**Status:** Accepted

The supervisor classifies a session from its own state plus the termination status it observes. It
does not claim to know *why* a process exited. `user-exit` means termination was requested and the
handler exited successfully within `GRACE`. `clean` means it exited successfully with no request
outstanding. A handler killed by the requested SIGTERM itself is `terminated`, which exposes handlers
that do not flush on termination.

Adapters may normalize an application's internal result but must not alter process-level outcome. A
wrapper that exits with a status code when its child dies from SIGSEGV would turn a `crash` into an
`error`, so wrapping adapters forward SIGTERM and re-raise fatal signals, or `exec` the program.

## D-023 Handler state is per handler, not per entry
**Status:** Accepted

`SYS_SAVE_DIR` is per entry (`/data/saves/<entry-id>/`). `SYS_DATA_DIR` and `SYS_CONFIG_DIR` are per
handler (`/data/handlers/<handler-id>/`, `/data/config/handlers/<handler-id>/`), because emulator
settings and caches are shared by all of a handler's entries. Installed application payload lives
separately in `/data/apps/<app-id>/` and is not writable by the application. This keeps handlers
from sharing a writable parent directory, so the isolation boundary is real once enforced.

---

## Open questions

| # | Question | Resolve by |
|---|---|---|
| Q1 | Bootloader: systemd-boot with boot counting, or GRUB with environment state. Behavior on old UEFI firmware is unverified. Decide by a small spike, not in advance: can the candidate boot our image, hide its menu, provide the active slot, offer recovery selection, survive repeated cold boots, and do all of that on both reference machines? | Phase 1 real-hardware work |
| Q2 | Does stock `mgba-sdl` flush saves and exit cleanly on SIGTERM? Depends on T-HANDLER-2; may force a small patch, an adapter workaround, or an earlier own-frontend decision. | T-HANDLER-2 |
| Q3 | Is mGBA's save writing atomic? Selects the adapter's save-integrity strategy. | T-SAVE-1 |
| Q4 | The two real test machines. Their firmware and GPU shape the kernel configuration, so choose them before writing the config. | Before Phase 1 build |
| Q5 | Hotplug mechanism for storage and controllers that respects "no unnecessary daemons" (BusyBox `mdev`, polling on demand). | Phase 1 |
| Q6 | Frame-pacing strategy: vsync vs audio-driven timing, given about 59.73 Hz content on a 60 Hz panel. | T-AV-2 |
| Q7 | The system-button combination. (The privilege model is settled for Phase 1 by D-020.) | Phase 1 |
| Q8 | Logical canvas resolution for the launcher. | Phase 3 |

## Process notes

- `GAME_PLAN.md` is not modified until this contract has been reviewed against it. The reconciliation
  happens in one deliberate commit afterward.
- Rationale belongs here. `CONTRACT.md` stays purely normative and implementation-agnostic.
