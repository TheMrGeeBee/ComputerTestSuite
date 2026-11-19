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


def _mount_by_label(label: str) -> tuple[Path | None, str]:
    """Find the partition with ``label``, mounting it if needed.

    Returns (mount point or None, error text). Error text is '' when the label does not exist.
    """
    dev = Path("/dev/disk/by-label") / label
    if not dev.exists():
        return None, ""
    real = str(dev.resolve())
    target = _mounted_at(real)
    if target:
        return Path(target), ""
    if os.geteuid() != 0:
        return None, "not root, cannot mount"
    mp = MOUNT_ROOT / label.lower()
    mp.mkdir(parents=True, exist_ok=True)
    r = proc.run(["mount", real, str(mp)], timeout=30)
    if r.returncode != 0:
        return None, r.output.strip().splitlines()[-1] if r.output.strip() else f"mount exit {r.returncode}"
    return mp, ""


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
