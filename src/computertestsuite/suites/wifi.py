# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

"""Wi-Fi: adapter, driver, radio and reception checks; throughput when connected.

Without a connection (always):
  * a Wi-Fi adapter on the PCI bus must have a network interface (driver + firmware loaded)
  * the radio must not be blocked (rfkill)
  * a scan must find access points; signal strengths show antenna health
  * the kernel log is watched for driver/firmware crashes (core.monitor)

When connected (e.g. through the terminal interface's Wi-Fi screen):
  * link signal and bitrate
  * iperf3 throughput over Wi-Fi with --iperf-server
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from ..core import proc
from ..core.base import StressTest
from ..core.context import RunContext
from ..core.model import Status, TestResult
from .network import run_iperf

SYS_NET = Path("/sys/class/net")
WEAK_LINK_DBM = -75  # connected signal weaker than this: WARN
WEAK_SCAN_DBM = -80  # even the strongest access point weaker than this: WARN (antenna?)


# ------------------------------------------------------------------ hardware discovery

def wifi_interfaces(root: Path = SYS_NET) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(i.name for i in root.iterdir() if (i / "phy80211").exists() or (i / "wireless").exists())


def wifi_pci_devices(root: Path = Path("/sys/bus/pci/devices")) -> list[dict[str, str]]:
    """PCI network controllers of class 0x0280 (wireless): slot, vendor:device id, modalias, kernel driver."""
    out = []
    if not root.is_dir():
        return out

    def rd(path: Path) -> str:
        try:
            return path.read_text().strip()
        except OSError:
            return ""

    for dev in sorted(root.iterdir()):
        if rd(dev / "class").startswith("0x0280"):
            drv = dev / "driver"
            out.append({
                "slot": dev.name,
                "id": f"{rd(dev / 'vendor').removeprefix('0x')}:{rd(dev / 'device').removeprefix('0x')}",
                "modalias": rd(dev / "modalias"),
                "driver": os.path.basename(os.readlink(drv)) if drv.is_symlink() else "",
            })
    return out


def driver_modules(modalias: str) -> list[str]:
    """Kernel modules on this system that claim a device (empty: Linux on this stick has no driver for it)."""
    if not modalias or not proc.have("modprobe"):
        return []
    return proc.capture(["modprobe", "--resolve-alias", modalias], timeout=10).split()


# Chips that only Broadcom's proprietary "wl" driver supports; the open drivers (brcmfmac, b43) do not.
WL_ONLY_HINT = ("Broadcom chips like this one need Broadcom's proprietary 'wl' driver (broadcom-wl), "
                "which is not on the stick")


def missing_driver_verdict(dev: dict[str, str], modules: list[str]) -> tuple[Status, str]:
    """Verdict for a Wi-Fi PCI device that has no network interface."""
    where = f"Wi-Fi chip {dev['id']} at {dev['slot']}"
    if dev["driver"]:
        return Status.FAIL, (f"{where}: driver {dev['driver']} is loaded but created no Wi-Fi interface "
                             "(firmware missing or failed; see kernel log)")
    if modules:
        return Status.FAIL, (f"{where}: driver {', '.join(modules)} exists but did not take the device "
                             "(load or probe failed; see kernel log)")
    hint = f"; {WL_ONLY_HINT}" if dev["id"].startswith("14e4:") else ""
    return Status.SKIP, f"{where}: no Linux driver for this chip on the stick, Wi-Fi not tested{hint}"


def interface_driver(iface: str, root: Path = SYS_NET) -> str:
    drv = root / iface / "device" / "driver"
    return os.path.basename(os.readlink(drv)) if drv.is_symlink() else ""


def rfkill_wlan(root: Path = Path("/sys/class/rfkill")) -> list[dict[str, str | bool]]:
    out = []
    if not root.is_dir():
        return out
    for rf in sorted(root.glob("rfkill*")):
        def rd(name: str) -> str:
            try:
                return (rf / name).read_text().strip()
            except OSError:
                return ""
        if rd("type") == "wlan":
            out.append({"name": rd("name"), "soft": rd("soft") == "1", "hard": rd("hard") == "1"})
    return out


# ------------------------------------------------------------------ scanning

@dataclass
class WifiNetwork:
    bssid: str
    ssid: str
    signal_dbm: float | None
    freq_mhz: float | None
    secure: bool

    @property
    def band(self) -> str:
        f = self.freq_mhz or 0
        return "6 GHz" if f >= 5925 else "5 GHz" if f >= 5000 else "2.4 GHz" if f else "?"


_ESC = re.compile(rb"\\x([0-9a-fA-F]{2})")


def unescape_ssid(s: str) -> str:
    """iw prints non-printable SSID bytes as \\xNN; turn them back into text."""
    raw = _ESC.sub(lambda m: bytes([int(m.group(1), 16)]), s.encode("utf-8", "surrogateescape"))
    return raw.decode("utf-8", "replace")


def parse_iw_scan(text: str) -> list[WifiNetwork]:
    nets: list[WifiNetwork] = []
    cur: dict | None = None

    def flush() -> None:
        if cur is not None:
            nets.append(WifiNetwork(cur["bssid"], cur.get("ssid", ""), cur.get("signal"), cur.get("freq"),
                                    cur.get("secure", False)))

    for line in text.splitlines():
        m = re.match(r"^BSS ([0-9a-fA-F:]{17})", line)
        if m:
            flush()
            cur = {"bssid": m.group(1).lower()}
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.startswith("SSID:"):
            cur["ssid"] = unescape_ssid(s[5:].strip())
        elif s.startswith("signal:"):
            m = re.search(r"(-?\d+(?:\.\d+)?)\s*dBm", s)
            if m:
                cur["signal"] = float(m.group(1))
        elif s.startswith("freq:"):
            m = re.search(r"(\d+(?:\.\d+)?)", s)
            if m:
                cur["freq"] = float(m.group(1))
        elif s.startswith("capability:") and "Privacy" in s:
            cur["secure"] = True
        elif s.startswith(("RSN:", "WPA:")):
            cur["secure"] = True
    flush()
    return nets


def best_per_ssid(nets: list[WifiNetwork]) -> list[WifiNetwork]:
    """One entry per visible network name (strongest access point), strongest first; hidden ones dropped."""
    best: dict[str, WifiNetwork] = {}
    for n in nets:
        if not n.ssid:
            continue
        cur = best.get(n.ssid)
        if cur is None or (n.signal_dbm or -999) > (cur.signal_dbm or -999):
            best[n.ssid] = n
    return sorted(best.values(), key=lambda n: -(n.signal_dbm if n.signal_dbm is not None else -999))


def scan(iface: str) -> tuple[list[WifiNetwork], str]:
    """Scan for access points. Returns (networks, problem text or '')."""
    r = proc.run(["iw", "dev", iface, "scan"], timeout=30)
    if r.returncode == 0:
        return parse_iw_scan(r.output), ""
    problem = r.output.strip().splitlines()[-1] if r.output.strip() else f"iw exit {r.returncode}"
    # Busy (iwd is scanning) or not permitted: ask iwd to scan, then read the cached results.
    if proc.have("iwctl"):
        proc.run(["iwctl", "station", iface, "scan"], timeout=15)
        time.sleep(4)
    d = proc.run(["iw", "dev", iface, "scan", "dump"], timeout=15)
    nets = parse_iw_scan(d.output) if d.returncode == 0 else []
    return nets, "" if nets else problem


# ------------------------------------------------------------------ link / connection

def parse_iw_link(text: str) -> dict[str, str | float | bool | None]:
    info: dict[str, str | float | bool | None] = {"connected": False, "ssid": None, "signal_dbm": None,
                                                  "tx_mbps": None, "rx_mbps": None, "freq_mhz": None}
    if not text.startswith("Connected to"):
        return info
    info["connected"] = True
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("SSID:"):
            info["ssid"] = unescape_ssid(s[5:].strip())
        elif s.startswith("signal:"):
            m = re.search(r"(-?\d+(?:\.\d+)?)\s*dBm", s)
            info["signal_dbm"] = float(m.group(1)) if m else None
        elif s.startswith(("tx bitrate:", "rx bitrate:")):
            m = re.search(r"(\d+(?:\.\d+)?)\s*MBit/s", s)
            info["tx_mbps" if s.startswith("tx") else "rx_mbps"] = float(m.group(1)) if m else None
        elif s.startswith("freq:"):
            m = re.search(r"(\d+(?:\.\d+)?)", s)
            info["freq_mhz"] = float(m.group(1)) if m else None
    return info


def link_info(iface: str) -> dict[str, str | float | bool | None]:
    return parse_iw_link(proc.capture(["iw", "dev", iface, "link"]))


def ipv4_address(iface: str) -> str:
    out = proc.capture(["ip", "-4", "-o", "addr", "show", "dev", iface])
    m = re.search(r"\binet (\d+\.\d+\.\d+\.\d+)", out)
    return m.group(1) if m else ""


def connect(iface: str, ssid: str, passphrase: str | None, wait_ip: float = 20) -> tuple[bool, str]:
    """Connect with iwd (the live system's Wi-Fi service). Never logs the passphrase."""
    if not proc.have("iwctl"):
        return False, "connecting needs iwd (iwctl), which the live system has"
    cmd = ["iwctl"]
    if passphrase:
        cmd += ["--passphrase", passphrase]
    cmd += ["station", iface, "connect", ssid]
    r = proc.run(cmd, timeout=45)  # no log file: the command line contains the passphrase
    if r.returncode != 0:
        msg = r.output.strip().splitlines()[-1] if r.output.strip() else f"iwctl exit {r.returncode}"
        return False, f"could not connect to {ssid}: {msg}"
    deadline = time.monotonic() + wait_ip
    while time.monotonic() < deadline:
        ip = ipv4_address(iface)
        if ip:
            return True, f"connected to {ssid}, IP {ip}"
        time.sleep(1)
    return True, f"connected to {ssid}, but no IP address yet (DHCP)"


def disconnect(iface: str) -> tuple[bool, str]:
    if not proc.have("iwctl"):
        return False, "disconnecting needs iwd (iwctl)"
    r = proc.run(["iwctl", "station", iface, "disconnect"], timeout=15)
    return r.returncode == 0, "disconnected" if r.returncode == 0 else (r.output.strip() or "failed")


# ------------------------------------------------------------------ the test

class Wifi(StressTest):
    name = "wifi"
    subsystem = "network"
    description = "Wi-Fi adapter, driver and radio; scan for access points; throughput if connected"
    required_tools = ["iw"]
    order = 61

    def hardware_available(self, ctx: RunContext) -> str | None:
        if not wifi_interfaces() and not wifi_pci_devices():
            return "no Wi-Fi adapter"
        return None

    def estimate(self, ctx: RunContext) -> int:
        connected = any(link_info(i)["connected"] for i in wifi_interfaces())
        return 20 + (ctx.seconds if ctx.iperf_server and connected else 0)

    def run(self, ctx: RunContext, result: TestResult) -> None:
        ifaces = wifi_interfaces()
        pci = wifi_pci_devices()
        if pci and not ifaces:
            with open(ctx.log_path(self.name), "a") as f:
                for dev in pci:
                    modules = driver_modules(dev["modalias"])
                    f.write(f"{dev['slot']} {dev['id']} modalias={dev['modalias']} bound driver="
                            f"{dev['driver'] or '-'} matching modules={', '.join(modules) or '-'}\n")
                    result.add(*missing_driver_verdict(dev, modules))
            return
        for rf in rfkill_wlan():
            if rf["hard"]:
                result.add(Status.WARN, f"Wi-Fi radio {rf['name']} is switched off by a hardware switch or the BIOS")
            elif rf["soft"] and ctx.is_root:
                proc.run(["rfkill", "unblock", "wifi"], timeout=10)
        if any(rf["hard"] for rf in rfkill_wlan()):
            return

        for iface in ifaces:
            result.metrics[f"{iface}.driver"] = interface_driver(iface) or "?"
            if ctx.is_root:
                proc.run(["ip", "link", "set", iface, "up"], timeout=10)
            nets, problem = scan(iface)
            visible = best_per_ssid(nets)
            with open(ctx.log_path(self.name), "a") as f:
                f.write(f"scan {iface}: {len(nets)} access points, {len(visible)} named networks\n")
                for n in visible:
                    f.write(f"  {n.signal_dbm} dBm  {n.band:7}  {'secured' if n.secure else 'open   '}  {n.ssid}\n")
            if not nets:
                result.add(Status.WARN, f"{iface}: scan found no access points"
                                        + (f" ({problem})" if problem else " (nothing in range, or antenna problem)"))
            else:
                strongest = max((n.signal_dbm for n in nets if n.signal_dbm is not None), default=None)
                bands = sorted({n.band for n in nets if n.band != "?"})
                result.metrics[f"{iface}.scan"] = (f"{len(nets)} access points, strongest {strongest:.0f} dBm, "
                                                   f"bands: {', '.join(bands) or '?'}"
                                                   if strongest is not None else f"{len(nets)} access points")
                if strongest is not None and strongest < WEAK_SCAN_DBM:
                    result.add(Status.WARN, f"{iface}: strongest access point only {strongest:.0f} dBm "
                                            "(check antenna cables if an access point is close)")

            link = link_info(iface)
            if not link["connected"]:
                result.metrics[f"{iface}.link"] = "not connected (connect from the interface's Wi-Fi screen)"
                if ctx.iperf_server:
                    result.metrics[f"{iface}.iperf3"] = "skipped: not connected"
                continue
            sig, tx = link["signal_dbm"], link["tx_mbps"]
            result.metrics[f"{iface}.link"] = (f"{link['ssid']}  {sig:.0f} dBm  tx {tx:.0f} Mbit/s"
                                               if sig is not None and tx is not None else str(link["ssid"]))
            if sig is not None and sig < WEAK_LINK_DBM:
                result.add(Status.WARN, f"{iface}: weak signal {sig:.0f} dBm to {link['ssid']}")
            if ctx.iperf_server and proc.have("iperf3"):
                # Wi-Fi rarely reaches its PHY rate; only warn below 25% of the negotiated bitrate.
                run_iperf(ctx, result, self.name, link_mbps=tx or 0, min_ratio=0.25,
                          extra=["--bind-dev", iface], label=f"{iface}.iperf3")
