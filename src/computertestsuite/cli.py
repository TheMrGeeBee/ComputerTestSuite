# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

"""Command-line interface: ``cts info | list | run | report``."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import __version__
from .core import report, storage_target, sysinfo
from .core.context import RunContext
from .core.disks import destructive_refusal, list_disks, test_targets
from .core.model import Status, overall
from .core.profiles import PROFILES, fmt_duration, parse_duration
from .core.registry import discover, select
from .core.runner import Runner


def _csv(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cts", description="ComputerTestSuite: hardware stress testing",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version",
                   version=f"cts {__version__}\nLicense GPL-3.0-or-later <https://www.gnu.org/licenses/gpl-3.0.html>\n"
                           "This is free software: you are free to change and redistribute it.\n"
                           "There is NO WARRANTY, to the extent permitted by law.")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="show a hardware inventory")

    lp = sub.add_parser("list", help="list tests and whether they can run here")
    lp.add_argument("--destructive", action="store_true", help=argparse.SUPPRESS)

    rp = sub.add_parser("run", help="run stress tests")
    rp.add_argument("--profile", choices=PROFILES, default="quick",
                    help="; ".join(f"{p.name}: {p.description}" for p in PROFILES.values()))
    rp.add_argument("--duration", type=parse_duration, help="override the per-test duration (e.g. 90s, 15m, 2h)")
    rp.add_argument("--only", type=_csv, help="comma-separated tests or subsystems to run")
    rp.add_argument("--exclude", type=_csv, help="comma-separated tests or subsystems to skip")
    rp.add_argument("--device", action="append", default=[], metavar="DEV",
                    help="limit disk tests to this device (repeatable); required with --destructive")
    rp.add_argument("--destructive", action="store_true",
                    help="enable the write+verify disk test. ERASES the --device disks")
    rp.add_argument("--iperf-server", metavar="HOST", help="iperf3 server for the network throughput test")
    rp.add_argument("-v", "--verbose", action="store_true")

    sub.add_parser("autorun", help=argparse.SUPPRESS)

    rep = sub.add_parser("report", help="show a saved report")
    rep.add_argument("path", nargs="?", type=Path, help="report.json or run directory (default: latest)")
    return p


def cmd_info() -> int:
    print(sysinfo.format_info(sysinfo.collect()))
    return 0


def cmd_list(ctx: RunContext) -> int:
    tests = discover()
    print(f"{'Test':16} {'Subsystem':10} {'Status':10} Description")
    for t in tests:
        reason = t.available(ctx)
        state = "ready" if reason is None else "skip"
        if not t.default and reason is None:
            state = "opt-in"
        print(f"{t.name:16} {t.subsystem:10} {state:10} {t.description}")
        if reason:
            print(f"{'':38}-> {reason}")
    print("\nDisks:")
    for d in list_disks():
        why = destructive_refusal(d)
        print(f"  {d.describe()}" + (f"  [protected: {why}]" if why else ""))
    return 0


def confirm_destructive(devices: list[str]) -> bool:
    """Interactive per-device confirmation. Returns False if anything is refused."""
    try:
        disks = test_targets(list_disks(), devices)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return False
    for d in disks:
        why = destructive_refusal(d)
        if why:
            print(f"REFUSED: {d.path} {why}", file=sys.stderr)
            return False
    if not sys.stdin.isatty():
        print("error: destructive tests need an interactive confirmation", file=sys.stderr)
        return False
    print("\n" + "!" * 72)
    print(" DESTRUCTIVE TEST: ALL DATA on the following disks will be ERASED:")
    for d in disks:
        print(f"   {d.describe()}")
    print("!" * 72)
    for d in disks:
        ans = input(f"Type the device path '{d.path}' to confirm erasing it: ").strip()
        if ans != d.path:
            print("Not confirmed, aborting.")
            return False
    return True


def cmd_run(args: argparse.Namespace, profile_override: str | None = None) -> int:
    ctx = RunContext(
        profile=PROFILES[profile_override or args.profile],
        duration=args.duration,
        destructive=args.destructive,
        devices=args.device,
        iperf_server=args.iperf_server,
        verbose=args.verbose,
    )
    if ctx.destructive and not ctx.devices:
        print("error: --destructive requires at least one --device", file=sys.stderr)
        return 2
    try:
        tests = select(discover(), args.only, args.exclude)
        if ctx.devices:
            test_targets(list_disks(), ctx.devices)  # validate early
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if ctx.destructive:
        if not any(t.name == "storage-write" for t in tests):  # opt-in test: add it
            tests += [t for t in discover() if t.name == "storage-write"]
            tests.sort(key=lambda t: (t.order, t.name))
        if not confirm_destructive(ctx.devices):
            return 2

    run_dir, persistent = storage_target.new_run_dir()
    ctx.log_dir = run_dir / "logs"
    if not ctx.is_root:
        print("note: not running as root; disk, GPU and kernel-log checks will be skipped or limited.")
    print(f"Profile {ctx.profile.name}: {ctx.profile.description}")
    print("Tests  : " + ", ".join(t.name for t in tests))
    est = sum(t.estimate(ctx) for t in tests if t.available(ctx) is None)
    print(f"Estimated time: {fmt_duration(est)}   Reports: {run_dir}"
          + ("" if persistent else "  (in RAM: lost on reboot; see README for CTS_REPORTS)"))
    if not persistent and storage_target.fallback_hint:
        print("note: " + storage_target.fallback_hint)

    info = sysinfo.collect()
    started = time.time()
    runner = Runner(ctx, tests)
    results = runner.run()
    meta = {
        "version": __version__,
        "profile": ctx.profile.name,
        "test_seconds": ctx.seconds,
        "started": started,
        "finished": time.time(),
        "aborted": runner.aborted,
        "root": ctx.is_root,
        "kmsg_available": runner.monitor.kmsg_available,
        "args": sys.argv[1:],
    }
    rep = report.build(results, info, meta)
    _, txt = report.write(run_dir, rep)
    os.sync()  # reports may be on a USB stick that gets pulled right after
    print()
    print(report.format_text(rep))
    print(f"Report saved to {txt}")
    verdict = overall([r for r in results if r.status != Status.SKIP])
    return 1 if verdict >= Status.FAIL else 0


def cmd_autorun(parser: argparse.ArgumentParser) -> int:
    """Run the profile named by ``cts.autorun=<profile>`` on the kernel command line."""
    try:
        cmdline = Path("/proc/cmdline").read_text().split()
    except OSError:
        return 0
    prof = next((a.split("=", 1)[1] for a in cmdline if a.startswith("cts.autorun=")), None)
    if not prof:
        return 0
    if prof not in PROFILES:
        print(f"cts.autorun: unknown profile {prof!r}")
        return 2
    print(f"cts.autorun={prof}: starting in 10 seconds (Ctrl+C to cancel and get a shell)")
    try:
        time.sleep(10)
    except KeyboardInterrupt:
        print()
        return 0
    return cmd_run(parser.parse_args(["run", "--profile", prof]))


def cmd_report(path: Path | None) -> int:
    path = path or storage_target.latest_report()
    if not path or not path.exists():
        print("no report found", file=sys.stderr)
        return 1
    print(report.format_text(report.load(path)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    match args.command:
        case "info":
            return cmd_info()
        case "list":
            return cmd_list(RunContext(destructive=args.destructive))
        case "run":
            return cmd_run(args)
        case "autorun":
            return cmd_autorun(parser)
        case "report":
            return cmd_report(args.path)
    return 2
