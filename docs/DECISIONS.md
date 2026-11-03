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

---

## D-024 Library search is keyboard-only, and lives in the shell

**Decision.** Incremental search is part of the launcher, not a separate screen, and it is driven by
plain typing rather than a chord. A gamepad has no text entry, so pads navigate and search is a
keyboard or USB-keyboard affordance.

**Why.** A console appliance with a growing library needs a way to jump to a title without scrolling.
Streaming the filter into the existing list keeps one interaction model (navigate → confirm) and one
piece of layout code.

**Consequence.** Typing must never double as navigation, so the shell implements its own held-key
repeat for arrow keys instead of enabling global key repeat.

---

## D-025 Play history is launcher state, not save data

**Decision.** Launch counts, last-played timestamps and cumulative play time live in
`DATA/config/history.json`, written by the launcher. The supervisor still never reads or writes save
data (S5), and it does not parse history either.

**Why.** Focusing the last-played entry is the single most requested console behaviour, but it must
not create a second, competing model of "what is a save". Config is disposable-with-consequences:
losing it costs a recency hint, not a save.

---

## D-026 Saves get a pre-session backup, not a save-state protocol

**Decision.** The adapter copies `.sav`/`.srm` files to `<name>.bak` before mGBA starts and restores
the backup when the live file is missing or zero-length afterwards, in addition to fsyncing file and
directory on exit (SV2). Backups are refreshed only when the file size changed.

**Why.** SV4 requires a recoverable prior state after a hard power loss. A pre-session mirror is the
cheapest mechanism that survives mGBA being killed mid-write, and it composes with the emulator's own
save handling instead of replacing it. It is explicitly *not* a savestate feature.

---

## D-027 Display scaling is a user preference that both shell and handler obey

**Decision.** `DATA/config/display.json` holds `scaling: integer|stretch`. The supervisor exports it
as `SYS_DISPLAY_SCALING`; the adapter maps it to mGBA's `lockIntegerScaling` and the shell maps it to
its own presentation. The setting previously existed only as a modal label.

**Why.** D2 asks for integer scaling with borders when the aspect ratio does not match. Making the
choice persistent and shared avoids the common failure where the menu says one thing and the
emulator does another.

---

## D-028 The shell owns log hygiene; the supervisor owns orphan cleanup

**Decision.** Log rotation (2 MB, 5 archives) and pruning of old boot logs happen in `common.py` and
are invoked by whichever component opens the log and by the supervisor at boot. Orphan cleanup
matches `argv[0]`/script basename against a fixed executable list, never against a command-line
substring.

**Why.** ST5 requires logs to be unable to fill DATA, and S8 requires stale foreground sessions to be
terminated. Both are cheap, but both are dangerous if implemented loosely: rotating in the wrong
place truncates a live session's log, and substring matching can signal unrelated processes belonging
to the user.

---

## D-029 The shell draws at native resolution; D1 is amended

**Decision.** The launcher composes its frame on a surface that is exactly the window size and lays
everything out in "design units" against a 320-unit reference height (`ui_scale = height / 320`).
`scaling=integer` becomes an opt-in retro mode that composes on 480x320 and upscales by a whole
factor. D1 in the contract is amended accordingly.

**Why.** The original 480x320 canvas was scaled up to the display, which produced a small, blurry
window in the middle of a laptop panel and wasted the screen the user actually has. Drawing at native
resolution costs nothing per frame with pygame's blits and makes every glyph crisp; expressing the
layout in design units keeps the same composition on a 480x320 handheld panel and on a 1440p laptop,
so no separate layout code is needed. Fonts are TTF and sized in the same units for the same reason.

---

## D-030 A four-palette theme system instead of hard-coded colours

**Decision.** `theme.py` defines one shared set of neutrals plus four accent palettes (`indigo`,
`violet`, `amber`, `mint`), and every surface, primitive and font goes through a `Theme` object. The
choice persists in `DATA/config/settings.json`.

**Why.** "Not getting a GBA colour vibe" is a real design defect: the previous UI used ad-hoc greys
and a single blue accent. A palette table makes the identity explicit (indigo shell, magenta logo,
amber power LED) and lets a user switch the mood without touching code. Keeping the neutrals shared
means the four themes cannot drift apart in contrast.

---

## D-031 Handlers receive a resolved display environment, not the inherited one

**Decision.** `config.prepare_child_environment()` classifies the session (`x11`, `wayland`,
`console`, `headless`) by looking at `DISPLAY`/`WAYLAND_DISPLAY` *and whether their sockets exist*,
then pins `SDL_VIDEODRIVER` accordingly, drops a stale `DISPLAY`, and forces a dummy audio driver
when no sound device is present. The adapter uses it for mGBA and also passes `-f` plus explicit
`-C fullscreen=...` overrides, and retries once windowed if mGBA dies in under five seconds with an
error inside a desktop session.

**Why.** "Launching a game shows a full black screen" is an environment bug, not an emulator bug:
SDL happily grabs a KMS/DRM master it cannot present when a desktop owns the GPU, and a dead
`DISPLAY` silently routes rendering nowhere. Resolving the session instead of inheriting it fixes
the common causes; the retry keeps a machine with an unusual graphics stack playable instead of
bouncing the user straight back to the library. This is also why the shell tears down pygame before
exiting with code 10: the handler must not inherit a half-torn-down display context (D4).

---

## D-032 ROM import is the single sanctioned write path into GAMES

**Decision.** A new `docs/CONTRACT.md` rule, ST7, allows the shell to write ROM files into GAMES, but
only after an explicit confirmation, only by creating new files, and never by overwriting: identical
files are skipped, name clashes get a `(n)` suffix, and partial copies are removed. ST4 is amended to
name this as its only exception.

**Why.** The appliance model treats GAMES as user-owned and read-only to the system. Importing from a
pendrive is a direct user request to change its contents, so it has to be ring-fenced: a rule that
says "never write" and an implementation that writes quietly would be worse than either. The
confirmation step is deliberate product behaviour, not a nicety.

---

## D-033 The terminal is a real PTY, not a command runner

**Decision.** The TERMINAL tab runs `$SHELL -i` on a pseudo-terminal in its own session. Output is
parsed by a small ANSI screen emulator (SGR colours, cursor movement, erase, scrollback capped at
500 rows) and input is translated from pygame key events to terminal byte sequences.

**Why.** A terminal that only executes one command at a time is not a terminal: no `cd`, no `top`, no
prompts, no colour. A PTY gives all of it for the price of ~500 lines of stdlib code, keeps the tab
useful for debugging on a machine with no X11, and means the shell never parses command lines itself.
`Ctrl+Alt+T` toggles the tab from anywhere, including from a modal.

---

## D-034 The browser is a text renderer with a hard budget

**Decision.** `gba_browser.py` fetches with `urllib`, caps documents at 2 MB and images at 1 MB with
an 8-second timeout, extracts headings/paragraphs/lists/quotes/pre/links/images from HTML with
`html.parser`, and lays text out itself with the theme fonts. No JavaScript, no CSS layout, no
external dependencies. Unreadable pages degrade to their text rather than rendering badly.

**Why.** The user asked for a "very light-weight browser", and on an appliance the failure mode of a
real engine is a memory-hungry, network-exposed process that breaks the boot-to-play budget. A
reader-shaped browser is honest about what it is: it makes documentation, wikis and text sites
usable, it is auditable in one file, and it cannot be a vector for scripted content.

---

## D-035 Settings are declarative pages over one JSON store

**Decision.** Every settings row is a dictionary (`toggle`, `choice`, `slider`, `action`, `info`)
bound to a getter and a setter; the page renderer is generic. All values live in
`DATA/config/settings.json`, written atomically, with `config/display.json` migrated on first read.

**Why.** The previous settings screens were lists that mostly printed information; making every row a
real control with a declared type means the UI cannot drift from what is persisted, new settings cost
one row, and the store is inspectable by a user with a text editor. Migration exists because losing a
scaling preference on upgrade is a small but real regression.

---

## D-036 Main navigation is five tabs with global shortcuts

**Decision.** GAMES, APPS, BROWSER, TERMINAL and SETTINGS are peer tabs. `Alt+Tab` /
`Alt+Shift+Tab` cycle them, `Ctrl+Alt+T` toggles the terminal from any screen or modal, and mouse
clicks hit the same tab rectangles the renderer paints. Library entries (games, apps, settings
sections) are all drawn as cartridges, with a list view available.

**Why.** Apps and settings used to be hidden behind modals reachable only from the library, which
made a working shell feel like a demo. Peer tabs give the shell a stable mental model, and the two
global shortcuts are the ones a user coming from a desktop will try first. Cartridges everywhere is
the visual answer to "games and apps should look like game cartridges".

---

## D-037 Installing is plan → validate → confirm → apply, never "just do it"

**Decision.** `src/gba_installer.py` always builds an `InstallPlan` first, passes it through
`validate_plan` (nine refusal rules, IN1), asks for a confirmation, and only then executes the steps
in a fixed order: partition → format → mount → copy → verify → bootloader → unmount. The same code
path serves `--plan` (print and exit), `--dry-run` and `--install`.

**Why.** Every remaining way this project could damage a user's machine runs through disk writes.
Splitting "decide" from "act" means the decision can be printed, diffed, tested without root, and
refused. It also means the guided setup and the command line cannot drift apart: they both hand the
same plan to the same validator.

---

## D-038 Irreversible means typed, not `--yes`

**Decision.** Whole-disk installation requires the phrase `ERASE <disk>`; partition installation
requires `FORMAT <partition>`. `--yes` only suppresses cosmetic prompts; unattended installs must
pass `--accept-phrase` with the exact text. `gb_confirm_typed` in `scripts/lib/common.sh` implements
the same rule for shell code, and refuses when stdin is not a terminal.

**Why.** `--yes` is muscle memory and gets pasted from documentation. A phrase that names the
specific device cannot be reused from another machine's instructions, and typing it means the
operator has read which disk is about to disappear.

---

## D-039 The bootloader is the last step, and it is verified

**Decision.** `Installer.install_bootloader` runs after copy and verification. UEFI installs use
`--no-nvram --removable` (firmware boot order is never edited). On a running Linux,
`scripts/install.sh` regenerates `grub.cfg`, greps for its own entry, and restores the previous file
byte-for-byte on any mismatch, then removes its drop-ins.

**Why.** A half-written bootloader is the only failure mode that can make a *different* operating
system unbootable. Anything that fails before it is recoverable by rebooting into the old system;
anything that fails after it is reported as `EXIT_BOOT_FAILED` with the old menu intact.

---

## D-040 Installing into an empty partition is the default; whole-disk is opt-in

**Decision.** The guided setup lists empty partitions first, marks them "keeps everything else",
defaults to the largest internal one, and only offers a whole disk when that disk holds no operating
system — as a destructive option behind a typed phrase. A USB stick is never the recommended target.

**Why.** Dual-boot safety is not a feature you enable; it is the shape of the default. Erasing a
whole disk is legitimate (a spare SSD, a dedicated handheld) but it must be a conscious choice, and
the recovery path for a mistake is "your other system still boots".

---

## D-041 The setup asks two questions, and the first one has an answer

**Decision.** The guided setup shows a disk table and a recommended target number, asks
"Number [n], or Q to quit", then a single confirmation that defaults to yes for the safe option.
Failure screens state the machine's state in words. Declining always ends at a shell: the live
medium is a normal Linux, so "no" is never a dead end.

**Why.** The user's bar was "easier than installing Windows". Windows Setup is long because it
decides many things on the user's behalf in sequence; here almost every decision has an obvious right
answer, so the setup decides them and shows the result. The one decision that is genuinely the
user's — which disk — is the one that gets a screen.

---

## D-042 An ISO that cannot be built must say so, loudly

**Decision.** `scripts/build_iso.sh` has a `--check` mode that lists every missing prerequisite and
exits non-zero, and the full build refuses to start without them. It writes only inside
`build/iso/` and `build/`, prints the `dd` command with a warning that the stick is the only thing
erased, and records a sha256 next to the image.

**Why.** A build script that silently produces a 3 MB "ISO" wastes the user's evening and, worse,
teaches them to distrust the project. The sandbox this was developed in has no `xorriso`, so the
check path is the one that gets exercised there — and it is the path a user without the right
packages will hit first.

---

## D-043 Every entry point has a fallback, and the fallbacks are tested

**Decision.** Missing pygame exits 103 with instructions; the supervisor treats 103 as fatal and
goes to recovery instead of counting a crash; the adapter prints the `apt install mgba-sdl` line on
the console; `gba_init.sh` falls back from supervisor to recovery to a rescue shell that re-spawns
itself forever, and reboots via `/proc/sysrq-trigger` when the binary is gone.

**Why.** The user's requirement was that no single failing component may leave the machine unusable.
"Unusable" here means a black screen with no keyboard response, so every fallback ends in a shell
that can be retyped into a working system.

---

## D-044 Self-test is the answer to "is this machine OK?", not a log file

**Decision.** `src/gba_selftest.py` checks the interpreter, fonts, pty, display session, emulator
binary, every module import, a real pygame blit on the dummy driver, storage writability and space,
and a config round-trip. It prints one line per check with a fix hint, supports `--json` for
scripting, and exits 0 when nothing *fails* (warnings do not fail it).

**Why.** After an install the user needs a single, quotable answer to "will games run?" that is not
"look at `/data/logs`". The same binary runs in the installer's post-install step, in the boot
wrapper and by hand, so the answer cannot disagree with itself.

---

## D-045 An existing EFI partition is shared property, not scratch space

**Decision.** `InstallPlan.created_efi` is true only in whole-disk mode. Partition mode reuses the
machine's ESP as-is, installs GRUB with `--removable --no-nvram` (so `EFI/BOOT/BOOTX64.EFI`), and
copies any existing `EFI/BOOT` directory to `EFI/BOOT.gba-backup-<timestamp>` before writing.

**Why.** Formatting an ESP is how dual-boot installations kill Windows: the partition holds the
other system's boot manager, BitLocker keys and firmware update capsules. Writing only our own
fallback loader keeps both systems startable, and the backup makes even that one file recoverable by
hand.

---

## D-046 The installed image must boot the shell, never the installer

**Decision.** `Installer._configure_boot` removes any `gba-setup.service` inherited from the
installation medium, enables `gba-appliance.service` on `tty1` (running
`/usr/local/bin/gba-tty1-wrapper`), and writes `/etc/default/grub.d/99_gba-appliance.cfg` so the
installed system boots with `gba_mode=1`.

**Why.** The ISO is both installer and payload, so the copy inherits the live session. Without this
step the installed machine would offer to install itself on every boot — the single most confusing
thing an OS can do. The unit conflicts with `getty@tty1` and the wrapper falls back to `agetty` when
`gba_mode` is absent, so a user who edits the kernel line still gets a login prompt.

---

## D-047 A black screen is a video-backend bug, so the backend gets a ladder

**Decision.** The adapter classifies a session that died non-zero in under five seconds as a video
failure and retries once: windowed with no `SDL_VIDEODRIVER` pinning under x11/wayland, and the next
`/dev/dri/card*` on a bare console (with `SDL_VIDEO_KMSDRM_DEVICE` pinned to the first card up front).

**Why.** The launcher hands the display to mGBA on every console, appliance or desktop, and a single
wrong guess used to mean "the game opens and immediately drops you back to the library". Which guess
is wrong is not knowable in advance — a stale `DISPLAY`, a DRM master already held, a hybrid GPU
whose first card drives no output — so the fix is to try the plausible alternatives once, cheaply,
and only when the failure looks like a video failure (fast exit, not a clean one, not a long session,
not headless).

---

## D-048 The import prompt measures new content, not discovery

**Decision.** A plugged-in volume is offered for import only when it holds ROMs whose content ids
(Rule L2 hashes — the library's existing identity, not a second system) are absent from
`Library.known_content_ids()`. That known-id set is backed by the on-disk library cache; the source
side is memoised in `DATA/library/usb_sources.json` (path + size + mtime, Rule L4 semantics, ST2
disposable) so a stick is fully hashed at most once per machine. When the library knows nothing
(fresh install) every discovered ROM is provably new and the prompt appears without hashing
anything. All walking and hashing happens on the watcher thread, off its lock; `refresh()` re-arms
detection instead of analysing synchronously. Imports register their copies' ids
(`register_content`) the moment the files land, and the library cache flushes every 16 freshly
hashed files mid-scan because the scanner is a daemon thread that dies when the launcher exits into
a game.

**Why.** The supervisor restarts the launcher process after every game session and every boot, so
any memory-only notion of "this stick was already offered" evaporates — a 1130-ROM stick was
re-announced forever. Persisting "the prompt was shown" is the wrong state: it silently suppresses
genuinely new content added to the same stick and lies after a reinstall. The only durable truth
about "already imported" is the library's own content identity, which already exists on disk; the
prompt just had to be computed from it. Discovery minus known library equals new content — that
difference, not a flag, is what the user is asked about.

---

## D-049 The emulator window is born screen-sized, because fullscreen needs a window manager

**Decision.** The adapter reads the physical display mode without acquiring the display
(`/sys/class/drm/*/status,modes`, internal panel preferred, `SDL_VIDEO_KMSDRM_DEVICE` pin honoured,
`xrandr` fallback under X11) and passes it to mGBA as `-C width=… -C height=…` alongside `-f`,
`lockAspectRatio=1` and `lockIntegerScaling`. When the mode cannot be determined it falls back to
`-4` (4× native) rather than the default 1×. It also passes `-C rewindEnable=0` (and writes it into
the managed `config.ini`), disabling mgba-sdl's default every-frame rewind snapshot recording that
the appliance never exposes as a control.

**Why.** SDL2's X11 backend implements `FULLSCREEN_DESKTOP` purely as a `_NET_WM_STATE_FULLSCREEN`
client message, and the appliance session (`xinit gba-session -- :0 vt1`) has no window manager:
the request is dropped, the window keeps its birth size (mGBA's default viewport = native ×1), and
mGBA's one-shot post-start `SDL_SetWindowSize` is a no-op under the fullscreen flag — a 240×160 GBA
image centred on a laptop screen, 160×144 for GBC. Explicit screen-sized geometry produces the same
result with or without a WM, and mGBA's own viewport math then centres the largest
correctly-proportioned integer- or fit-scaled image inside it: no crop, no stretch, correct 3:2 /
10:9. KMSDRM sessions ignore window size (scanout is the mode), so the change is inert there. The
rewind default costs a full core snapshot per emulated frame into a 600-second buffer on the
emulation thread — the only default-on per-frame overhead the adapter controls, and the prime
suspect for stutter on modest hardware; pacing (`audioSync`/`videoSync`/`fpsTarget`) is untouched.
Verified against mgba-sdl 0.9.3, 0.10.1, 0.10.3 and master (identical GL viewport path; `-C`
overrides win over `config.ini`; all needed keys mapped in every version).
