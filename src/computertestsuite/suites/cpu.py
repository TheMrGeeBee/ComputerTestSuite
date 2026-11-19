# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

"""CPU stability: integer/float/bit ops with result verification, then FPU matrix math."""

from __future__ import annotations

from ..core.base import StressTest
from ..core.context import RunContext
from ..core.model import TestResult
from . import _stressng


class CpuStress(StressTest):
    name = "cpu"
    subsystem = "cpu"
    description = "All cores: every stress-ng CPU method, then matrix/FPU, with result verification"
    required_tools = ["stress-ng"]
    order = 20

    def run(self, ctx: RunContext, result: TestResult) -> None:
        log = ctx.log_path(self.name)
        total = ctx.seconds
        phases = [
            ("cpu-methods", ["--cpu", "0", "--cpu-method", "all"], int(total * 0.6)),
            ("matrix", ["--matrix", "0", "--matrix-method", "all"], total - int(total * 0.6)),
        ]
        for label, args, secs in phases:
            info = _stressng.run(args, max(10, secs), result, log, label)
            for k, v in info["metrics"].items():
                result.metrics[f"{label}.{k}.bogo_ops_per_s"] = v["bogo_ops_per_s"]
