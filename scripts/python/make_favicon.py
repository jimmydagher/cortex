#!/usr/bin/env python3
"""Build src/cortex/static/favicon.ico from the shapes in favicon.svg (standard library only).

favicon.svg is the source; favicon.ico is generated for browsers that ask for /favicon.ico.
Never edit the .ico by hand: change the SHAPES below (kept in step with the SVG) and rerun:

    python scripts/python/make_favicon.py
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

SIZE = 32
SAMPLES = 4  # supersampling per axis, for smooth edges
OUTPUT = Path(__file__).resolve().parents[2] / "src" / "cortex" / "static" / "favicon.ico"
BACKGROUND = (0x1D, 0x1B, 0x2E)
LINE = (0x8B, 0x7C, 0xF6)
NODE = (0xC4, 0xB8, 0xFF)
CORNER_RADIUS = 8.0
LINE_WIDTH = 1.6
SEGMENTS = [((9, 20), (16, 9)), ((16, 9), (23, 20)), ((23, 20), (9, 20)), ((16, 9), (16, 23))]
NODES = [((16, 9), 2.6), ((9, 20), 2.6), ((23, 20), 2.6), ((16, 23), 2.0)]
Color = tuple[int, int, int]


def _inside_rounded_square(x: float, y: float) -> bool:
    nearest_x = min(max(x, CORNER_RADIUS), SIZE - CORNER_RADIUS)
    nearest_y = min(max(y, CORNER_RADIUS), SIZE - CORNER_RADIUS)
    return math.hypot(x - nearest_x, y - nearest_y) <= CORNER_RADIUS


def _near_segment(x: float, y: float, start: tuple[int, int], end: tuple[int, int]) -> bool:
    (ax, ay), (bx, by) = start, end
    length = (bx - ax) ** 2 + (by - ay) ** 2
    t = max(0.0, min(1.0, ((x - ax) * (bx - ax) + (y - ay) * (by - ay)) / length))
    return math.hypot(x - (ax + t * (bx - ax)), y - (ay + t * (by - ay))) <= LINE_WIDTH / 2


def _color_at(x: float, y: float) -> Color | None:
    if not _inside_rounded_square(x, y):
        return None
    if any(math.hypot(x - cx, y - cy) <= radius for (cx, cy), radius in NODES):
        return NODE
    if any(_near_segment(x, y, start, end) for start, end in SEGMENTS):
        return LINE
    return BACKGROUND


def render() -> bytes:
    """Rasterize the icon to RGBA rows.

    Returns:
        SIZE*SIZE RGBA pixels, row by row, each row prefixed with PNG filter byte 0.
    """
    rows = bytearray()
    for row in range(SIZE):
        rows.append(0)
        for column in range(SIZE):
            hits = [_color_at(column + (sx + 0.5) / SAMPLES, row + (sy + 0.5) / SAMPLES)
                    for sy in range(SAMPLES) for sx in range(SAMPLES)]
            covered = [hit for hit in hits if hit is not None]
            if not covered:
                rows += bytes(4)
                continue
            channels = [round(sum(color[index] for color in covered) / len(covered)) for index in range(3)]
            rows += bytes([*channels, round(255 * len(covered) / len(hits))])
    return bytes(rows)


def png(pixels: bytes) -> bytes:
    """Wrap RGBA rows in a minimal PNG.

    Args:
        pixels: filtered RGBA rows from render().

    Returns:
        PNG file bytes.
    """
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(pixels, 9)) + chunk(b"IEND", b"")


def ico(image: bytes) -> bytes:
    """Wrap one PNG image in an ICO container.

    Args:
        image: PNG bytes of a SIZE x SIZE icon.

    Returns:
        ICO file bytes.
    """
    directory = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", SIZE, SIZE, 0, 0, 1, 32, len(image), 6 + 16)
    return directory + entry + image


def main() -> None:
    """Write favicon.ico next to favicon.svg."""
    OUTPUT.write_bytes(ico(png(render())))
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
