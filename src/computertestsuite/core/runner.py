# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

"""Runs the selected tests sequentially under the hardware monitor."""

from __future__ import annotations

import signal
import threading
import time
import traceback
from typing import Any

from . import proc
from .base import StressTest
from .context import RunContext
from .model import Status, TestResult
from .monitor import Monitor
from .profiles import fmt_duration


class Runner:
    def __init__(self, ctx: RunContext, tests: list[StressTest], monitor: Monitor | None = None,
                 progress_interval: float = 15.0) -> None:
        self.ctx = ctx
        self.tests = tests
        self.monitor = monitor or Monitor()
        self.progress_interval = progress_interval
        self.aborted = False
        self.results: list[TestResult] = []

    # -- signal handling
    def _on_sigint(self, signum: int, frame: Any) -> None:
        if self.aborted:
            print("\n!! second interrupt: killing everything", flush=True)
            proc.kill_all()
            return
        self.aborted = True
        proc.ABORT.set()
        print("\n!! interrupt: stopping current test and writing report (Ctrl+C again to force)", flush=True)

    # -- progress
    def _progress(self, test: StressTest, start: float, eta: int, stop: threading.Event) -> None:
        while not stop.wait(self.progress_interval):
            el = time.time() - start
            line = self.monitor.status_line()
            print(f"  [{test.name} {fmt_duration(el)}/{fmt_duration(eta)}] {line}", flush=True)

    def run_one(self, test: StressTest) -> TestResult:
        res = TestResult(test.name, test.subsystem)
        res.started = time.time()
        if self.aborted:
            res.add(Status.SKIP, "not run (aborted)")
            res.finished = time.time()
            return res
        reason = test.available(self.ctx)
        if reason:
            res.status = Status.SKIP
            res.add(Status.SKIP, reason)
            res.finished = time.time()
            return res

        res.log_file = str(self.ctx.log_path(test.name))
        eta = test.estimate(self.ctx)
        print(f"\n>>> {test.name}: {test.description} (about {fmt_duration(eta)})", flush=True)
        before = self.monitor.counters()
        stop = threading.Event()
        pt = threading.Thread(target=self._progress, args=(test, res.started, eta, stop), daemon=True)
        pt.start()
        try:
            test.run(self.ctx, res)
        except Exception as e:  # noqa: BLE001 - any test bug becomes an ERROR result
            res.add(Status.ERROR, f"test crashed: {e!r}")
            with open(res.log_file, "a") as f:
                f.write(traceback.format_exc())
        finally:
            stop.set()
            pt.join(timeout=2)
        time.sleep(min(2.0, self.monitor.interval))  # let the last sample/kmsg arrive
        res.finished = time.time()
        after = self.monitor.counters()

        ws = self.monitor.evaluate(res.started, res.finished, before, after)
        for f in ws.findings:
            res.add(f.status, f.message, f.source)
        if ws.temps:
            res.metrics["temps"] = ws.temps
        if ws.freq:
            res.metrics["cpu_freq"] = ws.freq
        if any(ws.counters.values()):
            res.metrics["counters"] = ws.counters
        if ws.kernel_events:
            res.metrics["kernel_events"] = ws.kernel_events
        if self.aborted:
            res.add(Status.ERROR, "aborted by user")
        print(f"<<< {test.name}: {res.status.name} ({fmt_duration(res.duration)})", flush=True)
        for f in res.findings:
            if f.status >= Status.WARN:
                print(f"    {f.status.name}: {f.message}", flush=True)
        return res

    def run(self) -> list[TestResult]:
        old = signal.signal(signal.SIGINT, self._on_sigint)
        self.monitor.start()
        try:
            for t in self.tests:
                self.results.append(self.run_one(t))
        finally:
            self.monitor.stop()
            signal.signal(signal.SIGINT, old)
            proc.ABORT.clear()
        return self.results
