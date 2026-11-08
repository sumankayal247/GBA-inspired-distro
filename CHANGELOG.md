# Changelog

All notable changes to GBA OS are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and versions align with the
contract version where applicable.

## [Unreleased] — Game Boy cheat validation and real mGBA application checks

Suite: **532 passing, 1 skipped** (`make test`).

### Fixed

- **False cheat success on Pokémon Yellow and other GB/GBC games.** The editor previously accepted
  any text even though mGBA uses a different GB parser; a GBA-style line can be truncated into an
  unintended GB write, while malformed text produces no operation at all. New GB entries now require
  GameShark, Game Genie, CodeBreaker or VBA syntax, with an inline error instead of false success.
- **Unsafe legacy invalid GB sets** remain visible for editing but are emitted `!disabled`, and the
  adapter logs the native file plus enabled/active/invalid counts. Spaced and lowercase eight-digit
  GameShark lines are normalized before writing, and VBA RAM writes become equivalent GameShark
  lines to avoid mGBA's VBA value-parsing bug. GBA multiline behavior is unchanged.
- **End-to-end verification:** the exact `CheatStore` file was parsed by a locally built mGBA core;
  `019947D3` produced the expected `D347 = 0x99` emulated-memory write, and an invalid trailing-data
  variant stayed disabled with memory unchanged. Documentation now distinguishes GB codes from GBA
  codes and notes that codes must match the ROM region/revision.

## [Unreleased] — Connected-only status icons, direct cheat editing and GBA boot animation

Suite: **526 passing, 1 skipped** (`make test`). This pass addresses four pieces of real-appliance UI
feedback without adding a dependency.

### Added

- **Original GBA OS boot animation** (`src/gba_boot.py`): a cartridge drops into an indigo handheld,
  the screen lights and the two-tone wordmark fades in. It is skippable after a short input guard,
  extends only while a first scan is active, has a six-second hard cap, and is bypassed when a game
  returns to the shelf.
- **In-overlay mGBA cheat editor**: the Cheats overlay always has **Add Cheat**, with name and
  multiline code fields, a controller/mouse on-screen keyboard and direct physical-keyboard input.
  `A` toggles/activates, `X` edits (or backspaces in the editor), `Y` switches fields or requests
  deletion, and deletion always asks first. New cheats start enabled; edits preserve enabled state.
- `CheatStore.update_cheat()` / `remove_cheat()` plus regression coverage for multiline native files
  and deletion of the final cheat without stale-file re-import.

### Fixed

- **Startup/rendering stalls from synchronous status probes** (`src/gba_status.py`). WiFi and
  Bluetooth subprocesses now run on one cached background monitor instead of inside every
  `draw_top_bar()` call. Network changes request a coalesced refresh.
- **Status-bar clutter and alignment**: the USB storage chip is gone; Bluetooth appears only for a
  connected device and WiFi only for a connected network. The WiFi glyph uses correct radians and a
  fixed bounding box, so it no longer spills into the clock. Settings remains available for pairing.
- **Selected-row arrowheads**: the shared shelf/list renderer uses its fill and outline alone, with
  no right-edge `>` marker.

### Tests

- `tests/test_status_boot.py`: slow-provider non-blocking behaviour, connected-only icon drawing,
  no synchronous probes/USB label in rendering, no shelf chevron, and bounded/skippable boot timing.
- Expanded cheat model, storage and pygame tests cover direct add, validation, edit, confirmed
  deletion, physical keyboard, gamepad and mouse input, and native mGBA persistence.

## [Unreleased] — In-shell self-update (SETTINGS → System → Check for updates)

The console can now pull its own updates from GitHub without a terminal, a login or a second
computer: an anonymous HTTPS check compares `origin/main` with the commit stamped in
`/opt/gba-distro/VERSION`, a modal lists the new commits, and confirming re-runs the shipped
`scripts/install.sh --apply --repair` from a shallow clone — the same code path as a manual
install, so saves, ROMs and settings stay untouched by contract. Suite: **509 passing, 1 skipped**.

### Added

- **`src/gba_updater.py`**: `network_ok()` reachability probe, `installed_version()` (VERSION stamp
  or local `.git`), `check_for_updates()` (`git ls-remote` + shallow clone for the commit list) and
  `apply_update()` (reset the clone to `origin/<branch>`, run its installer with `sudo -n` when
  unprivileged, `GIT_TERMINAL_PROMPT=0` everywhere so nothing can block on a credential prompt).
- **`UpdateOverlay`** in `src/gba_launcher.py`: a modal state machine (checking → prompt →
  applying → done / info / error) whose git and network work runs on a worker thread so the shell
  keeps drawing; on success it toasts and re-execs the launcher into the new version.
- **VERSION stamp**: `scripts/install.sh` now writes `$INSTALL_DIR/VERSION` (commit + timestamp)
  during `install_files`, and ships `gba_updater.py` with the rest of the modules.

### Tests

- `tests/test_updater.py` (14): reachability, version stamp, behind/up-to-date/unreachable checks
  and apply success/failure/missing-installer against throwaway `file://` git repositories.
- `tests/test_shell.py` `TestUpdateOverlay` (5): menu wiring, prompt → apply → restart, up-to-date
  toast, no-network error, and cancel-during-check never applying.

## [Unreleased] — Cartridge card and wordmark visual fixes

Three defects reported from a photo of the real shelf, all in the theme's cartridge painter and the
header wordmark. Suite: **490 passing, 1 skipped** (`make test`); `ui_preview_*.png` regenerated.

### Fixed

- **Square plastic corners on cartridge cards** (`src/theme.py`). The shell gradient was painted as
  plain square bands under a rounded 1px outline, so the corner pixels of the fill poked out of the
  curve. The gradient is now composited inside a rounded SRCALPHA silhouette
  (`BLEND_RGBA_MIN`), matching the outline, shadow and selection ring radius.
- **"GBA" seal text bleeding out of its circle.** The ring had a fixed radius smaller than the
  rendered word. The ring is now sized from the measured wordmark (new `seal` type size) and sat
  inside the sticker; the bottom row's height reservation follows the ring so titles keep their
  lines on the smallest grid tile.
- **Header wordmark overlap ("GBA OS").** The second word's x was hard-coded; `Theme.wordmark()`
  now places "OS" from the measured width of "GBA", at every scale.
- **Truncated titles dotted every line** ("Pocket.. / Rally.."). The ".." suffix belongs to the
  last shown line only; wrapping moved into a testable `_wrap_lines()`.

### Added

- Pixel-level regressions in `tests/test_theme_config.py` (`TestCartridgeVisuals`): corner cuts
  stay background/shadow, the seal bounding box stays square (text inside the ring) and on the
  sticker, wordmark rects never collide, and elision dots land on the last line only.

## [Unreleased] — Verbose boot diagnostics: evidence for a black screen

No behaviour change and no new dependency. Every stage of an emulator session — load, windowing,
running, closing — now writes its own evidence into `/data/logs/latest.log`, so "the game opened to a
black screen" can be answered from a log instead of guessed at. Suite: **478 passing, 1 skipped**
(`make test`).

### Added

- **Subsystem debug exports** (`src/gba_session.sh`). The appliance session now exports
  `SDL_LOG_PRIORITY=verbose` (plus the `_video`, `_audio` and `_render` categories),
  `LIBGL_DEBUG=verbose`, `MESA_DEBUG=1` and `GBA_DEBUG=1`, each written as `${VAR:-default}` so a
  single boot can raise or silence one without editing a file. It also announces a missing
  `xwininfo`/`xrandr` once, with the package names, instead of leaving a silent gap in the log.
- **mGBA's own log mask** (`src/gba_adapter_mgba.py`). `mgba_log_level()` passes `-l 31`
  (`FATAL|ERROR|WARN|INFO|DEBUG`, verified against `include/mgba/core/log.h`; mgba-sdl parses it as
  `--log-level` and applies it as a `logLevel` config *override*, so no `config.ini` can defeat it).
  mgba-sdl's default is `FATAL|ERROR|WARN`, which is why an emulator that never presented a frame
  printed nothing at all. `GBA_MGBA_LOG_LEVEL` overrides the mask (127 = `mLOG_ALL`) and an unusable
  value is clamped or replaced rather than reaching `getopt_long`, which would abort the whole
  command line. `build_mgba_command` still produces a byte-identical argv when no level is passed.
- **Window-mapping evidence** (`log_display_diagnostics`). About 1.5 s after *every* spawn — so also
  after each overlay handoff restart — the adapter samples `xrandr --current` and
  `xwininfo -root -tree`, then queries `xwininfo -id` for windows matching mGBA's title or SDL's
  WM_CLASS, and logs a verdict: `map-state=IsViewable … fills the active 1920x1080 mode`,
  `IsUnmapped` (created but never mapped), `lies entirely outside the active … mode` (off-screen),
  `overflows …` (clipped), `does not fill the display`, or `NO emulator window in the X11 tree`. A
  few pixels of window-manager frame are deliberately *not* reported as a fault
  (`DIAG_EDGE_TOLERANCE`), and a tool that exits non-zero is reported as `window tree unavailable`
  rather than as a missing window — the two are opposite diagnoses.
- **DRM connector evidence** (`_log_drm_connectors`). With no `DISPLAY` there is no window tree, so
  the same slot logs `/sys/class/drm` status/enabled/DPMS/preferred mode for the pinned card and
  warns when that card drives no connected output — the console equivalent of an unmapped window, and
  exactly what the D-047 card ladder exists to escape.
- **Load-stage evidence** (`log_load_diagnostics`). ROM path, size and parsed cartridge title; the
  expected `.sav`/`.srm` path and whether it exists yet; and the resolved BIOS situation (a
  configured-but-missing path warns, otherwise the built-in HLE BIOS is stated plainly). Recorded
  *before* the emulator starts, so a session that never gets that far still leaves its input behind.
- **Input evidence** (`InputMonitor`). The monitor logs the `/dev/input` devices it opened by name,
  the escape keys and combos it is watching, and — at debug level, so it reaches the file but not the
  console — every device it could not open, which is the usual reason an exit combo "does nothing".
  Exit gestures now name the actual trigger (`Exit key detected (evdev code 1)`,
  `Gamepad exit combo (button 10, held=[…])`) and confirm SIGTERM delivery.
- **Self-test coverage** (`src/gba_selftest.py`). A new `diagnostics` check reports the mask in
  force, the SDL/Mesa variables that arrived, and which of `xwininfo`/`xrandr` are missing, with the
  exact `apt install` line. Missing tools are a warning, never a failure.

### Changed

- **Closing is a trail, not a status code** (`MgbaSessionRunner.stop_emulator`). Each signal in the
  existing bounded ladder is logged as `Stop sequence: SIGTERM -> pid N (0.00s into the stop, grace
  2.0s)` with the wait outcome, escalations say `escalating`, and the end reports
  `reaped with status -15 after 0.02s`. A session that needed SIGKILL now says so.
- **The log pump reports both ends** (`_pump_stdout`). stderr was already merged into stdout; the
  pump now also records `output stream closed after N line(s)`, and a pump that dies early logs a
  warning instead of failing silently — which previously looked identical to an emulator that logged
  nothing.
- **`sync_directory` verifies out loud** (Rule SV2). It returns the number of files it fsynced and
  logs `Save fsync verified: N file(s) plus the directory …`; a per-file fsync failure is now
  attributed instead of swallowed. `main()` adds a `Teardown: exit status … after …s total` line.

### Trade-offs and escape hatch

`GBA_DEBUG=0` switches the capture off again, argv included — mGBA goes back to its default
`FATAL|ERROR|WARN` mask, and no `[load]`/`[diag]`/`[x11]`/`[drm]` lines are written. The
lifecycle lines (`Spawned mGBA`, `Stop sequence:`, `Save fsync verified`, `Teardown:`) stay on
purpose: one line each, and they are facts about the session rather than a capture. Diagnostic volume is
bounded three ways: tree/xrandr output is truncated (`DIAG_MAX_TREE_LINES`), every tool call is
time-bounded (`DIAG_TOOL_TIMEOUT`) on its own daemon thread so it can never delay a launch, and Rule
ST5 rotation still caps the log at 2 MB with five archives. `xwininfo`/`xrandr` stay optional and
read-only; nothing is installed, and no package was added to the ISO.

### Tests

- `tests/test_adapter_diagnostics.py` (43 tests): the mask and its overrides/clamping, argv with and
  without `-l`, and window verdicts driven by scripted `xwininfo`/`xrandr` on `PATH` — mapped,
  unmapped, off-screen, clipped, undersized, absent, renamed-but-SDL-classed, tools missing, a tool
  that hangs, and `GBA_DEBUG=0` producing no records at all. DRM evidence is tested against a
  temporary `/sys/class/drm`, including the pin filter and an unreadable sysfs. Also covered: the
  load-stage lines, fsync reporting, the merged stdout/stderr pump with a real subprocess, the full
  `SIGCONT → SIGTERM → SIGHUP → SIGKILL` trail with a child that ignores SIGTERM and SIGHUP (using
  the ready-file handshake from `tests/test_adapter_shutdown.py`), and the session wrapper's exports.

## [Unreleased] — Runtime bug-fix pass (seven reported defects)

Seven defects reported against the in-game and USB-import runtime, fixed in place. No module was
rewritten, no dependency was added, and the supervisor → launcher → handler architecture is
unchanged. Suite: **412 passing, 1 skipped** (`make test`).

### Fixed

- **Android MTP never detected** (`src/gba_usb.py`, `src/gba_launcher.py`, `src/gba_selftest.py`,
  `scripts/install.sh`). Five independent causes, each capable of hiding the phone by itself:
  `jmtpfs -l` prints its fields **quoted**, and the CSV parser dropped every such row; sysfs
  detection required interface protocol `1`, while several vendor stacks report `0` (they are still
  MTP, not PTP-only); a device known only from sysfs had no `mtp://` URI, so the GVfs fallback was
  never attempted even though it is what mounts the phone; MTP storages report `0`-byte file sizes
  until a file is read, so those ROMs were filtered out of the scan and aborted imports; and a phone
  that mounted while locked produced an empty storage that `VolumeWatcher` marked as *seen*, so
  unlocking it never re-prompted. Also: mount failures now say which stage failed and retry three
  times at 3 s, then every 15 s; PTP cameras are explicitly not claimed as phones; and the UI thread
  no longer runs `jmtpfs`/mount commands (it lists already-mounted volumes only).
- **In-game overlays did not open, and the game froze with no overlay** (`src/gba_ingame.py`,
  `src/gba_ingame_ui.py`, `src/gba_adapter_mgba.py`). The old design `SIGSTOP`ped mGBA and expected a
  second pygame process to take the display — but a stopped emulator keeps its SDL/KMS surface, so
  the overlay could not acquire it and the game stayed frozen behind it. Overlays now use a display
  handoff: capture a native state + frame while mGBA is running, stop it, draw with the existing
  `Theme`/pygame code, then start a new mGBA from the captured state (`-t`) at the current speed.
  If the capture fails the overlay does not open and the game keeps running; the handler's safety
  net resumes on any failure instead of leaving an emulator stopped.
- **Save states** (`src/gba_ingame.py`, `src/game_data.py`). Capture happened *after* the emulator was
  stopped, where native hotkeys are never read, so slots stored nothing. Capture is now part of the
  handoff while the game is live; the nine slots stay per game, are written atomically, verified by
  size + SHA-256, and an overwrite keeps the previous state until the new one is verified. The
  handler's private handoff state is stored outside the user's nine slots.
- **Cheats overlay** (`src/gba_ingame.py`). Same display cause; additionally a toggle now marks the
  session dirty, is persisted immediately, and reaches the running game through mGBA's own
  `-c` cheat file when the overlay closes (previously the game kept the cheat list it started with).
  The overlay only ever shows the current game's cheats.
- **Fast-forward** (`src/gba_ingame.py`, `src/gba_adapter_mgba.py`). `L + R` cycles
  `1x → 2x → 3x → 1x`; each step reloads the emulator from a freshly captured state (mgba-sdl has no
  runtime speed IPC) and passes `-C fpsTarget=60/120/180`. A reload that could not capture a state no
  longer runs, so the game cannot end up frozen; the combo is edge-triggered, so holding it is one
  step, not a loop.
- **Keyboard `A`/`B` reversed** (`src/common.py`). `DEFAULT_KEYBINDINGS` is now `keyA=122` (`Z`) and
  `keyB=120` (`X`), and the handler writes that pair into mGBA's `config.ini`. A stored profile that
  is *exactly* the old stock pair is migrated; any customised profile is left untouched.
- **No path sends a hotkey to a stopped emulator** (`src/gba_adapter_mgba.py`). State and screenshot
  capture refuse immediately when the emulator is stopped/handed off instead of burning the timeout.

### Added

- **MTP diagnostics.** `MtpManager.diagnose()` / `mtp_diagnostics()` and `gba-selftest --mtp` print
  sysfs interfaces, tools, mount root, GVfs, devices, import volumes, last error and one actionable
  hint. `VolumeWatcher` logs the whole chain once per distinct error.
- **`list_mounted_mtp_volumes()`** (exported through `src/gba_mtp.py`): lists already-mounted phone
  storages without running any command, for the import overlay and the launcher's UI thread.
- **`scripts/install.sh`** now distinguishes "no MTP tools", "no `/dev/fuse`" and "no `fusermount`",
  each with its own fix, and still only warns.
- **21 new tests**: `tests/test_mtp.py` grows from 15 to 27 (one per link of the MTP chain, the ISO
  package list, and the "no subprocess on the UI thread" rule); `tests/test_common.py` (5),
  `tests/test_adapter.py` (1) and `tests/test_ingame.py` (3) pin the `Z`→`A` / `X`→`B` defaults, the
  mGBA `config.ini` values, the migration rules and the fast-forward fallback when no overlay can
  take the display. The new `tests/test_ingame_handoff.py` (22) was added with the handoff itself.

### Changed

- `docs/CONTRACT.md`: new **ST8a** (MTP chain and diagnostics, 0-byte sizes, locked-phone retry),
  **I6** (shipped keyboard mapping and migration), **SV8** (nine save-state slots), section **5.6**
  (IG1–IG7: display handoff, no hotkeys to a stopped emulator, idempotent resume) and the matching
  acceptance tests **T-LIBRARY-5**, **T-INGAME-1…5**, **T-INPUT-4**.
- `docs/DECISIONS.md`: **D-052** (an overlay needs the display, so the emulator is stopped and
  restarted from a state — supersedes the `SIGSTOP` mechanism of D-050) and **D-053** (MTP detection
  is a chain, and every link says what it saw — amends D-051); D-050/D-051 carry amendment notes.
- `docs/SAFETY.md`: new §6.1 (stopping an emulator is not pausing it) plus fallback rows for a
  failed overlay, a dead renderer, missing `/dev/fuse` and unavailable input injection.
- `README.md`: the overlay section describes the handoff honestly, the controls table names the
  in-game `Z`/`X` mapping, the MTP paragraph names the diagnostics, and the troubleshooting table
  gains `gba-selftest --mtp` and "overlay does not open".
- `GAME_PLAN.md`: §19 records the V1 implementation status, the root cause of each of the seven
  defects, and what still needs physical hardware.

### Real-hardware-only verification

Unchanged and still open: real Android unlock/vendor MTP behaviour, FUSE read/unmount performance,
DRM/KMS release latency and audio across the overlay restart, `/dev/uinput` hotkey injection on the
appliance, 2x/3x frame/audio pacing, and overlay scaling on real panels. The headless suite simulates
states, processes, sysfs entries and mount tables; it does not claim any of the above.

---

## [Unreleased] — Android phone (MTP) import

An Android phone in **File Transfer (MTP)** mode is now a first-class ROM import source. It is
discovered as an MTP/FUSE device, never as a `/dev/sdX` removable disk, and then flows through the
same prompt, content-ID, verified-copy and library-registration pipeline as a pendrive.

### Added

- **MTP detection and storage enumeration** in `src/gba_usb.py`: `jmtpfs -l`, `mtp-detect`,
  `gio mount -l`, USB MTP interface descriptors under `/sys/bus/usb/devices`, and `mtp://`/GVfs FUSE
  entries in `/proc/mounts`. Devices are mounted with `jmtpfs -device=<bus>,<dev>` when no desktop
  session has already mounted them; unlocked internal storage and SD cards appear as separate
  `MtpVolume` import sources.
- **`.gba`/`.gbc`/`.gb` scanning** of accessible phone storages with Android media/app directories
  skipped. `MtpVolume` carries `transport="mtp"`, `removable=False`, a non-block device URI and its
  own `SourceIndex` namespace; it otherwise reuses `Volume.scan_roms`, `find_new_roms`,
  `import_roms`, `VolumeWatcher`, `Library.known_content_ids()` and `Library.register_content()`.
  ROMs already known by content ID are neither offered as new nor copied again, including when the
  same ROM was imported earlier from a pendrive under a different filename.
- **UI and diagnostics:** `USB / MTP IMPORT`, an `ANDROID MTP` source badge, dedicated phone prompt,
  unlock/file-transfer guidance, a non-blocking `R` rescan, an optional `gba-selftest` MTP check, and
  owner-only FUSE cleanup. Missing MTP tools produce a warning, never a boot failure.
- **`src/gba_mtp.py`**: a small MTP-focused API facade over the shared `gba_usb` implementation.
- **15 hardware-free tests** (`tests/test_mtp.py`) for libmtp/sysfs/GVfs parsers, FUSE mount tables,
  auto-mounting by USB address, generic `/dev/fuse` mount lines, missing FUSE mtimes, nested ROM
  scans, source-index namespacing, content-ID deduplication, mixed USB+MTP watcher queues, and
  idempotent imports. Suite: 370 tests.

### Changed

- `scripts/build_iso.sh` ships `jmtpfs` and `mtp-tools`; `scripts/install.sh`, the Makefile and
  `gba-selftest` install/lint/import the MTP facade, and the installer prints an optional dependency
  warning. `docs/CONTRACT.md` adds **ST8/T-LIBRARY-4**, `docs/DECISIONS.md` adds **D-051**, and
  README/SAFETY document the phone workflow and fallback.
- `parse_mounts()`/`list_mounted_volumes()` ignore `/dev/fuse` so a phone cannot enter the
  block-device path; their `/proc/mounts`/`/sys/block` behavior for real USB storage is unchanged.
  `import_roms()` now falls
  back from `copy2` to `copyfile` when a FUSE backend rejects metadata copying, while keeping the
  size-verification and partial-file cleanup rules.

### Real-hardware-only verification

Physical Android unlock/MTP negotiation, vendor quirks, large FUSE reads, SD-card layouts and
`fusermount` behavior need appliance verification. The headless suite simulates sysfs, libmtp
commands, GVfs paths and FUSE roots; it never claims a real device.

---

## [Unreleased] — in-game cheats, nine save states, fast-forward, favorites

Everything below is additive to the existing supervisor → launcher → mgba-adapter architecture.
The USB import path, the display/fullscreen fix, the existing `.sav/.srm` backup and the five-tab
shell are untouched; the overlays are rendered inside the handler with the existing pygame/Theme
toolkit, not a new GUI framework or a competing save system.

### Added

- **Three configurable in-game actions** (`Up+Left` cheats, `L+R` fast-forward, `Left+Right` save
  states) in `DATA/config/settings.json`, editable at **SETTINGS → Controls → In-game shortcuts**.
  `src/gba_ingame.py` has an edge-triggered `ComboRouter`: one action per deliberate two-button
  press, never on key repeat, with consumed buttons released so they cannot also reach gameplay.
- **Per-game Cheats overlay**: `CheatStore` in `src/game_data.py` keys `cheats.json` and an mGBA
  native `.cheats` file by the library's stable content id; mGBA loads it with `-c`, and
  `cheatAutoload=0` prevents a global list from appearing. Enabled/disabled state persists per game.
- **Fast-forward cycle** `1x → 2x → 3x → Normal` using mGBA's real `fpsTarget` (60/120/180), a
  temporary badge-only overlay, and a state-preserving in-session reload (mgba-sdl has no runtime
  speed IPC). No artificial frame skipping or manual timing.
- **Nine save-state slots**: real `.ss` files produced by mGBA's Shift+F1–F9 / F1–F9 hotkeys and
  separate F12 PNG thumbnails, archived under `DATA/saves/<content-id>/states/slot_N/` with
  `slots.json` metadata (timestamp, size, SHA-256). Occupied slots offer `Load Save` and a
  confirmation-gated `Overwrite Save`. Missing/corrupt/partial files are detected and fail
  gracefully; a failed metadata write rolls back to the previous good save.
- **Favorites** (`Ctrl+F` / pad `X`, `DATA/config/favorites.json`): stable favorites-first
  partition by content id, preserving relative order in both groups.
- **SETTINGS → Game Management**: multi-select `Delete Selected Games` with confirmation and
  `Delete All Imported Games` with confirmation plus a two-second hold. Deletion removes the ROM,
  its per-game cheats/states/runtime data, its favorite flag and its library-cache entry.
- `gba-selftest` now checks the three new modules and warns when neither `/dev/uinput` nor
  `xdotool` can deliver mGBA's native hotkeys.

### Changed

- `scripts/install.sh` and `Makefile` install/lint the three new handler modules.
- Overlay scaling hardened: launcher popups centre against the real `design_h`
  (not a literal 320, which drifts above 1920px where `ui_scale_for()` clamps),
  the in-game fullscreen overlay resolves SDL's actual desktop mode instead of
  its 960x640 fallback, and long cheat names / timestamps / dialog lines are
  clipped to their boxes (fonts round up to whole pixels on small panels).
  A headless suite renders every popup at 320x240 through 3840x2160.
- 94 focused tests added for cheat isolation/persistence, the exact speed cycle and held-combo
  behaviour, nine-slot save/load/overwrite/persistence/corruption, shortcut configuration, native
  mGBA command flags and hotkeys, headless overlay rendering, favorites, and deletion safety.

### Real-hardware-only verification

Native hotkey delivery (`/dev/uinput` or X11 `xdotool`), the exact audio pacing of `fpsTarget`,
sharing a KMS/DRM display between mGBA and the overlay, and the state-preserving speed reload are
not observable in the headless suite. Cheat toggles are persisted immediately but mGBA applies them
on the next launch, since closing an overlay must never restart the game.

---

## [Unreleased] — import identity and display geometry fixes

Two runtime bugs fixed with the smallest changes that solve them; no subsystem was redesigned
(supervisor, handlers, saves, library identity, installer, browser, terminal, settings, the five-tab
UI and the USB import flow all keep their architecture — see D-048 and D-049).

### Fixed

- **"1130 games detected" re-appeared after every game and every reboot** — the USB prompt was
  gated on discovery alone (`rom_count() > 0`) plus an in-memory `seen` set, and the supervisor
  restarts the launcher process after every game session, so a still-plugged stick was "new" again
  every time. Nothing ever compared stick content against imported content. Now the watcher
  measures *new content*: every ROM's content hash (Rule L2 — the library's existing identity, no
  second system) is checked against `Library.known_content_ids()`, which is backed by the on-disk
  library cache and therefore survives game returns, launcher restarts, reboots and rescans. The
  prompt reads "N new Game Boy ROMs" and stays silent at N=0; a renamed copy of an imported ROM is
  recognised as already imported. No "prompt was shown" flag exists — the durable state *is* the
  library's content identity. Re-imports are idempotent (content skip, then the existing
  name+size skip), and repeated scans never duplicate entries.
  - `src/gba_usb.py`: `SourceIndex` (persistent `DATA/library/usb_sources.json` cache keyed by
    relative path + size + mtime, so a stick is fully hashed at most once per machine),
    `find_new_roms()`, `Volume.new_roms`/`new_rom_count()`, `VolumeWatcher(known_ids_provider=…)`
    with all walking/hashing moved off the lock and off the UI thread, `refresh()` now re-arms
    detection instead of hashing synchronously, `import_roms(known_ids=…, index=…)` skips
    registered content and reports `result.registered`.
  - `src/library.py`: `known_content_ids()`, `register_content()` (imported files are registered
    durably the moment the copy lands), a cache lock, and mid-scan cache flushes every 16 freshly
    hashed files — the scanner is a daemon thread that dies when the launcher exits into a game,
    and without the flush that session's identity work was lost.
  - `src/gba_launcher.py`: watcher wired to the library identity at both construction sites,
    `_poll_usb` prompts only for volumes with new content, the prompt overlay counts only new ROMs
    (with a discovery fallback), and the import worker registers copied content before rescanning.
- **GBA games rendered as a tiny 240×160 rectangle centred on the screen (GBC 160×144)** — mGBA's
  `-f` maps to `SDL_WINDOW_FULLSCREEN_DESKTOP`, but SDL2's X11 backend implements that *purely* as a
  `_NET_WM_STATE_FULLSCREEN` window-manager request, and the appliance session (`xinit … -- :0 vt1`)
  runs without a WM: the request is silently dropped and the window keeps its birth size — mGBA's
  default viewport, native resolution ×1. mGBA's one-shot post-start `SDL_SetWindowSize` is a no-op
  under the FULLSCREEN flag (verified in SDL2 `SDL_video.c`), so nothing ever corrected it. The
  adapter now detects the physical mode read-only via `/sys/class/drm` (never acquiring the
  display — D4/D-031 hold) and passes `-C width=… -C height=…`, so the window is born screen-sized
  with or without a WM; mGBA's own `lockAspectRatio=1`/`lockIntegerScaling` viewport then centres
  the largest correctly-proportioned image (GBA 3:2, GBC 10:9 — integer 4× = 960×640 or fit
  1152×768 on a 1366×768 panel; no crop, no stretch). When no mode can be read the fallback is
  `-4` (4× native) instead of the useless 1× window. Verified against mgba-sdl 0.9.3/0.10.1/0.10.3
  and master (identical GL viewport math; `-C` overrides win over `config.ini`).
- **GBC sessions stuttering** — mgba-sdl enables rewind recording by default: a full core snapshot
  every emulated frame into a 600-second buffer, serialized on the emulation thread. The appliance
  exposes no rewind control, so this was pure per-frame overhead on modest hardware. The adapter
  now passes `-C rewindEnable=0` and writes `rewindEnable=0` into the managed `config.ini`
  (`[ports.sdl]`, whose lookup precedence was verified in mGBA's `config.c`). Pacing
  (`audioSync`/`videoSync`/`fpsTarget`) is untouched; the adapter additionally logs the detected
  screen mode, session and driver per launch so any residual machine-specific lag is diagnosable
  from `/data/logs`.

### Added

- `config.display_mode_size()` — read-only display-mode detection: connected DRM connectors,
  internal panel (eDP/LVDS/DSI/DPI) preferred, `SDL_VIDEO_KMSDRM_DEVICE` pin honoured, `xrandr`
  fallback under X11, `None` when genuinely unknown.

### Tests

- 32 new tests (261 total, all green): the full import lifecycle (first-boot prompt → import →
  silence across simulated restarts/reboots → rescan idempotence → one added ROM prompts "1 new" →
  re-import copies only the new file, no duplicates), renamed-ROM recognition, `SourceIndex`
  persistence and re-hash avoidance, incremental library cache flushes, `register_content`
  durability, display-mode parsing (panel preference, disconnected/garbage inputs, card pinning),
  the new mGBA command geometry, `rewindEnable=0`, and shell-level prompt gating/wiring.


## [Unreleased] — bootable ISO, guided setup, and a hard safety review

Everything in this section exists because of one question: *can booting this on a new PC brick it?*
The answer is documented in [docs/SAFETY.md](docs/SAFETY.md), enforced in code, and covered by tests.

### Added

- **Bootable installation ISO** — `scripts/build_iso.sh` (`make iso`) builds a hybrid BIOS+UEFI
  image with `debootstrap`, `mksquashfs` and `grub-mkrescue`. The medium boots to the guided setup
  on `tty1`, and its menu offers *Install*, *Install (safe graphics)*, *Shell on this medium* and
  *Boot from the internal disk instead*. `make iso-check` lists missing prerequisites; the builder
  refuses to start without them instead of producing a broken image.
- **Guided setup (`src/gba_setup.py`, `gba-setup`)** — two questions, safe default, recommended
  target marked in the table, plain-text so it works with no pygame. Success and every failure mode
  are described in words ("your boot menu was not modified, so this computer still starts its usual
  operating system").
- **Disk installer (`src/gba_installer.py`)** — plan → validate → confirm → apply. Nine refusal
  rules (running disk, live medium, mounted target, partition containing an OS, <8 GiB, removable
  media, cross-disk EFI, occupied mount point, missing source), typed phrases
  `ERASE /dev/sdX` / `FORMAT /dev/sdX1`, `--dry-run`, `--plan`, `--json`, exit codes 0/2/3/4/5, and a
  bootloader step that runs last, uses `--no-nvram --removable`, and never edits UEFI NVRAM.
- **Self-test (`src/gba_selftest.py`, `gba-selftest`)** — interpreter, fonts, pty, display session,
  mGBA, every module import, a real pygame blit on the dummy driver, storage space/writability and a
  config round-trip. One line per check with a fix hint, `--json`, exit 1 only when something failed.
- **`scripts/lib/common.sh`** — shared shell safety library: dry-run gating, confirmation helpers
  (typed confirmation for irreversible actions), file backup with an `EXIT`-trap rollback, and live/
  root device detection.
- **`--repair`** for `scripts/install.sh`, and a `gba-setup` wrapper installed with the appliance.

### Changed

- `scripts/install.sh` is a full rewrite: dry-run by default, platform and init detection,
  dependency preflight with an explicit override, backups of every file it modifies, `sh -n`
  validation of the generated GRUB script, regenerate-then-verify of `grub.cfg` with byte-for-byte
  restore on failure, and an uninstall that restores `/etc/inittab`.
- Boot menu entries are now `GBA OS (console appliance)` and
  `GBA OS (safe graphics / recovery)`; `GRUB_DEFAULT` is never modified.

### Fixed / hardened

- **Missing pygame** no longer produces a traceback: the launcher prints what to install and exits
  `103`; the supervisor treats that as fatal and goes to recovery instead of counting crashes.
- **Missing mGBA** now says so on the console (with the `apt install mgba-sdl` line) as well as in
  the log; saves are untouched.
- **Black screen on game start** — the fallback ladder is now explicit and testable: a session that
  dies non-zero within five seconds is retried once (windowed, unpinned, under x11/wayland; the next
  DRM card on a bare console), and `SDL_VIDEO_KMSDRM_DEVICE` is pinned on console boots so a hybrid
  GPU's output-less card cannot swallow the picture.
- **TERMINAL tab** shows "starting shell…" until the shell prints its first byte, so opening it
  never looks like an empty black rectangle.
- **`gba_init.sh`** honours `gba_mode=0` and `gba_safe=1`, detects a missing python3, reboots via
  `/proc/sysrq-trigger` when the binary is gone, and falls back to a rescue shell that never exits —
  a black screen with no keyboard response is no longer a reachable state.

### Tests

- 229 tests (was 133). New: `tests/test_installer.py` (35), `tests/test_setup.py` (21),
  `tests/test_safety.py` (30). Documented limitation: the sandbox has no `xorriso`, no loop devices
  and no root, so the ISO build is verified by `--check`, syntax checks and by reading the generated
  configuration — not by booting real hardware.

## [Unreleased] — second feature pass (full-screen shell, tabs, terminal, browser, USB)

A rework of the launcher around the feedback that the shell felt small, the colours were not
Game-Boy-enough, and everything except the game library needed redoing.

### Added

- **TERMINAL tab** — a real `$SHELL -i` on a pseudo-terminal, rendered by a small ANSI screen
  emulator (16/256-colour SGR, cursor movement, erase, 500 rows of scrollback) with full key
  translation. `Ctrl+Alt+T` toggles it from any tab or dialog.
- **BROWSER tab** — a deliberately light web reader: `urllib` fetch with a 2 MB document / 1 MB image
  cap and an 8 s timeout, HTML → styled text layout, images, link cycling, history, and an inline URL
  bar (`Ctrl+L`). `about:welcome` is the start page.
- **Pendrive import** — `/proc/mounts` + `/sys/block` discovery (no `udisks`/`udev` dependency),
  recursive ROM scan, and a progress dialog. Import only starts after an explicit confirmation;
  identical files are skipped, clashing names get a `(n)` suffix, and every copy is size-verified.
- **APPS tab** with six cartridge entries: System Monitor (live memory sparkline, storage, load,
  uptime, driver), Input Diagnostics (visual pad, axes, active control map), Save Manager, USB Import,
  Web Browser and Terminal.
- **Settings rework** — six declarative pages (Display, Audio, Controls, Library, Storage & Saves,
  System) plus Reboot and Power Off. Every row is a real control: choose scaling, theme, clock,
  brightness, volume/mute, remap all ten buttons, reset bindings, switch grid/list, toggle USB
  auto-detect and sub-folders, rescan, export saves, clean the library cache, read logs, view About.
- **Theme system** — four palettes (indigo, violet, amber, mint) over shared GBA neutrals, a TTF type
  ramp, and a proper cartridge renderer (notched shell, gradient plastic, grip ribs, magenta
  "GAME BOY GBA" sticker, id chip, GBA seal, selection ring, `NEW` badge).
- **Full-resolution display** — the shell composes at the window's native size and lays out in
  design units, so it fills a laptop panel instead of showing a 480x320 window. `integer` mode
  remains available as an opt-in 480x320 retro canvas.
- **Global shortcuts** — `Alt+Tab` / `Alt+Shift+Tab` cycle main tabs, `Ctrl+Alt+T` opens the terminal,
  `Ctrl+V` toggles cartridge/list view, `F1` help, `F12` screenshot to `DATA/screenshots/`.
- **Launch screen information** — save status, size, play count, total time and the control map before
  a session starts.

### Changed

- Launcher rewritten as a single event loop with an overlay stack, so modals and global shortcuts
  coexist instead of competing.
- Settings persist in `DATA/config/settings.json` (atomic writes, sanitised on load, legacy
  `display.json` migrated on first read). `common.load_display_config()` now delegates to it.
- The library tab is unchanged in behaviour (search, recency focus, atomic launch requests) but is
  rendered from the new theme at native resolution.

### Fixed

- **Black screen when starting a game** — the handler now resolves the display session itself
  (X11/Wayland/KMS-DRM/headless), drops a stale `DISPLAY`, refuses to inherit a DRM driver inside a
  live desktop, passes explicit `-C fullscreen=…` alongside `-f`, and retries once in a window if
  mGBA dies immediately. The shell also tears down pygame before exiting with code `10`, so the
  adapter never inherits a half-released display context.
- mGBA now reads the config the adapter actually writes: `XDG_CONFIG_HOME` is pointed at
  `SYS_CONFIG_DIR` (`…/mgba/config.ini`), with a copy kept at the historically documented path.
- Browser `<title>` was dropped together with `<head>`, so pages were titled by their URL.
- Inline `<a>`/`<b>`/`<span>` runs no longer break paragraphs into separate lines.

### Contract

- **D1 amended** — native-resolution rendering is now required; a fixed logical canvas is optional.
- **D4 extended** — the launcher must release the display, and handlers must not inherit an
  unusable display environment.
- **ST4 amended / ST7 added** — ROM import is the single sanctioned write path into GAMES, and it
  must be confirmation-gated, non-overwriting and size-verified.
- New decisions D-029 … D-036 cover scaling, theming, display handoff, the import rule, the PTY
  terminal, the browser budget, the settings store and the five-tab navigation model.

### Tests

- Test suite grown from 60 to 133: new modules for the shell (tabs, shortcuts, overlays, launch
  protocol), settings/theme/display environment, terminal emulation and PTY round-trip, browser
  extraction/layout/history, and pendrive scan/import/watcher behaviour.

---

## [Unreleased] — hardening & UX pass

A broad quality pass over the shipped V1 tree: every subsystem was reviewed
against `docs/CONTRACT.md`, latent bugs were fixed, and the shell gained the
features a console appliance actually needs day to day.

### Added

- **Library search** — start typing on the GAMES tab to filter incrementally;
  `Backspace` edits, `Esc` clears. The active query is shown in the top bar and
  the bottom bar switches to search-specific hints.
- **Play history** (`DATA/config/history.json`) — the shell boots focused on the
  last-played game, marks recent entries, and shows play count and total time on
  the detail card and launch screen.
- **Save Manager app** — inspect per-game save size/age and delete a save only
  after an explicit confirmation dialog (contract SV6 / F2).
- **Rescan Library setting** — re-index `/games` without rebooting.
- **Persistent display scaling** — `integer` (crisp, default) or `stretch`,
  stored in `DATA/config/display.json` and forwarded to mGBA as
  `SYS_DISPLAY_SCALING` so shell and emulator agree.
- **Pre-flight launch screen** — save status, play history and the full control
  reference before a session starts (replaces the 3.5 s auto-launch timer, which
  could start a game the user was still reading about).
- **Last-known-good save backups** — the adapter mirrors `.sav` files to `.bak`
  before play and restores them if a save is missing or zero-length afterwards.
- **Orphan sweep on supervisor start** — leftover `mgba` / `gba-launcher`
  processes from a previous boot are terminated (contract S8).
- **Log rotation** — session logs rotate at 2 MB with 5 archives, and old boot
  logs are pruned (contract ST5).
- **Recovery console upgrades** — system information, newest-log discovery via
  `latest.log`, save export, and explicit confirmation for reboot/poweroff.
- **`--demo` mode and `make screenshots`** — deterministic documentation
  previews generated by the real renderer.
- **`requirements.txt`, `make deps`, `make lint`, `make help`**, plus a test
  suite grown from 10 to 60 cases.

### Fixed

- **Supervisor signal handling was unreachable and deadlock-prone.** A duplicate
  `except KeyboardInterrupt:` swallowed the termination path, and the handler
  re-entered `Popen.wait()` on the child the main thread was already waiting on.
  Shutdown now sends SIGTERM and escalates to SIGKILL from a helper thread.
- **`Ctrl-C` on the console killed the launcher instead of the supervisor.**
  Children are now started in their own session; the supervisor shuts them down.
- **Boot mode detection.** `--appliance` relied on operator precedence that made
  an explicit flag lose to `DISPLAY`; `--desktop` was not recognised at all.
  Mode parsing is now explicit and unit-tested.
- **Orphan cleanup matched command-line substrings** and could kill unrelated
  shells or editors. Matching is now by executable/script basename of the
  process's own `argv`.
- **Gamepad hotkey false positives.** Masking joystick event type with `0x01`
  matched axis events, so ordinary movement could quit a game; "connections"
  (button 8/9 alone) also triggered the combo. Only SELECT+START and
  Guide/Home now exit, and the escape key comes from the shared profile.
- **Remapping could brick the shell.** Assigning `Esc`, arrows or a duplicate
  key left the appliance without usable navigation. Assignments are validated
  with an inline error and sanitized on load.
- **Game Boy / GBC titles** were read from the GBA header offset, producing
  garbage; `0x134` is now used for `.gb`/`.gbc`, filler header bytes are
  rejected, and dump decorations (`(USA) (Rev 1) [b]`) are stripped.
- **Library scan blocked first paint** — hashing now happens on a worker thread
  (contract L4) with a "scanning" indicator and progress banner.
- **Unstable library ordering** — entries are sorted case-insensitively so the
  shelf looks identical every boot.
- **Zero-byte and hidden files** no longer appear as broken entries.
- **Display scaling setting did nothing** — it now persists and reaches mGBA.
- **The keybinding writer hard-coded `/home/suman`** and ignored `SUDO_USER`.
- **Installer hard-coded the root user, a kernel version and a home directory**,
  and skipped dependency checks; it also wrote GRUB entries that could leave a
  machine unbootable if `/boot` changed. It now detects the running system,
  verifies packages, backs up `inittab`, and supports `--uninstall`.
- **Recovery console read a non-existent `supervisor.log`** and offered no
  confirmation before destructive actions.
- **UI hit boxes** were hard-coded pixel guesses (tabs, rows, buttons) that
  drifted from what was drawn; they are now derived from the same layout
  constants, so mouse and pad input land where the user expects.
- **Modal layouts**: text overflowing the save panel, overlapping mapping
  columns in Input Diagnostics, and a fake "full battery" on desktops without a
  battery (now an AC indicator, with a real gauge on laptops).
- **Fonts** now load a real TTF (bundled → system → pygame built-in) so
  typography is crisp and consistent across machines.

### Changed

- Typography and spacing refreshed on all three tabs; the list shows item
  counts, a scroll indicator and per-entry badges.
- The bottom bar shows outcome context ("Session recovered — save data was
  synced") and transient toasts instead of silently changing state.
- Analog sticks navigate with an initial delay and repeat; pads are re-detected
  on hot-plug/USB changes.
- `sys.exit(0)` during input handling was replaced by a clean shutdown flag so
  the shell never skips cleanup.
- `gba_init.sh` is POSIX `sh` (no bashisms), locates the install without a hard
  coded home path, and re-enters recovery when the supervisor dies.

### Verified

- 60 automated tests (`make test`), all headless, covering contracts L2–L6,
  H1, H10, S6–S8, SV2–SV4, ST2, ST5, I3, D1–D2 and F2.
- Manual end-to-end runs of the adapter (fake emulator) and the supervisor
  (windowed mode, SIGTERM shutdown) confirm process cleanup and save backups.
