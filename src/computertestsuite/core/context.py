# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

"""Per-run settings handed to every test."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .profiles import Profile, PROFILES


@dataclass
class RunContext:
    profile: Profile = field(default_factory=lambda: PROFILES["quick"])
    duration: int | None = None  # per-test override, seconds
    destructive: bool = False
    devices: list[str] = field(default_factory=list)  # explicit disk targets
    iperf_server: str | None = None
    log_dir: Path = Path("/tmp/cts")
    verbose: bool = False
    assume_yes_destructive: bool = False  # only for tests; never set from CLI

    @property
    def seconds(self) -> int:
        return self.duration if self.duration is not None else self.profile.test_seconds

    @property
    def is_root(self) -> bool:
        return os.geteuid() == 0

    def log_path(self, name: str) -> Path:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        return self.log_dir / f"{name}.log"
