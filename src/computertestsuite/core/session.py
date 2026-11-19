# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

"""One test run from start to saved report, shared by the CLI and the terminal UI."""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import __version__
from . import report, storage_target, sysinfo
from .base import StressTest
from .context import RunContext
from .model import Status, TestResult, overall
from .runner import RunListener, Runner


@dataclass
class Session:
    ctx: RunContext
    tests: list[StressTest]
    run_dir: Path
    persistent: bool
    fallback_hint: str = ""
    #: test name -> skip reason (None = will run)
    availability: dict[str, str | None] = field(default_factory=dict)
    #: test name -> estimated seconds (0 for tests that will be skipped)
    estimates: dict[str, int] = field(default_factory=dict)
    runner: Runner | None = None
    report: dict[str, Any] | None = None
    report_txt: Path | None = None

    @property
    def total_estimate(self) -> int:
        return sum(self.estimates.values())


def prepare(ctx: RunContext, tests: list[StressTest]) -> Session:
    """Create the report directory and work out what will run and for how long."""
    run_dir, persistent = storage_target.new_run_dir()
    ctx.log_dir = run_dir / "logs"
    s = Session(ctx, tests, run_dir, persistent, storage_target.fallback_hint)
    for t in tests:
        reason = t.available(ctx)
        s.availability[t.name] = reason
        s.estimates[t.name] = t.estimate(ctx) if reason is None else 0
    return s


def execute(s: Session, listener: RunListener | None = None, install_signal_handler: bool = True) -> Status:
    """Run the tests, write report.json/report.txt, return the overall verdict."""
    info = sysinfo.collect()
    started = time.time()
    s.runner = Runner(s.ctx, s.tests, listener=listener)
    results: list[TestResult] = s.runner.run(install_signal_handler=install_signal_handler)
    meta = {
        "version": __version__,
        "profile": s.ctx.profile.name,
        "test_seconds": s.ctx.seconds,
        "started": started,
        "finished": time.time(),
        "aborted": s.runner.aborted,
        "root": s.ctx.is_root,
        "kmsg_available": s.runner.monitor.kmsg_available,
        "args": sys.argv[1:],
    }
    s.report = report.build(results, info, meta)
    _, s.report_txt = report.write(s.run_dir, s.report)
    os.sync()  # reports may be on a USB stick that gets pulled right after
    return overall([r for r in results if r.status != Status.SKIP])
