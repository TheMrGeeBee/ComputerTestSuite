#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee
#
# Run by mkarchiso inside the ISO's root filesystem at build time, then deleted.
#
# Broadcom's proprietary "wl" Wi-Fi driver is only shipped as source (broadcom-wl-dkms), which
# dkms compiled for the ISO kernel when the packages were installed. Keep only the compiled
# module and remove the compiler, kernel headers and the package again: that keeps the ISO
# small, and also drops the package's modprobe blacklist of the open Broadcom drivers
# (brcmfmac, bcma, b43, ssb), which would break Wi-Fi and Ethernet on other machines.
# /usr/local/bin/cts-broadcom-wl loads wl at boot only for chips no open driver takes.
#
# Never fails the build: without the module the ISO simply has no wl driver.

set -u

mod=$(find /usr/lib/modules -name 'wl.ko*' -print -quit)
if [[ -n $mod ]]; then
    kver=$(cut -d/ -f5 <<<"$mod")   # /usr/lib/modules/<kver>/...
    file=${mod##*/}                  # wl.ko or wl.ko.zst
    cp -- "$mod" "/root/$file"
else
    echo "WARNING: the Broadcom wl module was not built; the ISO will have no wl driver" >&2
fi

# -s also removes dkms, gcc and their dependencies when nothing else needs them.
pacman -Rns --noconfirm broadcom-wl-dkms linux-headers || echo "WARNING: could not remove the wl build tools" >&2

if [[ -n $mod ]]; then
    install -Dm644 -- "/root/$file" "/usr/lib/modules/$kver/updates/$file"
    rm -f -- "/root/$file"
    depmod "$kver"
    echo "Broadcom wl driver kept: /usr/lib/modules/$kver/updates/$file"
fi
exit 0
