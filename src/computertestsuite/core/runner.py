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


class RunListener:
    """Receives runner events. The default does nothing; subclasses present them."""

    def test_started(self, test: StressTest, result: TestResult, eta: int) -> None: ...
    def test_progress(self, test: StressTest, elapsed: float, eta: int, status_line: str) -> None: ...
    def test_skipped(self, test: StressTest, result: TestResult) -> None: ...
    def test_finished(self, test: StressTest, result: TestResult) -> None: ...
    def message(self, text: str) -> None: ...


class ConsoleListener(RunListener):
    """Plain line-by-line output for ``cts run``."""

    def test_started(self, test: StressTest, result: TestResult, eta: int) -> None:
        print(f"\n>>> {test.name}: {test.description} (about {fmt_duration(eta)})", flush=True)

    def test_progress(self, test: StressTest, elapsed: float, eta: int, status_line: str) -> None:
        print(f"  [{test.name} {fmt_duration(elapsed)}/{fmt_duration(eta)}] {status_line}", flush=True)

    def test_finished(self, test: StressTest, result: TestResult) -> None:
        print(f"<<< {test.name}: {result.status.name} ({fmt_duration(result.duration)})", flush=True)
        for f in result.findings:
            if f.status >= Status.WARN:
                print(f"    {f.status.name}: {f.message}", flush=True)

    def message(self, text: str) -> None:
        print(f"\n!! {text}", flush=True)


class Runner:
    def __init__(self, ctx: RunContext, tests: list[StressTest], monitor: Monitor | None = None,
                 progress_interval: float = 15.0, listener: RunListener | None = None) -> None:
        self.ctx = ctx
        self.tests = tests
        self.monitor = monitor or Monitor()
        self.progress_interval = progress_interval
        self.listener = listener or ConsoleListener()
        self.aborted = False
        self.results: list[TestResult] = []

    # -- aborting
    def abort(self) -> None:
        """Stop the current test, skip the rest; the report is still written. Thread-safe."""
        if self.aborted:
            self.listener.message("second stop request: killing everything")
            proc.kill_all()
            return
        self.aborted = True
        proc.ABORT.set()
        self.listener.message("stopping the current test and writing the report (stop again to force)")

    def _on_sigint(self, signum: int, frame: Any) -> None:
        self.abort()

    # -- progress
    def _progress(self, test: StressTest, start: float, eta: int, stop: threading.Event) -> None:
        while not stop.wait(self.progress_interval):
            self.listener.test_progress(test, time.time() - start, eta, self.monitor.status_line())

    def run_one(self, test: StressTest) -> TestResult:
        res = TestResult(test.name, test.subsystem)
        res.started = time.time()
        if self.aborted:
            res.add(Status.SKIP, "not run (aborted)")
            res.finished = time.time()
            self.listener.test_skipped(test, res)
            return res
        reason = test.available(self.ctx)
        if reason:
            res.status = Status.SKIP
            res.add(Status.SKIP, reason)
            res.finished = time.time()
            self.listener.test_skipped(test, res)
            return res

        res.log_file = str(self.ctx.log_path(test.name))
        eta = test.estimate(self.ctx)
        self.listener.test_started(test, res, eta)
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
        self.listener.test_finished(test, res)
        return res

    def run(self, install_signal_handler: bool = True) -> list[TestResult]:
        """Run all tests. Pass ``install_signal_handler=False`` when not on the main thread."""
        old = signal.signal(signal.SIGINT, self._on_sigint) if install_signal_handler else None
        self.monitor.start()
        try:
            for t in self.tests:
                self.results.append(self.run_one(t))
        finally:
            self.monitor.stop()
            if install_signal_handler:
                signal.signal(signal.SIGINT, old)
            proc.ABORT.clear()
        return self.results
