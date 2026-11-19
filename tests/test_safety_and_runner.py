# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

import json

import pytest

from computertestsuite.core.base import StressTest
from computertestsuite.core.context import RunContext
from computertestsuite.core.disks import destructive_refusal, parse_lsblk, test_targets as disk_targets
from computertestsuite.core.model import Status
from computertestsuite.core.monitor import Monitor
from computertestsuite.core.registry import discover, select
from computertestsuite.core.runner import Runner


def lsblk(*devs):
    return json.dumps({"blockdevices": list(devs)})


def dev(name, children=(), **kw):
    d = {"name": name, "path": f"/dev/{name}", "type": "disk", "size": 500 * 10**9, "model": "X",
         "serial": "S", "tran": kw.pop("tran", "sata"), "rota": False, "rm": False, "ro": kw.pop("ro", False),
         "mountpoints": [None], "label": None, "children": list(children)}
    d.update(kw)
    return d


def part(name, mount=None, label=None):
    return {"name": name, "type": "part", "mountpoints": [mount], "label": label}


BOOT_USB = dev("sdb", [part("sdb1", "/run/archiso/bootmnt", "CTS_202611"), part("sdb2", None, "CTS_REPORTS")],
               tran="usb")
# Ventoy stick as seen from the host: the ISO-mapping device-mapper layer is absent here,
# so only the partition labels identify it.
VENTOY = dev("sde", [part("sde1", "/run/media/user/Ventoy", "Ventoy"), part("sde2", None, "VTOYEFI")],
             tran="usb")
BLANK = dev("sdc")
SYSTEM = dev("nvme0n1", [part("nvme0n1p1", "/boot"), part("nvme0n1p2", "/")], tran="nvme")
DATA_MOUNTED = dev("sda", [part("sda1", "/mnt/data")])


def test_parse_lsblk_skips_virtual():
    disks = parse_lsblk(lsblk(BLANK, dev("zram0"), {"name": "sr0", "type": "rom"}))
    assert [d.name for d in disks] == ["sdc"]


def test_boot_usb_excluded_from_default_targets():
    disks = parse_lsblk(lsblk(BOOT_USB, VENTOY, BLANK, SYSTEM))
    assert [d.name for d in disk_targets(disks)] == ["sdc", "nvme0n1"]


@pytest.mark.parametrize("d,allowed", [(BOOT_USB, False), (SYSTEM, False), (DATA_MOUNTED, False),
                                       (dict(VENTOY, children=[part("sde1", None, "Ventoy")]), False),
                                       (dev("sde", ro=True), False), (BLANK, True)])
def test_destructive_refusal(d, allowed):
    disk = parse_lsblk(lsblk(d))[0]
    assert (destructive_refusal(disk) is None) == allowed


def test_unknown_requested_disk():
    with pytest.raises(ValueError):
        disk_targets(parse_lsblk(lsblk(BLANK)), ["/dev/sdz"])


def test_registry_and_selection():
    tests = discover()
    names = {t.name for t in tests}
    assert {"cpu", "memory-pattern", "memory-stress", "storage-smart", "storage-read", "storage-write",
            "gpu", "network", "usb", "system"} <= names
    default = select(tests, None, None)
    assert "storage-write" not in {t.name for t in default}  # destructive is opt-in
    assert {t.name for t in select(tests, ["memory"], None)} == {"memory-pattern", "memory-stress"}
    assert "gpu" not in {t.name for t in select(tests, None, ["gpu"])}
    with pytest.raises(ValueError):
        select(tests, ["nope"], None)


def test_storage_write_refuses_without_flags():
    t = next(t for t in discover() if t.name == "storage-write")
    ctx = RunContext()
    assert t.hardware_available(ctx) is not None
    ctx.destructive = True
    assert "device" in t.hardware_available(ctx)


class _Ok(StressTest):
    name = "_ok"
    subsystem = "_t"

    def estimate(self, ctx):
        return 0

    def run(self, ctx, result):
        result.metrics["ran"] = True


class _Boom(StressTest):
    name = "_boom"
    subsystem = "_t"

    def run(self, ctx, result):
        raise RuntimeError("bug")


class _Missing(StressTest):
    name = "_missing"
    subsystem = "_t"
    required_tools = ["definitely-not-a-real-tool-xyz"]

    def run(self, ctx, result):
        raise AssertionError("must not run")


def test_runner_statuses(tmp_path):
    ctx = RunContext(log_dir=tmp_path)
    mon = Monitor(interval=0.05, root=tmp_path, kmsg="/nonexistent")
    results = Runner(ctx, [_Ok(), _Boom(), _Missing()], monitor=mon).run()
    by = {r.name: r for r in results}
    assert by["_ok"].status == Status.PASS and by["_ok"].metrics["ran"]
    assert by["_boom"].status == Status.ERROR
    assert "RuntimeError" in (tmp_path / "_boom.log").read_text()
    assert by["_missing"].status == Status.SKIP


def test_ventoy_remount_device_preferred():
    from computertestsuite.core.storage_target import devices_with_label

    # Layout seen on a laptop booted via Ventoy with VTOY_LINUX_REMOUNT=1.
    tree = {"blockdevices": [{"path": "/dev/sda", "type": "disk", "label": None, "children": [
        {"path": "/dev/sda1", "type": "part", "label": "Ventoy", "children": [
            {"path": "/dev/mapper/ventoy", "type": "dm", "label": "CTS_202611"},
            {"path": "/dev/mapper/sda1", "type": "dm", "label": "Ventoy"}]},
        {"path": "/dev/sda2", "type": "part", "label": "VTOYEFI"}]}]}
    assert devices_with_label(json.dumps(tree), "Ventoy") == ["/dev/mapper/sda1", "/dev/sda1"]
    assert devices_with_label(json.dumps(tree), "CTS_REPORTS") == []
    assert devices_with_label("not json", "Ventoy") == []
