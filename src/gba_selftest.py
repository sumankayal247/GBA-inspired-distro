#!/usr/bin/env python3
"""
GBA OS — environment self-test.

Answers one question: *will this machine actually run the shell and a game?*
It is deliberately non-destructive: it only reads, imports, and creates/removes
a temp file in the data directory. Safe to run as root, as a user, from a TTY,
from the installer, or from the ISO before touching a disk.

    gba-selftest                 human readable, exit 1 if anything FAILs
    gba-selftest --installed     check an installed tree (default paths)
    gba-selftest --json          machine readable
    gba-selftest --quiet         only failures (used at boot)

Exit codes: 0 = everything passed (warnings allowed), 1 = at least one FAIL,
2 = could not even import the shell modules.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
from typing import Dict, List

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

OK_MARK = {"PASS": "ok  ", "WARN": "warn", "FAIL": "FAIL"}


class Check:
    def __init__(self, name: str, status: str, detail: str, hint: str = ""):
        self.name = name
        self.status = status
        self.detail = detail
        self.hint = hint

    def as_dict(self) -> Dict[str, str]:
        return {"name": self.name, "status": self.status,
                "detail": self.detail, "hint": self.hint}


def _import(path_hint: str):
    """Puts the shell's src directory on sys.path and returns the modules."""
    candidates = [
        path_hint,
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"),
        "/opt/gba-distro/src",
        "/opt/gba-distro/bin",
    ]
    for candidate in candidates:
        if candidate and os.path.isdir(candidate) and candidate not in sys.path:
            sys.path.insert(0, candidate)
    import importlib
    for module in ("common", "config"):
        importlib.import_module(module)
    return True


def check_python() -> Check:
    version = sys.version_info
    detail = f"Python {version.major}.{version.minor}.{version.micro}"
    if version < (3, 8):
        return Check("python", FAIL, detail, "GBA OS needs Python 3.8 or newer.")
    return Check("python", PASS, detail)


def check_modules() -> List[Check]:
    results = []
    modules = ["common", "config", "theme", "library", "gba_terminal",
               "gba_browser", "gba_usb", "gba_mtp", "gba_launcher", "gba_supervisor",
               "game_data", "gba_ingame", "gba_ingame_ui"]
    for name in modules:
        try:
            __import__(name)
            results.append(Check(f"module:{name}", PASS, "imported"))
        except Exception as exc:
            results.append(Check(f"module:{name}", FAIL, f"{type(exc).__name__}: {exc}",
                                 "The install is incomplete; run scripts/install.sh --apply --repair."))
    return results


def check_pygame() -> List[Check]:
    results = []
    try:
        import pygame
        results.append(Check("pygame", PASS, f"pygame {pygame.version.ver}"))
    except Exception as exc:
        return [Check("pygame", FAIL, str(exc),
                      "Install it: sudo apt install python3-pygame   (or: pip install pygame)")]

    # Initialise against the dummy driver: this proves SDL works without
    # stealing the display from the desktop session running the check.
    previous = os.environ.get("SDL_VIDEODRIVER")
    os.environ["SDL_VIDEODRIVER"] = "dummy"
    try:
        pygame.display.init()
        pygame.font.init()
        screen = pygame.display.set_mode((64, 48))
        surface = pygame.Surface((64, 48))
        surface.fill((10, 10, 30))
        screen.blit(surface, (0, 0))
        pygame.display.flip()
        results.append(Check("pygame:display", PASS, "SDL display + blit + flip OK"))
        font = pygame.font.Font(None, 16)
        font.render("ok", True, (255, 255, 255))
        results.append(Check("pygame:font", PASS, "text rendering OK"))
    except Exception as exc:
        results.append(Check("pygame:display", FAIL, f"{type(exc).__name__}: {exc}",
                             "Video backend cannot create a surface."))
    finally:
        try:
            pygame.display.quit()
        except Exception:
            pass
        if previous is None:
            os.environ.pop("SDL_VIDEODRIVER", None)
        else:
            os.environ["SDL_VIDEODRIVER"] = previous
    return results


def check_fonts() -> Check:
    try:
        import theme
        candidates = theme._FONT_DIRS if hasattr(theme, "_FONT_DIRS") else []
    except Exception:
        candidates = []
    for directory in candidates:
        if directory and os.path.isdir(directory):
            for name in os.listdir(directory):
                if name.lower().endswith((".ttf", ".otf")):
                    return Check("fonts", PASS, os.path.join(directory, name))
    return Check("fonts", WARN, "no bundled DejaVu/Liberation TTF found",
                 "The shell falls back to pygame's built-in font; install fonts-dejavu for the real look.")


def check_storage(data_dir: str, games_dir: str) -> List[Check]:
    results = []
    for label, path in (("data", data_dir), ("games", games_dir)):
        if not os.path.isdir(path):
            results.append(Check(f"storage:{label}", WARN, f"{path} does not exist yet",
                                 "It is created automatically on first run."))
            continue
        try:
            handle = tempfile.NamedTemporaryFile(dir=path, prefix=".gba-selftest-", delete=True)
            handle.write(b"ok")
            handle.flush()
            handle.close()
            results.append(Check(f"storage:{label}", PASS, f"{path} is writable"))
        except Exception as exc:
            results.append(Check(f"storage:{label}", FAIL, f"{path}: {exc}",
                                 "Check ownership/mount options for this directory."))
    usage = shutil.disk_usage(data_dir if os.path.isdir(data_dir) else "/")
    free_gb = usage.free / (1024 ** 3)
    if free_gb < 0.2:
        results.append(Check("storage:free", FAIL, f"{free_gb:.2f} GB free",
                             "Free some space: saves and logs need room."))
    elif free_gb < 1:
        results.append(Check("storage:free", WARN, f"{free_gb:.2f} GB free",
                             "Less than 1 GB free; saves and logs may fail."))
    else:
        results.append(Check("storage:free", PASS, f"{free_gb:.1f} GB free"))
    return results


def check_pty() -> Check:
    try:
        import pty
        master, slave = pty.openpty()
        os.close(master)
        os.close(slave)
        return Check("pty", PASS, "pseudo-terminals available (TERMINAL tab will work)")
    except Exception as exc:
        return Check("pty", FAIL, f"{exc}",
                     "The TERMINAL tab needs /dev/pts; mount it or check your container setup.")


def check_mgba() -> Check:
    try:
        import gba_adapter_mgba
        binary = gba_adapter_mgba.find_mgba_binary()
        return Check("mgba", PASS, binary)
    except Exception as exc:
        return Check("mgba", FAIL, str(exc),
                     "Install mGBA: sudo apt install mgba-sdl (games cannot start without it).")


def check_display() -> Check:
    try:
        import config
        session = config.display_session()
        env = config.prepare_child_environment()
        driver = env.get("SDL_VIDEODRIVER", "auto")
    except Exception as exc:
        return Check("display", WARN, f"could not inspect: {exc}")
    if session == "headless":
        return Check("display", WARN, f"no graphical session detected (driver={driver})",
                     "Appliance mode will use KMS/DRM; run from a TTY to test it here.")
    return Check("display", PASS, f"{session} session, SDL driver {driver}")


def check_config_roundtrip(data_dir: str) -> Check:
    try:
        import config
    except Exception as exc:
        return Check("config", FAIL, f"config module unavailable: {exc}")
    previous = config.get_data_dir
    try:
        config.get_data_dir = lambda: data_dir
        settings = config.load_settings()
        settings["theme"] = settings.get("theme", "indigo")
        config.save_settings(settings)
        reloaded = config.load_settings()
        if reloaded.get("theme") != settings.get("theme"):
            return Check("config", FAIL, "settings did not round-trip")
        return Check("config", PASS, "settings.json readable and writable")
    except Exception as exc:
        return Check("config", FAIL, f"{exc}", "Check permissions on DATA/config.")
    finally:
        config.get_data_dir = previous


def check_mtp() -> Check:
    """Android phones need a libmtp/GVfs FUSE backend; USB sticks do not.

    A phone also needs `/dev/fuse` and a working `fusermount`: jmtpfs can be
    installed and still be unable to mount anything, which looks exactly like
    "the phone is not detected". Both are reported.
    """
    tools = {name: bool(shutil.which(name))
             for name in ("jmtpfs", "mtp-detect", "gio", "fusermount3", "fusermount")}
    fuse_device = os.path.exists("/dev/fuse")
    fuse = tools["fusermount3"] or tools["fusermount"]
    if not (tools["jmtpfs"] or tools["gio"]):
        if tools["mtp-detect"]:
            return Check("android:mtp", WARN,
                         "libmtp detection present, no FUSE backend",
                         "Install jmtpfs: sudo apt install jmtpfs "
                         "(USB stick import still works).")
        return Check("android:mtp", WARN, "no MTP backend installed",
                     "Android File Transfer import needs jmtpfs or GVfs; "
                     "install with: sudo apt install jmtpfs mtp-tools")
    backend = "jmtpfs" if tools["jmtpfs"] else "GVfs/GIO"
    if not fuse_device:
        return Check("android:mtp", WARN, f"{backend} present but /dev/fuse is missing",
                     "Load the fuse module (sudo modprobe fuse) or boot the "
                     "appliance kernel with FUSE enabled; USB stick import still works.")
    if not fuse:
        return Check("android:mtp", WARN, f"{backend} present but fusermount is missing",
                     "Install fuse3/fuse: sudo apt install fuse3")
    summary = f"{backend} backend ready (jmtpfs={int(tools['jmtpfs'])}, "
    summary += f"mtp-tools={int(tools['mtp-detect'])}, fusermount=yes)"
    return Check("android:mtp", PASS, summary)


def check_native_hotkeys() -> Check:
    """Save states/cheats need mGBA's native hotkeys to be deliverable."""
    if os.path.exists("/dev/uinput"):
        return Check("native:hotkeys", PASS, "/dev/uinput available")
    if os.environ.get("DISPLAY") and shutil.which("xdotool"):
        return Check("native:hotkeys", PASS, "xdotool available on X11")
    return Check("native:hotkeys", WARN,
                 "no /dev/uinput and no xdotool on X11",
                 "In-game save states, overlays and fast-forward will open but "
                 "native capture/load cannot run; add uinput access or xdotool.")


def mtp_report(attempt_mount: bool = True) -> str:
    """Full MTP detection-chain report for `gba-selftest --mtp`.

    `attempt_mount` is what makes this useful on hardware: it really runs
    `jmtpfs -l`/`mtp-detect`/`gio mount -l` and, if a phone is visible,
    attempts the same auto-mount the watcher would.
    """
    try:
        import gba_usb
    except Exception as exc:  # pragma: no cover - broken install
        return f"MTP diagnostics unavailable: {exc}"
    try:
        return gba_usb.mtp_diagnostics(auto_mount=attempt_mount)
    except Exception as exc:  # pragma: no cover - diagnostics never fatal
        return f"MTP diagnostics failed: {exc}"


def run_all(src_hint: str, data_dir: str, games_dir: str) -> List[Check]:
    try:
        _import(src_hint)
    except Exception as exc:
        return [Check("import", FAIL, f"{type(exc).__name__}: {exc}",
                      "The shell's src directory is missing from this install.")]

    checks: List[Check] = [check_python(), check_fonts(), check_pty(),
                           check_display(), check_mgba(), check_mtp(),
                           check_native_hotkeys()]
    checks.extend(check_modules())
    checks.extend(check_pygame())
    checks.extend(check_storage(data_dir, games_dir))
    checks.append(check_config_roundtrip(data_dir))
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="GBA OS environment self-test")
    parser.add_argument("--installed", action="store_true",
                        help="check the installed tree (/opt/gba-distro)")
    parser.add_argument("--src", default=os.environ.get("GBA_SRC", ""),
                        help="path to the shell's src directory")
    parser.add_argument("--data-dir", default=os.environ.get("GBA_DATA_DIR",
                                                             os.environ.get("DATA_DIR", "/data")))
    parser.add_argument("--games-dir", default=os.environ.get("GBA_GAMES_DIR",
                                                              os.environ.get("GAMES_DIR", "/games")))
    parser.add_argument("--mtp", action="store_true",
                        help="report the Android MTP detection chain "
                             "(sysfs, libmtp, GVfs, FUSE mount root) and exit")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="only print problems")
    args = parser.parse_args()

    if args.installed and not args.src:
        args.src = os.environ.get("GBA_INSTALL_DIR", "/opt/gba-distro")
        args.src = os.path.join(args.src, "src")

    if args.mtp:
        try:
            _import(args.src)
        except Exception as exc:
            print(f"cannot import the shell: {exc}")
            return 2
        print(mtp_report())
        return 0

    checks = run_all(args.src, args.data_dir, args.games_dir)
    failures = [c for c in checks if c.status == FAIL]
    warnings = [c for c in checks if c.status == WARN]

    if args.json:
        print(json.dumps({
            "ok": not failures,
            "summary": {"pass": len(checks) - len(failures) - len(warnings),
                        "warn": len(warnings), "fail": len(failures)},
            "checks": [c.as_dict() for c in checks],
        }, indent=2))
        return 1 if failures else 0

    if not args.quiet:
        print("GBA OS self-test")
        print("=" * 64)
        for check in checks:
            if args.quiet and check.status == PASS:
                continue
            print(f"  [{OK_MARK[check.status]}] {check.name:<22} {check.detail}")
            if check.hint and check.status != PASS:
                print(f"           -> {check.hint}")
        print("-" * 64)
        print(f"  {len(checks) - len(failures) - len(warnings)} passed, "
              f"{len(warnings)} warning(s), {len(failures)} failure(s)")
    elif failures or warnings:
        for check in failures + warnings:
            print(f"[{OK_MARK[check.status]}] {check.name}: {check.detail}")

    if failures:
        print("\nSome checks failed. The shell still starts, but games may not.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
