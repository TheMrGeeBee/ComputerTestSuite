# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

"""Background hardware health monitor.

Samples hwmon sensors, CPU frequency, thermal-throttle counters and EDAC
counters, and tails the kernel log for hardware error messages. Tests do not
talk to the monitor directly; the runner asks it to evaluate the time window
in which a test ran.
"""

from __future__ import annotations

import errno
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .model import Finding, Status

SYS = Path("/sys")

# Fallback critical temperatures (°C) for chips that do not expose *_crit.
FALLBACK_CRIT = {"k10temp": 95.0, "zenpower": 95.0, "coretemp": 100.0, "cpu_thermal": 95.0}
NEAR_CRIT_MARGIN = 5.0


# ---------------------------------------------------------------- kernel log

@dataclass(frozen=True)
class KmsgRule:
    pattern: re.Pattern[str]
    status: Status
    category: str


def _r(p: str) -> re.Pattern[str]:
    return re.compile(p, re.IGNORECASE)


KMSG_RULES: list[KmsgRule] = [
    # Order matters: the first match wins.
    KmsgRule(_r(r"\bAER:.*\bCorrected\b"), Status.WARN, "pcie"),
    KmsgRule(_r(r"\bAER:.*(Uncorrected|Fatal)|PCIe Bus Error.*severity=(Uncorrected|Fatal)"), Status.FAIL, "pcie"),
    KmsgRule(_r(r"EDAC.*\b(UE|uncorrect)"), Status.FAIL, "memory"),
    KmsgRule(_r(r"EDAC.*\b(CE|correct)"), Status.WARN, "memory"),
    KmsgRule(_r(r"\bmce:.*(Hardware Error|CPU \d+: Machine Check)|Machine check events logged|\[Hardware Error\]"), Status.FAIL, "cpu"),
    KmsgRule(_r(r"\b(soft lockup|hard LOCKUP)\b|rcu_\w+ (detected )?stalls?|\bBUG:|\bOops\b|general protection fault|Kernel panic"), Status.FAIL, "system"),
    KmsgRule(_r(r"critical medium error|Buffer I/O error|blk_update_request: (critical|I/O)|\bI/O error, dev"), Status.FAIL, "storage"),
    KmsgRule(_r(r"\bata\d+(\.\d+)?: (exception Emask|failed command|hard resetting link|SError)"), Status.FAIL, "storage"),
    KmsgRule(_r(r"\bnvme\w*: .*(I/O \d+ (QID \d+ )?timeout|controller is down|Device not ready|resetting controller|Abort status)"), Status.FAIL, "storage"),
    KmsgRule(_r(r"amdgpu.*(GPU reset|ring \S+ timeout|page fault|GPU fault)|i915.*(GPU HANG|Resetting)|\bxe .*(GT reset|timed out)|nouveau.*(fault|timeout)|NVRM: Xid"), Status.FAIL, "gpu"),
    KmsgRule(_r(r"NETDEV WATCHDOG|transmit queue \d+ timed out|Tx Unit Hang"), Status.FAIL, "network"),
    KmsgRule(_r(r"(iwlwifi|ath\d+k|mt7\w*|mt76\w*|rtw\d+\w*|rtl8\w*|brcmfmac).*"
                r"(Microcode SW error|firmware (crashed|has halted)|FW (error|crash)|Firmware trap|NMI_INTERRUPT"
                r"|fw crash|failed to (start|run) .*ucode|Hardware error detected)"), Status.FAIL, "wifi"),
    KmsgRule(_r(r"usb \S+: (device descriptor read.*error|device not accepting address|unable to enumerate)|over-current"), Status.WARN, "usb"),
    KmsgRule(_r(r"temperature above threshold|cpu clock throttled|thermal.*critical"), Status.WARN, "thermal"),
    KmsgRule(_r(r"\bOut of memory: Killed process|oom-kill:"), Status.WARN, "memory"),
    KmsgRule(_r(r"\bsegfault at\b|traps: .* (general protection|trap divide error|trap invalid opcode)"), Status.WARN, "system"),
]


def classify_kmsg(line: str) -> KmsgRule | None:
    for rule in KMSG_RULES:
        if rule.pattern.search(line):
            return rule
    return None


def parse_kmsg_record(raw: str) -> tuple[int, str]:
    """Parse a /dev/kmsg record 'prio,seq,usec,flags;message' -> (priority, message)."""
    head, _, msg = raw.partition(";")
    try:
        prio = int(head.split(",", 1)[0]) & 7
    except ValueError:
        prio = 6
    return prio, msg.split("\n", 1)[0].strip()


@dataclass
class KernelEvent:
    t: float
    message: str
    status: Status
    category: str


# ---------------------------------------------------------------- sensors

@dataclass
class TempSensor:
    key: str          # e.g. "k10temp/Tctl"
    path: Path
    crit: float | None


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _read_num(path: Path) -> float | None:
    v = _read(path)
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


def discover_temp_sensors(root: Path = SYS) -> list[TempSensor]:
    sensors: list[TempSensor] = []
    hwmon = root / "class" / "hwmon"
    if not hwmon.is_dir():
        return sensors
    seen: dict[str, int] = {}
    for hw in sorted(hwmon.iterdir()):
        chip = _read(hw / "name") or hw.name
        n = seen.get(chip, 0)
        seen[chip] = n + 1
        chip_id = chip if n == 0 else f"{chip}{n}"
        for inp in sorted(hw.glob("temp*_input")):
            base = inp.name[: -len("_input")]
            label = _read(hw / f"{base}_label") or base
            crit = _read_num(hw / f"{base}_crit")
            if crit is not None:
                crit /= 1000.0
                if not (20.0 < crit < 200.0):
                    crit = None
            if crit is None:
                crit = FALLBACK_CRIT.get(chip)
            sensors.append(TempSensor(f"{chip_id}/{label}", inp, crit))
    return sensors


def read_counters(root: Path = SYS) -> dict[str, int]:
    """Monotonic hardware error/throttle counters."""
    out: dict[str, int] = {}
    cpu = root / "devices" / "system" / "cpu"
    core = pkg = 0
    for f in cpu.glob("cpu[0-9]*/thermal_throttle/core_throttle_count"):
        core += int(_read_num(f) or 0)
    for f in cpu.glob("cpu[0-9]*/thermal_throttle/package_throttle_count"):
        pkg += int(_read_num(f) or 0)
    out["throttle_core"] = core
    out["throttle_package"] = pkg
    ce = ue = 0
    for mc in (root / "devices" / "system" / "edac" / "mc").glob("mc[0-9]*"):
        ce += int(_read_num(mc / "ce_count") or 0)
        ue += int(_read_num(mc / "ue_count") or 0)
    out["edac_ce"] = ce
    out["edac_ue"] = ue
    return out


def read_cpu_freqs_mhz(root: Path = SYS) -> list[float]:
    freqs = []
    for f in (root / "devices" / "system" / "cpu").glob("cpu[0-9]*/cpufreq/scaling_cur_freq"):
        v = _read_num(f)
        if v:
            freqs.append(v / 1000.0)
    return freqs


@dataclass
class Sample:
    t: float
    temps: dict[str, float]
    freq_min: float | None
    freq_max: float | None
    freq_avg: float | None


# ---------------------------------------------------------------- monitor

@dataclass
class WindowStats:
    findings: list[Finding] = field(default_factory=list)
    temps: dict[str, dict[str, float]] = field(default_factory=dict)  # key -> min/max/avg
    freq: dict[str, float] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)  # deltas
    kernel_events: list[str] = field(default_factory=list)


class Monitor:
    def __init__(self, interval: float = 2.0, root: Path = SYS, kmsg: str = "/dev/kmsg") -> None:
        self.interval = interval
        self.root = root
        self.kmsg_path = kmsg
        self.sensors = discover_temp_sensors(root)
        self.samples: list[Sample] = []
        self.events: list[KernelEvent] = []
        self.kmsg_available = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._kmsg_fd: int | None = None

    # -- lifecycle
    def start(self) -> None:
        self._open_kmsg()
        self._threads = [threading.Thread(target=self._sample_loop, daemon=True, name="cts-sensors")]
        if self._kmsg_fd is not None:
            self._threads.append(threading.Thread(target=self._kmsg_loop, daemon=True, name="cts-kmsg"))
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5)
        if self._kmsg_fd is not None:
            os.close(self._kmsg_fd)
            self._kmsg_fd = None

    # -- sampling
    def sample_now(self) -> Sample:
        temps = {}
        for s in self.sensors:
            v = _read_num(s.path)
            if v is not None:
                temps[s.key] = v / 1000.0
        freqs = read_cpu_freqs_mhz(self.root)
        return Sample(
            time.time(),
            temps,
            min(freqs) if freqs else None,
            max(freqs) if freqs else None,
            sum(freqs) / len(freqs) if freqs else None,
        )

    def _sample_loop(self) -> None:
        while not self._stop.is_set():
            s = self.sample_now()
            with self._lock:
                self.samples.append(s)
            self._stop.wait(self.interval)

    # -- kernel log
    def _open_kmsg(self) -> None:
        try:
            fd = os.open(self.kmsg_path, os.O_RDONLY | os.O_NONBLOCK)
            os.lseek(fd, 0, os.SEEK_END)  # only new messages
        except OSError:
            return
        self._kmsg_fd = fd
        self.kmsg_available = True

    def _kmsg_loop(self) -> None:
        assert self._kmsg_fd is not None
        while not self._stop.is_set():
            try:
                raw = os.read(self._kmsg_fd, 8192)
            except BlockingIOError:
                self._stop.wait(0.5)
                continue
            except OSError as e:
                if e.errno == errno.EPIPE:  # ring buffer overrun; keep reading
                    continue
                return
            if not raw:
                self._stop.wait(0.5)
                continue
            self.feed_kmsg(raw.decode("utf-8", "replace"))

    def feed_kmsg(self, raw: str, t: float | None = None) -> None:
        _prio, msg = parse_kmsg_record(raw)
        rule = classify_kmsg(msg)
        if rule:
            with self._lock:
                self.events.append(KernelEvent(t or time.time(), msg, rule.status, rule.category))

    # -- evaluation
    def counters(self) -> dict[str, int]:
        return read_counters(self.root)

    def evaluate(self, start: float, end: float, before: dict[str, int], after: dict[str, int]) -> WindowStats:
        ws = WindowStats()
        with self._lock:
            samples = [s for s in self.samples if start <= s.t <= end]
            events = [e for e in self.events if start <= e.t <= end]

        # Temperatures
        crit_by_key = {s.key: s.crit for s in self.sensors}
        series: dict[str, list[float]] = {}
        for s in samples:
            for k, v in s.temps.items():
                series.setdefault(k, []).append(v)
        for k, vals in series.items():
            ws.temps[k] = {"min": min(vals), "max": max(vals), "avg": round(sum(vals) / len(vals), 1)}
            crit = crit_by_key.get(k)
            if crit is None:
                continue
            peak = max(vals)
            if peak >= crit:
                ws.findings.append(Finding(Status.FAIL, f"{k} reached {peak:.0f}°C (critical {crit:.0f}°C)", "thermal"))
            elif peak >= crit - NEAR_CRIT_MARGIN:
                ws.findings.append(Finding(Status.WARN, f"{k} reached {peak:.0f}°C, near critical {crit:.0f}°C", "thermal"))

        # CPU frequency
        mins = [s.freq_min for s in samples if s.freq_min is not None]
        maxs = [s.freq_max for s in samples if s.freq_max is not None]
        avgs = [s.freq_avg for s in samples if s.freq_avg is not None]
        if avgs:
            ws.freq = {"min_mhz": round(min(mins)), "max_mhz": round(max(maxs)), "avg_mhz": round(sum(avgs) / len(avgs))}

        # Counters
        for k in after:
            ws.counters[k] = after[k] - before.get(k, 0)
        thr = ws.counters.get("throttle_core", 0) + ws.counters.get("throttle_package", 0)
        if thr > 0:
            ws.findings.append(Finding(Status.WARN, f"CPU thermal throttling occurred ({thr} events)", "thermal"))
        if ws.counters.get("edac_ue", 0) > 0:
            ws.findings.append(Finding(Status.FAIL, f"{ws.counters['edac_ue']} uncorrectable memory errors (EDAC)", "memory"))
        if ws.counters.get("edac_ce", 0) > 0:
            ws.findings.append(Finding(Status.WARN, f"{ws.counters['edac_ce']} corrected memory errors (EDAC)", "memory"))

        # Kernel log: report each distinct message once, cap the list.
        seen: set[str] = set()
        for e in events:
            norm = re.sub(r"\d+", "#", e.message)
            if norm in seen:
                continue
            seen.add(norm)
            ws.kernel_events.append(e.message)
            if len(seen) <= 20:
                ws.findings.append(Finding(e.status, f"kernel: {e.message[:200]}", e.category))
        return ws

    # -- live display
    def status_line(self) -> str:
        with self._lock:
            s = self.samples[-1] if self.samples else None
        if s is None:
            return ""
        parts = []
        hottest: dict[str, float] = {}
        for k, v in s.temps.items():
            chip = k.split("/", 1)[0]
            hottest[chip] = max(hottest.get(chip, -1000.0), v)
        for chip, v in sorted(hottest.items(), key=lambda kv: -kv[1])[:4]:
            parts.append(f"{chip} {v:.0f}°C")
        if s.freq_avg:
            parts.append(f"CPU {s.freq_min / 1000:.1f}-{s.freq_max / 1000:.1f} GHz")
        with self._lock:
            n_err = sum(1 for e in self.events if e.status >= Status.FAIL)
        if n_err:
            parts.append(f"!! {n_err} kernel errors")
        return " | ".join(parts)
