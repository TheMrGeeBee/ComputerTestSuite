# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

"""Result data model shared by all tests."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, asdict
from typing import Any


class Status(enum.IntEnum):
    """Ordered by severity so ``max()`` yields the overall verdict."""

    PASS = 0
    SKIP = 1
    WARN = 2
    FAIL = 3
    ERROR = 4

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.name


@dataclass
class Finding:
    """A single observation that influences a test's verdict."""

    status: Status
    message: str
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status.name, "message": self.message, "source": self.source}


@dataclass
class TestResult:
    name: str
    subsystem: str
    status: Status = Status.PASS
    started: float = 0.0
    finished: float = 0.0
    findings: list[Finding] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    log_file: str = ""

    __test__ = False  # stop pytest from collecting this class

    @property
    def duration(self) -> float:
        return max(0.0, self.finished - self.started)

    def add(self, status: Status, message: str, source: str = "") -> None:
        self.findings.append(Finding(status, message, source))
        self.status = max(self.status, status)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.name
        d["duration"] = round(self.duration, 1)
        d["findings"] = [f.to_dict() for f in self.findings]
        return d


def overall(results: list[TestResult]) -> Status:
    """Worst status across results; all-SKIP counts as SKIP, empty as SKIP."""
    if not results:
        return Status.SKIP
    return max(r.status for r in results)
