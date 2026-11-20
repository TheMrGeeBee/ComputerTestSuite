#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee
# Build the ComputerTestSuite live ISO from archiso's "releng" profile.
#
#   sudo scripts/build-iso.sh            -> out/cts-YYYY.MM.DD-x86_64.iso
#
# Requires: archiso (pacman -S archiso), run as root on an Arch-based host.
# The ISO uses the profile's own pacman.conf (official Arch repos), not the host's.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RELENG=/usr/share/archiso/configs/releng
BUILD="$ROOT/build"
PROFILE="$BUILD/profile"
# mkarchiso's work dir holds a full root filesystem with /proc, /sys and /dev
# bind-mounted into it. Keep it OUTSIDE the project tree, or IDEs and file
# watchers will try to index it (PyCharm runs out of heap and freezes).
WORK="${CTS_WORK_DIR:-/var/tmp/cts-archiso-work}"
OUT="$ROOT/out"

die() { echo "build-iso: $*" >&2; exit 1; }

# Unmount anything still mounted below $1 (deepest first), e.g. left behind
# by an interrupted mkarchiso. Never rm -rf a tree with live bind mounts in it.
unmount_under() {
  local dir m
  dir="$(realpath -m "$1")"
  [[ -d $dir ]] || return 0
  findmnt -rn -o TARGET | awk -v p="$dir/" 'index($0, p) == 1' | sort -r |
    while read -r m; do umount -R "$m" 2>/dev/null || umount -l "$m"; done
  if findmnt -rn -o TARGET | awk -v p="$dir/" 'index($0, p) == 1' | grep -q .; then
    die "filesystems still mounted under $dir; refusing to delete it"
  fi
}

safe_rm() {
  local d
  for d in "$@"; do
    unmount_under "$d"
    rm -rf --one-file-system "$d"
  done
}

[[ $EUID -eq 0 ]] || die "must be run as root (mkarchiso needs it): sudo $0"
command -v mkarchiso >/dev/null || die "archiso not installed: pacman -S archiso"
[[ -d $RELENG ]] || die "releng profile not found at $RELENG"

echo "==> Preparing profile in $PROFILE (work dir: $WORK)"
# Also clears build/work from older versions of this script.
safe_rm "$PROFILE" "$WORK" "$BUILD/work"
cleanup() {
  unmount_under "$WORK"
  # Hand build/ and out/ back to the invoking user, even if the build failed.
  if [[ -n ${SUDO_UID:-} ]]; then
    chown -R "$SUDO_UID:$SUDO_GID" "$BUILD" "$OUT" 2>/dev/null || true
  fi
}
trap cleanup EXIT
mkdir -p "$BUILD" "$OUT"
cp -a "$RELENG" "$PROFILE"

# Own package cache: a host such as CachyOS caches its own rebuilds under the same file
# names as Arch's packages, which then fail Arch's signature check (seen with dkms).
PKG_CACHE="${CTS_PKG_CACHE:-/var/cache/cts-archiso-pkg}"
mkdir -p "$PKG_CACHE"
sed -i "s|^#\?CacheDir.*|CacheDir    = $PKG_CACHE/|" "$PROFILE/pacman.conf"
grep -q "^CacheDir    = $PKG_CACHE/" "$PROFILE/pacman.conf" || die "could not set CacheDir in the profile's pacman.conf"

echo "==> Packages"
{
  cat "$RELENG/packages.x86_64"
  grep -Ev '^\s*(#|$)' "$ROOT/iso/packages.extra"
} | grep -Ev '^\s*(#|$)' | sort -u > "$PROFILE/packages.x86_64"

# Version from the latest git tag (v1.2.0 -> 1.2.0; commits after the tag and uncommitted
# changes are added: 1.2.0+3.gabc1234.dirty). git runs as the invoking user: the repo is theirs.
as_user=()
[[ -n ${SUDO_USER:-} ]] && as_user=(sudo -u "$SUDO_USER")
DESCRIBE="$("${as_user[@]}" git -C "$ROOT" describe --tags --match 'v[0-9]*' --dirty 2>/dev/null || true)"
CTS_VERSION="$(PYTHONPATH="$ROOT/src" python3 -c \
  'import sys; from computertestsuite import version_from_describe as v; print(v(sys.argv[1]))' "$DESCRIBE")"
[[ -n $DESCRIBE ]] || CTS_VERSION="unknown"
echo "==> Version $CTS_VERSION"

echo "==> Profile overrides"
printf '\n# ---- ComputerTestSuite overrides ----\n' >> "$PROFILE/profiledef.sh"
cat "$ROOT/iso/profiledef.overrides.sh" >> "$PROFILE/profiledef.sh"
# Name the ISO after the version (out/cts-1.2.0-x86_64.iso); '+' is avoided in file names.
printf 'iso_version="%s"\n' "${CTS_VERSION//+/-}" >> "$PROFILE/profiledef.sh"

echo "==> airootfs overlay"
cp -a "$ROOT/iso/airootfs/." "$PROFILE/airootfs/"
cat "$ROOT/iso/zlogin.append" >> "$PROFILE/airootfs/root/.zlogin"

echo "==> Installing ComputerTestSuite into /opt/cts"
mkdir -p "$PROFILE/airootfs/opt/cts"
cp -a "$ROOT/src/computertestsuite" "$PROFILE/airootfs/opt/cts/"
cp "$ROOT/LICENSE" "$PROFILE/airootfs/opt/cts/LICENSE"
find "$PROFILE/airootfs/opt/cts" -name '__pycache__' -type d -prune -exec rm -rf {} +
printf '# Written by scripts/build-iso.sh\nVERSION = "%s"\n' "$CTS_VERSION" \
  > "$PROFILE/airootfs/opt/cts/computertestsuite/_build_version.py"

echo "==> Boot menu entries"
python3 "$ROOT/scripts/patch_boot.py" "$PROFILE"

echo "==> Building ISO (this takes a while)"
mkarchiso -v -w "$WORK" -o "$OUT" "$PROFILE"

# Hand the output back to the invoking user.
if [[ -n ${SUDO_UID:-} ]]; then
  chown -R "$SUDO_UID:$SUDO_GID" "$OUT"
fi
safe_rm "$WORK"
echo "==> Done:"
ls -lh "$OUT"/*.iso
