#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee
# Boot the newest built ISO in QEMU for a smoke test.
#   scripts/run-qemu.sh          UEFI boot
#   scripts/run-qemu.sh bios     legacy BIOS boot
# Requires: archiso (run_archiso), qemu-desktop, edk2-ovmf.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ISO="$(ls -t "$ROOT"/out/cts-*.iso 2>/dev/null | head -n1 || true)"
[[ -n $ISO ]] || { echo "no ISO in out/ - run: sudo scripts/build-iso.sh" >&2; exit 1; }
command -v run_archiso >/dev/null || { echo "run_archiso missing: pacman -S archiso qemu-desktop edk2-ovmf" >&2; exit 1; }
echo "Booting $ISO"
if [[ ${1:-uefi} == bios ]]; then
  exec run_archiso -i "$ISO"
else
  exec run_archiso -u -i "$ISO"
fi
