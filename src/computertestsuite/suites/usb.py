# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

"""USB: enumerate the bus and watch for enumeration/over-current errors.

USB mass-storage devices are also exercised by storage-smart/storage-read.
"""

from __future__ import annotations

import time
from pathlib import Path

from ..core import proc
from ..core.base import StressTest
from ..core.context import RunContext
from ..core.model import Status, TestResult

USB_ROOT = Path("/sys/bus/usb/devices")


def usb_devices(root: Path = USB_ROOT) -> list[dict[str, str]]:
    devs = []
    if not root.is_dir():
        return devs
    for d in sorted(root.iterdir()):
        if ":" in d.name or not (d / "idVendor").exists():
            continue  # interfaces, not devices

        def rd(n: str) -> str:
            try:
                return (d / n).read_text().strip()
            except OSError:
                return ""

        devs.append({
            "port": d.name,
            "id": f"{rd('idVendor')}:{rd('idProduct')}",
            "product": rd("product") or rd("manufacturer") or "unknown",
            "speed_mbps": rd("speed"),
            "max_child": rd("maxchild"),
        })
    return devs


class Usb(StressTest):
    name = "usb"
    subsystem = "usb"
    description = "USB device inventory and link speeds; watches the kernel log for USB errors"
    order = 70

    def hardware_available(self, ctx: RunContext) -> str | None:
        return None if usb_devices() else "no USB bus found"

    def estimate(self, ctx: RunContext) -> int:
        return 30

    def run(self, ctx: RunContext, result: TestResult) -> None:
        before = {d["port"] for d in usb_devices()}
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not proc.ABORT.is_set():
            time.sleep(1)
        devs = usb_devices()
        after = {d["port"] for d in devs}
        for port in sorted(before - after):
            result.add(Status.WARN, f"USB device at {port} disappeared during the test")
        for d in devs:
            if d["max_child"] not in ("", "0"):
                continue  # hubs / root hubs
            result.metrics[f"{d['port']} {d['id']}"] = f"{d['product']} @ {d['speed_mbps']} Mb/s"
        result.metrics["devices"] = len(devs)
