# GBA OS — A Game Boy Advance Inspired Distro (V1)

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Contract](https://img.shields.io/badge/Contract-Draft%202%20(V1)-purple.svg)](docs/CONTRACT.md)
[![Tests](https://img.shields.io/badge/Tests-526%20passing-brightgreen.svg)](tests)
[![Safe by default](https://img.shields.io/badge/install-safe%20by%20default-blue.svg)](docs/SAFETY.md)

**GBA OS** is an appliance-style Linux operating system experience designed to turn any PC or laptop into a dedicated Game Boy Advance console.

It completely dispenses with conventional desktop environments (GNOME, KDE, XFCE). When the machine powers on, it boots straight into an authentic retro console game library.

![GBA OS Games Library](ui_preview_games.png)

---

## 🎮 Vision & Experience

```text
Power On -> GRUB Menu -> GBA OS -> Game Library -> Play Game -> Return to Library
```

- **Zero Desktop Bloat:** Direct KMS/DRM display output. No window manager, panels, or login screens.
- **Original GBA-style startup:** a skippable cartridge-and-handheld boot animation covers first-scan
  work without holding the UI hostage; returning from a game goes straight back to the shelf.
- **Controller-First:** Full gamepad support (including hot-plug) with a keyboard fallback.
- **Managed Saves:** Saves and states are stored per game under `/data/saves/<entry-id>/`, with a
  last-known-good backup that survives a hard power cut.
- **Persistent GRUB Safety:** Rebooting always returns to the GRUB menu so you can easily switch back to your primary desktop OS.
- **Ultra-Lightweight:** Runs well under 500 MB RAM idle, and under 1 GB RAM during emulation.
- **Recoverable:** A text-only recovery console is always reachable when the graphics stack is broken.
- **Full-screen by design:** the shell renders at your display's native resolution and fills the
  whole panel — a 1366x768 laptop and a 4K monitor both look deliberate, with no 480x320 window in
  the middle of the screen.
- **A cartridge shelf for everything:** games, built-in apps and settings pages are all drawn as
  Game Boy Advance cartridges (notched shell, grip ribs, magenta sticker, GBA seal).
- **Five tabs:** GAMES · APPS · BROWSER · TERMINAL · SETTINGS.
- **Terminal and browser built in:** a real PTY shell and a deliberately minimal web reader. Useful
  for fixing a machine with no desktop, and both are stdlib-only.
- **Pendrive and Android MTP import:** plug in a stick, or unlock a phone in File Transfer mode,
  get a prompt, and import its ROMs on request — never silently.

---

## 📚 Documentation

- [docs/SAFETY.md](docs/SAFETY.md) — what can and cannot change on your machine.
- [docs/CONTRACT.md](docs/CONTRACT.md) — the boot/storage/handler/failure contract (incl. the
  installation contract, IN1–IN8).
- [docs/DECISIONS.md](docs/DECISIONS.md) — why each rule exists.

## 📸 Screenshots

| Cartridge shelf (GAMES) | Built-in apps (APPS) | Settings |
| :---: | :---: | :---: |
| ![Games](ui_preview_games.png) | ![Apps](ui_preview_apps.png) | ![Settings](ui_preview_settings.png) |

| Lightweight browser (BROWSER) | Terminal (TERMINAL) |
| :---: | :---: |
| ![Browser](ui_preview_browser.png) | Open **Ctrl+Alt+T** — a real `$SHELL` on a PTY |

---

## ⚙️ Architecture

Built strictly against the specifications in [`docs/CONTRACT.md`](docs/CONTRACT.md) and [`docs/DECISIONS.md`](docs/DECISIONS.md):

```text
PID 1 (init / gba-init)
  └── gba-supervisor      (Headless session manager, outcome classifier, crash-loop guard)
        ├── gba-launcher  (Cartridge-shell desktop; exits on selection to release the display)
        └── mgba-adapter  (Isolated handler running mGBA with dedicated save routing)
```

The shell itself is deliberately small and single-purpose per module:

| Module | Responsibility |
|---|---|
| `src/gba_launcher.py` | window, tabs, cartridge shelf, modals, input routing, launch protocol |
| `src/theme.py` | palettes, TTF font ramp, design-unit scaling, the cartridge renderer |
| `src/config.py` | `settings.json` store, SDL/audio/backlight helpers, display-session resolution |
| `src/gba_terminal.py` | PTY session, ANSI screen emulator, key translation, terminal view |
| `src/gba_browser.py` | HTTP fetch, HTML extraction, text layout, history, URL bar |
| `src/gba_usb.py` | `/proc/mounts` block discovery, Android MTP/FUSE sources, ROM scan, content-ID cache, verified import, background watcher |
| `src/gba_mtp.py` | small MTP API facade over the shared `gba_usb` implementation |
| `src/game_data.py` | per-content-id cheats, nine save-state slots, favorites, game deletion |
| `src/gba_ingame.py` | shortcut router, fast-forward cycle, overlay models, session state (no GUI dependency) |
| `src/gba_updater.py` | anonymous GitHub self-update: reachability probe, commit compare, shallow clone, installer hand-off |
| `src/gba_ingame_ui.py` | pygame/Theme overlay renderer used *inside the mGBA handler* |
| `src/gba_boot.py` | original cartridge/handheld startup animation and bounded timing policy |
| `src/gba_status.py` | background WiFi/Bluetooth probes with lock-protected, non-blocking UI snapshots |

### Process Lifecycle
1. **Supervisor** starts and executes `gba-launcher`.
2. When the user confirms a game, `gba-launcher` atomically writes a launch request to
   `/run/gba/launch_request.json` and exits cleanly (exit code `10`).
3. **Supervisor** invokes `mgba-adapter` with an isolated environment:
   - `SYS_CONTRACT=1`
   - `SYS_ENTRY_ID=<hash>`
   - `SYS_CONTENT_PATH=/games/<rom>`
   - `SYS_SAVE_DIR=/data/saves/<hash>/`
   - `SYS_DISPLAY_SCALING=fit|integer`
4. **mgba-adapter** backs up the existing save, resolves the display session (X11 / Wayland / KMS-DRM
   / headless) so mGBA cannot start on a backend with no display, runs mGBA with save routing
   (`-f`, explicit `-C fullscreen=1`), then fsyncs the save files and their directory before exiting.
5. **Supervisor** requests a filesystem `sync`, records the outcome (`clean`, `crash`, `launch-failure`, …),
   and restarts `gba-launcher` focused on the previous game with the outcome banner shown.

A launcher that crashes repeatedly (3 times in 30 s) is not respawned; the supervisor drops into the
recovery console instead (contract rule S7).

---

## 🚀 Usage & Booting

### 0. Install from a USB stick (the normal way)

```bash
make iso-check                    # lists anything missing to build the ISO
sudo make iso                     # builds build/gba-os-<version>-amd64.iso
```

Write the image to a USB stick (**the stick is the only thing that is erased**):

```bash
sudo dd if=build/gba-os-<version>-amd64.iso of=/dev/sdX bs=4M status=progress oflag=sync
```

Boot the stick on the target PC and answer **two questions**:

1. *Where should GBA OS be installed?* — press **Enter** to accept the recommended free
   partition. Options that would destroy your existing system are marked and require the exact
   phrase `ERASE /dev/sdX`.
2. *Install GBA OS on /dev/sdX1, keeping your current system?* — press **Enter** again.

Setup then formats that one partition, copies GBA OS, verifies it, and only then adds a boot menu
entry. If anything fails it prints what state the machine is in — and in every failure before the
boot menu is written, **your computer still starts its usual operating system**.

**"Can this brick my PC?"** No — see [docs/SAFETY.md](docs/SAFETY.md) for the exact list of things
that can change, the installer's nine refusal rules, and the fallback for every component. The short
version:

- the live stick writes only to RAM and to itself;
- the installer refuses the disk it booted from, any mounted partition, any partition that contains
  an operating system, anything under 8 GB, and removable media without explicit consent;
- the bootloader is written last, verified, and never touches UEFI NVRAM or another OS's entry;
- the on-PC installer (`scripts/install.sh`) is dry-run by default and restores the previous
  `grub.cfg` if the regenerated one does not contain its own entry.

### 1. Booting via GRUB (Bare-Metal Console Appliance)
During system boot, select:
```text
GBA OS (V1 Appliance Mode)
```
The machine boots directly into the fullscreen retro GBA console shell.

To return to your normal OS: navigate to **SETTINGS → Reboot to GRUB Menu**. The GRUB menu
appears with your primary operating system still selected as the default.

### 2. Testing in Windowed Mode (Inside an Existing Desktop)
```bash
make deps            # one-off: installs pygame
make run-window      # or ./scripts/launch_desktop.sh
```
The windowed session runs the real supervisor → launcher → emulator chain, so what you test is what
ships. Reboot/poweroff are simulated in this mode so your desktop is never touched.

### 3. Installing on a running Linux machine

```bash
./scripts/install.sh                    # dry-run: prints every change, writes nothing
sudo ./scripts/install.sh --apply       # do it (one confirmation)
sudo ./scripts/install.sh --apply --uninstall
sudo ./scripts/install.sh --apply --repair   # re-copy files, keep saves, restore boot entry
```

To install GBA OS onto a *disk* from an existing system, use the same guided setup the USB stick
runs:

```bash
sudo gba-setup            # guided: pick a target, confirm, done
sudo gba-setup --dry-run  # show the plan without writing
```

Check any machine before or after an install:

```bash
gba-selftest              # PASS/WARN/FAIL per component, with the fix for each FAIL
gba-selftest --json       # same, for scripts
gba-selftest --mtp        # the whole Android MTP chain, link by link, with one hint
```

The installer:

- copies the runtime into `/opt/gba-distro` and prepares `/data` + `/games`;
- writes the shared keyboard profile to `/data/config/keybindings.json` and `~/.config/mgba`;
- determines the root filesystem UUID and the newest installed kernel instead of hard-coding them;
- hooks tty1 (SysVinit / runit) and appends `GBA OS (console appliance)` plus a
  `GBA OS (safe graphics / recovery)` GRUB entry without changing the default boot target;
- backs up every system file it touches and rolls them back if a later step fails;
- reports missing runtime packages (`python3`, `python3-pygame`, `mgba-sdl`) instead of failing at boot,
  and warns (without failing) when the optional `jmtpfs`/GVfs Android MTP backend is absent.

---

## 🕹️ Controls

| Action | Controller | Keyboard |
| :--- | :--- | :--- |
| **Navigate the shelf** | D-Pad / Analog Stick | Arrows (grid aware) |
| **Switch tab** | START / shoulder buttons | `Tab`, `Alt+Tab`, `Alt+Shift+Tab` |
| **Open the terminal** | — | `Ctrl+Alt+T` (toggles, works from any tab or dialog) |
| **Launch / Select (A)** | `A` Button (Button 0) | `Enter` / `Z` / `Space` |
| **Back / Cancel (B)** | `B` Button (Button 1) | `Esc` / `X` / `Backspace` |

In a game the shipped keyboard profile is straight GBA: `Z` = **A**, `X` = **B**, arrows = D-pad,
`A` / `S` = **L** / **R**, `Enter` = **START**, `Backspace` = **SELECT**.
| **Search library** | — | just start typing (`Backspace` edits, `Esc` clears) |
| **Toggle cartridge/list view** | — | `Ctrl+V` |
| **Help / screenshot** | — | `F1` / `F12` (saved to `DATA/screenshots/`) |
| **Emergency Recovery** | — | boot failure, or **SETTINGS → System → Reboot** after a crash |

Browser: `Ctrl+L` types an address, `Tab` / `1`–`9` pick a link, `Enter` opens it, `Alt+Left` /
`Alt+Right` walk history, `R` reloads, `Up`/`Down`/`PgUp`/`PgDn` scroll.

All ten GBA buttons are remappable under **SETTINGS → Controls → Remap buttons**; the same profile
drives both the shell and mGBA (written into the handler's `config.ini`), so a gamepad behaves
identically in the two.

The bindings go into `[gba.input.SDLK]` — the only section mGBA's SDL frontend reads for the
keyboard (it builds the name from `GBAInputInfo.platformName` plus the `SDLK` binding id). Anything
written elsewhere is ignored without an error and the game quietly keeps mGBA's built-in
`X` = `A` / `Z` = `B` defaults, which is why an earlier release could report success while the
controls were still swapped in-game. Each launch re-writes the file, merges it into the existing
mGBA config (your other mGBA settings are kept) and re-reads it with mGBA's own lookup rules. To
check what the emulator will get:

```bash
grep -A11 '\[gba.input.SDLK\]' "${XDG_CONFIG_HOME:-$HOME/.config}/mgba/config.ini"
grep -i 'keyboard profile' "$(ls -t /data/logs/*.log | head -1)"
```

The session log must say `mGBA keyboard profile verified ... A=122 B=120` (122 = `Z`, 120 = `X`).
If it says anything else it also says why — a Qt build of mGBA or a `portable.ini` next to the
binary both make mGBA read a different profile. Reserved shell keys (Esc, Tab, arrows, Ctrl/Alt) and duplicate assignments
are rejected with an inline explanation. The default profile changed in this release: `Z` is now
**A** and `X` is **B** (they were swapped). A profile you customised is never overwritten — only a
stored profile that is still exactly the old stock pair is migrated.

---

## 🎛️ In-Game Overlays & Shortcuts

These live in the mGBA handler, not in a second application and not in a second GUI toolkit. An
overlay needs the screen, and a *stopped* mGBA still owns its SDL/KMS surface, so opening one is a
four-step handoff:

1. while the game is still running, the handler captures a **native mGBA save state** and a
   **screenshot** of the current frame;
2. mGBA is stopped and releases the display;
3. the overlay is drawn with the launcher's existing `Theme`/pygame code;
4. on close the overlay gives the display back and a **new mGBA is started from the captured state**
   (`-t`), at the current fast-forward speed.

If step 1 fails, the overlay does not open and the game keeps running — a game is never left frozen
behind a window that failed to appear, and no key is ever sent to a stopped emulator. Navigation is
controller-first (D-pad/hat/buttons) with keyboard and mouse fallback. Combos are edge-triggered:
holding them does not fire again, and the consumed buttons are released so they cannot also act as
gameplay input.

| Action | Default combo | Behaviour |
| :--- | :--- | :--- |
| **Cheats Overlay** | `Up + Left` | Only this game's cheats. `A` toggles a cheat or opens **Add Cheat**, `X` edits, `Y` asks before deleting, and `B` goes back/closes. |
| **Fast-Forward Cycle** | `L + R` | Each deliberate press cycles `1x → 2x → 3x → Normal`, showing a temporary `2x` / `3x` / `Normal` badge. |
| **Save States Overlay** | `Left + Right` | Exactly nine slots in a 3×3 grid with thumbnails and timestamps; `Load Save` / `Overwrite Save` for occupied slots. |

In the cheat list, **Pad X** edits and **Pad Y** requests deletion. On a physical keyboard, use `E`
to edit and `Delete` to request deletion; `N` or `F2` opens Add Cheat directly, while `Esc` /
`Backspace` retains the usual B/back behaviour.

All three are configurable under **SETTINGS → Controls → In-game shortcuts** (press `A` on a row,
then two distinct controller or keyboard buttons; `R` resets to defaults). They are stored in the
existing `DATA/config/settings.json` as `shortcut_cheats`, `shortcut_fast_forward` and
`shortcut_save_states`. Defaults work on a fresh install.

Per-game data uses the library's stable content id (SHA-256 of the ROM — never the filename), which
the supervisor already passes to the handler as `SYS_ENTRY_ID`:

```text
DATA/saves/<content-id>/
├── cheats.json                  # metadata only: names, codes, enabled flags
├── cheats/<rom-base>.cheats     # mGBA's native # name / !disabled / code format
├── states/slots.json            # metadata only: timestamp, size, SHA-256, has_thumbnail
├── states/slot_N/state.ss       # the real mGBA state (F1–F9 / Shift+F1–F9)
├── states/slot_N/thumbnail.png  # visual thumbnail only (F12), never state data
└── runtime/                     # mGBA's native slot files + speed-reload state
DATA/config/favorites.json       # content ids, stable favorites-first ordering
```

Choose **+ ADD CHEAT** in the overlay to enter a name and one or more mGBA code lines directly. The
on-screen keyboard works with a D-pad and `A` (including **NAME**, **CODE**, **DELETE**, **NEW LINE**
and **SAVE** actions), or you can type on a physical keyboard: `Tab` changes field, `Enter` saves,
and `Shift+Enter` inserts a code newline. New cheats start enabled; edits preserve the existing
enabled/disabled choice. GameShark, Action Replay and CodeBreaker text uses the same native mGBA
format as manually imported cheats, so no file-manager trip is needed.

**Use codes for the game's actual platform.** Pokémon Yellow is a Game Boy game, so GBA Action
Replay/GameShark codes will not work. GB/GBC entries accept eight-digit GameShark codes (for example
`019947D3`; spaced forms such as `01 99 47 D3` are normalized), Game Genie `XXX-XXX[-XXX]`,
CodeBreaker `XXXXXX-XX`, and VBA `AAAA:VV` lines (converted to the equivalent GameShark RAM write).
Invalid GB lines are rejected with an editor
message instead of producing a false “added and enabled” success. An invalid set from an older
profile remains available to edit but is deliberately written `!disabled`, because mGBA can
otherwise truncate a GBA-style code and write the wrong Game Boy address. Launch diagnostics report
the native file, enabled sets, valid enabled lines and invalid sets. Syntax validation cannot prove
that a code targets the correct ROM region/revision, so use codes published for the exact game dump.

Cheats are loaded through mGBA's own `-c <file>` mechanism (`cheatAutoload=0` when a game has none,
so no global list leaks in); adding, editing or toggling one persists immediately and the restart
above picks it up, so the change is active in the running game. Save/load use mGBA's native state hotkeys — sent only while
mGBA is *running*, which is exactly the moment the handoff captures them. Screenshots are separate
PNGs. Speed uses mGBA's real `fpsTarget` (60/120/180) rather than skipped frames or hand-written
timing. Slots are written atomically (temp file + `os.replace`), verified by size + SHA-256 on load,
and rolled back to the previous good state if a later write fails; missing/corrupt data never
crashes the emulator or supervisor, and the user's nine slots never include the handler's private
handoff slot.

**SETTINGS → Game Management** lists imported games and offers `Delete Selected Games` (multi-select,
confirmation) and `Delete All Imported Games` (confirmation plus a two-second hold). Deletion removes
the ROM, its per-game cheats/states/runtime data, its favorite flag and its library-cache entry.
**GAME LIST → Favorites** (`Ctrl+F` / pad `X`) keeps favorites first as a stable partition: relative
order is preserved in both groups, unfavoriting returns a game to its original position, and no game
is duplicated.

> **Real-hardware caveats.** Native hotkeys are delivered through `/dev/uinput` (or `xdotool` on
> X11); without either, overlays still open/close but native capture, load and speed reload degrade
> gracefully. `gba-selftest` reports this. Each overlay open/close costs one emulator restart
> (≈0.3–1 s), and audio may click across it. Speed changes use the same state-preserving reload
> (`-t` with a freshly captured native state) because mgba-sdl has no runtime speed IPC; the exact
> DRM release latency, audio pacing at 2x/3x and restart cost need verification on the appliance.
> None of this is verifiable in the headless test suite.

---

## 📂 Managing ROMs, Saves & Settings

- **Games Directory:** `/games/` (sub-folders are allowed)
  Place any `.gba`, `.gbc` or `.gb` file here. Titles are read from the cartridge header, falling
  back to a cleaned-up filename (`Pokemon Emerald (USA).gba` → `Pokemon Emerald`).
  The library rescans automatically; use **SETTINGS → Library → Rescan library** after copying files over.
- **Saves Directory:** `/data/saves/<entry-id>/`
  Save batteries (`.sav`) and savestates live here, keyed by content hash, so renaming a ROM never
  orphans its save. **Apps → Save Manager** shows sizes and can delete a save after confirmation.
- **Pendrive import:** plug the stick in and the shell offers to import its `.gba`/`.gbc`/`.gb`
  files. Nothing is copied until you confirm; existing files are never overwritten (identical files
  are skipped, name clashes get a `(n)` suffix) and every copy is size-verified. Files land in
  `/games/<drive label>/` unless you turn sub-folders off. Mount detection reads `/proc/mounts` and
  `/sys/block`, so it needs no `udisks`/`udev`.
- **Android phone import (MTP):** unlock the phone and select **File Transfer / MTP**. The shell
  detects the USB MTP interface and exposes its internal storage/SD card through `jmtpfs` (or an
  existing GVfs `mtp://` mount). It is shown as `ANDROID MTP`, never as a removable disk. The same
  extension filter, confirmation prompt, content-ID deduplication, size verification and `(n)` clash
  rule apply. Install `sudo apt install jmtpfs mtp-tools` on a system image built without them;
  normal USB sticks work without MTP support. A locked screen or charging-only mode yields no phone
  source, but the import screen says *why* and keeps retrying (three quick attempts, then every
  15 s), so unlocking the phone in the same session still produces the prompt. MTP storages that
  report `0`-byte file sizes are scanned and imported normally. `gba-selftest --mtp` prints every
  link of the chain (sysfs interface, `jmtpfs`/`mtp-detect`, GVfs, FUSE mount root, devices,
  volumes, last error) with one actionable hint.
- **Settings:** `DATA/config/settings.json` is the single store (scaling, theme, clock, view mode,
  volume, USB behaviour, launch confirmation). `keybindings.json` and `history.json` live beside it;
  a legacy `display.json` is migrated automatically on first run.
- **Updates:** SETTINGS → System → *Check for updates* asks github.com (anonymous HTTPS, no login)
  whether `origin/main` is ahead of the commit stamped in `/opt/gba-distro/VERSION`, shows the new
  commits, and on confirm shallow-clones them and re-runs the shipped `scripts/install.sh --apply
  --repair` from the clone. Saves, ROMs and settings live in `DATA/` and `GAMES/` and are never
  touched; the launcher re-execs itself into the new version afterwards.

---

## 🧪 Testing & Development

```bash
make test       # 412 tests (headless, no display needed)
make lint       # byte-compile Python + syntax-check every shell script
make selftest   # check *this* machine: interpreter, fonts, pty, display, mGBA, storage, config
make iso-check  # what is missing to build a bootable ISO here
make help       # list every target
```

The suite covers the numbered contract rules directly — library identity and caching (L2, L3, L4, L6),
handler environment isolation (H1), outcome classification (H10), crash-loop handling (S6, S7), orphan
cleanup (S8), save durability and restoration (SV2–SV4), log rotation (ST5), input-profile validation
(I3), ROM import safety (ST7, ST8), the settings store and theme system, the ANSI terminal emulator, the
browser's HTML extractor/layout/history, and shell behaviour (tabs, `Alt+Tab`, `Ctrl+Alt+T`, search,
empty state, confirmation-gated launches). The in-game suites cover the display handoff end to end
(`tests/test_ingame_handoff.py`: mGBA is dead while the overlay renders and a *new* child starts from
the captured state, at the inherited speed), all nine save-state slots
(`tests/test_game_data.py`), cheat persistence and application after the overlay closes, the
`1x → 2x → 3x → 1x` cycle and its edge-triggering, and the `Z`→`A` / `X`→`B` defaults with migration
for stock profiles only. `tests/test_mtp.py` covers each link of the phone chain separately (sysfs
protocol 0, quoted `jmtpfs -l`, PTP-camera exclusion, GVfs URI derivation, locked-phone re-prompt,
0-byte storages, mount-only listing with no subprocess, and `gba-selftest --mtp`).

Installer safety has its own suites: `tests/test_installer.py` (every refusal rule, the
copy → verify → bootloader order, dry runs, confirmation gating), `tests/test_setup.py` (which
targets the guided setup offers and recommends, and what it does with a wrong answer), and
`tests/test_safety.py` (shell syntax in the shell that will run it, "no script writes a disk", the
GRUB entry invariants, the ISO builder's loud failure, and the missing-pygame fallback).

Screenshots are generated from the real renderer using synthetic content:

```bash
make screenshots   # regenerates the four README images
```

---

## 🛟 Troubleshooting

| Symptom | What to do |
| :--- | :--- |
| Boots to a black screen | Wait for the 10 s GRUB menu, pick your primary OS, then check `/data/logs/latest.log`. |
| Shell starts but no games | Copy ROMs into `/games/`, then **SETTINGS → Library → Rescan library**. |
| Game starts to a black screen | The adapter resolves the display session itself (a stale `DISPLAY` or a KMS/DRM driver inside a live desktop is the usual cause) and retries once in a window. `/data/logs/latest.log` now records the whole chain: `Display session=...`, the exact `Executing mGBA:` argv, `[mgba]`/SDL2/Mesa output, and an `[x11]` (or `[drm]`) verdict on whether the emulator's window actually mapped. |
| Window is there but nothing shows | Read the `[x11] emulator window ...` line: `map-state=IsUnmapped` means SDL created it and something never mapped it, `lies entirely outside the active ... mode` means it is off-screen, `does not fill the display` means it is smaller than the screen. `NO emulator window in the X11 tree` means mGBA died first — the `[mgba]` lines above it say why. The window sample needs `sudo apt install x11-utils x11-server-utils`; without them the log says so and everything else still runs. |
| Capturing a full diagnostic session | On by default: `gba-session` exports `SDL_LOG_PRIORITY=verbose`, `LIBGL_DEBUG=verbose`, `MESA_DEBUG=1` and `GBA_DEBUG=1`, and the adapter adds mGBA's `-l 31` log mask. Set `GBA_DEBUG=0` for the previous quiet behaviour, or `GBA_MGBA_LOG_LEVEL=127` for everything mGBA can print. `gba-selftest` reports what will and will not be captured. |
| Game exits immediately | Check `/data/logs/latest.log` for `[handler]` lines, the `Stop sequence:` signal trail and `Teardown: exit status ...`; verify `mgba` is installed (`which mgba`). |
| Pendrive not detected | The filesystem must be mounted (any desktop does that automatically). Open **SETTINGS → Library → Import from USB / MTP** and press `R` to rescan. |
| Android phone not detected | Unlock it, choose **File Transfer / MTP** (not charging only), and press `R` in the import screen. Run `gba-selftest --mtp`: it names the failing link. Install `sudo apt install jmtpfs mtp-tools` when the tools are missing, `sudo modprobe fuse` when `/dev/fuse` is missing, and `fuse3` when no `fusermount` is found. GVfs `mtp://` mounts are reused automatically. |
| Overlay does not open | The game keeps running on purpose: the native state/frame capture failed (usually `/dev/uinput` unavailable). `gba-selftest` reports input injection; overlay save/load then degrades but never freezes gameplay. |
| Terminal is unavailable | The shell needs a PTY; check that `/dev/pts` is mounted (`SHELL=/bin/sh src/gba_launcher.py` still works for a quick test). |
| Xbox-style pad exits games | The SELECT+START, Guide, and `Esc` combinations are the system exit gestures — see the launch screen. |
| Everything is broken | At the GRUB menu pick `GBA OS (safe graphics / recovery)`, or add `gba_mode=0` to the kernel line for a normal login prompt. The recovery console (option 4) exports your saves first. |
| A component is missing | Run `gba-selftest`; every FAIL line names the package to install. |
| Install failed half way | Your boot menu was not modified — the machine still starts its usual system. Re-run the setup with another target. |

---

## 📄 License

Apache 2.0 — see [LICENSE](LICENSE). Game Boy Advance is a trademark of Nintendo; this project ships
no ROMs, BIOS files or proprietary assets.

Headless pendrive mounting uses udiskie + udisks2, without GNOME. See
[USB automount setup and verification](docs/USB_AUTOMOUNT.md).
