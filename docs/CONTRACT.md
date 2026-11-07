# System Contract

`contract = 1`

Status: **Draft 2 for Phase 1 (post-review).** This document is normative. It defines externally
observable behavior and interfaces. It deliberately does not define implementation. Rationale lives
in [`DECISIONS.md`](DECISIONS.md).

---

## 1. Scope & Terminology

### 1.1 Normative language

The words MUST, MUST NOT, SHOULD, SHOULD NOT and MAY are used as in RFC 2119.

### 1.2 Purpose

The system is a console-like appliance. The intended experience is:

```text
power on -> boot -> library -> select entry -> run -> exit -> library
```

The library is the desktop. There is no conventional desktop environment.

### 1.3 Terms

| Term | Meaning |
|---|---|
| **System image** | The immutable software the system boots from: kernel, root filesystem, supervisor, launcher, bundled handlers. |
| **Slot** | A named copy of the system image (`A`, `B`). Phase 1 has exactly one slot, but the runtime treats slots as a concept (see B4). |
| **Supervisor** | The headless process started by PID 1 that manages foreground sessions. |
| **Launcher** | The foreground process that renders the library and accepts a selection. |
| **Handler** | An executable that runs one entry in the foreground (for example an emulator, or a native application). |
| **Adapter** | A thin, handler-owned shim that wraps a third-party program so it satisfies the Handler Contract. |
| **Entry** | One item in the library: a game, an application, or a settings page. |
| **Content** | User-supplied data that an entry refers to (for example a ROM file). |
| **DATA** | The persistent, writable, system-owned storage. |
| **GAMES** | The optional, user-visible content storage. |
| **Foreground session** | The period during which one process owns the display and input. |
| **GRACE** | The time a handler is given to exit after a termination request. |

### 1.4 Verification classes

Every MUST/MUST NOT clause carries exactly one verification tag directly after its ID:

| Tag | Meaning |
|---|---|
| `[TEST: ids]` | Verified by an executable or manual acceptance test in section 12. The test's **Verifies** column lists the clause back. |
| `[REVIEW]` | A structural invariant, verified by source or configuration inspection. No standalone runtime test. |
| `[DEFERRED: phase]` | Normative now. Implementation and acceptance are assigned to a later phase. |

### 1.5 Non-goals

This contract does not cover a browser, a general file manager, Bluetooth, networking, update
application, metadata and artwork, multiple emulated systems, or a third-party application
ecosystem. Several are planned for later phases. None are Phase 1 deliverables.

---

## 2. Boot Contract

- **B1** [TEST: T-BOOT-1] The system MUST boot on x86_64 machines using UEFI.
- **B2** [TEST: T-BOOT-1] In normal operation the system MUST reach the launcher without a login
  prompt, a desktop, or visible console text.
- **B3** [TEST: T-BOOT-4] The system image MUST be immutable during normal operation. Nothing in
  normal operation may write to it.
- **B4** [REVIEW] The active slot MUST be provided by the bootloader and exposed to the runtime in
  exactly one place. The runtime MUST NOT hardcode paths that assume a particular slot, and MUST
  NOT assume only one slot exists.
- **B5** [REVIEW] In Phase 1 the system image consists of one slot.
- **B6** [TEST: T-STORE-1] The system MUST boot to a usable state when GAMES is absent.
- **B7** [TEST: T-BOOT-5, T-STORE-5] If DATA is absent (first boot), the system MUST create it. If
  DATA is present but fails its consistency check, the system MUST go to the recovery path (see
  F2) and MUST NOT reformat silently.
- **B8** [TEST: T-BOOT-3] A documented key held during boot MUST reach the recovery path.
- **B9** [TEST: T-BOOT-2] The system MUST survive repeated cold boots and power cycles without
  filesystem corruption or boot failure.

The system image MUST be immutable and boot-selectable. Where it physically lives (for example on
the ESP, or on dedicated system partitions) is provisional and is not part of this contract.

---

## 3. Storage & Filesystem Contract

### 3.1 Storage areas

```text
BOOT    (FAT32/ESP)  bootloader, boot state, system image slot(s)   [physical placement provisional]
DATA    (journaling) saves, states, screenshots, config, handler state, apps, library cache, logs
GAMES   (optional)   user-visible content, readable from other operating systems
```

### 3.2 Runtime layout

```text
/data/
  saves/<entry-id>/             per-entry save data               (precious)
  handlers/<handler-id>/        writable state of one handler     (precious)
  config/handlers/<handler-id>/ configuration of one handler      (precious)
  apps/<app-id>/                installed application payload     (precious)
  library/                      cache and thumbnails              (disposable)
  logs/                         rotating, size-capped
/games/                         content, scanned by the system
```

### 3.3 Rules

- **ST1** [REVIEW] DATA MUST use a journaling filesystem. Nothing the system needs for correctness
  may live on GAMES.
- **ST2** [TEST: T-STORE-4] `/data/library/` MUST be treated as disposable. Deleting it MUST NOT
  cause data loss; the library MUST be rebuilt from content and saves.
- **ST3** [REVIEW] Precious paths MUST NOT be removed or rewritten by any automatic process other
  than the component that owns them.
- **ST4** [TEST: T-STORE-1] [AMENDED 2026-11-02, see D-032] GAMES MUST be mounted read-only
  during normal operation. The system MUST NOT write to `/games/`, including beside a content
  file. The single exception is a user-confirmed ROM import (ST7, ST8), which is the only write
  path and MUST NOT create, modify or delete anything except new ROM files.
- **ST5** [TEST: T-FAULT-4] Logs MUST be size-capped and rotated. A runaway log MUST NOT be able
  to fill DATA.
- **ST6** [TEST: T-FAULT-5] DATA MUST reserve free space for system use so that logging and saving
  remain possible when users fill the volume.
- **ST7** [TEST: T-LIBRARY-3] ROM import from removable media:
  - discovery MUST NOT require `udisks`, `udevadm` or `lsblk`; mount state is read from
    `/proc/mounts` and removability from `/sys/block`;
  - nothing MUST be copied before the user explicitly confirms the import;
  - an existing file MUST NOT be overwritten: identical files are skipped and a name clash is
    copied under a `(n)` suffix;
  - every copy MUST be verified by size and a partial copy MUST be removed;
  - the destination MUST stay inside GAMES.
- **ST8** [TEST: T-LIBRARY-4] Android phones in File Transfer (MTP) mode MUST be supported as
  import sources without being treated as block/removable disks:
  - discovery MUST use MTP/USB-interface and FUSE-mount information, never `/sys/block`
    removability, partitioning or a `/dev/sdX` mount;
  - accessible phone storages MUST be scanned for the same `.gba`, `.gbc` and `.gb` extensions
    as ST7, with normal block-device USB detection unaffected;
  - MTP imports MUST reuse ST7's confirmation, size verification, destination, `L2` content-ID
    and library-registration paths, so content already known by ID is neither offered as new nor
    copied again;
  - a missing MTP/FUSE backend, locked screen or charging-only USB mode MUST degrade to no MTP
    source without affecting pendrive import or boot.
- **ST8a** [TEST: T-LIBRARY-4, T-LIBRARY-5] Every link of the MTP chain MUST be individually
  observable, and no single link MAY veto the others:
  - device discovery MUST accept a USB interface class 6 / subclass 1 with protocol **0 or 1**
    (vendor stacks that do not advertise PTP-only), MUST accept a `"MTP"`/`"mtp"` interface name as
    a fallback, and MUST NOT claim a PTP camera as an MTP volume;
  - `jmtpfs -l` MUST be parsed with quoted and unquoted fields, and rows that cannot be parsed MUST
    be logged rather than dropped silently;
  - a mount attempt MUST record which stage failed (tool, mount, or storage enumeration) and MUST
    be retried quickly a few times before backing off, so unlocking a phone is picked up without
    restarting the session;
  - a phone that mounts while locked, i.e. yields zero readable ROMs, MUST NOT be remembered as
    "already offered": unlocking it later in the same session MUST produce the import prompt;
  - an MTP storage that reports a file size of `0` MUST NOT be excluded from scanning or importing
    (FUSE backends report 0 for unknown size); a *failed* `stat` still skips the file;
  - `gba-selftest --mtp` MUST print the state of each stage (sysfs, tools, mount root, GVfs,
    devices, volumes, last error) plus one actionable hint.

---

## 4. Library Entry Contract

- **L1** [REVIEW] An entry MUST have: `id`, `title`, `type` (`game`, `app` or `settings`),
  `handler`, and handler arguments. Game entries additionally refer to a content path.
- **L2** [TEST: T-LIBRARY-1] The `id` of a game entry MUST be derived from the content (a
  cryptographic hash of the content file), not from its name or location, so renaming a file does
  not orphan its saves.
- **L3** [TEST: T-LIBRARY-1] Game entries MUST be discovered automatically from `/games/`.
- **L4** [REVIEW] Library scanning MUST NOT block the launcher from rendering and accepting input.
  Scan cost (such as hashing) MUST be cached by (path, size, modification time).
- **L5** [TEST: T-STORE-1] An entry whose content is missing MUST NOT crash the launcher and MUST
  NOT affect other entries.
- **L6** [TEST: T-STORE-2] An empty library MUST render a usable launcher with an explicit empty
  state.
- **L7** [TEST: T-MANIFEST-1] Application manifests MUST declare `contract = <integer>`. A
  component MUST reject or ignore a manifest whose contract it does not support, and MUST NOT
  crash.
- **L8** [TEST: T-MANIFEST-1] A manifest MUST declare what an application needs. Absent
  declarations default to the most restrictive value:

  | Need | Default |
  |---|---|
  | `network` | `false` |
  | `input` | gamepad only |
  | `display` | direct (no compositor) |

---

## 5. Handler Contract

### 5.1 Start

- **H1** [TEST: T-HANDLER-1] The supervisor MUST pass a handler the following environment. The
  directories are specific to the handler or entry named, and none of them is a parent of another
  handler's directory or of another entry's save directory.

  | Variable | Value |
  |---|---|
  | `SYS_CONTRACT` | the contract number (`1`) |
  | `SYS_ENTRY_ID` | the entry being run |
  | `SYS_CONTENT_PATH` | the content file, if any |
  | `SYS_SAVE_DIR` | `/data/saves/<entry-id>/` |
  | `SYS_DATA_DIR` | `/data/handlers/<handler-id>/` |
  | `SYS_CONFIG_DIR` | `/data/config/handlers/<handler-id>/` |

  Interface names MUST NOT contain a system-specific prefix such as `GBA_`.
- **H2** [TEST: T-LAUNCH-1] A handler MUST acquire the display and input itself. It MUST NOT
  assume the previous foreground session restored the video mode.
- **H3** [DEFERRED: networking phase] A handler MUST NOT require network access unless its manifest declares it.
- **H4** [REVIEW] A handler MUST NOT require any other handler to be present.

### 5.2 Run

- **H5** [REVIEW] A handler MUST NOT write outside `SYS_SAVE_DIR`, `SYS_DATA_DIR` and
  `SYS_CONFIG_DIR`, and MUST NOT write to the content directory or to `/data/apps/`.
- **H6** [REVIEW] A handler MUST NOT start another foreground session.

### 5.3 Exit

- **H7** [TEST: T-HANDLER-2, T-FAULT-3] On a supervisor-requested graceful termination (SIGTERM),
  a handler MUST attempt to complete pending writes and terminate within `GRACE`. A handler that
  does not terminate within `GRACE` is classified `hung`, and the supervisor MAY escalate to
  SIGKILL. A handler that dies from the SIGTERM itself, without handling it, is classified
  `terminated`, which is a violation of this clause.
- **H8** [TEST: T-LAUNCH-1] A handler MUST release the display and input when it terminates.

### 5.4 Outcome classification

The supervisor is authoritative about process-level outcome. It classifies each session from its
own state and the termination status it observes. A classification records the **lifecycle state in
which the process ended**, not the reason it ended. A termination request is outstanding from the
moment the supervisor sends SIGTERM.

- **H10** [TEST: T-HANDLER-1] The supervisor MUST classify every session as follows:

  | Observed | Class |
  |---|---|
  | exit 0, no termination request outstanding | `clean` |
  | nonzero exit, no termination request outstanding | `error` |
  | termination requested, exit 0 within `GRACE` | `user-exit` |
  | termination requested, nonzero exit within `GRACE` | `error` |
  | termination requested, killed by that SIGTERM itself | `terminated` |
  | killed by any other signal, including SIGSEGV or a kernel OOM kill | `crash` |
  | still running after `GRACE`, then SIGKILLed by the supervisor | `hung` |
  | failed before the handler started (invalid configuration, missing content) | `launch-failure` |

- **H9** [TEST: T-FAULT-1] An adapter MAY normalize an application's *internal* result (for example
  translate emulator-specific exit codes). It MUST NOT alter the process-level outcome that the
  supervisor observes. If the adapter wraps a child process, it MUST forward SIGTERM to the child
  and, if the child dies from a signal, MUST re-raise that signal on itself. An adapter SHOULD
  `exec` the program when it has no post-exit work.

Post-exit adapter work (such as a save backup) runs only after the child's outcome has been
observed, and counts against `GRACE` when it follows a termination request.

### 5.5 Parameters (provisional)

- `GRACE` = 3 seconds.


### 5.6 In-game overlays and display handoff

Cheats, nine save-state slots and the fast-forward cycle are handler-side features (see
`docs/DECISIONS.md` D-050, D-052). mGBA is *stopped* while an overlay is drawn, because a stopped
process keeps its SDL/KMS surface and a second pygame window cannot acquire it.

- **IG1** [TEST: T-INGAME-1] An overlay MUST NOT be opened until a native mGBA save state and a
  screenshot of the live game have been captured, in that order, while mGBA is still running. If
  either capture fails, the overlay MUST NOT open and the game MUST keep running.
- **IG2** [TEST: T-INGAME-1, T-INGAME-5] The emulator MUST be released before the overlay window is
  created, and the overlay MUST be closed before a new emulator process is started: never two
  foreground display owners.
- **IG3** [TEST: T-INGAME-2] Closing an overlay MUST resume gameplay from the state captured for
  that handoff (or, when no state exists, by restarting the same command) and MUST restore the
  current fast-forward speed. Resume MUST be idempotent: repeated close/resume calls restart one
  emulator process, not several.
- **IG4** [TEST: T-INGAME-3] No key event, hotkey or signal MAY be sent to a stopped or exited
  emulator. State capture MUST be refused while the emulator is stopped instead of waiting for a
  timeout.
- **IG5** [TEST: T-INGAME-4] Cheat additions, edits, deletions and toggles MUST persist immediately
  and MUST reach the restarted emulator through its own `-c` cheat file; a cheat change alone MUST
  NOT restart the emulator before the overlay has closed. The overlay MUST provide a controller-
  usable name/code editor, and destructive deletion MUST require confirmation.
- **IG6** [TEST: T-INGAME-3] The fast-forward cycle (`1x → 2x → 3x → 1x`) MUST be edge-triggered:
  holding `L + R` MUST produce exactly one transition, and every transition MUST preserve gameplay
  state.
- **IG7** [TEST: T-INGAME-1] If the overlay cannot be initialised at all (no display), the handler
  MUST resume the emulator and report the failure instead of leaving it stopped.

---

## 6. Supervisor Contract

- **S1** [TEST: T-BOOT-5, T-STORE-1] The supervisor MUST start without DATA or GAMES mounted.
- **S2** [TEST: T-LAUNCH-2] At most one process MAY own the foreground display and input at any
  time.
- **S3** [TEST: T-LAUNCH-2] The supervisor MUST NOT start the next foreground session until the
  previous session has released its resources or has been forcibly terminated.
- **S4** [TEST: T-INPUT-2] The supervisor owns the system-button combination. Handlers MUST NOT
  receive it.
- **S5** [REVIEW] The supervisor MUST NOT read, parse, copy or snapshot save data. Its interaction
  with handlers is lifecycle only.
- **S6** [TEST: T-HANDLER-1] When a session ends, the supervisor MUST classify the outcome (H10),
  request a filesystem `sync` as a final safeguard, and start the launcher with the previous entry
  and the outcome class so the launcher can present it (for example "Game crashed"). The `sync`
  is not the durability mechanism (SV7).
- **S7** [TEST: T-FAULT-6, T-FAULT-7] The supervisor MUST restart the launcher if it exits. If the
  launcher crashes N times within M seconds, the supervisor MUST enter the recovery path instead
  of respawning.
- **S8** [TEST: T-LAUNCH-1, T-FAULT-8] The supervisor MUST be restarted by PID 1 if it dies, MUST
  terminate any orphaned foreground session when it restarts, and MUST keep its own resource use
  bounded.
- **S9** [TEST: T-STORE-3, T-FAULT-1, T-FAULT-2] Any single broken handler, entry, or content
  medium MUST NOT prevent other entries from running.

Parameters (provisional): N = 3, M = 30.

---

## 7. Input Contract

- **I1** [REVIEW] The primary input model is a gamepad:

  | Control | Meaning |
  |---|---|
  | D-pad | navigate |
  | A | confirm / launch |
  | B | back |
  | START | menu |
  | SELECT | secondary action |

- **I2** [TEST: T-INPUT-1] Keyboard MUST be a fallback so the system is operable without a
  gamepad. Mouse SHOULD be a fallback.
- **I3** [TEST: T-INPUT-1] The system MUST define one input profile, and the launcher and the
  bundled handlers MUST derive their bindings from it. A pad MUST behave consistently in both.
- **I4** [TEST: T-INPUT-3] Controllers MUST be usable when connected after boot.
- **I5** [REVIEW] USB controllers are in scope for Phase 1. Bluetooth is not.
- **I6** [TEST: T-INPUT-1, T-INPUT-4] The shipped keyboard profile MUST map `Z` to the GBA `A`
  button and `X` to `B` (`DEFAULT_KEYBINDINGS` = 122/120, and the same values written into the
  mGBA `config.ini`). Changing that default MUST NOT overwrite a profile the user customised: a
  stored profile identical to the legacy reversed pair (and nothing else changed) is migrated, any
  other stored profile is left alone.

---

## 8. Display & Audio Contract

- **D1** [REVIEW] [AMENDED 2026-11-02, see D-029] The launcher MUST use the whole display: it
  renders at the display's native resolution and lays out in resolution-independent design units.
  It MUST NOT letterbox itself with a fixed 480x320 window. An optional "integer" mode MAY compose
  on a 480x320 canvas and upscale by a whole factor for a deliberately pixelated look.
- **D2** [REVIEW] Scaling of emulated content SHOULD use integer factors with borders when the
  aspect ratio does not match.
- **D3** [REVIEW] The launcher and the bundled emulator MUST NOT require an X11 or Wayland session.
- **D4** [TEST: T-LAUNCH-1] The launcher MUST establish its own display state on start (see H2)
  and MUST release it before a handler starts. A handler MUST NOT inherit a display environment
  that cannot present: a stale `DISPLAY` or a KMS/DRM driver inside a live desktop is a defect
  (see D-031).
- **D5** [TEST: T-AV-1] Audio MUST NOT underrun audibly during normal play.
- **D7** [TEST: T-DISPLAY-1] When a full-screen session exits non-zero within five seconds, the
  adapter MUST retry once through a fallback ladder: windowed with no driver pinning under
  x11/wayland, and the next `/dev/dri/card*` on a bare console. A clean exit, a long session, and a
  headless machine MUST NOT be retried.
- **D8** [TEST: T-DISPLAY-2] On a bare console the adapter MUST pin `SDL_VIDEO_KMSDRM_DEVICE` to a
  specific card rather than letting SDL choose among several GPUs.
- **D6** [TEST: T-AV-2] The frame-pacing strategy (vsync vs audio-driven timing) MUST be documented in
  `DECISIONS.md` with measurements.

---

## 9. Save/Data Contract

- **SV1** [REVIEW] Save data MUST be stored per entry under `SYS_SAVE_DIR`, identified by the entry `id`.
- **SV2** [TEST: T-SAVE-1, T-SAVE-2] A handler or its adapter MUST either write saves by atomic
  replacement with `fsync` of the file and its directory, or tolerate torn writes.
- **SV3** [REVIEW] The adapter MAY provide additional save integrity and recovery (for example a
  backup of the last known-good save). The mechanism is the adapter's responsibility and is not
  mandated. The adapter MUST NOT assume that a copy made after a clean exit is sufficient
  protection. The strategy MUST be selected from the evidence of T-SAVE-1.
- **SV4** [TEST: T-SAVE-2] A hard power loss during play MUST NOT destroy the entry's save without
  a recoverable prior state.
- **SV5** [TEST: T-SAVE-3] A clean exit MUST persist the save.
- **SV6** [TEST: T-STORE-5] The system MUST NOT overwrite or reformat DATA in response to detected
  corruption without explicit user confirmation.
- **SV7** [REVIEW] `sync` is a final safeguard and MUST NOT be relied on as the durability mechanism.
- **SV8** [TEST: T-INGAME-2] Save states MUST be native emulator states stored per entry, in nine
  fixed slots:
  - slot content is written atomically (temp file + `os.replace`) with size and SHA-256 recorded in
    the entry's metadata file; a slot MUST NOT be replaced by a partial or unverifiable state;
  - overwriting an occupied slot MUST keep the previous state recoverable until the new one has been
    verified, so a failed save never destroys a good slot;
  - loading MUST verify the state before use and MUST report a corrupt/missing slot instead of
    crashing the emulator;
  - slots MUST be isolated per game, and MUST persist across sessions and reboots;
  - the handler MAY keep one private slot for its own display handoff, which MUST NOT be visible as
    one of the user's nine slots.

---

## 10. Failure & Recovery Contract

**Principle: the system MUST remain usable when optional content, applications or handlers are
missing or broken.**

| Failure | Required result |
|---|---|
| No content | launcher works |
| GAMES absent | launcher works |
| Content removed while running | handler fails or continues; supervisor recovers; launcher works |
| Handler crash | launcher works, outcome shown |
| Handler hang | system button or supervisor escalation returns to launcher |
| Handler exits immediately | launcher works, outcome shown |
| Launcher crash | launcher restarted |
| Launcher crash loop, or broken launcher binary | recovery path |
| Supervisor dies | restarted; orphaned session terminated |
| DATA inconsistent | recovery path, no silent reformat |
| DATA nearly full | launcher works; logging bounded |
| Broken application | other applications still work |
| Broken system slot | previous slot boots (future: A/B) |

- **F1** [TEST: T-BOOT-3] The recovery path MUST be reachable without a working GPU userland. It
  MUST be a text interface on the kernel console.
- **F2** [TEST: T-STORE-5] Recovery MUST offer at least: continue anyway, export readable data,
  and reset, and MUST require explicit confirmation before any destructive action.
- **F3** [TEST: T-STORE-3, T-FAULT-1, T-FAULT-2, T-FAULT-3, T-FAULT-6] A failure of content, a
  handler, or a removable medium MUST NOT become a failure of the operating system.
- **F4** [TEST: T-FALLBACK-1] A missing runtime dependency (pygame, mGBA, python3) MUST be reported
  in words on the console and MUST lead to the recovery path, not to a crash loop or a black screen.
  The launcher signals this with exit code 103; the supervisor MUST NOT retry it.
- **F5** [TEST: T-FALLBACK-2] `gba_init.sh` MUST remain usable as PID 1 with no python3, no reboot
  binary and no supervisor: it MUST fall back to `/proc/sysrq-trigger` and to a rescue shell that
  never exits. `gba_mode=0` MUST disable the appliance and `gba_safe=1` MUST skip the graphical shell.

---

## 10b. Installation & Image Contract

The installer is the only part of the system that may write outside `/data` and
`/games`, so it carries its own rules. Each one has a test in
`tests/test_installer.py`, `tests/test_setup.py` or `tests/test_safety.py`.

- **IN1** [TEST: T-INSTALL-1] An installation MUST be planned, validated and confirmed before any
  write. `validate_plan` MUST refuse: the running root or live medium, a mounted target, a target
  partition containing an OS, a target below 8 GiB, a cross-disk EFI partition, an occupied mount
  point, a missing source, and removable media without explicit consent.
- **IN2** [TEST: T-INSTALL-2] Destructive whole-disk installation MUST require a typed phrase
  (`ERASE <disk>`) in addition to any `--yes`; a partition install MUST NOT require it, because it
  cannot destroy another operating system.
- **IN2b** [TEST: T-INSTALL-2b] An EFI system partition that already exists MUST NOT be formatted;
  an existing `EFI/BOOT` fallback loader MUST be copied aside before GRUB writes to it. Only an ESP
  created by the installer itself (whole-disk mode) may be formatted.
- **IN3** [TEST: T-INSTALL-3] The bootloader MUST be installed last, after the system has been
  copied and verified, and every exit path (including failure) MUST unmount the target. UEFI firmware
  MUST NOT be modified (`--no-nvram --removable`).
- **IN4** [TEST: T-INSTALL-4] The guided setup (`gba_setup.py`) MUST recommend only targets that keep
  the existing system bootable, MUST default to them, and MUST explain its exit state in words
  ("nothing was changed", "your boot menu was not modified", …).
- **IN5** [TEST: T-INSTALL-5] The on-system installer (`scripts/install.sh`) MUST be dry-run by
  default, MUST back up every file it modifies, MUST regenerate and then verify the boot menu,
  MUST restore the previous menu on any failure, and MUST NOT change the default boot target.
- **IN6** [TEST: T-INSTALL-6] No shell script may format, partition or write a block device. All disk
  writes MUST go through `src/gba_installer.py`, where IN1–IN3 are enforced.
- **IN7** [TEST: T-INSTALL-7] The ISO build MUST fail loudly, listing every missing prerequisite, and
  MUST write only inside its build directory. The produced image MUST boot on BIOS and UEFI and MUST
  offer an install entry, a safe-graphics entry, a shell entry and a "boot the internal disk" entry.
- **IN8** [TEST: T-INSTALL-8] `gba-selftest` MUST check interpreter, fonts, terminal, display
  session, emulator, module imports, storage and configuration, and MUST exit 0 only when no check
  fails (warnings are acceptable). It MUST NOT write outside its data directory.

---

## 11. Security & Isolation Contract

Phase 1 does not enforce isolation. It defines the interfaces so isolation can be added without
changing handlers.

- **SEC1** [REVIEW] Handlers cannot write to the content directory (H5, ST4).
- **SEC2** [REVIEW] Handlers receive controlled paths (H1) and MUST NOT assume other paths are
  writable.
- **SEC3** [DEFERRED: networking phase] Network is unavailable to a handler unless its manifest
  declares it.
- **SEC4** [REVIEW] Handlers MUST NOT start a foreground session of their own (H6).
- **SEC5** [REVIEW] Release images MUST NOT enable remote login. Development builds MAY.
- **SEC6** [REVIEW] Phase 1 MAY run all processes as root. This is a development simplification,
  not an application contract. Path provision (H1) and declared needs (L8) MUST work identically
  when privilege separation is introduced. Until then H5, SEC1 and SEC2 are obligations on handlers,
  verified by review of the bundled handlers.
- **SEC7** [DEFERRED: privilege-separation phase] Enforcement of write boundaries and network denial
  by the system, rather than by handler compliance.

---

## 12. Phase 1 Acceptance Tests

Phase 1 is complete when every test below passes on real hardware. Tests marked *spike* establish
facts that select an implementation. Their results are recorded in `DECISIONS.md`.

### Boot

| ID | Procedure | Pass criteria | Verifies |
|---|---|---|---|
| T-BOOT-1 | Boot on two real machines with different graphics vendors. | Launcher reached; no login; no visible console text. | B1, B2 |
| T-BOOT-2 | 20 consecutive cold boot / shutdown-or-reboot cycles. | No boot failure; no filesystem corruption. | B9 |
| T-BOOT-3 | Hold the recovery key; repeat with GPU userland deliberately unavailable. | Text recovery reached both times. | B8, F1 |
| T-BOOT-4 | Attempt to write to the system image in normal operation. | Write fails. | B3 |
| T-BOOT-5 | Boot from a blank disk, with no DATA and no GAMES. | Supervisor starts; DATA created; launcher shows empty state. | B7, S1 |

### Launch lifecycle

| ID | Procedure | Pass criteria | Verifies |
|---|---|---|---|
| T-LAUNCH-1 | 100 cycles of launcher -> handler -> exit -> launcher. | No hang; no leaked process; no unbounded fd or memory growth, including in the supervisor. | H2, H8, D4, S8 |
| T-LAUNCH-2 | Observe processes during transitions. | Never two foreground owners. | S2, S3 |

### Library

| ID | Procedure | Pass criteria | Verifies |
|---|---|---|---|
| T-LIBRARY-1 | Add game A without any manual step; record its entry ID; rename the file; rescan. | Entry appears automatically; ID and save directory unchanged after the rename. | L2, L3 |
| T-LIBRARY-3 | Plug in a stick with 10 ROMs, decline the prompt, re-plug, accept; import twice. | Nothing copied before confirmation; 10 files appear inside GAMES; the second import copies nothing; no partial files remain. | ST7 |
| T-LIBRARY-4 | Unlock an Android phone in File Transfer/MTP mode containing the same 10 ROMs plus two new ones; accept the prompt, then re-import. | The phone appears as one or more MTP storages, not a removable disk; only two new files are offered/copied; normal USB detection still works; no partial files remain. | ST8, ST8a |
| T-LIBRARY-5 | Plug in a locked phone; wait for the mount attempt; unlock it while the import screen is open; press `R`. Then repeat on a phone whose storage reports 0-byte sizes. | The first attempt reports "unlock the phone" instead of failing silently; the unlock produces the prompt in the same session; the 0-byte storage is still scanned and imported; `gba-selftest --mtp` names the failing stage. | ST8a |
| T-MANIFEST-1 | Create manifests omitting `network`, `input` and `display`; create one with `contract = 999`. | Omitted needs resolve to `false`, gamepad-only and direct; the unsupported manifest is rejected without crashing the launcher. | L7, L8 |

### Input

| ID | Procedure | Pass criteria | Verifies |
|---|---|---|---|
| T-INPUT-1 | One USB pad in launcher and in handler; repeat with keyboard only. | Same profile works in both; system fully operable by keyboard. | I2, I3 |
| T-INPUT-2 | Press the system button during a game, 20 times. | Returns to launcher every time. | S4 |
| T-INPUT-3 | Connect a pad after boot. | Usable without reboot. | I4 |

### Handler behavior

| ID | Procedure | Pass criteria | Verifies |
|---|---|---|---|
| T-HANDLER-1 | Use a diagnostic handler to produce each outcome class and to dump its environment. | Supervisor classifies each correctly, including the SIGTERM race; environment variables and directories match H1. | H1, H10, S6 |
| T-HANDLER-2 *(spike)* | Send SIGTERM to mGBA mid-game after an in-game save. | Documented: does it flush and exit cleanly, or die with `terminated`? If not clean, a remedy is chosen. | H7 |

### In-game overlays, save states, cheats, fast-forward

| ID | Procedure | Pass criteria | Verifies |
|---|---|---|---|
| T-INGAME-1 | Open the cheats and the save-state overlay 20 times each on the appliance; also run once with the display deliberately unavailable. | Overlay is drawn over a still frame every time; gameplay resumes at the same point; with no display the game keeps running and a failure is logged. | IG1, IG2, IG7 |
| T-INGAME-2 | Save into all nine slots, overwrite one, load each in turn, then exit and re-enter the game. | Nine slots, each with thumbnail and timestamp; load restores that exact state; slots are per game and survive a restart; a failed save leaves the previous slot intact. | IG3, SV8 |
| T-INGAME-3 | Add a multiline cheat using only the controller, toggle it, close/re-open, edit it, then delete it and confirm the prompt; also cycle speed with `L + R` and hold the combo. | Cheat changes are active after the handoff restart and persist after re-open; deletion never returns from a stale native file; one speed step per deliberate press, state preserved, never frozen. | IG4, IG5, IG6 |
| T-INGAME-4 | Open each overlay with no `/dev/uinput` access (chmod 000 as a test user). | Overlays still open and close; capture/load report a clear error; the emulator is never left stopped. | IG1, IG2, IG4 |
| T-INGAME-5 | Kill the overlay renderer (`SIGKILL`) while an overlay is open. | The handler resumes or exits, never leaving a stopped/orphaned mGBA process behind. | IG2, IG7, H8 |

### Keyboard defaults

| ID | Procedure | Pass criteria | Verifies |
|---|---|---|---|
| T-INPUT-4 | Fresh install: press `Z` then `X` in a game; then import a legacy profile and a customised one. | `Z` acts as `A`, `X` as `B`; the untouched legacy profile is migrated; a customised profile keeps every custom binding. | I6 |

### Saves

| ID | Procedure | Pass criteria | Verifies |
|---|---|---|---|
| T-SAVE-1 *(spike)* | Save in game; exit; inspect; cut power around a save; recover; repeat. | mGBA's actual write behavior is documented. | SV2 |
| T-SAVE-2 | Hard power cut during play, at least 20 repetitions (manual is acceptable). | A recoverable prior save exists every time. | SV2, SV4 |
| T-SAVE-3 | Clean exit after saving. | Save present on next boot. | SV5 |

### Storage

| ID | Procedure | Pass criteria | Verifies |
|---|---|---|---|
| T-STORE-1 | GAMES present, absent, reinserted; attempt a write to `/games/`. | System functional throughout; entries for missing content do not crash the launcher; write fails. | B6, ST4, L5, S1 |
| T-STORE-2 | Boot with no content. | Launcher shows empty state. | L6 |
| T-STORE-3 | Remove GAMES while a game runs. | Game fails or continues; supervisor recovers; launcher works. | S9, F3 |
| T-STORE-4 | Delete `/data/library/`. | Library rebuilt; no data loss. | ST2 |
| T-STORE-5 | Damage DATA's filesystem metadata and boot; then choose reset in recovery. | Recovery reached; no automatic format; reset requires explicit confirmation. | B7, F2, SV6 |

### Fault injection

| ID | Fault | Pass criteria | Verifies |
|---|---|---|---|
| T-FAULT-1 | Real mGBA, through its adapter, killed with SIGSEGV. | `crash` classified (not `error`); launcher works. | H9, S9, F3 |
| T-FAULT-2 | Handler exits 0 immediately. | Launcher works. | S9, F3 |
| T-FAULT-3 | Handler ignores SIGTERM. | `hung` classified; killed after `GRACE`; launcher works. | H7, F3 |
| T-FAULT-4 | Handler floods output. | Logs stay capped. | ST5 |
| T-FAULT-5 | DATA nearly full. | Launcher works; logging bounded. | ST6 |
| T-FAULT-6 | Launcher binary corrupted. | Recovery path reached. | S7, F3 |
| T-FAULT-7 | Launcher crashes repeatedly. | Recovery after N crashes in M seconds. | S7 |
| T-FAULT-8 | Kill the supervisor with SIGKILL during a game. | PID 1 restarts it; the orphaned game is terminated; launcher returns. | S8 |

### Installation, fallbacks and image

| ID | Procedure | Pass criteria | Verifies |
|---|---|---|---|
| T-INSTALL-1 | Run `validate_plan` for each refusal case in IN1 (unit tests use faked devices). | Every case refuses with a readable reason; a clean plan passes. | IN1 |
| T-INSTALL-2 | Run the guided setup, choose the whole disk, type the wrong phrase. | Refused, no command executed. With the exact phrase, partitioning starts. | IN2, IN4 |
| T-INSTALL-2b | Install into an empty partition on a disk that already has an ESP with a foreign loader. | The ESP is not formatted; the foreign loader is copied to `EFI/BOOT.gba-backup-*`. | IN2b |
| T-INSTALL-3 | Install to a partition, with `cp` and then `grub-install` forced to fail in turn. | No `grub-install` after a copy failure; `umount` always runs; exit codes 4 and 5 respectively. | IN3 |
| T-INSTALL-4 | Boot the ISO, read the first screen, press Enter twice. | Two questions, safe default recommended, explicit confirmation, readable success/failure. | IN4 |
| T-INSTALL-5 | `scripts/install.sh` with no arguments; then `--apply` on a scratch VM. | Dry-run changes nothing; apply backs up, verifies the entry, restores on failure. | IN5 |
| T-INSTALL-6 | Grep the shell scripts for disk-writing commands. | None found outside `gba_installer.py`. | IN6 |
| T-INSTALL-7 | `scripts/build_iso.sh --check` on a host without the tools; then a full build. | First run lists every missing tool and exits non-zero; second run produces a hybrid ISO. | IN7 |
| T-INSTALL-8 | `gba-selftest` on a machine with and without mGBA/pygame. | Exit 0 with warnings when only optional parts are missing; exit 1 and a named check when something required is missing. | IN8 |
| T-DISPLAY-1 | Force mGBA to exit 1 immediately: once under X11, once on a bare console with two DRM cards. | First retries windowed; second retries the other card; a clean exit and a 30 s session are not retried. | D7 |
| T-DISPLAY-2 | Boot the appliance on a hybrid-GPU laptop. | `SDL_VIDEO_KMSDRM_DEVICE` is set explicitly; no black screen from an output-less card. | D8 |
| T-FALLBACK-1 | Import the launcher with pygame unavailable. | Exit 103, message naming the fix, no traceback. | F4 |
| T-FALLBACK-2 | Boot with `gba_mode=0`, then with `gba_safe=1`, then without python3. | Shell, recovery console, and rescue shell respectively; never a black screen. | F5 |

### Audio, video, measurement

| ID | Procedure | Pass criteria | Verifies |
|---|---|---|---|
| T-AV-1 | 30 minutes of play. | No audible underruns. | D5 |
| T-AV-2 | Measure frame pacing under both strategies. | Strategy chosen and documented. | D6 |
| T-MEAS-1 | Measure image size, cold boot to first launcher frame, idle RAM, in-game RAM, and the size of the graphics stack. | Values recorded. No targets in Phase 1. | informational |

### Explicitly not Phase 1

Browser, Bluetooth, file manager, metadata/artwork, networking, update mechanism, A/B slots,
multiple emulated systems, application ecosystem, settings beyond the minimum, themes, cloud
features.
