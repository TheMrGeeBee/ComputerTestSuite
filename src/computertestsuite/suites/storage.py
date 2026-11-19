# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

"""Storage health and reliability.

* storage-smart   SMART/NVMe health, error counters, short self-test (read-only)
* storage-read    sequential surface read + random-read load with fio (read-only)
* storage-write   destructive write + verify with fio (opt-in, per-device confirmation)
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..core import proc
from ..core.base import StressTest
from ..core.context import RunContext
from ..core.disks import Disk, destructive_refusal, list_disks, test_targets
from ..core.model import Status, TestResult

# ---------------------------------------------------------------- SMART

ATA_BAD_ATTRS = {
    5: "Reallocated_Sector_Ct",
    187: "Reported_Uncorrect",
    196: "Reallocated_Event_Count",
    197: "Current_Pending_Sector",
    198: "Offline_Uncorrectable",
}


def smartctl_json(args: list[str], timeout: float = 60) -> dict[str, Any]:
    out = proc.capture(["smartctl", "-j", *args], timeout=timeout)
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {}


def smart_counters(data: dict[str, Any]) -> dict[str, int]:
    """Counters that must not increase during a test."""
    c: dict[str, int] = {}
    for a in data.get("ata_smart_attributes", {}).get("table", []):
        if a.get("id") in ATA_BAD_ATTRS:
            c[ATA_BAD_ATTRS[a["id"]]] = int(a.get("raw", {}).get("value", 0))
    nv = data.get("nvme_smart_health_information_log")
    if nv:
        c["media_errors"] = int(nv.get("media_errors", 0))
        c["critical_warning"] = int(nv.get("critical_warning", 0))
    return c


def evaluate_smart(data: dict[str, Any], dev: str) -> list[tuple[Status, str]]:
    out: list[tuple[Status, str]] = []
    if not data:
        return [(Status.WARN, f"{dev}: no SMART data (unsupported device or USB bridge)")]
    status = data.get("smart_status")
    if status is not None and status.get("passed") is False:
        out.append((Status.FAIL, f"{dev}: SMART overall health FAILED"))
    for name, val in smart_counters(data).items():
        if name == "critical_warning" and val:
            out.append((Status.FAIL, f"{dev}: NVMe critical warning flags 0x{val:02x}"))
        elif val:
            out.append((Status.WARN, f"{dev}: {name} = {val}"))
    nv = data.get("nvme_smart_health_information_log") or {}
    if nv.get("percentage_used", 0) >= 100:
        out.append((Status.WARN, f"{dev}: NVMe endurance used {nv['percentage_used']}%"))
    if nv.get("available_spare", 100) <= nv.get("available_spare_threshold", 0):
        out.append((Status.FAIL, f"{dev}: NVMe spare blocks below threshold"))
    errs = data.get("ata_smart_error_log", {}).get("summary", {}).get("count", 0)
    if errs:
        out.append((Status.WARN, f"{dev}: {errs} entries in the ATA error log"))
    return out


def selftest_running(data: dict[str, Any]) -> bool:
    ata = data.get("ata_smart_data", {}).get("self_test", {}).get("status", {})
    if ata and (int(ata.get("value", 0)) >> 4) == 0xF:
        return True
    nv = data.get("nvme_self_test_log", {}).get("current_self_test_operation", {})
    return bool(nv and int(nv.get("value", 0)) != 0)


def selftest_verdict(data: dict[str, Any]) -> tuple[bool | None, str]:
    """Latest self-test result: (passed, text). passed=None if unknown."""
    table = data.get("ata_smart_self_test_log", {}).get("standard", {}).get("table", [])
    if table:
        st = table[0].get("status", {})
        return st.get("passed"), st.get("string", "")
    table = data.get("nvme_self_test_log", {}).get("table", [])
    if table:
        res = table[0].get("self_test_result", {})
        return int(res.get("value", 0)) == 0, res.get("string", "")
    return None, "no self-test log"


def _targets(ctx: RunContext) -> list[Disk]:
    return test_targets(list_disks(), ctx.devices or None)


class StorageSmart(StressTest):
    name = "storage-smart"
    subsystem = "storage"
    description = "SMART/NVMe health, error counters and a short drive self-test"
    required_tools = ["smartctl"]
    needs_root = True
    order = 10

    def hardware_available(self, ctx: RunContext) -> str | None:
        return None if _targets(ctx) else "no disks found"

    def estimate(self, ctx: RunContext) -> int:
        return 150 if ctx.profile.name != "quick" else 15

    def run(self, ctx: RunContext, result: TestResult) -> None:
        disks = _targets(ctx)
        run_selftest = ctx.profile.name != "quick"
        started: list[Disk] = []
        for d in disks:
            data = smartctl_json(["-x", d.path])
            for status, msg in evaluate_smart(data, d.path):
                result.add(status, msg)
            result.metrics[f"{d.path}.smart"] = smart_counters(data) or "n/a"
            temp = data.get("temperature", {}).get("current")
            if temp is not None:
                result.metrics[f"{d.path}.temp_c"] = temp
            if run_selftest and data.get("smart_support", {}).get("enabled", bool(data.get("nvme_smart_health_information_log"))):
                r = proc.run(["smartctl", "-t", "short", d.path], timeout=60, log=ctx.log_path(self.name))
                if r.returncode == 0 or "Testing has begun" in r.output or "Self-test has begun" in r.output:
                    started.append(d)
        deadline = time.monotonic() + 12 * 60
        pending = list(started)
        while pending and time.monotonic() < deadline and not proc.ABORT.is_set():
            time.sleep(10)
            still = []
            for d in pending:
                data = smartctl_json(["-c", "-l", "selftest", d.path])
                if selftest_running(data):
                    still.append(d)
                    continue
                passed, text = selftest_verdict(data)
                if passed is False:
                    result.add(Status.FAIL, f"{d.path}: short self-test failed: {text}")
                else:
                    result.metrics[f"{d.path}.selftest"] = text or "completed"
            pending = still
        for d in pending:
            result.add(Status.WARN, f"{d.path}: self-test did not finish in time")
            proc.run(["smartctl", "-X", d.path], timeout=30)


# ---------------------------------------------------------------- fio

def fio_json(output: str) -> dict[str, Any]:
    """fio prints warnings before the JSON document; find the JSON part."""
    start = output.find("{")
    if start < 0:
        return {}
    try:
        return json.loads(output[start:])
    except json.JSONDecodeError:
        # trailing junk after the JSON object
        try:
            obj, _ = json.JSONDecoder().raw_decode(output[start:])
            return obj
        except json.JSONDecodeError:
            return {}


def fio_summary(data: dict[str, Any]) -> dict[str, Any]:
    out = {"errors": 0, "read_mib_s": 0.0, "write_mib_s": 0.0, "read_iops": 0.0, "read_gib": 0.0, "write_gib": 0.0}
    for job in data.get("jobs", []):
        out["errors"] += int(job.get("error", 0)) + int(job.get("total_err", 0))
        rd, wr = job.get("read", {}), job.get("write", {})
        out["read_mib_s"] += rd.get("bw", 0) / 1024
        out["write_mib_s"] += wr.get("bw", 0) / 1024
        out["read_iops"] += rd.get("iops", 0)
        out["read_gib"] += rd.get("io_bytes", 0) / 2**30
        out["write_gib"] += wr.get("io_bytes", 0) / 2**30
    return {k: round(v, 1) for k, v in out.items()}


def run_fio(args: list[str], timeout: float, log, result: TestResult, label: str) -> dict[str, Any]:
    r = proc.run(["fio", "--output-format=json", *args], timeout=timeout, log=log)
    summ = fio_summary(fio_json(r.output))
    if r.aborted:
        return summ
    if r.timed_out:
        result.add(Status.FAIL, f"{label}: fio hung past its time limit")
    elif r.returncode != 0:
        result.add(Status.FAIL, f"{label}: fio exited with {r.returncode}")
    if summ["errors"]:
        result.add(Status.FAIL, f"{label}: {summ['errors']} I/O errors")
    for ln in r.output.splitlines():
        if ("verify:" in ln and "bad" in ln) or "fio: io_u error" in ln:
            result.add(Status.FAIL, f"{label}: {ln.strip()[:200]}")
    return summ


class _SmartDelta:
    """Records SMART error counters before/after a test and flags increases."""

    def __init__(self, disks: list[Disk]) -> None:
        self.before = {d.path: smart_counters(smartctl_json(["-A", d.path])) for d in disks} if proc.have("smartctl") else {}

    def check(self, result: TestResult) -> None:
        for path, before in self.before.items():
            after = smart_counters(smartctl_json(["-A", path]))
            for k, v in after.items():
                if v > before.get(k, 0):
                    result.add(Status.FAIL, f"{path}: SMART {k} increased {before.get(k, 0)} -> {v} during test")


class StorageRead(StressTest):
    name = "storage-read"
    subsystem = "storage"
    description = "Read-only: sequential surface scan and random-read load on every disk"
    required_tools = ["fio"]
    needs_root = True
    order = 40

    def hardware_available(self, ctx: RunContext) -> str | None:
        return None if _targets(ctx) else "no disks found"

    def estimate(self, ctx: RunContext) -> int:
        disks = _targets(ctx)
        if ctx.profile.surface_scan and ctx.duration is None:
            # assume ~200 MB/s for HDD/USB, 1 GB/s for SSD
            return int(sum(d.size / (2e8 if d.rotational or d.is_usb else 1e9) for d in disks)
                       + len(disks) * ctx.seconds * 0.4)
        return self._per_disk_seconds(ctx, len(disks)) * len(disks)

    @staticmethod
    def _per_disk_seconds(ctx: RunContext, n_disks: int) -> int:
        """Time per disk for the time-limited (non-full-scan) mode."""
        if ctx.profile.shared_disk_budget and ctx.duration is None and n_disks:
            return max(30, ctx.seconds // n_disks)
        return ctx.seconds

    def run(self, ctx: RunContext, result: TestResult) -> None:
        disks = _targets(ctx)
        delta = _SmartDelta(disks)
        log = ctx.log_path(self.name)
        full = ctx.profile.surface_scan and ctx.duration is None
        secs = self._per_disk_seconds(ctx, len(disks))
        for d in disks:
            if proc.ABORT.is_set():
                break
            common = [f"--filename={d.path}", "--readonly", "--direct=1", "--ioengine=libaio",
                      "--group_reporting", "--name=" + d.name]
            seq_secs = int(secs * 0.6)
            seq = ["--rw=read", "--bs=1M", "--iodepth=16"]
            if full:
                seq_timeout = d.size / 2e7 + 600  # generous: 20 MB/s minimum
            else:
                seq += [f"--runtime={seq_secs}", "--time_based=0"]
                seq_timeout = seq_secs + 120
            s = run_fio(common + seq, seq_timeout, log, result, f"{d.path} sequential read")
            result.metrics[f"{d.path}.seq_read"] = f"{s['read_gib']} GiB at {s['read_mib_s']} MiB/s" + (
                " (full surface)" if full else "")

            rnd_secs = max(10, secs - seq_secs)
            rnd = ["--rw=randread", "--bs=4k", "--iodepth=32", "--numjobs=4",
                   f"--runtime={rnd_secs}", "--time_based=1"]
            s = run_fio(common + rnd, rnd_secs + 120, log, result, f"{d.path} random read")
            result.metrics[f"{d.path}.rand_read_iops"] = s["read_iops"]
        delta.check(result)


class StorageWrite(StressTest):
    name = "storage-write"
    subsystem = "storage"
    description = "DESTRUCTIVE: write + verify over the whole device (needs --destructive --device)"
    required_tools = ["fio"]
    needs_root = True
    order = 41
    default = False  # opt-in only

    def hardware_available(self, ctx: RunContext) -> str | None:
        if not ctx.destructive:
            return "destructive tests not enabled (--destructive)"
        if not ctx.devices:
            return "no --device given for the destructive test"
        return None

    def estimate(self, ctx: RunContext) -> int:
        disks = test_targets(list_disks(), ctx.devices)
        return int(sum(2 * d.size / (1.5e8 if d.rotational or d.is_usb else 5e8) for d in disks))

    def run(self, ctx: RunContext, result: TestResult) -> None:
        disks = test_targets(list_disks(), ctx.devices)
        delta = _SmartDelta(disks)
        log = ctx.log_path(self.name)
        for d in disks:
            if proc.ABORT.is_set():
                break
            why = destructive_refusal(d)
            if why:
                result.add(Status.ERROR, f"{d.path}: refused, device {why}")
                continue
            if not (ctx.destructive and (d.path in ctx.devices or d.name in ctx.devices)):
                result.add(Status.ERROR, f"{d.path}: not confirmed for destructive testing")
                continue
            size = "" if ctx.duration is None else f"--size={min(d.size, ctx.seconds * 100 * 2**20)}"
            args = [f"--filename={d.path}", "--direct=1", "--ioengine=libaio", "--rw=write", "--bs=1M",
                    "--iodepth=16", "--verify=crc32c", "--do_verify=1", "--verify_fatal=1",
                    "--name=" + d.name]
            if size:
                args.append(size)
            s = run_fio(args, d.size / 1e7 + 3600, log, result, f"{d.path} write+verify")
            result.metrics[f"{d.path}.written"] = f"{s['write_gib']} GiB at {s['write_mib_s']} MiB/s"
            result.metrics[f"{d.path}.verified"] = f"{s['read_gib']} GiB at {s['read_mib_s']} MiB/s"
        delta.check(result)
