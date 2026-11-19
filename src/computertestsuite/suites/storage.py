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


# ---------------------------------------------------------------- age and wear

WEAR_WARN_PCT = 90

# SATA SSD vendor attributes whose *normalized* value counts down from 100 to 0 (% life left).
# Matched by name too, since vendors reuse attribute numbers for other things.
ATA_LIFE_LEFT_ATTRS = {
    233: "Media_Wearout_Indicator",   # Intel
    177: "Wear_Leveling_Count",       # Samsung
    231: "SSD_Life_Left",             # SandForce-based (Kingston, OCZ, ...)
    202: "Percent_Lifetime_Remain",   # Crucial / Micron
    169: "Remaining_Lifetime_Perc",   # WD / SanDisk / Toshiba
}

# SATA attributes counting data written: name -> bytes per raw unit (None: the logical block size).
ATA_WRITTEN_ATTRS = {
    "Host_Writes_32MiB": 32 * 2**20,  # Intel
    "Host_Writes_GiB": 2**30,
    "Lifetime_Writes_GiB": 2**30,
    "Total_Writes_GiB": 2**30,
    "Total_LBAs_Written": None,
}


def _devstat(data: dict[str, Any], page: int, name: str) -> int | None:
    """A value from the ATA Device Statistics log (standardized, smartctl -x)."""
    for p in data.get("ata_device_statistics", {}).get("pages", []):
        if p.get("number") == page:
            for e in p.get("table", []):
                if e.get("name") != name or not isinstance(e.get("value"), int):
                    continue
                if e.get("flags", {}).get("valid") is False:
                    return None  # the drive marks this value as not valid
                return e["value"]
    return None


def drive_life(data: dict[str, Any]) -> dict[str, Any]:
    """Power-on hours, power cycles, % of rated write endurance used and bytes written, where reported."""
    life: dict[str, Any] = {}
    nv = data.get("nvme_smart_health_information_log") or {}
    attrs = {a.get("id"): a for a in data.get("ata_smart_attributes", {}).get("table", [])}

    hours = data.get("power_on_time", {}).get("hours", nv.get("power_on_hours"))
    if hours is None and 9 in attrs:
        hours = attrs[9].get("raw", {}).get("value")
    if hours is not None:
        life["power_on_hours"] = int(hours)
    cycles = data.get("power_cycle_count", nv.get("power_cycles"))
    if cycles is not None:
        life["power_cycles"] = int(cycles)

    wear, source = data.get("endurance_used", {}).get("current_percent"), "smartctl endurance"
    if wear is None:
        wear, source = nv.get("percentage_used"), "NVMe percentage used"
    if wear is None:
        wear, source = _devstat(data, 7, "Percentage Used Endurance Indicator"), "device statistics"
    if wear is None:
        for num, name in ATA_LIFE_LEFT_ATTRS.items():
            a = attrs.get(num)
            if a and a.get("name") == name and isinstance(a.get("value"), int) and a["value"] <= 100:
                wear, source = 100 - a["value"], f"attribute {num} {name}"
                break
    if wear is not None:
        life["wear_used_pct"] = int(wear)
        life["wear_source"] = source

    block = int(data.get("logical_block_size") or 512)
    written = None
    if "data_units_written" in nv:
        written = int(nv["data_units_written"]) * 512_000  # NVMe units are 1000 x 512 bytes
    elif (sectors := _devstat(data, 1, "Logical Sectors Written")) is not None:
        written = sectors * block
    else:
        for a in attrs.values():
            if a.get("name") in ATA_WRITTEN_ATTRS:
                unit = ATA_WRITTEN_ATTRS[a["name"]] or block
                written = int(a.get("raw", {}).get("value", 0)) * unit
                break
    if written:
        life["written_bytes"] = written
    return life


def format_life(life: dict[str, Any]) -> str:
    parts = []
    if "power_on_hours" in life:
        h = life["power_on_hours"]
        parts.append(f"{h} h powered on ({h / 8766:.1f} years)" if h >= 8766 else f"{h} h powered on")
    if "power_cycles" in life:
        parts.append(f"{life['power_cycles']} power cycles")
    if "wear_used_pct" in life:
        w = life["wear_used_pct"]
        parts.append(f"wear {w}% used ({max(0, 100 - w)}% left, from {life['wear_source']})")
    if "written_bytes" in life:
        parts.append(f"{life['written_bytes'] / 1e12:.1f} TB written")
    return ", ".join(parts)


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
    wear = drive_life(data).get("wear_used_pct")
    if wear is not None and wear >= WEAR_WARN_PCT:
        out.append((Status.WARN, f"{dev}: {wear}% of the rated write endurance used"
                                 + (" (worn out: replace soon)" if wear >= 100 else " (near the end of its rated life)")))
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


# NVMe self-test result codes (NVMe base spec, Device Self-test log):
# 0 completed without error; 1-4, 8, 9 aborted (by command, reset, namespace removal, format,
# unknown reason, sanitize); 5 fatal/unknown error; 6/7 completed with failed segment(s).
NVME_SELFTEST_FAILED = {5, 6, 7}


def selftest_entries(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Self-test log entries, newest first (ATA or NVMe)."""
    ata = data.get("ata_smart_self_test_log", {})
    table = ata.get("standard", {}).get("table") or ata.get("extended", {}).get("table")
    if table is None:
        table = data.get("nvme_self_test_log", {}).get("table")
    return list(table or [])


def selftest_log_args(path: str) -> list[str]:
    """smartctl options that read the self-test log. Some SATA SSDs only keep the extended (GP) log."""
    return ["-l", "selftest"] if "nvme" in path else ["-l", "selftest", "-l", "xselftest"]


# ATA self-test execution status (upper nibble), kept by the drive even without a self-test log:
# 0 completed without error, 1 aborted by host, 2 interrupted by reset, 3-8 failed, 15 running.
ATA_EXEC_ABORTED = {1, 2}


def selftest_exec_verdict(data: dict[str, Any]) -> tuple[Status | None, str]:
    """Result of the last ATA self-test from the drive's execution status (None if not reported)."""
    st = data.get("ata_smart_data", {}).get("self_test", {}).get("status", {})
    if not st or "value" not in st:
        return None, ""
    code, text = int(st["value"]) >> 4, st.get("string", "")
    if code == 0:
        return Status.PASS, text or "completed without error"
    if code in ATA_EXEC_ABORTED or code == 0xF:
        return Status.WARN, text
    return Status.FAIL, text


def selftest_signature(data: dict[str, Any]) -> tuple[int, str] | None:
    """Identifies the newest log entry, to tell whether a new test result was added."""
    entries = selftest_entries(data)
    if not entries:
        return None
    return len(entries), json.dumps(entries[0], sort_keys=True)


def selftest_verdict(data: dict[str, Any]) -> tuple[Status | None, str]:
    """Result of the newest self-test: (PASS / WARN for aborted / FAIL, text); None if no log."""
    entries = selftest_entries(data)
    if not entries:
        return None, "no self-test log"
    e = entries[0]
    if "self_test_result" in e:  # NVMe
        res = e["self_test_result"]
        value, text = int(res.get("value", 0)), res.get("string", "")
        if value == 0:
            return Status.PASS, text
        return (Status.FAIL if value in NVME_SELFTEST_FAILED else Status.WARN), text
    st = e.get("status", {})  # ATA
    text = st.get("string", "")
    if st.get("passed") is True:
        return Status.PASS, text
    if any(w in text.lower() for w in ("abort", "interrupt")):
        return Status.WARN, text
    if st.get("passed") is False:
        return Status.FAIL, text
    return Status.WARN, text or "unknown self-test result"


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
        log = ctx.log_path(self.name)

        def note(text: str) -> None:
            with open(log, "a") as f:
                f.write(f"[{time.strftime('%H:%M:%S')}] {text}\n")

        started: list[Disk] = []
        before: dict[str, tuple[int, str] | None] = {}
        for d in disks:
            data = smartctl_json(["-x", d.path])
            # Keep the drive's full SMART data with the report: readable text in the log, and the
            # JSON next to it, so any value in the verdict can be checked afterwards.
            proc.run(["smartctl", "-x", d.path], timeout=60, log=log)
            if data:
                log.with_name(f"{self.name}.{d.name}.json").write_text(json.dumps(data, indent=1))
            for status, msg in evaluate_smart(data, d.path):
                result.add(status, msg)
            result.metrics[f"{d.path}.smart"] = smart_counters(data) or "n/a"
            if life := format_life(drive_life(data)):
                result.metrics[f"{d.path}.life"] = life
            temp = data.get("temperature", {}).get("current")
            if temp is not None:
                result.metrics[f"{d.path}.temp_c"] = temp
            if run_selftest and data.get("smart_support", {}).get("enabled", bool(data.get("nvme_smart_health_information_log"))):
                before[d.path] = selftest_signature(smartctl_json([*selftest_log_args(d.path), d.path]))
                r = proc.run(["smartctl", "-t", "short", d.path], timeout=60, log=log)
                if r.returncode == 0 or "Testing has begun" in r.output or "Self-test has begun" in r.output:
                    started.append(d)
        t0 = time.monotonic()
        deadline = t0 + 12 * 60
        pending = list(started)
        seen_running: set[str] = set()
        while pending and time.monotonic() < deadline and not proc.ABORT.is_set():
            time.sleep(5 if time.monotonic() - t0 < 30 else 10)
            still = []
            for d in pending:
                data = smartctl_json(["-c", *selftest_log_args(d.path), d.path])
                running = selftest_running(data)
                op = data.get("nvme_self_test_log", {})
                pct = op.get("current_self_test_completion_percent")
                note(f"poll {d.path}: running={running}"
                     + (f" ({op.get('current_self_test_operation', {}).get('string', '')}, {pct}%)" if pct is not None else "")
                     + f" newest={selftest_signature(data)}")
                if running:
                    seen_running.add(d.path)
                    still.append(d)
                    continue
                secs = int(time.monotonic() - t0)
                if selftest_signature(data) != before.get(d.path):
                    status, text = selftest_verdict(data)
                else:
                    # No new log entry: some SSDs keep no self-test log. If the test was seen running, the
                    # drive's self-test execution status tells how it ended.
                    status, text = selftest_exec_verdict(data) if d.path in seen_running else (None, "")
                    if status is None:
                        result.add(Status.WARN, f"{d.path}: self-test result could not be confirmed "
                                                "(no new entry in the drive's self-test log)")
                        continue
                    text = f"{text} (from the drive's self-test status; it keeps no log entry)"
                if status is Status.FAIL:
                    result.add(Status.FAIL, f"{d.path}: short self-test failed: {text}")
                elif status is Status.WARN:
                    result.add(Status.WARN, f"{d.path}: short self-test did not complete: {text}")
                else:
                    result.metrics[f"{d.path}.selftest"] = f"{text or 'completed'} (finished within ~{secs}s)"
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
