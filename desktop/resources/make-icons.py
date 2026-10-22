"""Generate the desktop app icons without an image dependency.

Electron needs a PNG (window/tray) and electron-builder needs a multi-size ICO
(installer + shortcut). Both are written here from a tiny rasteriser so the build
has no external image tooling: a rounded square in the product accent with the
routing glyph (three nodes converging).

Run:  python desktop/resources/make-icons.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent

ACCENT = (37, 99, 235)        # #2563eb — the single accent colour of the design system
ACCENT_DARK = (30, 64, 175)   # #1e40af
SURFACE = (248, 250, 252)     # #f8fafc
NODE = (255, 255, 255)


def rounded_square_coverage(x: float, y: float, size: int, radius: int) -> float:
    """Anti-aliased coverage of a rounded square covering the whole canvas."""
    inset = size * 0.04
    left, top = inset, inset
    right, bottom = size - inset, size - inset
    if x < left or x > right or y < top or y > bottom:
        return 0.0
    cx = min(max(x, left + radius), right - radius)
    cy = min(max(y, top + radius), bottom - radius)
    dx, dy = x - cx, y - cy
    distance = (dx * dx + dy * dy) ** 0.5
    if distance <= radius - 0.75:
        return 1.0
    if distance >= radius + 0.75:
        return 0.0
    return (radius + 0.75 - distance) / 1.5


def disc_coverage(x: float, y: float, cx: float, cy: float, r: float) -> float:
    distance = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
    if distance <= r - 0.75:
        return 1.0
    if distance >= r + 0.75:
        return 0.0
    return (r + 0.75 - distance) / 1.5


def segment_coverage(x: float, y: float, x1: float, y1: float, x2: float, y2: float, half_width: float) -> float:
    vx, vy = x2 - x1, y2 - y1
    length_sq = vx * vx + vy * vy
    if length_sq == 0:
        return disc_coverage(x, y, x1, y1, half_width)
    t = ((x - x1) * vx + (y - y1) * vy) / length_sq
    t = max(0.0, min(1.0, t))
    px, py = x1 + t * vx, y1 + t * vy
    return disc_coverage(x, y, px, py, half_width)


def blend(base: tuple[int, int, int], over: tuple[int, int, int], alpha: float) -> tuple[int, int, int]:
    return (
        int(round(base[0] * (1 - alpha) + over[0] * alpha)),
        int(round(base[1] * (1 - alpha) + over[1] * alpha)),
        int(round(base[2] * (1 - alpha) + over[2] * alpha)),
    )


def render(size: int) -> bytes:
    """RGBA raw pixels for one icon size."""
    radius = int(size * 0.22)
    half_width = size * 0.035
    node_r = size * 0.105
    # Three input nodes on the left, one output node on the right.
    inputs = [
        (size * 0.30, size * 0.28),
        (size * 0.30, size * 0.50),
        (size * 0.30, size * 0.72),
    ]
    output = (size * 0.72, size * 0.50)

    rows = bytearray()
    for py in range(size):
        y = py + 0.5
        rows.append(0)  # PNG filter type 0
        for px in range(size):
            x = px + 0.5
            coverage = rounded_square_coverage(x, y, size, radius)
            if coverage <= 0:
                rows.extend((0, 0, 0, 0))
                continue
            # Vertical accent gradient, kept subtle (no loud gradient in the UI).
            blend_factor = y / size
            colour = blend(ACCENT, ACCENT_DARK, blend_factor * 0.55)

            glyph = 0.0
            for cx, cy in inputs:
                glyph = max(
                    glyph,
                    disc_coverage(x, y, cx, cy, node_r),
                    segment_coverage(x, y, cx, cy, output[0], output[1], half_width),
                )
            glyph = max(glyph, disc_coverage(x, y, output[0], output[1], node_r * 1.12))

            if glyph > 0:
                colour = blend(colour, NODE, min(1.0, glyph))
            elif (
                disc_coverage(x, y, size * 0.5, size * 0.5, size * 0.02) > 0.5
                and size <= 32
            ):
                colour = SURFACE  # keeps 16px tray icons legible

            alpha = int(round(255 * coverage))
            rows.extend((colour[0], colour[1], colour[2], alpha))
    return bytes(rows)


def png_bytes(size: int) -> bytes:
    raw = render(size)

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def ico_bytes(sizes: tuple[int, ...]) -> bytes:
    """ICO container with PNG-compressed entries (supported since Vista)."""
    images = [(size, png_bytes(size)) for size in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = bytearray()
    payload = bytearray()
    for size, data in images:
        dimension = 0 if size >= 256 else size
        entries.extend(
            struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(data), offset)
        )
        payload.extend(data)
        offset += len(data)
    return bytes(header + entries + payload)


def main() -> int:
    png = png_bytes(512)
    (OUT_DIR / "icon.png").write_bytes(png)
    (OUT_DIR / "icon-512.png").write_bytes(png)
    (OUT_DIR / "icon.ico").write_bytes(ico_bytes((16, 24, 32, 48, 64, 128, 256)))
    tray = png_bytes(32)
    (OUT_DIR / "tray.png").write_bytes(tray)
    for name in ("icon.png", "icon.ico", "tray.png"):
        path = OUT_DIR / name
        print(f"{name}: {path.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
