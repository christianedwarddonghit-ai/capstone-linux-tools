#!/usr/bin/env python3
# diagnose_offline_drive_cli.py
# Workstream A (bootable Debian live environment) ONLY.
#
# Quick command-line front-end for linux_drive_diagnostics.py, so this can
# be tested against a real broken PC's drive from inside the live Debian
# terminal RIGHT NOW, before any GUI/PyQt integration exists for it.
#
# Usage (run as root, or via sudo):
#   python3 diagnose_offline_drive_cli.py            # lists drives, asks which one
#   python3 diagnose_offline_drive_cli.py /dev/sda    # diagnoses that drive directly

import os
import sys

from linux_drive_diagnostics import list_physical_drives, run_full_drive_diagnosis

STATUS_LABELS = {
    "pass": "[ OK ]",
    "warning": "[WARN]",
    "fail": "[FAIL]",
    "skipped": "[SKIP]",
}


def main():
    if os.geteuid() != 0:
        print("This needs to run as root (smartctl and mount both require it). Try: sudo python3 diagnose_offline_drive_cli.py")
        sys.exit(1)

    target = sys.argv[1] if len(sys.argv) > 1 else None

    if not target:
        drives = list_physical_drives()
        if not drives:
            print("No physical drives detected at all. Check the drive is connected, or try a different SATA/USB port/cable.")
            sys.exit(1)

        print("Detected drives:\n")
        for i, drive in enumerate(drives):
            print(f"  [{i}] {drive['device']}  {drive['size']}  {drive['model']}  ({drive['transport']})")

        choice = input("\nWhich drive is the one to diagnose? Enter number: ").strip()
        try:
            target = drives[int(choice)]["device"]
        except (ValueError, IndexError):
            print("Invalid selection.")
            sys.exit(1)

    print(f"\nDiagnosing {target} — this can take a minute or two...\n")

    def on_check(result):
        label = STATUS_LABELS.get(result["status"], "[ ?? ]")
        print(f"{label} [{result['check']}] {result['message']}")

    run_full_drive_diagnosis(target, on_check=on_check)

    print("\nDone.")


if __name__ == "__main__":
    main()
