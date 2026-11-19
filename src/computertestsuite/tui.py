# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

"""Full-screen terminal interface (curses, standard library only).

Layout:  header (product, CPU, live temperature/clock)
         left: menu with hotkeys | right: the selected screen
         footer: key hints and status

Screens: Info, Tests (profile + tick tests), Run dashboard (progress bars),
Reports, Keyboard (runs cts-keymap). The run itself executes in a worker
thread through core.session, exactly like ``cts run``.
"""

from __future__ import annotations

import curses
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .core import report, session, storage_target, sysinfo
from .core.base import StressTest
from .core.context import RunContext
from .core.model import Status, TestResult
from .core.monitor import Monitor
from .core.profiles import PROFILES, fmt_duration
from .core.registry import discover
from .core.runner import RunListener

# (screen, label). The letter in parentheses is the hotkey.
MENU = [("info", "(I)nfo"), ("tests", "(T)ests"), ("run", "(S)tart"), ("reports", "(R)eports"),
        ("keyboard", "(K)eyboard"), ("quit", "(Q)uit")]
PROFILE_ORDER = ["quick", "standard", "burnin"]
MENU_W = 15
MIN_W, MIN_H = 72, 20
COUNTDOWN = 10
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


# ------------------------------------------------------------------ pure helpers (unit-tested)

def progress_fraction(state: str, started: float, eta: int, now: float) -> float:
    """0..1 for one test. Time-based; a running test never shows 100% until it finishes."""
    if state in ("WAIT", ""):
        return 0.0
    if state == "RUN":
        if eta <= 0:
            return 0.0
        return max(0.0, min(0.99, (now - started) / eta))
    return 1.0  # PASS/WARN/FAIL/ERROR/SKIP


def overall_fraction(rows: list[TestRow], now: float) -> float:
    total = sum(r.eta for r in rows if r.state != "SKIP")
    if total <= 0:
        return 1.0 if rows and all(r.state not in ("WAIT", "RUN") for r in rows) else 0.0
    done = sum(r.eta * progress_fraction(r.state, r.started, r.eta, now) for r in rows if r.state != "SKIP")
    return max(0.0, min(1.0, done / total))


def bar(frac: float, width: int) -> str:
    width = max(1, width)
    n = int(round(frac * width))
    return "#" * n + "." * (width - n)


def clean(line: str) -> str:
    """Make a log line safe to draw: handle backspaces/CRs, drop control characters."""
    line = line.rsplit("\r", 1)[-1]  # a carriage return restarts the line
    out: list[str] = []
    for c in line:
        if c == "\b":  # backspace erases the previous character, like a terminal
            if out:
                out.pop()
        else:
            out.append(c)
    return _CTRL.sub("", "".join(out).replace("\t", "    "))


def tail(path: str | Path, n: int) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 64 * 1024))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    return [clean(x) for x in data.splitlines()[-n:]]


# ------------------------------------------------------------------ run state

@dataclass
class TestRow:
    __test__ = False  # not a pytest test class

    name: str
    description: str
    eta: int
    state: str = "WAIT"  # WAIT RUN PASS WARN FAIL ERROR SKIP
    started: float = 0.0
    finished: float = 0.0
    log_file: str = ""
    result: TestResult | None = None
    reason: str = ""  # why it will be / was skipped


@dataclass
class RunState:
    session: session.Session
    rows: list[TestRow]
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    verdict: Status | None = None
    error: str = ""
    messages: list[str] = field(default_factory=list)
    thread: threading.Thread | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def active(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def row(self, name: str) -> TestRow | None:
        return next((r for r in self.rows if r.name == name), None)


class UiListener(RunListener):
    def __init__(self, state: RunState) -> None:
        self.s = state

    def test_started(self, test: StressTest, result: TestResult, eta: int) -> None:
        with self.s.lock:
            r = self.s.row(test.name)
            if r:
                r.state, r.started, r.eta, r.log_file = "RUN", result.started, eta, result.log_file

    def test_skipped(self, test: StressTest, result: TestResult) -> None:
        with self.s.lock:
            r = self.s.row(test.name)
            if r:
                r.state, r.result, r.finished = "SKIP", result, result.finished

    def test_finished(self, test: StressTest, result: TestResult) -> None:
        with self.s.lock:
            r = self.s.row(test.name)
            if r:
                r.state, r.result, r.finished = result.status.name, result, result.finished

    def message(self, text: str) -> None:
        with self.s.lock:
            self.s.messages.append(text)


# ------------------------------------------------------------------ the application

class App:
    def __init__(self, stdscr: curses.window, profile: str = "quick", only: list[str] | None = None,
                 autostart: bool = False) -> None:
        self.scr = stdscr
        self.screen = "tests"
        self.profile = profile
        self.all_tests = [t for t in discover() if t.default]  # destructive test stays CLI-only
        self.selected = {t.name for t in self.all_tests if not only or t.name in only or t.subsystem in only}
        self.cursor = 0
        self.scroll = 0
        self.run: RunState | None = None
        self.status_msg = ""
        self.overlay: str | None = None  # "log" | "report" | "confirm-quit" | "confirm-stop"
        self.report_lines: list[str] = []
        self.report_dirs: list[Path] = []
        self.info_lines: list[str] | None = None
        self.idle_mon = Monitor()
        self.cpu_model = ""
        self.avail_cache: dict[tuple[str, str], tuple[str | None, int]] = {}
        self.countdown_until = time.time() + COUNTDOWN if autostart else 0.0
        self.quit = False
        self.last_redraw = 0.0
        self._idle_sample = None
        self._idle_t = 0.0
        self._init_curses()

    # -- setup
    def _init_curses(self) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.scr.keypad(True)
        self.scr.timeout(500)
        self.color = curses.has_colors()
        if self.color:
            curses.start_color()
            try:
                curses.use_default_colors()
                bg = -1
            except curses.error:
                bg = curses.COLOR_BLACK
            curses.init_pair(1, curses.COLOR_GREEN, bg)
            curses.init_pair(2, curses.COLOR_YELLOW, bg)
            curses.init_pair(3, curses.COLOR_RED, bg)
            curses.init_pair(4, curses.COLOR_CYAN, bg)
            curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_CYAN)
        try:
            self.cpu_model = sysinfo.cpu_info().get("Model name", "")
        except Exception:  # noqa: BLE001 - cosmetic only
            self.cpu_model = ""

    def attr(self, kind: str) -> int:
        if not self.color:
            return curses.A_BOLD if kind in ("FAIL", "ERROR", "title") else curses.A_NORMAL
        return {
            "PASS": curses.color_pair(1) | curses.A_BOLD,
            "WARN": curses.color_pair(2) | curses.A_BOLD,
            "FAIL": curses.color_pair(3) | curses.A_BOLD,
            "ERROR": curses.color_pair(3) | curses.A_BOLD,
            "RUN": curses.color_pair(4) | curses.A_BOLD,
            "SKIP": curses.A_DIM,
            "title": curses.color_pair(5) | curses.A_BOLD,
            "dim": curses.A_DIM,
            "key": curses.color_pair(4) | curses.A_BOLD,
        }.get(kind, curses.A_NORMAL)

    def put(self, y: int, x: int, text: str, attr: int = 0, width: int | None = None) -> None:
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        maxw = (w - x - (1 if y == h - 1 else 0)) if width is None else min(width, w - x)
        if maxw <= 0:
            return
        try:
            self.scr.addnstr(y, x, text, maxw, attr)
        except curses.error:
            pass

    # -- test availability per profile (cached: some checks call lsblk)
    def availability(self, t: StressTest) -> tuple[str | None, int]:
        key = (self.profile, t.name)
        if key not in self.avail_cache:
            ctx = RunContext(profile=PROFILES[self.profile])
            try:
                reason = t.available(ctx)
                est = t.estimate(ctx) if reason is None else 0
            except Exception as e:  # noqa: BLE001
                reason, est = f"check failed: {e}", 0
            self.avail_cache[key] = (reason, est)
        return self.avail_cache[key]

    # -- run control
    def start_run(self) -> None:
        if self.run and self.run.active:
            self.screen = "run"
            return
        tests = [t for t in self.all_tests if t.name in self.selected]
        if not tests:
            self.status_msg = "No tests selected (Tests screen: Space to tick)"
            self.screen = "tests"
            return
        self.status_msg = "Preparing run..."
        self.draw()
        ctx = RunContext(profile=PROFILES[self.profile])
        try:
            s = session.prepare(ctx, tests)
        except Exception as e:  # noqa: BLE001
            self.status_msg = f"Could not prepare the run: {e}"
            return
        rows = []
        for t in tests:
            reason = s.availability.get(t.name)
            rows.append(TestRow(t.name, t.description, s.estimates.get(t.name, 0),
                                state="SKIP" if reason else "WAIT", reason=reason or ""))
        state = RunState(s, rows)
        listener = UiListener(state)

        def worker() -> None:
            try:
                state.verdict = session.execute(s, listener, install_signal_handler=False)
            except Exception:  # noqa: BLE001 - shown in the UI
                state.error = traceback.format_exc()
            finally:
                state.finished = time.time()

        state.thread = threading.Thread(target=worker, name="cts-run", daemon=True)
        self.run = state
        self.screen = "run"
        self.status_msg = ""
        state.thread.start()

    def stop_run(self) -> None:
        if self.run and self.run.active and self.run.session.runner:
            self.run.session.runner.abort()

    # -- input
    def handle_key(self, k: int) -> None:
        if self.countdown_until:
            self.countdown_until = 0.0
            self.status_msg = "Automatic start cancelled"
            return
        ch = chr(k).lower() if 0 <= k < 256 else ""

        if self.overlay in ("confirm-quit", "confirm-stop"):
            if ch == "y":
                if self.overlay == "confirm-quit":
                    self.stop_run()
                    self.quit = True
                else:
                    self.stop_run()
            self.overlay = None
            return
        if self.overlay in ("log", "report"):
            if self.overlay == "report" and k in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE,
                                                  curses.KEY_NPAGE, curses.KEY_HOME, curses.KEY_END):
                self.scroll_by(k, len(self.report_lines))
            else:
                self.overlay = None
                self.scroll = 0
            return

        if k == curses.KEY_RESIZE:
            return
        if ch == "q":
            if self.run and self.run.active:
                self.overlay = "confirm-quit"
            else:
                self.quit = True
            return
        if k == 27:  # Esc
            if self.run and self.run.active:
                self.overlay = "confirm-stop"
            return
        if ch == "i":
            self.screen, self.scroll = "info", 0
            return
        if ch == "t":
            self.screen = "tests"
            return
        if ch == "s":  # start with the current selection, or show the running test
            if self.run and self.run.active:
                self.screen = "run"
            else:
                self.start_run()
            return
        if ch == "r":
            self.screen, self.cursor = "reports", 0
            self.report_dirs = self.find_reports()
            return
        if ch == "k":
            self.keyboard()
            return
        if ch == "l" and self.run:
            self.overlay = "log"
            return

        getattr(self, f"keys_{self.screen}", lambda _k: None)(k)

    def scroll_by(self, k: int, total: int) -> None:
        h, _ = self.scr.getmaxyx()
        page = max(1, h - 6)
        step = {curses.KEY_UP: -1, curses.KEY_DOWN: 1, curses.KEY_PPAGE: -page, curses.KEY_NPAGE: page,
                curses.KEY_HOME: -10**9, curses.KEY_END: 10**9}.get(k, 0)
        self.scroll = max(0, min(max(0, total - page), self.scroll + step))

    def keys_tests(self, k: int) -> None:
        n = len(self.all_tests)
        if k == curses.KEY_UP:
            self.cursor = (self.cursor - 1) % n
        elif k == curses.KEY_DOWN:
            self.cursor = (self.cursor + 1) % n
        elif k == ord(" "):
            t = self.all_tests[self.cursor]
            self.selected ^= {t.name}
        elif k in (ord("a"), ord("A")):
            ready = {t.name for t in self.all_tests if self.availability(t)[0] is None}
            self.selected = set() if self.selected >= ready else ready
        elif k in (curses.KEY_LEFT, curses.KEY_RIGHT, ord("p"), ord("P")):
            i = PROFILE_ORDER.index(self.profile)
            i = i - 1 if k == curses.KEY_LEFT else i + 1
            self.profile = PROFILE_ORDER[i % len(PROFILE_ORDER)]
        elif k in (10, 13, curses.KEY_ENTER):
            self.start_run()

    def keys_info(self, k: int) -> None:
        self.scroll_by(k, len(self.info_lines or []))

    def keys_reports(self, k: int) -> None:
        n = len(self.report_dirs)
        if not n:
            return
        if k == curses.KEY_UP:
            self.cursor = (self.cursor - 1) % n
        elif k == curses.KEY_DOWN:
            self.cursor = (self.cursor + 1) % n
        elif k in (10, 13, curses.KEY_ENTER):
            self.open_report(self.report_dirs[self.cursor])

    def keys_run(self, k: int) -> None:
        if k in (10, 13, curses.KEY_ENTER) and self.run and not self.run.active and self.run.session.report_txt:
            self.open_report(self.run.session.run_dir)

    # -- actions
    def find_reports(self) -> list[Path]:
        try:
            base, _ = storage_target.reports_base()
            return sorted((p.parent for p in base.glob("*/report.json")), reverse=True)
        except OSError:
            return []

    def open_report(self, run_dir: Path) -> None:
        try:
            self.report_lines = report.format_text(report.load(run_dir)).splitlines()
        except (OSError, ValueError) as e:
            self.report_lines = [f"Cannot read report: {e}"]
        self.overlay, self.scroll = "report", 0

    def keyboard(self) -> None:
        tool = shutil.which("cts-keymap")
        if not tool:
            self.status_msg = "cts-keymap is only available on the live system"
            return
        curses.def_prog_mode()
        curses.endwin()
        try:
            subprocess.call([tool])
        finally:
            curses.reset_prog_mode()
            self.scr.clear()
            self.scr.refresh()

    # -- drawing
    def draw(self) -> None:
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        if h < MIN_H or w < MIN_W:
            self.put(0, 0, f"Terminal too small ({w}x{h}); need {MIN_W}x{MIN_H}. Q quits.")
            self.scr.refresh()
            return
        self.draw_header(w)
        self.draw_menu(h)
        try:
            self.scr.vline(2, MENU_W, curses.ACS_VLINE, h - 4)
            self.scr.hline(1, 0, curses.ACS_HLINE, w)
            self.scr.hline(h - 2, 0, curses.ACS_HLINE, w)
        except curses.error:
            pass
        x, y0, cw, ch = MENU_W + 2, 2, w - MENU_W - 3, h - 4
        if self.overlay == "log":
            self.draw_log(x, y0, cw, ch)
        elif self.overlay == "report":
            self.draw_text(self.report_lines, x, y0, cw, ch, "Report (Up/Down/PgUp/PgDn, other key closes)")
        else:
            getattr(self, f"draw_{self.screen}")(x, y0, cw, ch)
        self.draw_footer(h, w)
        self.scr.refresh()

    def draw_header(self, w: int) -> None:
        self.put(0, 0, " " * w, self.attr("title"))
        self.put(0, 1, f"ComputerTestSuite {__version__}", self.attr("title"))
        live = self.live_line()
        self.put(0, max(26, w - len(live) - 1), live, self.attr("title"))
        model = self.cpu_model[: max(0, w - 28 - len(live) - 2)]
        self.put(0, 26, model, self.attr("title"))

    def live_line(self) -> str:
        mon = self.run.session.runner.monitor if (self.run and self.run.active and self.run.session.runner) else None
        if mon and mon.samples:
            s = mon.samples[-1]
        else:
            if time.time() - self._idle_t > 2:
                self._idle_sample = self.idle_mon.sample_now()
                self._idle_t = time.time()
            s = self._idle_sample
        if not s:
            return ""
        cpu = [v for k, v in s.temps.items() if k.split("/")[0] in ("coretemp", "k10temp", "zenpower", "cpu_thermal")]
        temp = max(cpu) if cpu else (max(s.temps.values()) if s.temps else None)
        parts = []
        if temp is not None:
            parts.append(f"CPU {temp:.0f}°C")
        if s.freq_avg:
            parts.append(f"{s.freq_avg / 1000:.1f} GHz")
        return "  ".join(parts)

    def draw_menu(self, h: int) -> None:
        y = 3
        for key, label in MENU:
            if key == "run" and self.run and self.run.active:
                label = "(S)tatus"
            active = self.screen == key and self.overlay is None
            self.put(y, 0, f" {label}".ljust(MENU_W), curses.A_REVERSE if active else 0)
            y += 2
        if self.run and self.run.active:
            self.put(h - 4, 1, "TEST RUNNING", self.attr("RUN"))

    def draw_footer(self, h: int, w: int) -> None:
        hints = {
            "tests": "Up/Down move  Space tick  A all  Left/Right profile  Enter/S start",
            "run": "Esc stop test  L live log",
            "info": "Up/Down/PgUp/PgDn scroll",
            "reports": "Up/Down select  Enter open",
        }.get(self.screen, "")
        if self.run and not self.run.active and self.screen == "run":
            hints = "Enter view report  L last log  T choose tests  S run again"
        if self.overlay in ("confirm-quit", "confirm-stop"):
            q = "Stop the running test and quit? (y/n)" if self.overlay == "confirm-quit" else \
                "Stop the running test? The report is still written. (y/n)"
            self.put(h - 1, 1, q, self.attr("WARN"))
            return
        if self.countdown_until:
            left = max(0, int(self.countdown_until - time.time() + 0.99))
            self.put(h - 1, 1, f"Starting the {self.profile} profile in {left} s - press any key to cancel",
                     self.attr("WARN"))
            return
        msg = self.status_msg
        self.put(h - 1, 1, hints, self.attr("dim"))
        if msg:
            self.put(h - 1, max(1, w - len(msg) - 2), msg, self.attr("WARN"))

    def draw_text(self, lines: list[str], x: int, y0: int, cw: int, ch: int, title: str) -> None:
        self.put(y0, x, title, curses.A_BOLD)
        visible = ch - 2
        for i, line in enumerate(lines[self.scroll:self.scroll + visible]):
            self.put(y0 + 2 + i, x, line, 0, cw)
        if len(lines) > visible:
            self.put(y0, x + cw - 18, f"{self.scroll + 1}-{min(len(lines), self.scroll + visible)}/{len(lines)}",
                     self.attr("dim"))

    def draw_info(self, x: int, y0: int, cw: int, ch: int) -> None:
        if self.info_lines is None:
            self.put(y0, x, "Collecting hardware information...")
            self.scr.refresh()
            try:
                self.info_lines = sysinfo.format_info(sysinfo.collect()).splitlines()
            except Exception as e:  # noqa: BLE001
                self.info_lines = [f"Could not collect: {e}"]
        self.draw_text(self.info_lines, x, y0, cw, ch, "System information")

    def draw_tests(self, x: int, y0: int, cw: int, ch: int) -> None:
        p = PROFILES[self.profile]
        self.put(y0, x, "Profile: ", curses.A_BOLD)
        px = x + 9
        for name in PROFILE_ORDER:
            label = f" {name} "
            self.put(y0, px, label, curses.A_REVERSE if name == self.profile else self.attr("dim"))
            px += len(label) + 1
        self.put(y0 + 1, x, p.description, self.attr("dim"), cw)

        total = 0
        y = y0 + 3
        for i, t in enumerate(self.all_tests):
            reason, est = self.availability(t)
            on = t.name in self.selected
            if on and reason is None:
                total += est
            mark = "[-]" if reason else ("[x]" if on else "[ ]")
            line = f"{mark} {t.name:15} {fmt_duration(est) if reason is None else '':>8}  "
            attr = curses.A_REVERSE if i == self.cursor else 0
            self.put(y, x, line, attr | (self.attr("dim") if reason else 0))
            desc = f"skip: {reason}" if reason else t.description
            self.put(y, x + len(line), desc, self.attr("SKIP" if reason else "") | (attr if i == self.cursor else 0),
                     cw - len(line))
            y += 1
        y += 1
        sel = [t for t in self.all_tests if t.name in self.selected and self.availability(t)[0] is None]
        self.put(y, x, f"{len(sel)} tests selected, estimated {fmt_duration(total)}", curses.A_BOLD)
        if self.status_msg.startswith("No tests"):
            self.put(y + 1, x, self.status_msg, self.attr("WARN"))

    def draw_run(self, x: int, y0: int, cw: int, ch: int) -> None:
        r = self.run
        if r is None:
            self.put(y0, x, "No run yet.", curses.A_BOLD)
            self.put(y0 + 2, x, "Choose a profile and tests on the Tests screen (T), then press S or Enter.")
            return
        now = time.time()
        with r.lock:
            rows = list(r.rows)
            msgs = list(r.messages)
        end = r.finished or now
        failures = sum(1 for row in rows if row.state in ("FAIL", "ERROR"))
        s = r.session
        self.put(y0, x, f"Profile: {s.ctx.profile.name}", curses.A_BOLD)
        self.put(y0, x + 20, f"Elapsed {fmt_duration(end - r.started)}")
        remaining = max(0, s.total_estimate - (end - r.started)) if r.active else 0
        if r.active:
            self.put(y0, x + 38, f"Left ~{fmt_duration(remaining)}", self.attr("dim"))
        self.put(y0, x + cw - 12, f"Errors: {failures}", self.attr("FAIL" if failures else "PASS"))

        bw = max(10, cw - 18)
        frac = overall_fraction(rows, now)
        self.put(y0 + 1, x, "Overall")
        self.put(y0 + 1, x + 8, f"[{bar(frac, bw)}] {frac * 100:3.0f}%", self.attr("RUN" if r.active else ""))

        y = y0 + 3
        name_w = 15
        tbw = max(10, cw - name_w - 24)
        for row in rows:
            f = progress_fraction(row.state, row.started, row.eta, now)
            tag = "" if row.state == "WAIT" else row.state
            self.put(y, x, f"{tag:5}", self.attr(row.state))
            self.put(y, x + 6, f"{row.name:{name_w}}")  # tag column is 5 wide + space
            if row.state == "SKIP":
                reason = (row.result.findings[0].message if row.result and row.result.findings
                          else row.reason or "skipped")
                self.put(y, x + 6 + name_w + 1, f"skipped: {reason}", self.attr("dim"), cw - name_w - 8)
            else:
                timing = ""
                if row.state == "RUN":
                    timing = f"{fmt_duration(now - row.started)}/{fmt_duration(row.eta)}"
                elif row.result:
                    timing = fmt_duration(row.result.duration)
                self.put(y, x + 6 + name_w + 1, f"[{bar(f, tbw)}] {f * 100:3.0f}% {timing}",
                         self.attr(row.state) if row.state != "WAIT" else 0)
            y += 1

        # findings so far (most recent first), kernel/health messages and run messages
        y += 1
        notes: list[tuple[str, str]] = []
        for row in rows:
            if row.result and row.state not in ("SKIP",):
                for fnd in row.result.findings:
                    if fnd.status >= Status.WARN:
                        notes.append((fnd.status.name, f"{row.name}: {fnd.message}"))
        notes += [("WARN", m) for m in msgs]
        if r.error:
            notes.append(("ERROR", "internal error: " + r.error.strip().splitlines()[-1]))
        room = (y0 + ch) - y - 3
        for kind, text in notes[-max(0, room):]:
            self.put(y, x, f"{kind:5} {text}", self.attr(kind), cw)
            y += 1

        # live sensors / result line at the bottom of the panel
        yb = y0 + ch - 2
        if r.active and s.runner:
            self.put(yb, x, s.runner.monitor.status_line() or "waiting for sensor data...", self.attr("dim"), cw)
        elif r.finished:
            verdict = r.verdict.name if r.verdict is not None else "ERROR"
            self.put(yb - 1, x, f"Finished: {verdict}", self.attr(verdict) | curses.A_BOLD)
            where = str(s.report_txt) if s.report_txt else "(report not written)"
            self.put(yb, x, f"Report: {where}" + ("" if s.persistent else "  (in RAM: lost at power-off)"),
                     0, cw)
        if r.active and not s.persistent:
            self.put(yb + 1, x, "Reports are in RAM: " + (s.fallback_hint or "lost at power-off"),
                     self.attr("WARN"), cw)

    def draw_reports(self, x: int, y0: int, cw: int, ch: int) -> None:
        self.put(y0, x, "Saved reports (newest first)", curses.A_BOLD)
        if not self.report_dirs:
            self.put(y0 + 2, x, "No reports found yet.")
            return
        for i, d in enumerate(self.report_dirs[: ch - 3]):
            try:
                verdict = report.load(d).get("verdict", "?")
            except (OSError, ValueError):
                verdict = "?"
            attr = curses.A_REVERSE if i == self.cursor else 0
            self.put(y0 + 2 + i, x, f"{verdict:6}", self.attr(verdict) | attr)
            self.put(y0 + 2 + i, x + 7, d.name, attr, cw - 7)
        self.put(y0 + ch - 1, x, f"Folder: {self.report_dirs[0].parent}", self.attr("dim"), cw)

    def draw_log(self, x: int, y0: int, cw: int, ch: int) -> None:
        r = self.run
        path = ""
        if r:
            with r.lock:
                running = [row for row in r.rows if row.state == "RUN"]
                done = [row for row in r.rows if row.log_file]
            target = running[0] if running else (done[-1] if done else None)
            if target:
                path = target.log_file
                self.put(y0, x, f"Log: {target.name}  (any key closes)", curses.A_BOLD)
        if not path:
            self.put(y0, x, "No log yet (any key closes)", curses.A_BOLD)
            return
        lines = tail(path, ch - 2)
        if not lines:
            self.put(y0 + 2, x, "(nothing logged yet - this test does not run external tools)", self.attr("dim"))
        for i, line in enumerate(lines):
            self.put(y0 + 2 + i, x, line, 0, cw)

    # -- main loop
    def loop(self) -> None:
        while not self.quit:
            now = time.time()
            if self.countdown_until and now >= self.countdown_until:
                self.countdown_until = 0.0
                self.start_run()
            if now - self.last_redraw > 5:
                self.scr.clearok(True)  # repair any kernel message printed over the screen
                self.last_redraw = now
            self.draw()
            try:
                k = self.scr.getch()
            except KeyboardInterrupt:
                if self.run and self.run.active:
                    self.stop_run()
                    self.status_msg = "Ctrl+C: stopping the current test"
                continue
            if k != -1:
                self.handle_key(k)
        # Quit requested: if a run was stopped, wait for its report.
        if self.run and self.run.thread and self.run.thread.is_alive():
            self.status_msg = "Stopping and writing the report..."
            while self.run.thread.is_alive():
                self.draw()
                self.run.thread.join(timeout=0.5)


def quiet_console() -> None:
    """On the live system, keep kernel messages from being printed over the interface.

    They are still read and evaluated through /dev/kmsg by the monitor.
    """
    if os.geteuid() == 0 and Path("/run/archiso").exists():
        subprocess.run(["dmesg", "--console-level", "crit"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_ui(profile: str = "quick", only: list[str] | None = None, autostart: bool = False) -> int:
    quiet_console()
    holder: dict[str, App] = {}

    def main(stdscr: curses.window) -> None:
        app = App(stdscr, profile, only, autostart)
        holder["app"] = app
        app.loop()

    os.environ.setdefault("ESCDELAY", "50")  # make Esc respond quickly
    curses.wrapper(main)
    app = holder.get("app")
    if app and app.run and app.run.session.report_txt:
        print(f"Last report: {app.run.session.report_txt}")
        if app.run.verdict is not None:
            return 1 if app.run.verdict >= Status.FAIL else 0
    print("Type 'cts' to open the interface again.")
    return 0
