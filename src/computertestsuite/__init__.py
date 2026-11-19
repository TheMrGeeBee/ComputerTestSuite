# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

"""ComputerTestSuite: hardware stress testing for endurance, reliability and stability."""

__version__ = "0.1.0"


def main() -> int:
    from .cli import main as cli_main

    return cli_main()
