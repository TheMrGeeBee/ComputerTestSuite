# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

"""Hardware inventory collected at the start of every run."""

from __future__ import annotations

import json
import os
import platform
import re
from pathlib import Path
from typing import Any

from . import proc
from .disks import list_disks


def meminfo() -> dict[str, int]:
    """/proc/meminfo values in bytes."""
    out: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                mult = 1024 if len(parts) > 1 and parts[1] == "kB" else 1
                out[key] = int(parts[0]) * mult
    except OSError:
        pass
    return out


def cpu_info() -> dict[str, Any]:
    info: dict[str, Any] = {"logical_cpus": os.cpu_count() or 1}
    raw = proc.capture(["lscpu", "-J"])
    try:
        for f in json.loads(raw)["lscpu"]:
            key = f["field"].rstrip(":")
            if key in ("Model name", "Vendor ID", "Socket(s)", "Core(s) per socket",
                       "Thread(s) per core", "CPU max MHz", "Architecture", "Virtualization",
                       "Hypervisor vendor"):
                info[key] = f["data"]
            for child in f.get("children", []) or []:
                ck = child["field"].rstrip(":")
                if ck in ("Model name", "Socket(s)", "Core(s) per socket", "Thread(s) per core", "CPU max MHz"):
                    info[ck] = child["data"]
    except (json.JSONDecodeError, KeyError, TypeError):
        try:
            txt = Path("/proc/cpuinfo").read_text()
            m = re.search(r"model name\s*:\s*(.+)", txt)
            if m:
                info["Model name"] = m.group(1)
        except OSError:
            pass
    return info


def dmi() -> dict[str, str]:
    out = {}
    base = Path("/sys/class/dmi/id")
    for key in ("sys_vendor", "product_name", "board_vendor", "board_name", "bios_vendor",
                "bios_version", "bios_date"):
        try:
            out[key] = (base / key).read_text().strip()
        except OSError:
            pass
    return out


def memory_modules() -> list[dict[str, str]]:
    """DIMM list from dmidecode (root only)."""
    raw = proc.capture(["dmidecode", "-t", "17"])
    modules = []
    for block in raw.split("\n\n"):
        if "Memory Device" not in block:
            continue
        fields = dict(
            (k.strip(), v.strip())
            for k, _, v in (ln.partition(":") for ln in block.splitlines() if ":" in ln)
        )
        size = fields.get("Size", "")
        if not size or "No Module" in size:
            continue
        modules.append({k: fields.get(k, "") for k in
                        ("Locator", "Size", "Type", "Speed", "Configured Memory Speed", "Manufacturer", "Part Number")})
    return modules


def gpus() -> list[str]:
    out = proc.capture(["lspci", "-mm"])
    res = []
    for line in out.splitlines():
        parts = re.findall(r'"([^"]*)"', line)
        if len(parts) >= 3 and re.search(r"VGA|3D|Display", parts[0]):
            res.append(f"{parts[1]} {parts[2]}")
    return res


def drm_cards() -> list[dict[str, str]]:
    """DRM cards with their kernel driver."""
    cards = []
    for card in sorted(Path("/sys/class/drm").glob("card[0-9]")):
        drv = card / "device" / "driver"
        driver = os.path.basename(os.readlink(drv)) if drv.is_symlink() else ""
        cards.append({"card": card.name, "driver": driver, "render": _render_node(card)})
    return cards


def _render_node(card: Path) -> str:
    dev = card / "device" / "drm"
    if dev.is_dir():
        for n in dev.iterdir():
            if n.name.startswith("renderD"):
                return f"/dev/dri/{n.name}"
    return ""


def net_interfaces() -> list[dict[str, Any]]:
    out = []
    for iface in sorted(Path("/sys/class/net").iterdir()) if Path("/sys/class/net").is_dir() else []:
        if iface.name == "lo" or not (iface / "device").exists():
            continue  # skip virtual interfaces

        def rd(name: str) -> str:
            try:
                return (iface / name).read_text().strip()
            except OSError:
                return ""

        out.append({
            "name": iface.name,
            "mac": rd("address"),
            "state": rd("operstate"),
            "speed_mbps": int(rd("speed")) if rd("speed").lstrip("-").isdigit() and int(rd("speed")) > 0 else None,
            "wireless": (iface / "wireless").exists(),
        })
    return out


def is_virtual_machine() -> bool:
    return bool(cpu_info().get("Hypervisor vendor")) or "QEMU" in dmi().get("sys_vendor", "")


def collect() -> dict[str, Any]:
    mi = meminfo()
    return {
        "hostname": platform.node(),
        "kernel": platform.release(),
        "dmi": dmi(),
        "cpu": cpu_info(),
        "memory": {"total_bytes": mi.get("MemTotal", 0), "modules": memory_modules()},
        "disks": [d.describe() for d in list_disks()],
        "gpus": gpus(),
        "drm": drm_cards(),
        "network": net_interfaces(),
    }


def format_info(info: dict[str, Any]) -> str:
    lines = []
    d = info["dmi"]
    lines.append(f"System : {d.get('sys_vendor', '?')} {d.get('product_name', '')}".rstrip())
    lines.append(f"Board  : {d.get('board_vendor', '?')} {d.get('board_name', '')}  "
                 f"BIOS {d.get('bios_version', '?')} ({d.get('bios_date', '?')})")
    c = info["cpu"]
    lines.append(f"CPU    : {c.get('Model name', '?')}  ({c['logical_cpus']} threads)")
    m = info["memory"]
    lines.append(f"Memory : {m['total_bytes'] / 2**30:.1f} GiB")
    for mod in m["modules"]:
        lines.append(f"         {mod['Locator']}: {mod['Size']} {mod['Type']} "
                     f"{mod['Configured Memory Speed'] or mod['Speed']} {mod['Manufacturer']} {mod['Part Number']}")
    lines.append("Disks  :" + ("" if info["disks"] else " none"))
    lines += [f"         {x}" for x in info["disks"]]
    lines.append("GPUs   :" + ("" if info["gpus"] else " none"))
    lines += [f"         {x}" for x in info["gpus"]]
    for card in info["drm"]:
        lines.append(f"         {card['card']}: driver={card['driver'] or 'none'} {card['render']}")
    lines.append("Network:" + ("" if info["network"] else " none"))
    for n in info["network"]:
        sp = f"{n['speed_mbps']} Mb/s" if n["speed_mbps"] else "no link"
        lines.append(f"         {n['name']} {n['mac']} {n['state']} {sp}{' (wifi)' if n['wireless'] else ''}")
    lines.append(f"Kernel : {info['kernel']}")
    return "\n".join(lines)
