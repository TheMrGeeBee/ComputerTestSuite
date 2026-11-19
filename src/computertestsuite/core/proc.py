# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

"""Subprocess helpers: timeouts, logging to file, killing whole process groups."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# Set by the runner on SIGINT so long-running tools are torn down promptly.
ABORT = threading.Event()

_live: set[subprocess.Popen] = set()
_live_lock = threading.Lock()


@dataclass
class ProcResult:
    cmd: list[str]
    returncode: int
    output: str
    timed_out: bool = False
    aborted: bool = False
    elapsed: float = 0.0


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def missing(tools: list[str]) -> list[str]:
    return [t for t in tools if not have(t)]


def _kill_group(p: subprocess.Popen, sig: int = signal.SIGTERM) -> None:
    try:
        os.killpg(p.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def kill_all() -> None:
    """Terminate every process started through :func:`run`."""
    with _live_lock:
        procs = list(_live)
    for p in procs:
        _kill_group(p, signal.SIGTERM)
    deadline = time.monotonic() + 5
    for p in procs:
        try:
            p.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            _kill_group(p, signal.SIGKILL)


def run(
    cmd: list[str],
    timeout: float | None = None,
    log: Path | None = None,
    echo: bool = False,
    env: dict[str, str] | None = None,
) -> ProcResult:
    """Run ``cmd``, capturing combined stdout/stderr.

    The process runs in its own session so a timeout or abort kills all of
    its children (stress-ng and fio fork workers).
    """
    start = time.monotonic()
    full_env = None
    if env:
        full_env = os.environ.copy()
        full_env.update(env)
    try:
        p = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            errors="replace",
            start_new_session=True,
            env=full_env,
        )
    except FileNotFoundError as e:
        return ProcResult(cmd, 127, str(e))

    with _live_lock:
        _live.add(p)

    chunks: list[str] = []
    logf = open(log, "a", encoding="utf-8") if log else None
    if logf:
        logf.write(f"$ {' '.join(cmd)}\n")
        logf.flush()

    def reader() -> None:
        assert p.stdout is not None
        for line in p.stdout:
            chunks.append(line)
            if logf:
                logf.write(line)
                logf.flush()
            if echo:
                print("    " + line, end="", flush=True)

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    timed_out = aborted = False
    while True:
        try:
            p.wait(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            pass
        if ABORT.is_set():
            aborted = True
            break
        if timeout is not None and time.monotonic() - start > timeout:
            timed_out = True
            break

    if timed_out or aborted:
        _kill_group(p, signal.SIGTERM)
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _kill_group(p, signal.SIGKILL)
            p.wait()

    t.join(timeout=5)
    with _live_lock:
        _live.discard(p)
    if logf:
        logf.write(f"[exit {p.returncode}{' timeout' if timed_out else ''}{' aborted' if aborted else ''}]\n\n")
        logf.close()

    return ProcResult(cmd, p.returncode, "".join(chunks), timed_out, aborted, time.monotonic() - start)


def capture(cmd: list[str], timeout: float = 30) -> str:
    """Quick helper for short informational commands; returns '' on failure."""
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
    except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError):
        return ""
    return cp.stdout
