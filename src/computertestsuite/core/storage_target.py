# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

"""Where reports are written.

If a partition labelled CTS_REPORTS exists (usually created on the free space
of the boot USB stick), reports go there so they survive a reboot. Otherwise
they go to /root/cts-reports, which lives in RAM on the live system.
"""

from __future__ import annotations

import os
import socket
import time
from pathlib import Path

from . import proc

REPORT_LABEL = "CTS_REPORTS"
MOUNT_POINT = Path("/mnt/cts-reports")


def _mounted_at(device: str) -> str | None:
    out = proc.capture(["findmnt", "-n", "-o", "TARGET", "--source", device]).strip()
    return out.splitlines()[0] if out else None


def reports_base() -> tuple[Path, bool]:
    """Return (directory, persistent)."""
    env = os.environ.get("CTS_REPORT_DIR")
    if env:
        return Path(env), True
    dev = Path("/dev/disk/by-label") / REPORT_LABEL
    if dev.exists():
        real = str(dev.resolve())
        target = _mounted_at(real)
        if target is None and os.geteuid() == 0:
            MOUNT_POINT.mkdir(parents=True, exist_ok=True)
            if proc.run(["mount", real, str(MOUNT_POINT)], timeout=30).returncode == 0:
                target = str(MOUNT_POINT)
        if target:
            return Path(target) / "cts-reports", True
    if os.geteuid() == 0:
        return Path("/root/cts-reports"), Path("/run/archiso").exists() is False
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
