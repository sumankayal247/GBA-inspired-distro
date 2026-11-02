#!/usr/bin/env python3
"""
GBA OS Text-Based Recovery Console.

Implements Rules F1, F2, SV6 from CONTRACT.md.
Runs on the kernel console with no GPU userland, no graphics drivers and no
display server. Every destructive action requires an explicit confirmation.
"""

import os
import sys
import glob
import time
import shutil
import subprocess

# Ensure local src directory is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from common import (
        get_data_dir,
        get_games_dir,
        get_logs_dir,
        get_base_dir,
        SYS_CONTRACT_VERSION,
    )
except Exception:  # pragma: no cover - recovery must survive a broken tree
    def get_data_dir():
        return "/data" if os.path.isdir("/data") else os.path.expanduser("~/gba-data")

    def get_games_dir():
        return "/games" if os.path.isdir("/games") else os.path.expanduser("~/gba-games")

    def get_logs_dir():
        return os.path.join(get_data_dir(), "logs")

    def get_base_dir():
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    SYS_CONTRACT_VERSION = 1


SAVES_DIR = os.path.join(get_data_dir(), "saves")
LIBRARY_DIR = os.path.join(get_data_dir(), "library")
EXPORT_DIR = os.environ.get("GBA_RECOVERY_EXPORT", "/tmp")


def clear_screen():
    os.system("clear")


def ask_confirm(prompt: str) -> bool:
    """Explicit confirmation for destructive actions (F2, SV6)."""
    print()
    print(f"  !! {prompt}")
    answer = input("  Type 'yes' to continue, anything else to cancel: ").strip().lower()
    return answer in ("yes", "y")


def newest_log() -> str:
    """Returns the newest session log, following the latest.log symlink."""
    logs_dir = get_logs_dir()
    latest = os.path.join(logs_dir, "latest.log")
    if os.path.islink(latest) or os.path.isfile(latest):
        try:
            target = os.path.realpath(latest)
            if os.path.isfile(target):
                return target
        except Exception:
            pass
    candidates = sorted(glob.glob(os.path.join(logs_dir, "boot_*.log*")),
                        key=lambda p: os.path.getmtime(p), reverse=True)
    return candidates[0] if candidates else ""


def print_header():
    print("=" * 68)
    print("            GBA OS - SYSTEM RECOVERY ENVIRONMENT")
    print("=" * 68)
    print(f"  Contract v{SYS_CONTRACT_VERSION}   DATA: {get_data_dir()}")
    print("  Recovery is text-only: no GPU, display server or input daemon needed.")
    print("-" * 68)


def show_logs():
    clear_screen()
    path = newest_log()
    print(f"--- LOG: {path or '(no session log found)'} ---")
    if path:
        try:
            with open(path, "r", errors="ignore") as f:
                lines = f.readlines()
            for line in lines[-40:]:
                print(line.rstrip())
            errors = [l for l in lines if "[ERROR]" in l or "[CRITICAL]" in l]
            print("-" * 68)
            print(f"{len(lines)} lines, {len(errors)} error/critical entries")
        except Exception as exc:
            print(f"Could not read log: {exc}")
    input("\nPress ENTER to return to the menu...")


def reset_library_cache():
    """Rule ST2: /data/library is disposable, so this is safe."""
    clear_screen()
    print("--- RESET LIBRARY CACHE ---")
    print("Only the disposable index is removed. ROMs and saves are untouched.")
    print(f"Path: {LIBRARY_DIR}")
    if ask_confirm("Clear the library cache?"):
        shutil.rmtree(LIBRARY_DIR, ignore_errors=True)
        os.makedirs(LIBRARY_DIR, exist_ok=True)
        print("Library cache cleared; it will be rebuilt on next launch.")
    else:
        print("Cancelled.")
    time.sleep(1.2)


def export_saves():
    """F2: export readable data before anything destructive."""
    clear_screen()
    print("--- EXPORT SAVE DATA ---")
    print(f"Source: {SAVES_DIR}")
    if not os.path.isdir(SAVES_DIR) or not os.listdir(SAVES_DIR):
        print("No saves found.")
        input("\nPress ENTER to return to the menu...")
        return
    os.makedirs(EXPORT_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = os.path.join(EXPORT_DIR, f"gba_saves_{stamp}")
    try:
        archive = shutil.make_archive(target, "gztar", SAVES_DIR)
        size_mb = os.path.getsize(archive) / (1024 * 1024)
        print(f"\nSaves archived to: {archive}  ({size_mb:.2f} MB)")
        print("Copy this file off the machine before resetting data.")
    except Exception as exc:
        print(f"Export failed: {exc}")
    input("\nPress ENTER to return to the menu...")


def system_info():
    clear_screen()
    print("--- SYSTEM INFORMATION ---")
    mem = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, value = line.partition(":")
                mem[key] = int(value.split()[0]) // 1024
        print(f"Memory: {mem.get('MemAvailable', '?')} MB available of {mem.get('MemTotal', '?')} MB")
    except Exception:
        print("Memory: unavailable")
    for label, path in (("DATA", get_data_dir()), ("GAMES", get_games_dir())):
        try:
            usage = shutil.disk_usage(path)
            print(f"{label}: {usage.free / 1024 ** 3:.1f} GB free of "
                  f"{usage.total / 1024 ** 3:.1f} GB  ({path})")
        except Exception:
            print(f"{label}: not mounted ({path})")
    try:
        with open("/proc/uptime") as f:
            print(f"Uptime: {float(f.read().split()[0]) / 60:.1f} min")
    except Exception:
        pass
    roms = 0
    try:
        for entry in os.listdir(get_games_dir()):
            if entry.lower().endswith((".gba", ".gbc", ".gb")):
                roms += 1
    except Exception:
        pass
    print(f"ROMs visible: {roms}")
    gamepads = len(glob.glob("/dev/input/js*"))
    print(f"Gamepads: {gamepads}")
    input("\nPress ENTER to return to the menu...")


def main():
    while True:
        clear_screen()
        print_header()
        print("  [1] Continue boot (return to the GBA OS shell)")
        print("  [2] View recent system / supervisor logs")
        print("  [3] System information (memory, storage, devices)")
        print("  [4] Export save data to an archive")
        print("  [5] Reset disposable library cache")
        print("  [6] Open an emergency root shell")
        print("  [7] Reboot machine")
        print("  [8] Power off machine")
        print("-" * 68)
        try:
            choice = input("  Select an option [1-8]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if choice == "1":
            print("\nReturning to the supervisor...")
            return

        elif choice == "2":
            show_logs()

        elif choice == "3":
            system_info()

        elif choice == "4":
            export_saves()

        elif choice == "5":
            reset_library_cache()

        elif choice == "6":
            print("\nSpawning emergency shell. Type 'exit' to return.\n")
            subprocess.call([os.environ.get("SHELL", "/bin/bash")])

        elif choice == "7":
            if ask_confirm("Reboot the machine now?"):
                os.sync()
                os.system("reboot")
                return
            print("Cancelled.")
            time.sleep(0.8)

        elif choice == "8":
            if ask_confirm("Power off the machine now?"):
                os.sync()
                os.system("poweroff")
                return
            print("Cancelled.")
            time.sleep(0.8)

        else:
            print("Unknown option.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
