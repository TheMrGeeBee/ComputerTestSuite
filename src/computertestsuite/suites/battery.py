# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

"""Laptop battery: health, charging, and how fast it drains at idle and under full load.

Always: capacity left compared with new (health), charge cycles, whether the charger is seen
and the battery charges.

Only on battery power (unplug the charger before starting; this test runs first):
  * power draw at idle and under full CPU load -> estimated runtime for each
  * sudden drops in the charge level, a typical sign of a weak cell
The load part stops early at LOW_STOP_PCT so a run never empties the battery.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..core import proc
from ..core.base import StressTest
from ..core.context import RunContext
from ..core.model import Status, TestResult
from . import _stressng

PS_ROOT = Path("/sys/class/power_supply")
HEALTH_WARN_PCT = 60   # below: WARN, the battery is weak
HEALTH_WORN_PCT = 80   # below: noted as worn
LOW_STOP_PCT = 15      # stop the drain measurement at this charge level
MIN_START_PCT = 30     # don't start the drain measurement below this
SUDDEN_DROP_PCT = 8    # charge falling this much between two samples (~5 s): weak cell
IDLE_SECONDS = 60
SAMPLE_SECONDS = 5


# ------------------------------------------------------------------ reading sysfs

def _read(d: Path, name: str) -> str:
    try:
        return (d / name).read_text().strip()
    except OSError:
        return ""


def _num(d: Path, name: str) -> int | None:
    v = _read(d, name)
    try:
        return int(v)
    except ValueError:
        return None


@dataclass
class Battery:
    name: str
    present: bool = True
    status: str = ""          # Charging, Discharging, Full, Not charging, Unknown
    capacity_pct: int | None = None
    full_wh: float | None = None
    design_wh: float | None = None
    now_wh: float | None = None
    power_w: float | None = None
    cycles: int | None = None
    info: dict[str, str] = field(default_factory=dict)

    @property
    def health_pct(self) -> int | None:
        if self.full_wh and self.design_wh:
            return round(100 * self.full_wh / self.design_wh)
        return None


def read_battery(d: Path) -> Battery:
    """One battery from /sys/class/power_supply/BATn. Energy is reported either directly
    (energy_*, µWh) or as charge (charge_*, µAh), which is converted with the design voltage."""
    b = Battery(d.name, present=_read(d, "present") != "0", status=_read(d, "status"),
                capacity_pct=_num(d, "capacity"))
    volt = _num(d, "voltage_min_design") or _num(d, "voltage_now")  # µV

    def wh(kind: str) -> float | None:
        e = _num(d, f"energy_{kind}")
        if e is not None:
            return e / 1e6
        c = _num(d, f"charge_{kind}")
        if c is not None and volt:
            return c * volt / 1e12
        return None

    b.full_wh, b.design_wh, b.now_wh = wh("full"), wh("full_design"), wh("now")
    p = _num(d, "power_now")
    if p is None and (i := _num(d, "current_now")) is not None and (v := _num(d, "voltage_now")):
        p = i * v // 10**6
    b.power_w = abs(p) / 1e6 if p else None
    cycles = _num(d, "cycle_count")
    b.cycles = cycles if cycles else None  # many batteries report 0: unknown
    for k in ("manufacturer", "model_name", "technology"):
        if v := _read(d, k):
            b.info[k] = v
    return b


def batteries(root: Path = PS_ROOT) -> list[Battery]:
    if not root.is_dir():
        return []
    return [read_battery(d) for d in sorted(root.iterdir())
            if _read(d, "type") == "Battery" and _read(d, "scope") != "Device"]  # not mice/keyboards


def ac_online(root: Path = PS_ROOT) -> bool | None:
    """True/False if a mains adapter is reported, None if the system has none (or doesn't say)."""
    if not root.is_dir():
        return None
    states = [_read(d, "online") == "1" for d in root.iterdir() if _read(d, "type") == "Mains"]
    return any(states) if states else None


# ------------------------------------------------------------------ verdicts

def health_findings(b: Battery) -> list[tuple[Status, str]]:
    out: list[tuple[Status, str]] = []
    if not b.present:
        return [(Status.WARN, f"{b.name}: battery not detected (removed, or not making contact)")]
    h = b.health_pct
    if h is not None and h < HEALTH_WARN_PCT:
        out.append((Status.WARN, f"{b.name}: battery holds only {h}% of its design capacity: weak, replace soon"))
    return out


def charging_findings(b: Battery, ac: bool | None) -> list[tuple[Status, str]]:
    if not ac or b.capacity_pct is None or b.capacity_pct >= 95:
        return []
    if b.status == "Discharging":
        return [(Status.WARN, f"{b.name}: the charger is connected but the battery is discharging "
                              "(charger, cable, charging port or charging circuit)")]
    return []


def drain_summary(samples: list[tuple[float, float | None, float | None, int | None]]) -> dict[str, float]:
    """From (time, energy_wh, power_w, capacity_pct) samples: average watts, and the largest drop
    in charge between two consecutive samples."""
    out: dict[str, float] = {}
    powers = [p for _, _, p, _ in samples if p]
    if powers:
        out["watts"] = sum(powers) / len(powers)
    elif len(samples) >= 2:
        (t0, e0, _, _), (t1, e1, _, _) = samples[0], samples[-1]
        if e0 is not None and e1 is not None and t1 > t0 and e0 > e1:
            out["watts"] = (e0 - e1) * 3600 / (t1 - t0)
    caps = [c for *_, c in samples if c is not None]
    out["max_drop_pct"] = max((a - b for a, b in zip(caps, caps[1:])), default=0)
    return out


def runtime_text(wh: float | None, watts: float | None) -> str:
    if not wh or not watts:
        return "?"
    h = wh / watts
    return f"{int(h)} h {round(h % 1 * 60):02d} min"


# ------------------------------------------------------------------ the test

class BatteryTest(StressTest):
    name = "battery"
    subsystem = "power"
    description = "Battery health, cycles and charging; idle and full-load drain when unplugged"
    order = 5  # first, so a run can start unplugged and the charger go back in afterwards

    def hardware_available(self, ctx: RunContext) -> str | None:
        return None if batteries() else "no battery"

    def _load_seconds(self, ctx: RunContext) -> int:
        return min(ctx.seconds, 600)

    def estimate(self, ctx: RunContext) -> int:
        if ac_online() is False:
            return IDLE_SECONDS + self._load_seconds(ctx)
        return 5

    def run(self, ctx: RunContext, result: TestResult) -> None:
        log = ctx.log_path(self.name)
        bats = batteries()
        ac = ac_online()
        with open(log, "a") as f:
            f.write(f"charger connected: {ac}\n")
            for b in bats:
                f.write(f"{b}\n")
        for b in bats:
            for status, msg in health_findings(b) + charging_findings(b, ac):
                result.add(status, msg)
            parts = []
            if b.health_pct is not None:
                worn = " (worn)" if b.health_pct < HEALTH_WORN_PCT else ""
                parts.append(f"health {b.health_pct}%{worn}: {b.full_wh:.1f} of {b.design_wh:.1f} Wh")
            if b.cycles:
                parts.append(f"{b.cycles} charge cycles")
            if b.capacity_pct is not None:
                parts.append(f"charged {b.capacity_pct}% ({b.status or '?'})")
            result.metrics[f"{b.name}"] = ", ".join(parts) or "present"
            if b.info:
                result.metrics[f"{b.name}.model"] = " ".join(b.info.values())
            if ac and b.status == "Not charging" and b.capacity_pct is not None and b.capacity_pct < 95:
                result.metrics[f"{b.name}.charging"] = ("not charging although the charger is connected: a charge "
                                                        "limit set in the firmware, or the laptop refuses this battery")

        if ac is not False:
            result.metrics["drain"] = ("not measured: on charger power. To measure runtime, unplug the charger "
                                       "and run the battery test (it runs first in a full run)")
            return
        main = min(bats, key=lambda b: b.capacity_pct if b.capacity_pct is not None else 100)
        if main.capacity_pct is not None and main.capacity_pct < MIN_START_PCT:
            result.metrics["drain"] = (f"not measured: battery only {main.capacity_pct}% charged "
                                       f"(needs {MIN_START_PCT}%)")
            return
        self._drain(ctx, result, log)
        result.metrics["note"] = "plug the charger back in"

    def _sample(self) -> tuple[float, float | None, float | None, int | None]:
        bats = batteries()
        energy = sum(b.now_wh for b in bats if b.now_wh is not None) if bats else None
        power = sum(b.power_w for b in bats if b.power_w is not None) or None
        caps = [b.capacity_pct for b in bats if b.capacity_pct is not None]
        return time.monotonic(), energy, power, (min(caps) if caps else None)

    def _drain(self, ctx: RunContext, result: TestResult, log: Path) -> None:
        full = sum(b.full_wh for b in batteries() if b.full_wh) or None

        def note(text: str) -> None:
            with open(log, "a") as f:
                f.write(f"[{time.strftime('%H:%M:%S')}] {text}\n")

        # Idle: what light use (reading, a web page) roughly costs with the screen on.
        idle = []
        end = time.monotonic() + IDLE_SECONDS
        while time.monotonic() < end and not proc.ABORT.is_set():
            idle.append(self._sample())
            note(f"idle {idle[-1]}")
            time.sleep(SAMPLE_SECONDS)

        # Full CPU load, sampled from a second thread while stress-ng runs in short chunks
        # (so it can stop soon after the charge reaches LOW_STOP_PCT).
        load: list[tuple[float, float | None, float | None, int | None]] = []
        stop = threading.Event()
        low = threading.Event()

        def sampler() -> None:
            while not stop.is_set():
                s = self._sample()
                load.append(s)
                note(f"load {s}")
                if s[3] is not None and s[3] <= LOW_STOP_PCT:
                    low.set()  # the load loop stops after the current short chunk
                    return
                stop.wait(SAMPLE_SECONDS)

        t = threading.Thread(target=sampler, daemon=True)
        t.start()
        end = time.monotonic() + (self._load_seconds(ctx) if proc.have("stress-ng") else 0)
        try:
            while not (stop.is_set() or low.is_set() or proc.ABORT.is_set()):
                left = int(end - time.monotonic())
                if left < 5:
                    break
                _stressng.run(["--cpu", "0"], min(10, left), result, log, "battery load")
        finally:
            stop.set()
            t.join(timeout=10)

        for label, samples in (("idle", idle), ("full load", load)):
            s = drain_summary(samples)
            if "watts" in s:
                result.metrics[f"drain.{label}"] = (f"{s['watts']:.1f} W, about {runtime_text(full, s['watts'])} "
                                                    "on a full charge")
            else:
                result.metrics[f"drain.{label}"] = "not measured (the battery reports no power or energy readings)"
            if s["max_drop_pct"] >= SUDDEN_DROP_PCT:
                result.add(Status.WARN, f"battery charge dropped {s['max_drop_pct']:.0f}% within a few seconds "
                                        f"during {label}: a weak cell (expect sudden shutdowns)")
        if low.is_set():
            result.metrics["drain.stopped"] = f"load stopped early at {LOW_STOP_PCT}% charge"
