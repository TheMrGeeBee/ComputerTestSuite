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
    assert res == [(Status.WARN, "/dev/nvme0n1: 2 reads the drive could not correct: data was lost at some point "
                                 "(media_errors = 2)")]
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


def test_selftest_without_standard_log():
    """SATA SSDs that keep no standard self-test log: the extended log, then the execution status."""
    from computertestsuite.suites.storage import (selftest_exec_verdict, selftest_log_args, selftest_signature,
                                                  selftest_verdict)

    ext = {"ata_smart_self_test_log": {"standard": {"revision": 1, "count": 0},
                                       "extended": {"table": [{"status": {"value": 0, "passed": True,
                                                                          "string": "Completed without error"}}]}}}
    assert selftest_signature(ext) is not None
    assert selftest_verdict(ext) == (Status.PASS, "Completed without error")
    assert selftest_log_args("/dev/sda") == ["-l", "selftest", "-l", "xselftest"]
    assert selftest_log_args("/dev/nvme0n1") == ["-l", "selftest"]

    def status(value, text=""):
        return {"ata_smart_data": {"self_test": {"status": {"value": value, "string": text}}}}
    assert selftest_exec_verdict(status(0, "completed without error")) == (Status.PASS, "completed without error")
    assert selftest_exec_verdict(status(0x10))[0] == Status.WARN   # aborted by host
    assert selftest_exec_verdict(status(0x73))[0] == Status.FAIL   # read failure, 30% remaining
    assert selftest_exec_verdict({}) == (None, "")


def _attr(num, name, value, raw):
    return {"id": num, "name": name, "value": value, "raw": {"value": raw}}


def test_drive_life_intel_sata_ssd():
    from computertestsuite.suites.storage import drive_life, format_life

    data = {"power_on_time": {"hours": 21000}, "power_cycle_count": 3100,
            "ata_smart_attributes": {"table": [_attr(9, "Power_On_Hours_and_Msec", 100, 21000),
                                               _attr(233, "Media_Wearout_Indicator", 94, 0),
                                               _attr(241, "Host_Writes_32MiB", 100, 300_000)]}}
    life = drive_life(data)
    assert life == {"power_on_hours": 21000, "power_cycles": 3100, "wear_used_pct": 6,
                    "wear_source": "attribute 233 Media_Wearout_Indicator", "written_bytes": 300_000 * 32 * 2**20}
    assert format_life(life) == ("21000 h powered on (2.4 years), 3100 power cycles, "
                                 "wear 6% used (94% left, from attribute 233 Media_Wearout_Indicator), 10.1 TB written")


def test_drive_life_sources_in_order():
    from computertestsuite.suites.storage import drive_life

    # The standardized device statistics win over vendor attributes.
    data = {"ata_device_statistics": {"pages": [
                {"number": 1, "table": [{"name": "Logical Sectors Written", "value": 2_000_000}]},
                {"number": 7, "table": [{"name": "Percentage Used Endurance Indicator", "value": 12}]}]},
            "ata_smart_attributes": {"table": [_attr(177, "Wear_Leveling_Count", 50, 900),
                                               _attr(241, "Total_LBAs_Written", 100, 5)]}}
    assert drive_life(data) == {"wear_used_pct": 12, "wear_source": "device statistics",
                                "written_bytes": 2_000_000 * 512}
    # A device statistic the drive marks as not valid is ignored: the vendor attribute is used.
    data["ata_device_statistics"]["pages"][1]["table"][0].update(value=255, flags={"valid": False})
    assert drive_life(data)["wear_used_pct"] == 50
    # A reused attribute number with another name is not taken as wear.
    other = {"ata_smart_attributes": {"table": [_attr(177, "Some_Other_Counter", 60, 1)]}}
    assert "wear_used_pct" not in drive_life(other)
    # NVMe
    nv = {"nvme_smart_health_information_log": {"percentage_used": 3, "power_on_hours": 500,
                                                  "power_cycles": 40, "data_units_written": 1_000_000}}
    assert drive_life(nv) == {"power_on_hours": 500, "power_cycles": 40, "wear_used_pct": 3,
                              "wear_source": "NVMe percentage used", "written_bytes": 512_000_000_000}
    # A hard disk: only age.
    assert drive_life({"ata_smart_attributes": {"table": [_attr(9, "Power_On_Hours", 60, 40000)]}}) == \
        {"power_on_hours": 40000}


def test_wear_placeholder_255_uses_the_vendor_attribute():
    """Values from an Intel SSDSC2BW180A3L (SandForce): the standard indicator says 255 while
    Intel's own attribute says unused, with ~150 average erase cycles behind it."""
    from computertestsuite.suites.storage import drive_life

    data = {"power_on_time": {"hours": 50639, "minutes": 27}, "power_cycle_count": 3936,
            "endurance_used": {"current_percent": 255},
            "ata_device_statistics": {"pages": [
                {"number": 1, "table": [{"name": "Logical Sectors Written", "value": 61824683025,
                                         "flags": {"valid": True}}]},
                {"number": 7, "table": [{"name": "Percentage Used Endurance Indicator", "value": 255,
                                         "flags": {"valid": True}}]}]},
            "ata_smart_attributes": {"table": [_attr(226, "Workld_Media_Wear_Indic", 100, 65535),
                                               _attr(233, "Media_Wearout_Indicator", 100, 0),
                                               _attr(241, "Host_Writes_32MiB", 100, 940252)]}}
    life = drive_life(data)
    assert life["wear_used_pct"] == 0
    assert life["wear_source"].startswith("attribute 233 Media_Wearout_Indicator; the standard indicator shows 255")
    assert life["written_bytes"] == 61824683025 * 512
    assert not any("endurance" in m for _, m in evaluate_smart({**data, "smart_status": {"passed": True}}, "/dev/sda"))
    # Without a vendor attribute, 255 stays (it may be real) and warns.
    del data["ata_smart_attributes"]
    assert drive_life(data)["wear_used_pct"] == 255


def test_drive_events():
    from computertestsuite.suites.storage import drive_events, format_events

    def stat(name, value):
        return {"name": name, "value": value, "flags": {"valid": True}}
    intel = {"ata_smart_attributes": {"table": [_attr(174, "Unexpect_Power_Loss_Ct", 100, 3517),
                                                _attr(192, "Power-Off_Retract_Count", 100, 3517),
                                                _attr(199, "UDMA_CRC_Error_Count", 100, 0)]},
             "ata_device_statistics": {"pages": [{"number": 6, "table": [
                 stat("Number of Hardware Resets", 25016), stat("Number of ASR Events", 19848),
                 stat("Number of Interface CRC Errors", 0)]}]}}
    ev = drive_events(intel)
    assert ev == {"power_losses": 3517, "link_resets": 25016, "link_recoveries": 19848, "crc_errors": 0}
    assert format_events(ev, 3936) == ("3517 unexpected power losses of 3936 power-ons; "
                                       "SATA link: 25016 resets, 19848 recoveries, 0 transfer (CRC) errors")
    # Hard disk without device statistics: CRC errors from the attribute, with a hint.
    hdd = {"ata_smart_attributes": {"table": [_attr(192, "Power-Off_Retract_Count", 100, 40),
                                              _attr(199, "UDMA_CRC_Error_Count", 200, 12)]}}
    assert drive_events(hdd) == {"power_losses": 40, "crc_errors": 12}
    assert "bad SATA cable" in format_events(drive_events(hdd))
    assert drive_events({"nvme_smart_health_information_log": {"unsafe_shutdowns": 9}}) == {"power_losses": 9}
    assert format_events({}) == ""


def test_worn_ssd_is_a_warning():
    data = {"smart_status": {"passed": True},
            "ata_smart_attributes": {"table": [_attr(233, "Media_Wearout_Indicator", 8, 0)]}}
    assert [s for s, m in evaluate_smart(data, "/dev/sda") if "endurance" in m] == [Status.WARN]
    data["ata_smart_attributes"]["table"][0]["value"] = 40
    assert not any("endurance" in m for _, m in evaluate_smart(data, "/dev/sda"))


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


@pytest.mark.parametrize("line", [
    "iwlwifi 0000:00:14.3: Microcode SW error detected. Restarting 0x0.",
    "ath11k_pci 0000:02:00.0: firmware crashed: MHI_CB_EE_RDDM",
    "brcmfmac: brcmf_fw_crashed: Firmware has halted or crashed",
    "rtw89_8852be 0000:03:00.0: FW error, dumping",
])
def test_kmsg_wifi_firmware_crash(line):
    rule = classify_kmsg(line)
    assert rule is not None and (rule.status, rule.category) == (Status.FAIL, "wifi"), line


@pytest.mark.parametrize("line", [
    "iwlwifi 0000:00:14.3: Direct firmware load for iwlwifi-QuZ-a0-hr-b0-92.ucode failed with error -2",
    "iwlwifi 0000:00:14.3: loaded firmware version 89.202a2f7b.0 QuZ-a0-hr-b0-89.ucode op_mode iwlmvm",
    "wlan0: associated",
])
def test_kmsg_wifi_benign(line):
    assert classify_kmsg(line) is None, line


def test_iw_scan_parsing():
    from computertestsuite.suites.wifi import best_per_ssid, parse_iw_scan

    nets = parse_iw_scan((FIX / "iw_scan.txt").read_text())
    assert len(nets) == 4
    home5 = nets[0]
    assert (home5.bssid, home5.ssid, home5.signal_dbm, home5.band, home5.secure) == \
        ("02:11:22:33:44:55", "HomeNet", -48.0, "5 GHz", True)
    assert nets[2].ssid == "Café Guest" and not nets[2].secure and nets[2].band == "2.4 GHz"
    assert nets[3].ssid == "" and nets[3].band == "6 GHz"
    visible = best_per_ssid(nets)
    assert [n.ssid for n in visible] == ["HomeNet", "Café Guest"]  # hidden dropped, strongest AP kept
    assert visible[0].signal_dbm == -48.0


def test_iw_link_parsing():
    from computertestsuite.suites.wifi import parse_iw_link

    assert parse_iw_link("Not connected.\n")["connected"] is False
    info = parse_iw_link(
        "Connected to 02:11:22:33:44:55 (on wlan0)\n\tSSID: HomeNet\n\tfreq: 5180.0\n"
        "\tRX: 1234 bytes (10 packets)\n\tsignal: -52 dBm\n"
        "\trx bitrate: 866.7 MBit/s VHT-MCS 9 80MHz short GI VHT-NSS 2\n"
        "\ttx bitrate: 780.0 MBit/s VHT-MCS 8 80MHz short GI VHT-NSS 2\n")
    assert (info["connected"], info["ssid"], info["signal_dbm"], info["tx_mbps"], info["rx_mbps"]) == \
        (True, "HomeNet", -52.0, 780.0, 866.7)


def test_wifi_hardware_discovery(tmp_path):
    from computertestsuite.suites.wifi import rfkill_wlan, wifi_interfaces, wifi_pci_devices

    net = tmp_path / "net"
    (net / "wlan0" / "phy80211").mkdir(parents=True)
    (net / "enp0s31f6").mkdir()
    assert wifi_interfaces(net) == ["wlan0"]
    pci = tmp_path / "pci"
    (pci / "0000:00:14.3").mkdir(parents=True)
    for k, v in {"class": "0x028000", "vendor": "0x14e4", "device": "0x4365",
                 "modalias": "pci:v000014E4d00004365sv000017AAsd0000061Bbc02sc80i00"}.items():
        (pci / "0000:00:14.3" / k).write_text(v + "\n")
    (pci / "0000:00:1f.6").mkdir()
    (pci / "0000:00:1f.6" / "class").write_text("0x020000\n")
    assert wifi_pci_devices(pci) == [{"slot": "0000:00:14.3", "id": "14e4:4365",
                                      "modalias": "pci:v000014E4d00004365sv000017AAsd0000061Bbc02sc80i00",
                                      "driver": ""}]
    rf = tmp_path / "rfkill" / "rfkill0"
    rf.mkdir(parents=True)
    for k, v in {"type": "wlan", "name": "phy0", "soft": "0", "hard": "1"}.items():
        (rf / k).write_text(v + "\n")
    assert rfkill_wlan(tmp_path / "rfkill") == [{"name": "phy0", "soft": False, "hard": True}]


def test_wifi_missing_driver_verdict():
    """No driver on the stick for the chip is not a hardware fault; a driver that fails is."""
    from computertestsuite.suites.wifi import missing_driver_verdict

    broadcom = {"slot": "0000:02:00.0", "id": "14e4:4365", "modalias": "pci:x", "driver": ""}
    status, msg = missing_driver_verdict(broadcom, [])
    assert status == Status.SKIP and "'wl' driver" in msg
    status, msg = missing_driver_verdict(broadcom, [], wl_available=True)  # wl exists but doesn't know the chip
    assert status == Status.SKIP and "'wl'" not in msg
    intel = {"slot": "0000:00:14.3", "id": "8086:a370", "modalias": "pci:y", "driver": ""}
    status, msg = missing_driver_verdict(intel, [])
    assert status == Status.SKIP and "'wl'" not in msg
    assert missing_driver_verdict(intel, ["iwlwifi"])[0] == Status.FAIL
    assert missing_driver_verdict({**intel, "driver": "iwlwifi"}, ["iwlwifi"])[0] == Status.FAIL


def _monitor_with_temp(tmp_path, milli_c):
    hw = tmp_path / "class" / "hwmon" / "hwmon0"
    hw.mkdir(parents=True)
    (hw / "name").write_text("coretemp\n")
    (hw / "temp1_input").write_text(f"{milli_c}\n")
    (hw / "temp1_label").write_text("Package id 0\n")
    (hw / "temp1_crit").write_text("100000\n")
    (tmp_path / "devices" / "system" / "cpu").mkdir(parents=True)
    m = Monitor(root=tmp_path, kmsg="/nonexistent")
    m.samples.append(m.sample_now())
    return m, m.samples[0].t


def test_throttling_alone_is_information(tmp_path):
    m, t = _monitor_with_temp(tmp_path, 85000)  # well below critical
    m.feed_kmsg("4,1,1,-;CPU2: Core temperature above threshold, cpu clock throttled", t=t)
    ws = m.evaluate(t - 1, t + 1, {"throttle_core": 0}, {"throttle_core": 1378})
    assert ws.findings == []
    assert "1378 events" in ws.info["thermal_throttling"]
    assert ws.kernel_events  # still recorded for the report


def test_throttling_near_critical_is_a_warning(tmp_path):
    m, t = _monitor_with_temp(tmp_path, 98000)  # within 5 °C of critical
    ws = m.evaluate(t - 1, t + 1, {"throttle_core": 0}, {"throttle_core": 7100})
    msgs = [(f.status, f.message) for f in ws.findings]
    assert (Status.WARN, "CPU thermal throttling occurred (7100 events)") in msgs
    assert any("near critical" in m for _, m in msgs)
    assert "thermal_throttling" not in ws.info


def test_retired_ssd_blocks_are_explained():
    # Samsung 860 EVO after 2.8 years: 18 failed blocks replaced from the spare pool, nothing lost.
    data = {"rotation_rate": 0, "smart_status": {"passed": True}, "ata_smart_attributes": {"table": [
        _attr(5, "Reallocated_Sector_Ct", 98, 18), _attr(177, "Wear_Leveling_Count", 96, 68),
        _attr(179, "Used_Rsvd_Blk_Cnt_Tot", 98, 18), _attr(187, "Uncorrectable_Error_Cnt", 100, 0)]}}
    [(status, msg)] = evaluate_smart(data, "/dev/sda")
    assert status == Status.WARN
    assert msg.startswith("/dev/sda: 18 flash blocks failed and were replaced from spare blocks "
                          "(98% of the spare left), no data lost: ")
    assert msg.endswith("(Reallocated_Sector_Ct = 18)")
    # A hard disk with unreadable sectors: no "no data lost", and the event count is not repeated.
    data = {"rotation_rate": 5400, "ata_smart_attributes": {"table": [
        _attr(5, "Reallocated_Sector_Ct", 100, 16), _attr(196, "Reallocated_Event_Count", 100, 16),
        _attr(197, "Current_Pending_Sector", 100, 2)]}}
    msgs = [m for _, m in evaluate_smart(data, "/dev/sdb")]
    assert len(msgs) == 2 and "sectors on the disk surface" in msgs[0] and "no data lost" not in msgs[0]
    assert "cannot be read" in msgs[1]
