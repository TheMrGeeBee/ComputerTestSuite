# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

from computertestsuite import version_from_describe


def test_version_from_git_describe():
    assert version_from_describe("v1.2.0\n") == "1.2.0"
    assert version_from_describe("v1.2.0-3-gabc1234") == "1.2.0+3.gabc1234"
    assert version_from_describe("v1.2.0-dirty") == "1.2.0+dirty"
    assert version_from_describe("v1.2.0-3-gabc1234-dirty") == "1.2.0+3.gabc1234.dirty"
    assert version_from_describe("") == "unknown"
