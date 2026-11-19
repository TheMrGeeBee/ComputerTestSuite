# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

from computertestsuite.core.model import Status
from computertestsuite.suites.battery import (ac_online, batteries, charging_findings, drain_summary,
                                              health_findings, runtime_text)


def _supply(root, name, **files):
    d = root / name
    d.mkdir(parents=True)
    for k, v in files.items():
        (d / k).write_text(f"{v}\n")


def test_energy_battery_and_charger(tmp_path):
    _supply(tmp_path, "BAT0", type="Battery", present=1, status="Discharging", capacity=74,
            energy_full=27_600_000, energy_full_design=48_000_000, energy_now=20_400_000,
            power_now=9_500_000, cycle_count=0, manufacturer="SANYO", model_name="L12S4Z01")
    _supply(tmp_path, "AC", type="Mains", online=0)
    _supply(tmp_path, "hidpp_battery_0", type="Battery", scope="Device", capacity=50)  # a wireless mouse
    [b] = batteries(tmp_path)
    assert (b.full_wh, b.design_wh, b.now_wh, b.power_w) == (27.6, 48.0, 20.4, 9.5)
    assert b.health_pct == 58 and b.cycles is None  # 0 cycles = not reported
    assert ac_online(tmp_path) is False
    [(status, msg)] = health_findings(b)
    assert status == Status.WARN and "58%" in msg


def test_charge_battery_is_converted_to_wh(tmp_path):
    # charge_* in µAh with the design voltage in µV; current_now * voltage_now for power.
    _supply(tmp_path, "BAT1", type="Battery", status="Charging", capacity=40,
            charge_full=4_000_000, charge_full_design=5_000_000, charge_now=1_600_000,
            voltage_min_design=11_100_000, voltage_now=11_500_000, current_now=1_000_000, cycle_count=312)
    [b] = batteries(tmp_path)
    assert round(b.full_wh, 2) == 44.4 and round(b.design_wh, 2) == 55.5
    assert b.health_pct == 80 and b.cycles == 312
    assert round(b.power_w, 2) == 11.5
    assert ac_online(tmp_path) is None  # no mains adapter listed
    assert health_findings(b) == []


def test_charger_connected_but_discharging(tmp_path):
    _supply(tmp_path, "BAT0", type="Battery", status="Discharging", capacity=60)
    [b] = batteries(tmp_path)
    assert charging_findings(b, True)[0][0] == Status.WARN
    assert charging_findings(b, False) == []
    b.status = "Not charging"  # a charge limit, or a refused battery: information only
    assert charging_findings(b, True) == []


def test_missing_battery():
    from computertestsuite.suites.battery import Battery
    assert health_findings(Battery("BAT0", present=False))[0][0] == Status.WARN


def test_drain_summary():
    # Power readings are averaged; the charge falling 10% between two samples is a sudden drop.
    s = drain_summary([(0, 20.0, 10.0, 70), (5, 19.9, 12.0, 70), (10, 19.8, 11.0, 60)])
    assert s == {"watts": 11.0, "max_drop_pct": 10}
    # Without power readings: the energy used over the time.
    s = drain_summary([(0, 20.0, None, 70), (360, 19.0, None, 68)])
    assert s == {"watts": 10.0, "max_drop_pct": 2}
    assert runtime_text(27.6, 9.2) == "3 h 00 min"
    assert runtime_text(None, 9.2) == "?"
