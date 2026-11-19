# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

"""RAM reliability: memtester pattern tests plus stress-ng VM verification."""

from __future__ import annotations

import os
import re
import threading
import time

from ..core import proc
from ..core.base import StressTest
from ..core.context import RunContext
from ..core.model import Status, TestResult
from ..core.sysinfo import meminfo
from . import _stressng

MIB = 2**20


def memtester_plan(available: int, cpus: int) -> tuple[int, int]:
    """Return (instances, MiB per instance) leaving headroom for the OS."""
    margin = max(384 * MIB, int(available * 0.08))
    usable = max(0, available - margin)
    instances = max(1, min(cpus, 4))
    per = usable // instances // MIB
    return instances, per


_FAILURE = re.compile(r"FAILURE:.*", re.I)


def parse_memtester(output: str) -> list[str]:
    clean = output.replace("\b", "")
    return [m.group(0).strip() for m in _FAILURE.finditer(clean)]


class MemoryPattern(StressTest):
    name = "memory-pattern"
    subsystem = "memory"
    description = "memtester bit-pattern tests over (almost) all free RAM"
    required_tools = ["memtester"]
    order = 30

    def run(self, ctx: RunContext, result: TestResult) -> None:
        if not ctx.is_root:
            result.add(Status.WARN, "not root: memtester cannot lock memory, pages may be swapped")
        avail = meminfo().get("MemAvailable", 0)
        n, per_mib = memtester_plan(avail, os.cpu_count() or 1)
        if per_mib < 16:
            result.add(Status.SKIP, "not enough free memory to test")
            return
        result.metrics["tested_mib"] = n * per_mib
        result.metrics["instances"] = n
        log = ctx.log_path(self.name)
        loops = ctx.profile.memory_loops
        outputs: list[proc.ProcResult] = []

        def worker(i: int) -> None:
            outputs.append(proc.run(["memtester", f"{per_mib}M", str(loops)], timeout=ctx.seconds,
                                    log=log.with_name(f"{self.name}.{i}.log")))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        t0 = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        result.metrics["seconds"] = round(time.monotonic() - t0)

        completed = 0
        for r in outputs:
            for f in parse_memtester(r.output)[:10]:
                result.add(Status.FAIL, f"memtester: {f}")
            completed += len(re.findall(r"^Loop \d+", r.output, re.M))
            if r.returncode not in (0, None) and not (r.timed_out or r.aborted):
                # memtester exit bits: 1=error alloc/lock, 2=stuck address, 4=other test failed
                status = Status.FAIL if r.returncode & 0x6 else Status.ERROR
                result.add(status, f"memtester exited with {r.returncode}")
        if any(r.timed_out for r in outputs):
            result.metrics["note"] = "time limit reached before all loops completed (partial coverage)"
        result.metrics["loops_started"] = completed


class MemoryStress(StressTest):
    name = "memory-stress"
    subsystem = "memory"
    description = "stress-ng VM stressors with all access methods and data verification"
    required_tools = ["stress-ng"]
    order = 31

    def run(self, ctx: RunContext, result: TestResult) -> None:
        workers = str(max(1, min(os.cpu_count() or 1, 8)))
        info = _stressng.run(
            ["--vm", workers, "--vm-bytes", "70%", "--vm-method", "all", "--vm-keep"],
            ctx.seconds, result, ctx.log_path(self.name), "vm",
        )
        for k, v in info["metrics"].items():
            result.metrics[f"{k}.bogo_ops_per_s"] = v["bogo_ops_per_s"]
