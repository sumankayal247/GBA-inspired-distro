# Changelog

All notable changes to GBA OS are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and versions align with the
contract version where applicable.

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
- **`gba_init.sh`** honours `gba_mode=0` and `gba_safe=1`, detects a missing python3, reboots via
  `/proc/sysrq-trigger` when the binary is gone, and falls back to a rescue shell that never exits —
  a black screen with no keyboard response is no longer a reachable state.

### Tests

- 221 tests (was 133). New: `tests/test_installer.py` (35), `tests/test_setup.py` (21),
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
