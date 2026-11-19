# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

"""Base class every stress test derives from."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from . import proc
from .context import RunContext
from .model import TestResult


class StressTest(ABC):
    #: Unique short identifier, used with --only / --exclude.
    name: ClassVar[str]
    #: Subsystem group (cpu, memory, storage, gpu, network, usb, system).
    subsystem: ClassVar[str]
    description: ClassVar[str] = ""
    #: Binaries that must be on PATH.
    required_tools: ClassVar[list[str]] = []
    needs_root: ClassVar[bool] = False
    #: Run order; lower runs first.
    order: ClassVar[int] = 100
    #: Included in the default selection (False for opt-in tests).
    default: ClassVar[bool] = True

    def available(self, ctx: RunContext) -> str | None:
        """Return None if the test can run, otherwise a human-readable skip reason."""
        if self.needs_root and not ctx.is_root:
            return "requires root"
        miss = proc.missing(self.required_tools)
        if miss:
            return "missing tools: " + ", ".join(miss)
        return self.hardware_available(ctx)

    def hardware_available(self, ctx: RunContext) -> str | None:
        """Override to check for the presence of hardware."""
        return None

    def estimate(self, ctx: RunContext) -> int:
        """Rough expected run time in seconds, for progress display."""
        return ctx.seconds

    @abstractmethod
    def run(self, ctx: RunContext, result: TestResult) -> None:
        """Execute the test, recording findings/metrics in ``result``.

        Raising an exception marks the result as ERROR.
        """
