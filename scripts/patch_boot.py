#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee
"""Add "CTS auto-run" boot menu entries to a copied archiso releng profile.

Each entry duplicates the default Linux entry and appends ``cts.autorun=<profile>``
to the kernel command line. Works on whichever boot loaders the profile has
(systemd-boot, syslinux, GRUB) and warns, without failing, if one's layout changed.

usage: patch_boot.py PROFILE_DIR
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

AUTORUN = [
    ("quick", "CTS auto-run: quick (~15-20 min)"),
    ("standard", "CTS auto-run: standard (~2-3 h)"),
    ("burnin", "CTS auto-run: burn-in (endurance, many hours)"),
]


def warn(msg: str) -> None:
    print(f"patch_boot: WARNING: {msg}", file=sys.stderr)


def systemd_boot(profile: Path) -> None:
    entries = profile / "efiboot" / "loader" / "entries"
    if not entries.is_dir():
        return
    base = next((p for p in sorted(entries.glob("*.conf"))
                 if "linux" in p.name and not re.search(r"speech|access", p.name)), None)
    if base is None:
        warn("systemd-boot: no default linux entry found")
        return
    text = base.read_text()
    for i, (prof, title) in enumerate(AUTORUN):
        new = re.sub(r"^title\s+.*$", f"title    {title}", text, count=1, flags=re.M)
        new = re.sub(r"^sort-key\s+(\S+)$", rf"sort-key \g<1>zcts{i}", new, count=1, flags=re.M)
        new, n = re.subn(r"^(options\s+.*)$", rf"\1 cts.autorun={prof}", new, count=1, flags=re.M)
        if n == 0:
            warn(f"systemd-boot: no options line in {base.name}")
            return
        (entries / f"{base.stem}-zcts{i}-{prof}.conf").write_text(new)
    print(f"patch_boot: systemd-boot entries added next to {base.name}")


def syslinux(profile: Path) -> None:
    cfg = profile / "syslinux" / "archiso_sys-linux.cfg"
    if not cfg.is_file():
        return
    text = cfg.read_text()
    m = re.search(r"^LABEL .*?(?=^LABEL |\Z)", text, flags=re.M | re.S)
    if not m:
        warn("syslinux: no LABEL block found")
        return
    # drop trailing comment lines that belong to the next entry
    block = re.sub(r"(\n#[^\n]*)+\s*$", "", m.group(0).rstrip()) + "\n"
    extra = ""
    for prof, title in AUTORUN:
        b = re.sub(r"^LABEL .*$", f"LABEL cts{prof}", block, count=1, flags=re.M)
        b = re.sub(r"^TEXT HELP\n.*?^ENDTEXT$", f"TEXT HELP\nBoot and automatically start the cts '{prof}' test profile.\nENDTEXT",
                   b, count=1, flags=re.M | re.S)
        b = re.sub(r"^MENU LABEL .*$", f"MENU LABEL {title}", b, count=1, flags=re.M)
        b, n = re.subn(r"^(APPEND .*)$", rf"\1 cts.autorun={prof}", b, count=1, flags=re.M)
        if n == 0:
            warn("syslinux: no APPEND line")
            return
        extra += "\n" + b
    cfg.write_text(text[: m.end()].rstrip() + "\n" + extra + "\n" + text[m.end():])
    print("patch_boot: syslinux entries added")


def grub(profile: Path) -> None:
    for cfg in (profile / "grub" / "grub.cfg", profile / "grub" / "loopback.cfg"):
        if not cfg.is_file():
            continue
        text = cfg.read_text()
        m = re.search(r"^menuentry .*?^\}\s*$", text, flags=re.M | re.S)
        if not m:
            warn(f"grub: no menuentry in {cfg.name}")
            continue
        block = m.group(0)
        extra = ""
        for prof, title in AUTORUN:
            b = re.sub(r'^menuentry\s+"[^"]*"', f'menuentry "{title}"', block, count=1, flags=re.M)
            b = re.sub(r"--id\s+'?[\w-]+'?", f"--id 'cts-{prof}'", b, count=1)
            b, n = re.subn(r"^(\s*linux\s+.*)$", rf"\1 cts.autorun={prof}", b, count=1, flags=re.M)
            if n == 0:
                warn(f"grub: no linux line in {cfg.name}")
                break
            extra += "\n\n" + b
        else:
            cfg.write_text(text[: m.end()] + extra + text[m.end():])
            print(f"patch_boot: grub entries added to {cfg.name}")


def rebrand(profile: Path) -> None:
    """Rename 'Arch Linux install medium' to 'ComputerTestSuite' in boot menu titles."""
    files = [*profile.glob("efiboot/loader/entries/*.conf"), *profile.glob("syslinux/*.cfg"), *profile.glob("grub/*.cfg")]
    for f in files:
        t = f.read_text()
        n = t.replace("Arch Linux install medium", "ComputerTestSuite live system")
        n = n.replace("It allows you to install Arch Linux or perform system maintenance.",
                      "Hardware stress testing from a root shell (type: cts).")
        if n != t:
            f.write_text(n)


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    profile = Path(sys.argv[1])
    systemd_boot(profile)
    syslinux(profile)
    grub(profile)
    rebrand(profile)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
