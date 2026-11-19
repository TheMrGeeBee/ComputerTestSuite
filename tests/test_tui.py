# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

from computertestsuite.core.model import Status, TestResult
from computertestsuite.tui import TestRow, bar, clean, overall_fraction, progress_fraction, tail


def test_progress_fraction():
    assert progress_fraction("WAIT", 0, 100, 50) == 0.0
    assert progress_fraction("RUN", 0, 100, 50) == 0.5
    assert progress_fraction("RUN", 0, 100, 500) == 0.99  # overrun: never 100% while running
    assert progress_fraction("RUN", 0, 0, 50) == 0.0
    for done in ("PASS", "WARN", "FAIL", "ERROR", "SKIP"):
        assert progress_fraction(done, 0, 100, 1) == 1.0


def test_overall_fraction_weights_by_estimate_and_ignores_skips():
    rows = [TestRow("a", "", 100, state="PASS"), TestRow("b", "", 300, state="RUN", started=0),
            TestRow("c", "", 0, state="SKIP")]
    assert overall_fraction(rows, now=150) == (100 + 150) / 400
    assert overall_fraction([TestRow("c", "", 0, state="SKIP")], now=0) == 1.0
    assert overall_fraction([], now=0) == 0.0


def test_bar():
    assert bar(0.0, 10) == ".........."
    assert bar(0.5, 10) == "#####....."
    assert bar(1.0, 4) == "####"


def test_clean_handles_memtester_backspaces_and_control_chars():
    assert clean("Random Value : \b\b\bok") == "Random Valueok"  # each \b erases one character
    assert "\b" not in clean("abc\b\b\bxyz")
    assert clean("progress 10%\rprogress 20%") == "progress 20%"
    assert clean("a\tb\x07") == "a    b"


def test_tail(tmp_path):
    f = tmp_path / "x.log"
    f.write_text("\n".join(f"line {i}" for i in range(100)))
    assert tail(f, 3) == ["line 97", "line 98", "line 99"]
    assert tail(tmp_path / "missing.log", 3) == []


def test_listener_updates_rows():
    import threading
    from computertestsuite.tui import RunState, UiListener

    class FakeTest:
        name, description = "cpu", ""

    rows = [TestRow("cpu", "", 60)]
    state = RunState.__new__(RunState)
    state.rows, state.messages, state.lock = rows, [], threading.Lock()
    lst = UiListener(state)
    res = TestResult("cpu", "cpu", started=10.0)
    lst.test_started(FakeTest(), res, 90)
    assert (rows[0].state, rows[0].eta, rows[0].started) == ("RUN", 90, 10.0)
    res.add(Status.WARN, "hot")
    lst.test_finished(FakeTest(), res)
    assert rows[0].state == "WARN" and rows[0].result is res


def test_newest_log_prefers_recent_instance_log(tmp_path):
    import os
    from computertestsuite.tui import newest_log

    base = tmp_path / "memory-pattern.log"
    base.write_text("index")
    inst = tmp_path / "memory-pattern.2.log"
    inst.write_text("progress")
    os.utime(base, (1, 1))
    assert newest_log(base) == str(inst)
    assert newest_log(tmp_path / "cpu.log") == str(tmp_path / "cpu.log")  # nothing yet
