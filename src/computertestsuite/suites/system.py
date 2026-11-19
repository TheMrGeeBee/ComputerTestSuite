# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

"""Combined full-system load: CPU + RAM + disks + GPU together.

Pulls maximum power through the PSU and VRMs and heats the whole case, which
exposes marginal power delivery and cooling that single-subsystem tests miss.
"""

from __future__ import annotations

import os
import threading

from ..core import proc
from ..core.base import StressTest
from ..core.context import RunContext
from ..core.model import Status, TestResult
from . import _stressng
from .gpu import GLMARK2, accelerated_cards
from .storage import _targets


class SystemBurnin(StressTest):
    name = "system"
    subsystem = "system"
    description = "Everything at once: CPU + memory + disk reads + GPU (PSU / thermal / VRM stability)"
    required_tools = ["stress-ng"]
    order = 90

    def estimate(self, ctx: RunContext) -> int:
        return ctx.duration or ctx.profile.system_seconds

    def run(self, ctx: RunContext, result: TestResult) -> None:
        secs = self.estimate(ctx)
        log = ctx.log_path(self.name)
        cpus = os.cpu_count() or 1
        jobs: list[tuple[str, threading.Thread]] = []
        outcomes: dict[str, proc.ProcResult] = {}

        def bg(label: str, cmd: list[str], timeout: float) -> None:
            def target() -> None:
                outcomes[label] = proc.run(cmd, timeout=timeout, log=log.with_name(f"{self.name}.{label}.log"))
            t = threading.Thread(target=target, daemon=True)
            jobs.append((label, t))

        # Disk read load (read-only, needs root for raw device access)
        if ctx.is_root and proc.have("fio"):
            for d in _targets(ctx):
                bg(f"fio-{d.name}", ["fio", f"--filename={d.path}", "--readonly", "--direct=1",
                                    "--ioengine=libaio", "--rw=randread", "--bs=64k", "--iodepth=16",
                                    f"--runtime={secs}", "--time_based=1", "--name=" + d.name,
                                    "--output-format=json"], secs + 120)
        # GPU load
        gl = next((t for t in GLMARK2 if proc.have(t)), None)
        if ctx.is_root and gl and accelerated_cards():
            bg("gpu", [gl, "--off-screen", "--run-forever"], secs)

        for _, t in jobs:
            t.start()
        # CPU + memory in the foreground, leaving one thread for the GPU/disk feeders.
        workers = max(1, cpus - 1)
        _stressng.run(["--cpu", str(workers), "--cpu-method", "all",
                       "--vm", str(max(1, min(4, cpus // 2))), "--vm-bytes", "50%",
                       "--vm-method", "all"], secs, result, log, "cpu+vm")
        for _, t in jobs:
            t.join(timeout=secs + 180)

        result.metrics["components"] = ", ".join(["cpu", "memory"] + [label for label, _ in jobs])
        for label, r in outcomes.items():
            if r.aborted:
                continue
            if label == "gpu":
                if not r.timed_out:
                    result.add(Status.FAIL, f"gpu load stopped early (exit {r.returncode})")
            elif r.returncode != 0 or r.timed_out:
                result.add(Status.FAIL, f"{label}: exit {r.returncode}{' (hung)' if r.timed_out else ''}")
