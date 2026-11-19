# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

"""Where reports are written.

In order of preference:
1. ``$CTS_REPORT_DIR`` if set.
2. A partition labelled CTS_REPORTS (e.g. on the boot stick or a second stick).
3. The Ventoy data partition (label "Ventoy") when booted from a Ventoy stick.
   Ventoy keeps that partition busy while a Linux ISO runs, so mounting it only
   works with ``VTOY_LINUX_REMOUNT`` enabled in ``ventoy/ventoy.json``.
4. /root/cts-reports, which lives in RAM on the live system (lost at power-off).

Reports always go into a ``cts-reports/`` folder, next to whatever else is on
the partition; nothing else on it is touched.
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

from . import proc

REPORT_LABEL = "CTS_REPORTS"
VENTOY_LABEL = "Ventoy"
MOUNT_ROOT = Path("/mnt")

# Set by reports_base() when the RAM fallback is used, to explain why.
fallback_hint = ""


def _mounted_at(device: str) -> str | None:
    out = proc.capture(["findmnt", "-n", "-o", "TARGET", "--source", device]).strip()
    return out.splitlines()[0] if out else None


def devices_with_label(lsblk_json: str, label: str) -> list[str]:
    """All block devices carrying ``label``, device-mapper devices first.

    When Ventoy boots a Linux ISO with VTOY_LINUX_REMOUNT, the real partition
    (e.g. /dev/sda1) is held by device-mapper and cannot be mounted, but Ventoy
    exposes the same filesystem as a dm device (/dev/mapper/sda1). Both carry
    the label, and /dev/disk/by-label may point at either one.
    """
    try:
        data = json.loads(lsblk_json)
    except json.JSONDecodeError:
        return []
    found: list[tuple[int, str]] = []

    def walk(node: dict) -> None:
        if node.get("label") == label and node.get("path"):
            found.append((0 if node.get("type") == "dm" else 1, node["path"]))
        for child in node.get("children") or []:
            walk(child)

    for dev in data.get("blockdevices", []):
        walk(dev)
    seen: set[str] = set()
    out = []
    for _, path in sorted(found):
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _first_error_line(output: str, fallback: str) -> str:
    for line in output.strip().splitlines():
        if line.strip() and not line.lstrip().startswith("dmesg(1)"):
            return line.strip()
    return fallback


def _mount_by_label(label: str) -> tuple[Path | None, str]:
    """Find the filesystem with ``label``, mounting it if needed.

    Returns (mount point or None, error text). Error text is '' when the label does not exist.
    """
    devices = devices_with_label(proc.capture(["lsblk", "-J", "-o", "PATH,TYPE,LABEL"]), label)
    if not devices:
        by_label = Path("/dev/disk/by-label") / label
        if not by_label.exists():
            return None, ""
        devices = [str(by_label.resolve())]
    for dev in devices:
        target = _mounted_at(dev)
        if target:
            return Path(target), ""
    if os.geteuid() != 0:
        return None, "not root, cannot mount"
    mp = MOUNT_ROOT / label.lower()
    mp.mkdir(parents=True, exist_ok=True)
    errors = []
    for dev in devices:
        r = proc.run(["mount", dev, str(mp)], timeout=30)
        if r.returncode == 0:
            return mp, ""
        errors.append(f"{dev}: {_first_error_line(r.output, f'mount exit {r.returncode}')}")
    return None, "; ".join(errors)


def reports_base() -> tuple[Path, bool]:
    """Return (directory, persistent)."""
    global fallback_hint
    fallback_hint = ""
    env = os.environ.get("CTS_REPORT_DIR")
    if env:
        return Path(env), True

    mp, _ = _mount_by_label(REPORT_LABEL)
    if mp:
        return mp / "cts-reports", True

    mp, err = _mount_by_label(VENTOY_LABEL)
    if mp:
        return mp / "cts-reports", True
    if err:
        fallback_hint = (
            f"the Ventoy partition could not be mounted ({err}). To save reports on the Ventoy stick, "
            'add {"control": [{"VTOY_LINUX_REMOUNT": "1"}]} to ventoy/ventoy.json on the stick; see README'
        )

    if os.geteuid() == 0:
        live = Path("/run/archiso").exists()
        return Path("/root/cts-reports"), not live
    return Path.home() / "cts-reports", True


def new_run_dir() -> tuple[Path, bool]:
    base, persistent = reports_base()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    d = base / f"{stamp}-{socket.gethostname()}"
    d.mkdir(parents=True, exist_ok=True)
    return d, persistent


def latest_report() -> Path | None:
    base, _ = reports_base()
    runs = sorted(base.glob("*/report.json")) if base.is_dir() else []
    return runs[-1] if runs else None
