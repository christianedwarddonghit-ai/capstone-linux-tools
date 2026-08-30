# linux_drive_diagnostics.py
# Workstream A (bootable Debian live environment) ONLY — do not import this
# from the Windows-side app (diagnostics.py / ai_agent.py / screens/*).
# Requires: smartmontools (smartctl), util-linux (lsblk, blkid), ntfs-3g,
# and root privileges (run as root, or the live user needs passwordless sudo).
#
# Purpose: diagnose a PC that won't boot at all ("No bootable device found")
# by booting THIS tool from USB instead, then inspecting the target machine's
# internal drive(s) as offline/unmounted volumes. Answers three questions:
#   1. Is the drive itself failing (SMART health)?
#   2. Is there a Windows installation on any partition, and if so, does it
#      look intact or corrupted?
#   3. Is there a valid boot configuration at all (why POST can't find one)?
#
# NOTE ON PRIVILEGES: smartctl and mount both need root. All subprocess calls
# below assume this script is already running as root (typical for a live
# boot environment). If not, prefix commands with sudo -- not done here to
# keep this a pure diagnostics library with no interactive sudo prompts.

import json
import os
import subprocess
import tempfile


def _run(cmd, timeout=30):
    """Small subprocess wrapper — never raises, always returns a CompletedProcess-like result."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, stdout="", stderr=f"{cmd[0]} not found — is it installed?")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, stdout="", stderr="Command timed out.")
    except Exception as e:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Step 1: enumerate physical drives
# ─────────────────────────────────────────────────────────────────────────────

def list_physical_drives():
    """
    Returns a list of {device, size, model, transport} dicts for every
    physical disk detected (excludes the live-boot USB itself is NOT done
    automatically here — the caller/UI should let the user confirm which
    drive is the target, since we can't reliably tell "the USB we booted
    from" apart from another USB drive without more context).
    """
    result = _run(["lsblk", "-J", "-o", "NAME,SIZE,MODEL,TRAN,TYPE"])
    if result.returncode != 0:
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    drives = []
    for entry in data.get("blockdevices", []):
        if entry.get("type") == "disk":
            drives.append({
                "device": f"/dev/{entry['name']}",
                "size": entry.get("size", "unknown"),
                "model": (entry.get("model") or "Unknown model").strip(),
                "transport": entry.get("tran", "unknown"),
            })
    return drives


# ─────────────────────────────────────────────────────────────────────────────
# Step 2: SMART health — is the drive itself failing?
# ─────────────────────────────────────────────────────────────────────────────

def check_drive_smart_health(device):
    """
    Runs a SMART health check on a physical drive (e.g. "/dev/sda").
    Returns {"check": "SMART Health", "device": ..., "status": ..., "message": ...}

    status meanings:
      pass    — SMART reports the drive as healthy
      fail    — SMART reports the drive has failed or is failing (PRE-FAIL
                attributes), i.e. this is very likely the actual cause of
                "no bootable device" — replace the drive
      warning — SMART is unsupported/unavailable on this device (common on
                some USB-to-SATA bridges), so we genuinely can't tell
      skipped — smartctl itself isn't installed
    """
    check_result = _run(["smartctl", "-H", device])

    if check_result.returncode == 127:
        return {"check": "SMART Health", "device": device, "status": "skipped",
                "message": "smartctl not installed — add smartmontools to the live-build package list."}

    output = (check_result.stdout + check_result.stderr).lower()

    if "smart overall-health self-assessment test result: passed" in output:
        status, message = "pass", "SMART self-check: PASSED — drive hardware reports healthy."
    elif "failed" in output and "self-assessment" in output:
        status, message = "fail", "SMART self-check: FAILED — this drive is reporting hardware failure. Back up any recoverable data and replace it."
    elif "smart support is" in output and "unavailable" in output:
        status, message = "warning", "SMART not supported on this device (common for some USB/RAID bridges) — health could not be determined this way."
    else:
        status, message = "warning", "Could not get a clear SMART health result. Attempting a deeper attribute check..."

    # Even if the pass/fail line was unclear, look at key wear/error
    # attributes directly — a drive can still be dying with borderline
    # SMART firmware that doesn't flag itself as FAILED outright.
    attrs_result = _run(["smartctl", "-A", device])
    attrs_output = attrs_result.stdout.lower()
    concerning = []
    for attr_name in ("reallocated_sector_ct", "current_pending_sector", "offline_uncorrectable", "reallocated_event_count"):
        for line in attrs_output.splitlines():
            if attr_name in line:
                parts = line.split()
                if parts and parts[-1].isdigit() and int(parts[-1]) > 0:
                    concerning.append(f"{attr_name.replace('_', ' ').title()}: {parts[-1]}")

    if concerning and status == "pass":
        status = "warning"
        message = "SMART overall result is PASSED, but concerning attributes were found: " + "; ".join(concerning) + ". Drive may be degrading."
    elif concerning:
        message += " Concerning attributes: " + "; ".join(concerning) + "."

    return {"check": "SMART Health", "device": device, "status": status, "message": message}


# ─────────────────────────────────────────────────────────────────────────────
# Step 3: does a Windows installation exist, and does it look intact?
# ─────────────────────────────────────────────────────────────────────────────

def _list_ntfs_partitions(device):
    """Returns partition device paths (e.g. /dev/sda2) on `device` that are NTFS."""
    result = _run(["lsblk", "-J", "-o", "NAME,FSTYPE", device])
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    partitions = []
    root_name = os.path.basename(device)
    for entry in data.get("blockdevices", []):
        if entry["name"] != root_name:
            continue
        for child in entry.get("children", []):
            if (child.get("fstype") or "").lower() == "ntfs":
                partitions.append(f"/dev/{child['name']}")
    return partitions


def detect_windows_installation(partition):
    """
    Mounts an NTFS partition READ-ONLY, checks for the presence and basic
    integrity of a Windows installation, then unmounts it again.

    Returns {"check": "Windows Installation", "partition": ..., "status": ...,
             "message": ...}

    status meanings:
      pass    — Windows found, key files present and look intact
      fail    — Windows found but key files are missing/corrupted
      warning — mount failed (could itself be a sign of filesystem
                corruption) or partition inconclusive
      skipped — no Windows folder on this partition at all (not necessarily
                a problem — could just be a data partition)
    """
    with tempfile.TemporaryDirectory(prefix="winmount_") as mount_point:
        mount_result = _run(["mount", "-t", "ntfs-3g", "-o", "ro", partition, mount_point], timeout=20)

        if mount_result.returncode != 0:
            return {
                "check": "Windows Installation", "partition": partition, "status": "warning",
                "message": f"Could not mount {partition} read-only: {mount_result.stderr.strip() or 'unknown error'}. "
                           "This itself can indicate a corrupted/damaged filesystem.",
            }

        try:
            windows_dir = os.path.join(mount_point, "Windows")
            system32_dir = os.path.join(windows_dir, "System32")
            sam_hive = os.path.join(system32_dir, "config", "SAM")
            system_hive = os.path.join(system32_dir, "config", "SYSTEM")
            ntoskrnl = os.path.join(system32_dir, "ntoskrnl.exe")
            bcd_legacy = os.path.join(mount_point, "Boot", "BCD")
            bootmgr = os.path.join(mount_point, "bootmgr")

            if not os.path.isdir(windows_dir):
                return {
                    "check": "Windows Installation", "partition": partition, "status": "skipped",
                    "message": f"No \\Windows folder on {partition} — likely a data partition, not an OS partition.",
                }

            missing = [name for name, path in {
                "System32": system32_dir,
                "ntoskrnl.exe (kernel)": ntoskrnl,
                "SYSTEM registry hive": system_hive,
                "SAM registry hive": sam_hive,
            }.items() if not os.path.exists(path)]

            has_boot_files = os.path.exists(bcd_legacy) or os.path.exists(bootmgr)

            if missing:
                return {
                    "check": "Windows Installation", "partition": partition, "status": "fail",
                    "message": f"Windows found on {partition} but corrupted — missing: {', '.join(missing)}.",
                }

            if not has_boot_files:
                return {
                    "check": "Windows Installation", "partition": partition, "status": "warning",
                    "message": f"Windows core files found on {partition} and look intact, but no boot files "
                               "(bootmgr/BCD) were found on this partition — boot files may be on a separate "
                               "EFI/System Reserved partition, which is normal on many setups.",
                }

            return {
                "check": "Windows Installation", "partition": partition, "status": "pass",
                "message": f"Windows found on {partition} — core system files and boot files both present and intact.",
            }
        finally:
            _run(["umount", mount_point], timeout=15)


# ─────────────────────────────────────────────────────────────────────────────
# Step 4: boot configuration — is there anything for firmware/POST to find?
# ─────────────────────────────────────────────────────────────────────────────

def check_boot_configuration(device):
    """
    Looks at the partition table itself for signs of why firmware (BIOS/UEFI)
    might report "No bootable device found": missing/invalid partition
    table, no EFI System Partition, or (legacy BIOS) no active/boot flag.
    """
    result = _run(["fdisk", "-l", device])
    output = result.stdout

    if result.returncode != 0 or not output.strip():
        return {"check": "Boot Configuration", "device": device, "status": "fail",
                "message": f"Could not read a partition table on {device} at all — "
                           "the partition table itself may be missing or corrupted."}

    has_esp = "efi system" in output.lower()
    has_boot_flag = " * " in output or output.strip().endswith("*") or "\n*" in output

    if has_esp:
        return {"check": "Boot Configuration", "device": device, "status": "pass",
                "message": f"{device}: EFI System Partition found — UEFI firmware should be able to find a bootloader here."}
    if has_boot_flag:
        return {"check": "Boot Configuration", "device": device, "status": "pass",
                "message": f"{device}: legacy BIOS boot/active flag found on a partition."}

    return {"check": "Boot Configuration", "device": device, "status": "fail",
            "message": f"{device}: no EFI System Partition and no active/boot flag found. "
                       "This is a likely cause of \"No bootable device found\" — the firmware has "
                       "nothing to point it at, even if Windows itself is otherwise intact."}


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration — run everything for one physical drive
# ─────────────────────────────────────────────────────────────────────────────

def run_full_drive_diagnosis(device, on_check=None):
    """
    Runs the full offline diagnosis on one physical drive: SMART health,
    boot configuration, and a Windows-installation check on every NTFS
    partition found. Mirrors diagnostics.py's run_all_checks() pattern —
    `on_check`, if given, is called with each result the instant it's ready,
    for live UI progress.

    Returns the full list of result dicts.
    """
    results = []

    def emit(result):
        results.append(result)
        if on_check:
            on_check(result)

    emit(check_drive_smart_health(device))
    emit(check_boot_configuration(device))

    ntfs_partitions = _list_ntfs_partitions(device)
    if not ntfs_partitions:
        emit({
            "check": "Windows Installation", "device": device, "status": "fail",
            "message": f"No NTFS partitions found on {device} at all — no Windows installation exists on this drive.",
        })
    else:
        for partition in ntfs_partitions:
            emit(detect_windows_installation(partition))

    return results
