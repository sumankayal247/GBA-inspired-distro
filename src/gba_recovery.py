#!/usr/bin/env python3
"""
GBA OS Text-Based Recovery Console.
Implements Rules F1, F2, SV6 from CONTRACT.md.
Provides a foolproof text console interface for troubleshooting and recovery.
"""

import os
import sys
import shutil
import time

def clear_screen():
    os.system("clear")

def print_header():
    print("=" * 60)
    print("        GBA OS — SYSTEM RECOVERY ENVIRONMENT")
    print("=" * 60)
    print("System has entered recovery mode (Rule F1, F2).")
    print("No GUI or graphics drivers are required here.")
    print("-" * 60)

def main():
    while True:
        clear_screen()
        print_header()
        print(" [1] Continue Boot (Try launching GBA OS again)")
        print(" [2] View System / Supervisor Logs")
        print(" [3] Reset Disposable Library Cache (/data/library/)")
        print(" [4] Backup Saves to External / Home directory")
        print(" [5] Open Emergency Root Shell (/bin/bash)")
        print(" [6] Reboot System")
        print(" [7] Power Off Machine")
        print("-" * 60)
        choice = input("Select an option [1-7]: ").strip()

        if choice == "1":
            print("\nReturning to supervisor...")
            sys.exit(0)

        elif choice == "2":
            clear_screen()
            print("--- RECENT SYSTEM LOGS ---")
            log_path = "/data/logs/supervisor.log"
            if os.path.exists(log_path):
                with open(log_path, "r", errors="ignore") as f:
                    lines = f.readlines()
                    print("".join(lines[-30:]))
            else:
                print("No log file found at", log_path)
            input("\nPress ENTER to return to menu...")

        elif choice == "3":
            print("\nResetting library cache...")
            cache_dir = "/data/library"
            if os.path.exists(cache_dir):
                shutil.rmtree(cache_dir, ignore_errors=True)
                os.makedirs(cache_dir, exist_ok=True)
                print("Cache cleared! The library will re-index content on next launch.")
            else:
                print("Cache directory not present.")
            time.sleep(1.5)

        elif choice == "4":
            print("\nExporting saves to /tmp/gba_saves_backup.tar.gz...")
            saves_dir = "/data/saves"
            if os.path.exists(saves_dir):
                out = shutil.make_archive("/tmp/gba_saves_backup", "gztar", saves_dir)
                print(f"Saves successfully archived to: {out}")
            else:
                print("No saves found in /data/saves.")
            input("\nPress ENTER to return to menu...")

        elif choice == "5":
            print("\nSpawning emergency shell. Type 'exit' to return to recovery menu.\n")
            os.system("/bin/bash")

        elif choice == "6":
            print("\nRebooting system...")
            os.system("reboot")
            break

        elif choice == "7":
            print("\nPowering off...")
            os.system("poweroff")
            break

if __name__ == "__main__":
    main()
