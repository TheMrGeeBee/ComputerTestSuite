# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

"""Run profiles: how long each test gets."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    test_seconds: int  # default duration for each time-boxed stress test
    memory_loops: int  # memtester loops
    surface_scan: bool  # full read-only surface scan of disks
    system_seconds: int  # combined full-system burn-in duration
    shared_disk_budget: bool = False  # storage-read: test_seconds is split across all disks


PROFILES: dict[str, Profile] = {
    "quick": Profile("quick", "Short check, about 15-20 minutes in total", 150, 1, False, 180, True),
    "standard": Profile("standard", "About 15 minutes per test, full disk read scan", 900, 2, True, 1800),
    "burnin": Profile("burnin", "Endurance: 1 hour per test, 4 hours combined load", 3600, 4, True, 14400),
}


_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> int:
    """Parse '90', '90s', '15m', '2h', '1h30m' into seconds."""
    text = text.strip().lower()
    if text.isdigit():
        return int(text)
    parts = re.findall(r"(\d+)\s*([smhd])", text)
    if not parts or "".join(n + u for n, u in parts) != re.sub(r"\s+", "", text):
        raise ValueError(f"invalid duration: {text!r} (use e.g. 90s, 15m, 2h, 1h30m)")
    return sum(int(n) * _UNITS[u] for n, u in parts)


def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"
