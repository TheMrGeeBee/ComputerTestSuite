# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

import pytest

from computertestsuite.lcd import PATTERNS, FbInfo, checkerboard, grid, pack

XRGB = dict(bpp=32, red=(16, 8), green=(8, 8), blue=(0, 8))
RGB565 = dict(bpp=16, red=(11, 5), green=(5, 6), blue=(0, 5))


def test_pack_formats():
    info = FbInfo(4, 2, stride=16, **XRGB)
    assert pack(info, (255, 0, 0)) == bytes([0, 0, 255, 0])  # little-endian XRGB: B G R X
    assert pack(info, (0, 255, 0)) == bytes([0, 255, 0, 0])
    info16 = FbInfo(4, 2, stride=8, **RGB565)
    assert pack(info16, (255, 255, 255)) == b"\xff\xff"
    assert pack(info16, (255, 0, 0)) == (0b11111 << 11).to_bytes(2, "little")


@pytest.mark.parametrize("fmt", [XRGB, RGB565])
@pytest.mark.parametrize("name,_hint,build", PATTERNS)
def test_every_pattern_fills_exactly_one_frame(fmt, name, _hint, build):
    # odd sizes and a padded stride, like real hardware
    bpp = fmt["bpp"] // 8
    info = FbInfo(13, 9, stride=13 * bpp + 12, **fmt)
    assert len(build(info)) == info.frame_size, name


def test_padding_bytes_stay_zero():
    info = FbInfo(3, 2, stride=16, **XRGB)  # 12 bytes of pixels + 4 padding per row
    frame = PATTERNS[1][2](info)  # white
    assert frame[12:16] == b"\0\0\0\0" and frame[0:12] == pack(info, (255, 255, 255)) * 3


def test_checkerboard_alternates():
    info = FbInfo(4, 2, stride=16, **XRGB)
    w, b = pack(info, (255, 255, 255)), pack(info, (0, 0, 0))
    frame = checkerboard(info)
    assert frame[:16] == w + b + w + b and frame[16:32] == b + w + b + w


def test_grid_has_white_border():
    info = FbInfo(16, 16, stride=64, **XRGB)
    w = pack(info, (255, 255, 255))
    frame = grid(info)
    assert frame[:64] == w * 16  # top row
    assert frame[-64:] == w * 16  # bottom row
    row5 = frame[5 * 64:6 * 64]
    assert row5[:4] == w and row5[-4:] == w  # left and right edge


def test_drawing_area():
    from computertestsuite.lcd import drawing_area

    # Two monitors: reported mode 1920x1080, buffer 3440x1440 (values seen on a real system)
    assert drawing_area(1920, 1080, 3440, 1440, 13824, 32, 19906560) == (3440, 1440)
    # Laptop panel, double-buffered: keep the visible size
    assert drawing_area(1920, 1080, 1920, 2160, 7680, 32, 7680 * 2160) == (1920, 1080)
    # Plain single buffer
    assert drawing_area(1366, 768, 1366, 768, 5504, 32, 5504 * 768) == (1366, 768)


def test_write_frame_writes_everything_at_offset(tmp_path):
    import os
    from computertestsuite.lcd import write_frame

    f = tmp_path / "fb"
    f.write_bytes(b"\xee" * 64)
    fd = os.open(f, os.O_RDWR)
    try:
        write_frame(fd, b"\x01" * 32, 16)
    finally:
        os.close(fd)
    data = f.read_bytes()
    assert data[:16] == b"\xee" * 16 and data[16:48] == b"\x01" * 32 and data[48:] == b"\xee" * 16
