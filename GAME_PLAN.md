# GBA-Inspired Distro — Game Plan

## 1. Vision

Build a lightweight, appliance-style Linux distribution for PCs/laptops that feels like turning on a Game Boy rather than booting a conventional desktop OS.

The system will use **Tiny Core Linux as its base** and will intentionally provide only the small set of PC capabilities needed around the core gaming experience.

The primary purpose is:

> **Boot → game library → select a game → play → return to library.**

The desktop itself is the game library. A conventional desktop environment is not part of the design.

---

## 2. Core Product Principles

- Tiny Core Linux is the base operating system.
- Keep the system lightweight and fast to boot.
- No full desktop environment such as GNOME, KDE, XFCE, etc.
- The game library is the main shell/desktop.
- `.gba` files are first-class content and can be launched directly from the library.
- Controllers are a first-class input method.
- Keyboard and mouse remain available for PC-oriented tasks.
- Saves are treated as managed game data, not scattered files the user has to hunt for.
- Only essential PC utilities are included.
- Future applications made specifically for this distro can appear alongside games in the same library.
- The UI should consistently evoke the visual language and interaction model of classic Game Boy/GBA hardware without copying proprietary artwork or branding.
- Performance and reliability take priority over adding unnecessary features.

---

## 3. Intended User Experience

### Boot

```text
PC powers on
    ↓
Bootloader
    ↓
Tiny Core Linux
    ↓
GBA-inspired startup
    ↓
Game Library
```

The user should not need to interact with a normal Linux login or desktop.

### Main Library

Conceptually:

```text
┌────────────────────────────────────┐
│            GAME LIBRARY            │
│                                    │
│   > Pokémon Emerald                │
│     Zelda: Minish Cap              │
│     Metroid Fusion                 │
│     Mario Kart                     │
│                                    │
│     APPS                           │
│     SETTINGS                       │
│     FILE MANAGER                   │
└────────────────────────────────────┘
```

The exact visual design will be defined during UI implementation, but navigation should be usable without a mouse.

---

# 4. System Architecture

Initial target architecture:

```text
                    ┌────────────────────┐
                    │      PC / Laptop   │
                    └─────────┬──────────┘
                              │
                         BIOS / UEFI
                              │
                              ▼
                    ┌────────────────────┐
                    │    Bootloader      │
                    └─────────┬──────────┘
                              ▼
                    ┌────────────────────┐
                    │ Tiny Core Linux    │
                    │                    │
                    │ Kernel + drivers   │
                    │ Minimal userspace  │
                    └─────────┬──────────┘
                              ▼
                    ┌────────────────────┐
                    │ Graphics / Audio   │
                    │ Input / Storage    │
                    └─────────┬──────────┘
                              ▼
                    ┌────────────────────┐
                    │ GBA OS Shell       │
                    │                    │
                    │ Game Library       │
                    │ App Library        │
                    │ Settings           │
                    │ File Manager       │
                    │ Browser            │
                    │ Terminal           │
                    └─────────┬──────────┘
                              ▼
                    ┌────────────────────┐
                    │ GBA Emulator       │
                    └─────────┬──────────┘
                              ▼
                         User .gba files
```

The shell should be the main user-facing application and should launch the emulator in fullscreen/game mode.

---

# 5. Initial Built-in Components

Only the following capabilities are planned for the initial distro.

## 5.1 Game Library

The primary desktop/shell.

Responsibilities:

- Discover installed `.gba` games.
- Display game entries.
- Launch games.
- Display game metadata/artwork where supported.
- Provide controller/keyboard navigation.
- Show installed distro-compatible applications.
- Separate or filter Games and Apps without turning the system into a conventional desktop.
- Provide access to Settings and essential utilities.

## 5.2 GBA Emulator

The distro must have built-in GBA emulation so the user does not have to install an emulator manually.

Requirements:

- Launch `.gba` files directly.
- Fullscreen mode.
- Keyboard input.
- USB controller input.
- Bluetooth controller support where available through the underlying Linux stack.
- Audio.
- Save files.
- Save states where supported.
- RTC/peripherals where supported by the chosen emulator.
- Clean return to the library.

The emulator will be selected and integrated as a system component rather than exposed as a normal desktop application.

**ROMs themselves will not be bundled with the distro.** Users provide their own legally obtained game files.

## 5.3 File Manager

A lightweight file manager is required.

It should make the filesystem understandable without exposing unnecessary complexity.

Important locations will have clear purposes, for example:

```text
Games/
Saves/
Apps/
Downloads/
Screenshots/
```

The file manager should allow users to:

- Copy/move games.
- Delete games.
- Manage save files.
- Move downloaded files into the game library.
- Access USB storage.
- Create/delete directories where appropriate.

## 5.4 Lightweight Browser

A very lightweight browser will be included primarily for downloading `.gba` files and other supported content.

It is **not intended to become a full desktop browsing environment**.

Initial goal:

```text
Browser
  ↓
Download file
  ↓
Downloads/
  ↓
User moves/imports game
  ↓
Game appears in library
```

The browser must not become a major RAM or storage consumer relative to the rest of the system.

## 5.5 Terminal

A terminal will be available for advanced users and troubleshooting.

It should remain accessible from the library but should not be the default interface.

Useful system commands and distro-specific commands can eventually be documented and/or wrapped by simple utilities.

---

# 6. Game and Application Model

The library should not permanently assume that every item is a game.

The architecture should support two top-level content types from the beginning:

```text
Games
Apps
```

Example:

```text
LIBRARY
│
├── GAMES
│   ├── Pokémon Emerald
│   ├── Metroid Fusion
│   └── Zelda Minish Cap
│
└── APPS
    ├── Example Distro App
    └── Future App
```

A future application built specifically for this distro should be able to register itself with the library rather than creating a separate desktop shortcut system.

This makes the distro extensible without abandoning the console-like experience.

---

# 7. Save System

Save handling is a core feature, not an afterthought.

Target structure:

```text
/Saves/
    <game-id>/
        game-save files
        save states
        metadata
```

The exact emulator-specific format will be determined during integration.

Future possibilities:

- Automatic save discovery.
- Save backup.
- Save restore.
- Export/import saves.
- Per-game save management.
- Optional automatic backup to another drive.

The user should never need to understand emulator-specific internal paths just to find their save.

---

# 8. Controller System

The distro should be controller-first.

Target controls for the library:

```text
D-Pad     Navigate
A         Select / Confirm
B         Back
START     Open / Launch
SELECT    Secondary action / Menu
```

The actual mapping must remain configurable.

Support targets:

- USB gamepads.
- Common Xbox-style controllers.
- Common PlayStation-style controllers.
- Generic USB controllers.
- Bluetooth controllers where the hardware/software stack supports them.
- Keyboard fallback.
- Mouse fallback.

A controller configuration screen should eventually allow users to remap controls.

---

# 9. Storage Model

The distro should distinguish between operating-system files and user content.

Conceptually:

```text
System
├── Tiny Core base
├── system extensions
└── distro components

User Data
├── Games
├── Saves
├── Apps
├── Downloads
├── Screenshots
└── Settings
```

This separation will make reinstalling/upgrading the OS safer and make user-data backup easier.

Persistent storage must be designed deliberately because Tiny Core commonly uses RAM-based operation and persistence mechanisms rather than behaving like a conventional Ubuntu installation.

---

# 10. Resource Targets

The distro should be aggressively lightweight, but the goal is not to optimize toward an arbitrary number at the cost of usability.

### Initial targets

- Idle system: ideally well below 500 MB RAM on supported hardware.
- GBA gameplay: target well below 1 GB RAM where practical.
- Hard design ceiling: **less than 2 GB RAM during normal GBA gameplay** on supported hardware.
- Avoid unnecessary background services.
- Avoid a heavyweight desktop environment.
- Avoid Electron-based components unless a future requirement makes one unavoidable.
- Fast boot should be treated as a first-class performance metric.

Actual resource consumption must be measured on real hardware during development rather than assumed from package sizes.

---

# 11. UI / Visual Direction

The system should feel inspired by the GBA era without becoming a literal copy of Nintendo's interface.

Design goals:

- Pixel-oriented visual language.
- Strong game-library focus.
- Compact menus.
- Simple navigation.
- Controller-first interaction.
- GBA-inspired color and typography direction.
- Optional themes in the future.
- Minimal animations so the UI remains responsive on weak hardware.

The desktop should never feel like a normal Linux desktop with a Game Boy wallpaper.

It should feel like a **console interface implemented on a PC**.

---

# 12. Boot and Session Lifecycle

Target lifecycle:

```text
Power on
  ↓
Boot
  ↓
Initialize only required services
  ↓
Start GBA shell
  ↓
Game Library
  ↓
Launch game
  ↓
Emulator fullscreen
  ↓
Exit game
  ↓
Return to exact library position
```

Future:

```text
Suspend / sleep
Quick resume
Automatic crash recovery
Last played game
```

---

# 13. Installation and Distribution

The project should eventually produce a bootable image suitable for:

- USB boot.
- Installation to internal storage.
- Testing in virtual machines.

Initial development should prioritize **bootable USB/VM testing** before creating a polished installer.

Potential final artifacts:

```text
GBA-Distro.iso
GBA-Distro.img
```

The exact image format and installation mechanism will be decided after the first working Tiny Core build.

---

# 14. Development Phases

## Phase 0 — Project Foundation

- [ ] Define project name and branding.
- [ ] Document licensing strategy.
- [ ] Define supported architecture(s), initially x86-64.
- [ ] Establish repository structure.
- [ ] Establish development/build environment.

## Phase 1 — Tiny Core Boot Prototype

- [ ] Build a minimal Tiny Core image.
- [ ] Boot on a VM.
- [ ] Boot from USB.
- [ ] Confirm networking.
- [ ] Confirm audio.
- [ ] Confirm graphics.
- [ ] Confirm USB storage.
- [ ] Confirm keyboard.
- [ ] Confirm basic gamepad input.
- [ ] Measure idle RAM and boot time.

**Milestone:** Tiny Core boots into a controlled minimal environment with no conventional desktop.

## Phase 2 — Emulator Integration

- [ ] Select emulator implementation.
- [ ] Build/package emulator for Tiny Core.
- [ ] Launch a user-provided `.gba` file.
- [ ] Verify fullscreen rendering.
- [ ] Verify audio.
- [ ] Verify keyboard controls.
- [ ] Verify USB controllers.
- [ ] Verify save files.
- [ ] Verify clean exit back to the shell.
- [ ] Measure RAM/CPU/GPU usage.

**Milestone:** A `.gba` file can be launched and played directly from the distro.

## Phase 3 — Game Library Shell

- [ ] Build the first GBA-inspired UI.
- [ ] Replace the normal desktop/session with the shell.
- [ ] Scan the Games directory.
- [ ] Display `.gba` files.
- [ ] Launch selected games.
- [ ] Return to the library after exit.
- [ ] Implement controller navigation.
- [ ] Implement keyboard fallback.

**Milestone:** The system feels like a dedicated GBA game console.

## Phase 4 — Save and Storage Management

- [ ] Define persistent user-data layout.
- [ ] Implement save discovery.
- [ ] Implement save management UI.
- [ ] Add file manager.
- [ ] Add USB storage workflow.
- [ ] Add import/export where practical.

**Milestone:** Users can manage games and saves without using a terminal.

## Phase 5 — Essential PC Utilities

- [ ] Integrate lightweight browser.
- [ ] Integrate terminal.
- [ ] Integrate settings.
- [ ] Integrate network configuration.
- [ ] Integrate display configuration where necessary.
- [ ] Integrate controller configuration.

**Milestone:** The distro is useful as a standalone lightweight PC appliance.

## Phase 6 — Unified Apps Library

- [ ] Define application registration format.
- [ ] Allow distro applications to declare metadata/icon/category.
- [ ] Display apps alongside games.
- [ ] Launch apps through the same shell.
- [ ] Keep third-party applications sandboxed/isolated where practical.

**Milestone:** The platform supports a small ecosystem of distro-native applications.

## Phase 7 — Hardware Compatibility

- [ ] Test Intel graphics.
- [ ] Test AMD graphics.
- [ ] Test common NVIDIA configurations where practical.
- [ ] Test different audio devices.
- [ ] Test USB controllers.
- [ ] Test Bluetooth controllers.
- [ ] Test laptops with different displays.
- [ ] Test sleep/wake.
- [ ] Test different storage devices.

**Milestone:** Define a clear supported-hardware baseline.

## Phase 8 — Performance and Reliability

- [ ] Measure boot time.
- [ ] Measure idle RAM.
- [ ] Measure gameplay RAM.
- [ ] Measure CPU usage.
- [ ] Identify unnecessary background services.
- [ ] Optimize startup.
- [ ] Optimize library scanning.
- [ ] Optimize emulator startup.
- [ ] Test long-duration gameplay.
- [ ] Test crashes and recovery.

**Milestone:** Stable, fast and resource-efficient release candidate.

## Phase 9 — Release System

- [ ] Build reproducible images.
- [ ] Add version information.
- [ ] Add release notes.
- [ ] Add installation instructions.
- [ ] Add troubleshooting documentation.
- [ ] Add checksums.
- [ ] Create first public alpha release.

---

# 15. Future Features — Only After the Core Works

Potential future additions:

- Game screenshots.
- Automatic artwork detection.
- Recently played section.
- Favorites.
- Search.
- Categories.
- Playtime tracking.
- Achievements for supported games/emulators.
- Save backup/export tools.
- Multiple themes.
- GBC/GB support.
- Additional emulators.
- Native distro-app store/catalog.
- Network file transfer.
- Wi-Fi multiplayer features where technically/legalistically appropriate.
- TV/HDMI-focused mode.
- Handheld-PC optimized mode.
- Steam/desktop integration as an optional feature, not part of the minimal base.

These must not be allowed to turn the base system into a conventional heavyweight desktop.

---

# 16. Non-Goals

The project is **not** intended to become:

- A general-purpose Linux desktop distribution.
- An Ubuntu replacement.
- A full office/workstation OS.
- A browser-centric operating system.
- A bundled ROM distribution.
- A heavyweight multimedia platform.
- A system containing dozens of unrelated desktop applications.

The core identity remains:

> **A lightweight PC operating system that behaves like a GBA-inspired game console and provides a small, expandable application library.**

---

# 17. First Practical Goal

Do not attempt to build the entire distro immediately.

The first working prototype should accomplish only this:

```text
Tiny Core Linux
      ↓
Boot automatically
      ↓
Launch our shell
      ↓
Show a fake/test game library
      ↓
Select test.gba
      ↓
Launch emulator fullscreen
      ↓
Play using keyboard/controller
      ↓
Exit
      ↓
Return to library
```

Once this loop works reliably, add the file manager, saves, browser, settings and application system one at a time.

---

# 18. Definition of Done — Version 1.0

Version 1.0 should satisfy all of the following:

- [ ] Boots directly into the GBA-inspired shell.
- [ ] No conventional desktop is required.
- [ ] User-provided `.gba` files appear in the library.
- [ ] Games launch directly from the library.
- [ ] Games run fullscreen.
- [ ] Audio works.
- [ ] Keyboard controls work.
- [ ] Common USB controllers work.
- [ ] Save files persist correctly.
- [ ] File manager is available.
- [ ] Lightweight browser is available.
- [ ] Terminal is available.
- [ ] Settings are available.
- [ ] Apps can eventually coexist with games in the library architecture.
- [ ] User data is separated from the system sufficiently for upgrades/reinstallation.
- [ ] The system remains lightweight and responsive.
- [ ] Normal GBA gameplay stays comfortably below the 2 GB RAM target on supported hardware.
- [ ] The complete system can be distributed as a bootable image.

---

## Guiding Rule

**Every component must justify its existence.**

If a feature does not improve the game-library experience, enable a necessary PC capability, or provide a foundation for future distro-native applications, it should not be part of the minimal base system.
