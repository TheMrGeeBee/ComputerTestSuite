# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

"""ComputerTestSuite: hardware stress testing for endurance, reliability and stability."""

import re
import subprocess
from pathlib import Path


def version_from_describe(describe: str) -> str:
    """'v1.2.0' -> '1.2.0'; 'v1.2.0-3-gabc1234-dirty' -> '1.2.0+3.gabc1234.dirty'
    (3 commits after the tag, uncommitted changes)."""
    m = re.fullmatch(r"v?(\d[^-]*)(?:-(\d+)-(g[0-9a-f]+))?(-dirty)?", describe.strip())
    if not m:
        return describe.strip() or "unknown"
    tag, ahead, commit, dirty = m.groups()
    local = [x for x in (ahead, commit, dirty and "dirty") if x]
    return tag + ("+" + ".".join(local) if local else "")


def _version() -> str:
    # 1. Stamped into the ISO by scripts/build-iso.sh.
    try:
        from ._build_version import VERSION  # type: ignore[import-not-found]
        return VERSION
    except ImportError:
        pass
    # 2. Running from a git checkout (development).
    try:
        out = subprocess.run(["git", "describe", "--tags", "--match", "v[0-9]*", "--dirty"],
                             cwd=Path(__file__).parent, capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            return version_from_describe(out.stdout)
    except (OSError, subprocess.SubprocessError):
        pass
    # 3. An installed package.
    try:
        from importlib.metadata import version
        return version("computertestsuite")
    except Exception:
        return "unknown"


__version__ = _version()


def main() -> int:
    from .cli import main as cli_main

    return cli_main()
