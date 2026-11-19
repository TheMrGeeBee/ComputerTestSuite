# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

"""Interactive LCD / display test: full-screen patterns drawn straight into the framebuffer.

Needs a Linux text console (not a terminal window or SSH) and root, which the live
system has. The console is switched to graphics mode while the patterns are shown,
so no text is drawn over them, and it is always switched back afterwards.

Keys: Right / Space / Enter next pattern, Left / Backspace previous, Esc / Q quit.
"""

from __future__ import annotations

import fcntl
import mmap
import os
import select
import struct
import sys
import termios
import tty
from collections.abc import Callable
from dataclasses import dataclass

FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602
KDSETMODE = 0x4B3A
KD_TEXT, KD_GRAPHICS = 0, 1


@dataclass(frozen=True)
class FbInfo:
    xres: int
    yres: int
    bpp: int
    stride: int  # bytes per line, may include padding
    red: tuple[int, int]  # (bit offset, bit length)
    green: tuple[int, int]
    blue: tuple[int, int]
    yoffset: int = 0
    smem_len: int = 0

    @property
    def frame_size(self) -> int:
        return self.stride * self.yres


def fb_info(fd: int) -> FbInfo:
    var = bytearray(160)
    fcntl.ioctl(fd, FBIOGET_VSCREENINFO, var)
    xres, yres, xvirt, yvirt, _xo, yoffset, bpp, _gray = struct.unpack_from("8I", var, 0)
    ro, rl, _, go, gl, _, bo, bl, _ = struct.unpack_from("9I", var, 32)
    fix = bytearray(80)
    fcntl.ioctl(fd, FBIOGET_FSCREENINFO, fix)
    smem_len = struct.unpack_from("I", fix, 24)[0]
    stride = struct.unpack_from("I", fix, 48)[0] or xres * bpp // 8
    width, height = drawing_area(xres, yres, xvirt, yvirt, stride, bpp, smem_len)
    return FbInfo(width, height, bpp, stride, (ro, rl), (go, gl), (bo, bl), yoffset if height == yres else 0,
                  smem_len)


def drawing_area(xres: int, yres: int, xvirt: int, yvirt: int, stride: int, bpp: int,
                 smem_len: int) -> tuple[int, int]:
    """The area to paint.

    With several monitors, the kernel's framebuffer is sized for the largest one while the
    reported mode may be a smaller one; every monitor shows the buffer from the top-left.
    A buffer *wider* than the mode means that, so paint all of it. A buffer that is only
    *taller* is double buffering: keep the reported height.
    """
    if xvirt > xres:
        width = min(xvirt, stride * 8 // bpp)
        height = min(yvirt, smem_len // stride) if smem_len else yvirt
        return width, height
    return xres, yres


def pack(info: FbInfo, rgb: tuple[int, int, int]) -> bytes:
    """One pixel in the framebuffer's native format."""
    value = 0
    for c, (off, length) in zip(rgb, (info.red, info.green, info.blue)):
        if length:
            value |= (c >> (8 - length)) << off
    return value.to_bytes(info.bpp // 8, "little")


def _row(info: FbInfo, pixels: bytes) -> bytes:
    return pixels + b"\0" * (info.stride - len(pixels))


def _rows(info: FbInfo, row_for_y: Callable[[int], bytes]) -> bytes:
    """Build a frame from a function giving the (unpadded) pixels of row y; equal rows are reused."""
    cache: dict[bytes, bytes] = {}
    out = []
    for y in range(info.yres):
        px = row_for_y(y)
        if px not in cache:
            cache[px] = _row(info, px)
        out.append(cache[px])
    return b"".join(out)


# ------------------------------------------------------------------ patterns

def solid(rgb: tuple[int, int, int]) -> Callable[[FbInfo], bytes]:
    def build(info: FbInfo) -> bytes:
        return _row(info, pack(info, rgb) * info.xres) * info.yres
    return build


def gray_gradient(info: FbInfo) -> bytes:
    w = max(1, info.xres - 1)
    row = b"".join(pack(info, (v, v, v)) for v in (x * 255 // w for x in range(info.xres)))
    return _row(info, row) * info.yres


def color_gradients(info: FbInfo) -> bytes:
    """Four horizontal bands: red, green, blue and gray ramps."""
    w = max(1, info.xres - 1)
    ramps = []
    for k in range(4):
        px = []
        for x in range(info.xres):
            v = x * 255 // w
            px.append(pack(info, (v, v, v) if k == 3 else tuple(v if i == k else 0 for i in range(3))))
        ramps.append(b"".join(px))
    band = max(1, info.yres // 4)
    return _rows(info, lambda y: ramps[min(3, y // band)])


def checkerboard(info: FbInfo) -> bytes:
    w, b = pack(info, (255, 255, 255)), pack(info, (0, 0, 0))
    a = (w + b) * (info.xres // 2) + (w if info.xres % 2 else b"")
    c = (b + w) * (info.xres // 2) + (b if info.xres % 2 else b"")
    return _rows(info, lambda y: a if y % 2 == 0 else c)


def grid(info: FbInfo) -> bytes:
    """1-pixel white border, a grid every 1/8 of the screen and a center cross on black."""
    white, black = pack(info, (255, 255, 255)), pack(info, (0, 0, 0))
    xs = {0, info.xres - 1, info.xres // 2} | {info.xres * i // 8 for i in range(1, 8)}
    ys = {0, info.yres - 1, info.yres // 2} | {info.yres * i // 8 for i in range(1, 8)}
    line = white * info.xres
    normal = b"".join(white if x in xs else black for x in range(info.xres))
    return _rows(info, lambda y: line if y in ys else normal)


PATTERNS: list[tuple[str, str, Callable[[FbInfo], bytes]]] = [
    ("Black", "stuck bright pixels, backlight bleed at the edges", solid((0, 0, 0))),
    ("White", "dead (dark) pixels, dirt, uneven backlight", solid((255, 255, 255))),
    ("Red", "dead or stuck red sub-pixels", solid((255, 0, 0))),
    ("Green", "dead or stuck green sub-pixels", solid((0, 255, 0))),
    ("Blue", "dead or stuck blue sub-pixels", solid((0, 0, 255))),
    ("Gray 50%", "clouding, pressure marks, uneven brightness", solid((128, 128, 128))),
    ("Gray gradient", "banding or jumps in brightness", gray_gradient),
    ("Colour gradients", "banding, missing colour depth, colour casts", color_gradients),
    ("Checkerboard", "flicker, crosstalk, panel driver faults (1-pixel pattern)", checkerboard),
    ("Grid and borders", "geometry, and the outermost pixels on all four edges", grid),
]


# ------------------------------------------------------------------ running it

def console_problem() -> str | None:
    """Why the test cannot run here, or None."""
    if os.geteuid() != 0:
        return "the display test needs root (it is root on the live system)"
    try:
        name = os.ttyname(sys.stdin.fileno())
    except OSError:
        return "not running on a terminal"
    if not (name.startswith("/dev/tty") and name[8:].isdigit()):
        return f"the display test only works on the text console (Ctrl+Alt+F1..F6), not on {name}"
    if not os.path.exists("/dev/fb0"):
        return "no framebuffer device (/dev/fb0)"
    return None


def _read_key(fd: int) -> str:
    """'next', 'prev', 'quit' or '' for other keys."""
    data = os.read(fd, 1)
    if data == b"\x1b":
        r, _, _ = select.select([fd], [], [], 0.05)
        if not r:
            return "quit"  # a lone Esc
        seq = os.read(fd, 8)
        return {b"[C": "next", b"[D": "prev", b"[A": "prev", b"[B": "next"}.get(seq[:2], "")
    if data in (b" ", b"\r", b"\n", b"n", b"N"):
        return "next"
    if data in (b"\x7f", b"\x08", b"p", b"P"):
        return "prev"
    if data in (b"q", b"Q", b"\x03"):
        return "quit"
    return ""


def run_display_test() -> str:
    """Show the patterns. Returns a short summary ('' if it could not run)."""
    problem = console_problem()
    if problem:
        return f"Display test not started: {problem}"

    print("\033[2J\033[H", end="")
    print("Display test\n============\n")
    for i, (name, hint, _) in enumerate(PATTERNS, 1):
        print(f"  {i:2}. {name:17} {hint}")
    print("\nLook at the whole screen for each pattern, ideally from straight in front.")
    print("Right / Space: next   Left: previous   Esc: quit\n\nPress any key to start...", flush=True)

    fd = sys.stdin.fileno()
    old_term = termios.tcgetattr(fd)
    fb = os.open("/dev/fb0", os.O_RDWR)
    try:
        tty.setraw(fd)
        _read_key(fd)
        info = fb_info(fb)
        start = info.yoffset * info.stride
        size = info.smem_len or (start + info.frame_size)  # never map more than the device has
        if start + info.frame_size > size:
            raise OSError("framebuffer is smaller than the visible screen")
        mm = mmap.mmap(fb, size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
        fcntl.ioctl(fd, KDSETMODE, KD_GRAPHICS)  # stop the console drawing text over the patterns
        try:
            i, shown, drawn = 0, 0, -1
            while i < len(PATTERNS):
                if i != drawn:
                    frame = PATTERNS[i][2](info)
                    mm[start:start + len(frame)] = frame
                    drawn, shown = i, max(shown, i + 1)
                key = _read_key(fd)
                if key == "quit":
                    break
                if key == "next":
                    i += 1
                elif key == "prev":
                    i = max(0, i - 1)
        finally:
            mm[start:start + info.frame_size] = b"\0" * info.frame_size
            fcntl.ioctl(fd, KDSETMODE, KD_TEXT)
            mm.close()
    finally:
        os.close(fb)
        termios.tcsetattr(fd, termios.TCSADRAIN, old_term)
        print("\033[2J\033[H", end="", flush=True)
    return f"Display test: {shown} of {len(PATTERNS)} patterns shown ({info.xres}x{info.yres}, {info.bpp} bpp)"
