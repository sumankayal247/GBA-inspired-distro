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
- **ST4** [TEST: T-STORE-1] GAMES MUST be mounted read-only. The system MUST NOT write to
  `/games/`, including beside a content file.
- **ST5** [TEST: T-FAULT-4] Logs MUST be size-capped and rotated. A runaway log MUST NOT be able
  to fill DATA.
- **ST6** [TEST: T-FAULT-5] DATA MUST reserve free space for system use so that logging and saving
  remain possible when users fill the volume.

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

---

## 8. Display & Audio Contract

- **D1** [REVIEW] The launcher MUST render to a fixed logical resolution and scale it to the display.
- **D2** [REVIEW] Scaling of emulated content SHOULD use integer factors with borders when the
  aspect ratio does not match.
- **D3** [REVIEW] The launcher and the bundled emulator MUST NOT require an X11 or Wayland session.
- **D4** [TEST: T-LAUNCH-1] The launcher MUST establish its own display state on start (see H2).
- **D5** [TEST: T-AV-1] Audio MUST NOT underrun audibly during normal play.
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
