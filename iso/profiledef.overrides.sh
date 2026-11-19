# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

# Sourced after releng's profiledef.sh by scripts/build-iso.sh.
# The label MUST start with CTS_ : the test suite uses it to recognise (and never write to) the boot stick.
iso_name="cts"
iso_label="CTS_$(date --date="@${SOURCE_DATE_EPOCH:-$(date +%s)}" +%Y%m)"
iso_publisher="ComputerTestSuite"
iso_application="ComputerTestSuite live stress-test system"
file_permissions+=(
  ["/usr/local/bin/cts"]="0:0:755"
)
