# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

"""Block device enumeration and safety checks."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import proc

LSBLK_COLS = "NAME,PATH,TYPE,SIZE,MODEL,SERIAL,TRAN,ROTA,RM,RO,MOUNTPOINTS,LABEL,FSTYPE"
# Mount points that identify the live medium / the running system.
PROTECTED_MOUNTS = {"/", "/boot", "/boot/efi", "/efi", "/run/archiso/bootmnt", "/run/archiso/cowspace"}
PROTECTED_LABEL_PREFIXES = ("CTS_",)  # ISO label and the CTS_REPORTS partition


@dataclass
class Disk:
    name: str
    path: str
    size: int
    model: str = ""
    serial: str = ""
    tran: str = ""
    rotational: bool = False
    removable: bool = False
    readonly: bool = False
    mountpoints: list[str] = field(default_factory=list)  # of the disk and all its partitions
    labels: list[str] = field(default_factory=list)

    @property
    def is_nvme(self) -> bool:
        return self.tran == "nvme" or self.name.startswith("nvme")

    @property
    def is_usb(self) -> bool:
        return self.tran == "usb"

    @property
    def protected_reason(self) -> str | None:
        """Why this disk must never be written to, or None."""
        hits = sorted(set(self.mountpoints) & PROTECTED_MOUNTS)
        if hits:
            return f"holds the running system ({', '.join(hits)})"
        for lbl in self.labels:
            if lbl.upper().startswith(PROTECTED_LABEL_PREFIXES):
                return f"is the CTS boot/report medium (label {lbl})"
        return None

    @property
    def in_use_reason(self) -> str | None:
        if self.mountpoints:
            return "has mounted filesystems: " + ", ".join(sorted(self.mountpoints))
        return None

    def describe(self) -> str:
        gb = self.size / 1e9
        kind = "NVMe" if self.is_nvme else ("HDD" if self.rotational else "SSD")
        if self.is_usb:
            kind = "USB " + kind
        model = self.model or "unknown model"
        return f"{self.path}  {gb:,.1f} GB  {kind}  {model}  {self.serial}".rstrip()


def _walk(node: dict, mounts: list[str], labels: list[str]) -> None:
    for mp in node.get("mountpoints") or []:
        if mp:
            mounts.append(mp)
    if node.get("mountpoint"):  # older util-linux
        mounts.append(node["mountpoint"])
    if node.get("label"):
        labels.append(node["label"])
    for child in node.get("children") or []:
        _walk(child, mounts, labels)


def parse_lsblk(text: str) -> list[Disk]:
    data = json.loads(text) if text.strip() else {"blockdevices": []}
    disks = []
    for dev in data.get("blockdevices", []):
        if dev.get("type") != "disk":
            continue
        name = dev.get("name", "")
        if name.startswith(("zram", "ram", "loop")):
            continue
        mounts: list[str] = []
        labels: list[str] = []
        _walk(dev, mounts, labels)
        disks.append(
            Disk(
                name=name,
                path=dev.get("path") or f"/dev/{name}",
                size=int(dev.get("size") or 0),
                model=(dev.get("model") or "").strip(),
                serial=(dev.get("serial") or "").strip(),
                tran=dev.get("tran") or "",
                rotational=bool(dev.get("rota")),
                removable=bool(dev.get("rm")),
                readonly=bool(dev.get("ro")),
                mountpoints=mounts,
                labels=labels,
            )
        )
    return disks


def list_disks() -> list[Disk]:
    out = proc.capture(["lsblk", "-J", "-b", "-o", LSBLK_COLS])
    try:
        return [d for d in parse_lsblk(out) if d.size > 0]
    except json.JSONDecodeError:
        return []


def test_targets(disks: list[Disk], requested: list[str] | None = None) -> list[Disk]:
    """Disks to test: the requested ones, or every disk except the CTS medium."""
    if requested:
        by_path = {d.path: d for d in disks} | {d.name: d for d in disks}
        missing = [r for r in requested if r not in by_path and r.removeprefix("/dev/") not in by_path]
        if missing:
            raise ValueError(f"unknown disk(s): {', '.join(missing)}")
        return [by_path.get(r) or by_path[r.removeprefix("/dev/")] for r in requested]
    return [d for d in disks if not any(lbl.upper().startswith(PROTECTED_LABEL_PREFIXES) for lbl in d.labels)
            and "/run/archiso/bootmnt" not in d.mountpoints]


def destructive_refusal(disk: Disk) -> str | None:
    """Return why a destructive test on ``disk`` is not allowed, or None if allowed."""
    if disk.readonly:
        return "device is read-only"
    return disk.protected_reason or disk.in_use_reason
