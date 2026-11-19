# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

"""Network: link state, NIC error counters and (optionally) iperf3 throughput."""

from __future__ import annotations

import json
from pathlib import Path

from ..core import proc
from ..core.base import StressTest
from ..core.context import RunContext
from ..core.model import Status, TestResult
from ..core.sysinfo import net_interfaces

COUNTERS = ["rx_errors", "tx_errors", "rx_crc_errors", "rx_frame_errors", "rx_dropped", "tx_dropped",
            "rx_missed_errors", "tx_carrier_errors"]
SERIOUS = {"rx_crc_errors", "rx_frame_errors", "tx_carrier_errors"}


def read_stats(iface: str, root: Path = Path("/sys/class/net")) -> dict[str, int]:
    out = {}
    for c in COUNTERS:
        try:
            out[c] = int((root / iface / "statistics" / c).read_text())
        except (OSError, ValueError):
            pass
    return out


def iperf_bps(output: str) -> tuple[float, str]:
    """Return (received bits/s, error text)."""
    try:
        data = json.loads(output[output.find("{"):])
    except (json.JSONDecodeError, ValueError):
        return 0.0, "unparseable iperf3 output"
    if data.get("error"):
        return 0.0, data["error"]
    return float(data.get("end", {}).get("sum_received", {}).get("bits_per_second", 0.0)), ""


def run_iperf(ctx: RunContext, result: TestResult, log_name: str, link_mbps: float = 0,
              min_ratio: float = 0.7, extra: list[str] | None = None, label: str = "iperf3") -> None:
    """Upload then download throughput to ``ctx.iperf_server``, half the test time each.

    WARN when throughput is below ``min_ratio`` of ``link_mbps`` (if known).
    """
    half = max(10, ctx.seconds // 2)
    for direction, rev in (("upload", []), ("download", ["-R"])):
        if proc.ABORT.is_set():
            break
        r = proc.run(["iperf3", "-c", ctx.iperf_server, "-t", str(half), "-J", "-P", "4", *(extra or []), *rev],
                     timeout=half + 60, log=ctx.log_path(log_name))
        bps, err = iperf_bps(r.output)
        if err or r.returncode != 0:
            result.add(Status.FAIL, f"{label} {direction} failed: {err or r.returncode}")
            continue
        mbps = bps / 1e6
        result.metrics[f"{label}.{direction}_mbps"] = round(mbps)
        if link_mbps and mbps < min_ratio * link_mbps:
            result.add(Status.WARN, f"{label} {direction}: {mbps:.0f} Mb/s is below "
                                    f"{min_ratio:.0%} of the {link_mbps:.0f} Mb/s link")


class Network(StressTest):
    name = "network"
    subsystem = "network"
    description = "Wired NIC link/error counters; iperf3 throughput both ways if --iperf-server is given"
    order = 60

    def hardware_available(self, ctx: RunContext) -> str | None:
        if not [i for i in net_interfaces() if not i["wireless"]]:
            return "no wired network interfaces"
        return None

    def estimate(self, ctx: RunContext) -> int:
        return ctx.seconds if ctx.iperf_server else 10

    def run(self, ctx: RunContext, result: TestResult) -> None:
        ifaces = [i for i in net_interfaces() if not i["wireless"]]
        before = {i["name"]: read_stats(i["name"]) for i in ifaces}
        linked = [i for i in ifaces if i["state"] == "up" and i["speed_mbps"]]
        for i in ifaces:
            result.metrics[f"{i['name']}.link"] = f"{i['speed_mbps']} Mb/s" if i["speed_mbps"] else i["state"]
        if not linked:
            if ctx.iperf_server:
                result.add(Status.FAIL, f"--iperf-server {ctx.iperf_server} given, but no wired interface has a link")
            else:
                result.add(Status.SKIP, "no network cable connected: wired network not tested")
            return

        if ctx.iperf_server and proc.have("iperf3"):
            link = max((i["speed_mbps"] for i in linked), default=0)
            run_iperf(ctx, result, self.name, link_mbps=link, min_ratio=0.7)
        elif ctx.iperf_server:
            result.add(Status.SKIP, "iperf3 not installed")
        else:
            result.metrics["iperf3"] = "skipped (pass --iperf-server HOST; run 'iperf3 -s' on another machine)"

        for i in ifaces:
            after = read_stats(i["name"])
            for k, v in after.items():
                d = v - before[i["name"]].get(k, 0)
                if d > 0 and k in SERIOUS:
                    result.add(Status.WARN, f"{i['name']}: {k} increased by {d}")
                elif d > 0:
                    result.metrics[f"{i['name']}.{k}_delta"] = d
