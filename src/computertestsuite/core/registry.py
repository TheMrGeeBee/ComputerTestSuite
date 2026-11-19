# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

"""Discovers StressTest subclasses in the ``suites`` package."""

from __future__ import annotations

import importlib
import pkgutil

from .base import StressTest


def _all_subclasses(cls: type) -> list[type]:
    out = []
    for sub in cls.__subclasses__():
        out.append(sub)
        out.extend(_all_subclasses(sub))
    return out


def discover() -> list[StressTest]:
    from .. import suites

    for mod in pkgutil.iter_modules(suites.__path__):
        importlib.import_module(f"{suites.__name__}.{mod.name}")
    tests = [c() for c in _all_subclasses(StressTest)
             if "name" in c.__dict__ and c.__module__.startswith(suites.__name__ + ".")]
    return sorted(tests, key=lambda t: (t.order, t.name))


def select(
    tests: list[StressTest], only: list[str] | None, exclude: list[str] | None
) -> list[StressTest]:
    """Filter by test name or subsystem. ``only`` also enables opt-in tests."""
    def matches(t: StressTest, keys: list[str]) -> bool:
        return t.name in keys or t.subsystem in keys

    if only:
        known = {t.name for t in tests} | {t.subsystem for t in tests}
        unknown = [k for k in only if k not in known]
        if unknown:
            raise ValueError(f"unknown test/subsystem: {', '.join(unknown)}")
        chosen = [t for t in tests if matches(t, only)]
    else:
        chosen = [t for t in tests if t.default]
    if exclude:
        chosen = [t for t in chosen if not matches(t, exclude)]
    return chosen
