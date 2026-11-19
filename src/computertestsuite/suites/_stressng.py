# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

"""Shared helper for running stress-ng and interpreting its result."""

from __future__ import annotations

import re
from pathlib import Path

from ..core import proc
from ..core.model import Status, TestResult

# stress-ng exit codes (see stress-ng(1), EXIT STATUS)
EXIT_MEANING = {
    0: "success",
    1: "error (bad options or fatal issue)",
    2: "one or more stressors failed",
    3: "stressor failed due to lack of resources",
    4: "stressor not implemented",
    5: "stressor killed by an unexpected signal",
    6: "stressor exited via exit()",
    7: "metrics untrustworthy",
}

_FAILED = re.compile(r"\bfailed:\s*(\d+)")
_UNTRUST = re.compile(r"metrics untrustworthy:\s*(\d+)")
_METRIC = re.compile(r"metrc:\s*\[\d+\]\s+(\S+)\s+(\d+)\s+([\d.]+)\s+[\d.]+\s+[\d.]+\s+([\d.]+)")
_FAIL_LINE = re.compile(r"(\bfail:|verification failed|verify failure|checksum (error|mismatch)|miscompare)", re.I)


def parse(output: str) -> dict:
    """Extract failure counts, bogo-op metrics and failure lines."""
    failed = sum(int(m) for m in _FAILED.findall(output))
    untrust = sum(int(m) for m in _UNTRUST.findall(output))
    metrics = {}
    for name, ops, secs, rate in _METRIC.findall(output):
        metrics[name] = {"bogo_ops": int(ops), "seconds": float(secs), "bogo_ops_per_s": float(rate)}
    fail_lines = [ln.strip() for ln in output.splitlines() if _FAIL_LINE.search(ln)]
    return {"failed": failed, "untrustworthy": untrust, "metrics": metrics, "fail_lines": fail_lines}


def run(args: list[str], seconds: int, result: TestResult, log: Path | None, label: str = "stress-ng") -> dict:
    cmd = ["stress-ng", *args, "--timeout", f"{seconds}s", "--metrics-brief", "--verify", "--oom-avoid"]
    r = proc.run(cmd, timeout=seconds + 120, log=log)
    if "--oom-avoid" in cmd and "unrecognized option" in r.output and "oom-avoid" in r.output:
        cmd.remove("--oom-avoid")  # older stress-ng
        r = proc.run(cmd, timeout=seconds + 120, log=log)
    info = parse(r.output)
    if r.aborted:
        return info
    if r.timed_out:
        result.add(Status.FAIL, f"{label}: did not finish within its time limit (hung?)")
    for ln in info["fail_lines"][:10]:
        result.add(Status.FAIL, f"{label}: {ln[:200]}")
    if info["failed"]:
        result.add(Status.FAIL, f"{label}: {info['failed']} stressor instance(s) failed")
    if r.returncode not in (0, 7, None) and not r.timed_out:
        status = Status.FAIL if r.returncode in (2, 5) else Status.ERROR
        result.add(status, f"{label}: exit {r.returncode} ({EXIT_MEANING.get(r.returncode, 'unknown')})")
    return info
