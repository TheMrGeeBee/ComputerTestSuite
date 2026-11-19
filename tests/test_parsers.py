# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

import json
from pathlib import Path

import pytest

from computertestsuite.core.model import Status, TestResult, overall
from computertestsuite.core.monitor import Monitor, classify_kmsg, discover_temp_sensors, parse_kmsg_record
from computertestsuite.core.profiles import parse_duration
from computertestsuite.suites import _stressng
from computertestsuite.suites.memory import memtester_plan, parse_memtester
from computertestsuite.suites.network import iperf_bps
from computertestsuite.suites.storage import evaluate_smart, fio_json, fio_summary, selftest_running, smart_counters

FIX = Path(__file__).parent / "fixtures"


# ------------------------------------------------------------ profiles / model

@pytest.mark.parametrize("text,secs", [("90", 90), ("90s", 90), ("15m", 900), ("2h", 7200), ("1h30m", 5400)])
def test_parse_duration(text, secs):
    assert parse_duration(text) == secs


@pytest.mark.parametrize("bad", ["", "abc", "10x", "1h foo"])
def test_parse_duration_invalid(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)


def test_overall_is_worst():
    a, b = TestResult("a", "x"), TestResult("b", "x")
    b.add(Status.WARN, "hot")
    assert overall([a, b]) == Status.WARN
    b.add(Status.FAIL, "broken")
    assert overall([a, b]) == Status.FAIL
    assert overall([]) == Status.SKIP


# ------------------------------------------------------------ stress-ng

STRESSNG_OK = """\
stress-ng: info:  [1234] setting to a 60 secs run per stressor
stress-ng: metrc: [1234] stressor       bogo ops real time  usr time  sys time   bogo ops/s     bogo ops/s
stress-ng: metrc: [1234]                           (secs)    (secs)    (secs)   (real time) (usr+sys time)
stress-ng: metrc: [1234] cpu              123456     60.00    959.12      0.31      2057.60         128.67
stress-ng: info:  [1234] skipped: 0
stress-ng: info:  [1234] passed: 16: cpu (16)
stress-ng: info:  [1234] failed: 0
stress-ng: info:  [1234] metrics untrustworthy: 0
stress-ng: info:  [1234] successful run completed in 1 min
"""

STRESSNG_FAIL = """\
stress-ng: fail:  [1240] cpu: fft: data difference between identical fft computations
stress-ng: info:  [1234] passed: 15: cpu (15)
stress-ng: info:  [1234] failed: 1: cpu (1)
"""


def test_stressng_ok():
    info = _stressng.parse(STRESSNG_OK)
    assert info["failed"] == 0
    assert info["fail_lines"] == []
    assert info["metrics"]["cpu"]["bogo_ops"] == 123456


def test_stressng_fail():
    info = _stressng.parse(STRESSNG_FAIL)
    assert info["failed"] == 1
    assert len(info["fail_lines"]) == 1


# ------------------------------------------------------------ memtester

def test_memtester_failure_detected():
    out = "Loop 1/1:\n  Stuck Address       : ok\n  Random Value        : \b\b\bFAILURE: 0x1234 != 0x1235 at offset 0x00000010.\n"
    assert parse_memtester(out) == ["FAILURE: 0x1234 != 0x1235 at offset 0x00000010."]
    assert parse_memtester("Loop 1/1:\n  Stuck Address       : ok\nDone.\n") == []


def test_memtester_plan_leaves_headroom():
    gib = 2**30
    n, per = memtester_plan(16 * gib, 16)
    assert n == 4
    assert n * per * 2**20 < 16 * gib * 0.93


# ------------------------------------------------------------ kernel log

@pytest.mark.parametrize("line,status,cat", [
    ("mce: [Hardware Error]: CPU 3: Machine Check: 0 Bank 5: be00000000800400", Status.FAIL, "cpu"),
    ("EDAC MC0: 1 CE memory read error on CPU_SrcID#0_Ha#0_Chan#1_DIMM#0", Status.WARN, "memory"),
    ("EDAC MC0: 1 UE memory read error on DIMM_A1", Status.FAIL, "memory"),
    ("ata3.00: exception Emask 0x0 SAct 0x0 SErr 0x0 action 0x0", Status.FAIL, "storage"),
    ("I/O error, dev sda, sector 123456 op 0x0:(READ) flags 0x0", Status.FAIL, "storage"),
    ("nvme nvme0: I/O 12 QID 3 timeout, aborting", Status.FAIL, "storage"),
    ("amdgpu 0000:03:00.0: amdgpu: GPU reset begin!", Status.FAIL, "gpu"),
    ("i915 0000:00:02.0: [drm] GPU HANG: ecode 12:1:85dffffb", Status.FAIL, "gpu"),
    ("watchdog: BUG: soft lockup - CPU#2 stuck for 22s!", Status.FAIL, "system"),
    ("pcieport 0000:00:1c.0: AER: Corrected error message received from 0000:02:00.0", Status.WARN, "pcie"),
    ("CPU4: Core temperature above threshold, cpu clock throttled", Status.WARN, "thermal"),
    ("usb 1-4: device descriptor read/64, error -71", Status.WARN, "usb"),
])
def test_kmsg_classification(line, status, cat):
    rule = classify_kmsg(line)
    assert rule is not None, line
    assert (rule.status, rule.category) == (status, cat)


@pytest.mark.parametrize("line", [
    "usb 1-4: new high-speed USB device number 5 using xhci_hcd",
    "EXT4-fs (sda1): mounted filesystem with ordered data mode",
    "amdgpu 0000:03:00.0: amdgpu: SMU is initialized successfully!",
    "r8169 0000:06:00.0 enp6s0: Link is Up - 1Gbps/Full",
])
def test_kmsg_benign(line):
    assert classify_kmsg(line) is None


def test_parse_kmsg_record():
    prio, msg = parse_kmsg_record("3,1234,5678901,-;nvme nvme0: I/O 1 QID 2 timeout\n SUBSYSTEM=nvme")
    assert prio == 3
    assert msg == "nvme nvme0: I/O 1 QID 2 timeout"


def test_monitor_window(tmp_path):
    # Fake hwmon with one k10temp sensor (no crit -> fallback 95°C).
    hw = tmp_path / "class" / "hwmon" / "hwmon0"
    hw.mkdir(parents=True)
    (hw / "name").write_text("k10temp\n")
    (hw / "temp1_input").write_text("93000\n")
    (hw / "temp1_label").write_text("Tctl\n")
    (tmp_path / "devices" / "system" / "cpu").mkdir(parents=True)
    sensors = discover_temp_sensors(tmp_path)
    assert sensors[0].key == "k10temp/Tctl" and sensors[0].crit == 95.0

    m = Monitor(root=tmp_path, kmsg="/nonexistent")
    m.samples.append(m.sample_now())
    t = m.samples[0].t
    m.feed_kmsg("3,1,1,-;EDAC MC0: 1 UE memory read error", t=t)
    ws = m.evaluate(t - 1, t + 1, {"edac_ue": 0}, {"edac_ue": 0})
    statuses = sorted(f.status for f in ws.findings)
    assert statuses == [Status.WARN, Status.FAIL]  # near-crit temp + UE kernel message
    assert ws.temps["k10temp/Tctl"]["max"] == 93.0


# ------------------------------------------------------------ SMART / fio / iperf

def test_smart_ata_healthy():
    data = json.loads((FIX / "smart_ata_ok.json").read_text())
    assert evaluate_smart(data, "/dev/sda") == []
    assert smart_counters(data)["Reallocated_Sector_Ct"] == 0


def test_smart_ata_failing():
    data = json.loads((FIX / "smart_ata_bad.json").read_text())
    res = evaluate_smart(data, "/dev/sda")
    statuses = {s for s, _ in res}
    assert Status.FAIL in statuses
    assert any("Current_Pending_Sector = 8" in m for _, m in res)


def test_smart_nvme():
    data = json.loads((FIX / "smart_nvme.json").read_text())
    res = evaluate_smart(data, "/dev/nvme0n1")
    assert res == [(Status.WARN, "/dev/nvme0n1: media_errors = 2")]
    assert not selftest_running(data)


def test_fio_json_with_leading_warning():
    out = "fio: some warning here\n" + (FIX / "fio_read.json").read_text()
    s = fio_summary(fio_json(out))
    assert s["errors"] == 0
    assert s["read_mib_s"] == pytest.approx(500.0, rel=0.01)


def test_iperf():
    bps, err = iperf_bps(json.dumps({"end": {"sum_received": {"bits_per_second": 9.4e8}}}))
    assert err == "" and bps == 9.4e8
    bps, err = iperf_bps(json.dumps({"error": "unable to connect to server"}))
    assert bps == 0 and "unable" in err


def test_quick_profile_fits_15_to_20_minutes():
    """Rough budget of the default selection on a machine with one disk and a GPU."""
    from computertestsuite.core.profiles import PROFILES

    q = PROFILES["quick"]
    timed_tests = 5  # cpu, memory-pattern, memory-stress, storage-read (shared budget), gpu
    fixed = 15 + 10 + 30  # storage-smart, network (no iperf), usb
    total = timed_tests * q.test_seconds + q.system_seconds + fixed
    assert 15 * 60 <= total <= 20 * 60, total


def test_storage_read_shares_budget_in_quick():
    from computertestsuite.core.context import RunContext
    from computertestsuite.core.profiles import PROFILES
    from computertestsuite.suites.storage import StorageRead

    ctx = RunContext(profile=PROFILES["quick"])
    assert StorageRead._per_disk_seconds(ctx, 2) * 2 <= ctx.seconds
    assert StorageRead._per_disk_seconds(ctx, 6) * 6 <= max(ctx.seconds, 30 * 6)
    assert StorageRead._per_disk_seconds(ctx, 20) == 30  # floor per disk
    ctx = RunContext(profile=PROFILES["standard"])
    assert StorageRead._per_disk_seconds(ctx, 6) == ctx.seconds  # standard: per disk


# Self-test log shapes as reported by smartctl 7.5 for an NVMe drive (trimmed to the relevant part).
NVME_ST_RUNNING = {"nvme_self_test_log": {"nsid": -1, "current_self_test_operation": {
    "value": 1, "string": "Short self-test in progress"}, "current_self_test_completion_percent": 4}}


def _nvme_done(result_value, result_string, poh=16542, older=()):
    table = [{"self_test_code": {"value": 1, "string": "Short"},
              "self_test_result": {"value": result_value, "string": result_string}, "power_on_hours": poh}]
    return {"nvme_self_test_log": {"nsid": -1, "current_self_test_operation": {
        "value": 0, "string": "No self-test in progress"}, "table": table + list(older)}}


def test_selftest_running_and_verdicts():
    from computertestsuite.suites.storage import selftest_running, selftest_signature, selftest_verdict

    assert selftest_running(NVME_ST_RUNNING)
    assert selftest_verdict(NVME_ST_RUNNING) == (None, "no self-test log")  # no table while running
    done = _nvme_done(0, "Completed without error")
    assert not selftest_running(done)
    assert selftest_verdict(done) == (Status.PASS, "Completed without error")
    assert selftest_verdict(_nvme_done(2, "Aborted: Controller Reset"))[0] == Status.WARN
    assert selftest_verdict(_nvme_done(7, "Completed: failed segments"))[0] == Status.FAIL

    # A new entry changes the signature; reading the same old log does not.
    old = _nvme_done(0, "Completed without error", poh=100)
    new = _nvme_done(0, "Completed without error", poh=101,
                     older=old["nvme_self_test_log"]["table"])
    assert selftest_signature(old) == selftest_signature(old)
    assert selftest_signature(new) != selftest_signature(old)
    assert selftest_signature({}) is None


def test_selftest_verdict_ata():
    from computertestsuite.suites.storage import selftest_verdict

    def ata(passed, text):
        st = {"value": 0, "string": text}
        if passed is not None:
            st["passed"] = passed
        return {"ata_smart_self_test_log": {"standard": {"table": [{"status": st}]}}}

    assert selftest_verdict(ata(True, "Completed without error"))[0] == Status.PASS
    assert selftest_verdict(ata(False, "Completed: read failure"))[0] == Status.FAIL
    assert selftest_verdict(ata(None, "Aborted by host"))[0] == Status.WARN
    assert selftest_verdict(ata(False, "Interrupted (host reset)"))[0] == Status.WARN
