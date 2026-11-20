# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

"""JSON and plain-text reports."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .model import Status, TestResult, overall
from .profiles import fmt_duration

MARK = {"PASS": "PASS ", "WARN": "WARN ", "FAIL": "FAIL!", "SKIP": "skip ", "ERROR": "ERROR"}


def build(results: list[TestResult], info: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "verdict": overall([r for r in results if r.status != Status.SKIP] or results).name,
        "meta": meta,
        "system": info,
        "results": [r.to_dict() for r in results],
    }


def format_text(report: dict[str, Any]) -> str:
    meta = report["meta"]
    lines = [
        "=" * 72,
        f" ComputerTestSuite report   verdict: {report['verdict']}",
        "=" * 72,
        f" Started : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(meta.get('started', 0)))}",
        f" Duration: {fmt_duration(meta.get('finished', 0) - meta.get('started', 0))}",
        f" Profile : {meta.get('profile')}   per-test duration: {fmt_duration(meta.get('test_seconds', 0))}",
        f" Version : ComputerTestSuite {meta.get('version', '?')}",
    ]
    if meta.get("aborted"):
        lines.append(" NOTE    : run was aborted by the user")
    if not meta.get("kmsg_available", True):
        lines.append(" NOTE    : kernel log not readable (run as root); hardware errors may be missed")
    sysd = report.get("system", {})
    cpu = sysd.get("cpu", {})
    lines.append(f" System  : {sysd.get('dmi', {}).get('sys_vendor', '')} {sysd.get('dmi', {}).get('product_name', '')}")
    lines.append(f" CPU     : {cpu.get('Model name', '?')}")
    lines.append("")
    lines.append(f" {'Result':6} {'Test':22} {'Subsystem':10} {'Time':>9}")
    lines.append(" " + "-" * 50)
    for r in report["results"]:
        lines.append(f" {MARK[r['status']]:6} {r['name']:22} {r['subsystem']:10} {fmt_duration(r['duration']):>9}")
    lines.append("")
    for r in report["results"]:
        if not r["findings"] and not r["metrics"]:
            continue
        lines.append(f"--- {r['name']} [{r['status']}]")
        for f in r["findings"]:
            lines.append(f"   {f['status']:5} {f['message']}")
        m = r["metrics"]
        temps = m.get("temps") or {}
        if temps:
            hottest = sorted(temps.items(), key=lambda kv: -kv[1]["max"])[:5]
            lines.append("   peak temps: " + ", ".join(f"{k} {v['max']:.0f}°C" for k, v in hottest))
        if m.get("cpu_freq"):
            fq = m["cpu_freq"]
            lines.append(f"   CPU MHz: min {fq['min_mhz']} / avg {fq['avg_mhz']} / max {fq['max_mhz']}")
        for k, v in m.items():
            if k in ("temps", "cpu_freq", "counters", "kernel_events"):
                continue
            lines.append(f"   {k}: {v}")
        if r.get("log_file"):
            lines.append(f"   log: {r['log_file']}")
        lines.append("")
    return "\n".join(lines)


def write(run_dir: Path, report: dict[str, Any]) -> tuple[Path, Path]:
    jp = run_dir / "report.json"
    tp = run_dir / "report.txt"
    jp.write_text(json.dumps(report, indent=2, default=str))
    tp.write_text(format_text(report))
    for p in (jp, tp):
        try:
            fd = os.open(p, os.O_RDONLY)
            os.fsync(fd)
            os.close(fd)
        except OSError:
            pass
    return jp, tp


def load(path: Path) -> dict[str, Any]:
    if path.is_dir():
        path = path / "report.json"
    return json.loads(path.read_text())
