"""Generate the SatTrack icon (sattrack/assets/sattrack.ico and .png).

Pure numpy rendering (no image libraries): shapes are drawn as masks at high
resolution and averaged down for anti-aliasing. Run from the project root:

    .venv\\Scripts\\python tools\\make_icon.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np

HI = 1024                      # render resolution
SIZES = (256, 64, 48, 32, 24, 16)
OUT = Path(__file__).resolve().parents[1] / "sattrack" / "assets"


def hex_rgb(h: str) -> np.ndarray:
    return np.array([int(h[i:i + 2], 16) for i in (1, 3, 5)], dtype=float) / 255


def render() -> np.ndarray:
    """Return an HI x HI x 4 float RGBA image in [0, 1]."""
    v, u = np.mgrid[0:HI, 0:HI].astype(float) / HI   # u: x (right), v: y (down)
    img = np.zeros((HI, HI, 4))

    def paint(mask, color, alpha=1.0):
        c = hex_rgb(color) if isinstance(color, str) else color
        a = mask.astype(float) * alpha
        img[..., :3] = img[..., :3] * (1 - a[..., None]) + c * a[..., None]
        img[..., 3] = np.maximum(img[..., 3], a)

    # Background: rounded square with a vertical gradient.
    r, m = 0.19, 0.03
    dx = np.maximum(np.maximum(m + r - u, u - (1 - m - r)), 0)
    dy = np.maximum(np.maximum(m + r - v, v - (1 - m - r)), 0)
    bg = (dx ** 2 + dy ** 2 <= r ** 2) & (u >= m) & (u <= 1 - m) & (v >= m) & (v <= 1 - m)
    top, bottom = hex_rgb("#1d4ed8"), hex_rgb("#0b1a3a")
    grad = top * (1 - v[..., None]) + bottom * v[..., None]
    img[..., :3] = np.where(bg[..., None], grad, 0)
    img[..., 3] = bg

    # Faint orbit arc behind the satellite.
    ox, oy = u - 0.5, (v - 0.55) / 0.55
    ring = np.abs(np.sqrt(ox ** 2 + oy ** 2) - 0.36) < 0.008
    paint(ring & bg & (v < 0.62), "#93c5fd", 0.45)

    # Radio waves from the ground station (bottom left) towards the satellite.
    gx, gy = 0.22, 0.80
    d = np.sqrt((u - gx) ** 2 + (v - gy) ** 2)
    ang = np.degrees(np.arctan2(v - gy, u - gx))          # -45 deg = up-right
    sector = np.abs(ang + 45) < 38
    for radius in (0.13, 0.23, 0.33):
        paint((np.abs(d - radius) < 0.028) & sector, "#f59e0b")
    paint(d < 0.055, "#f59e0b")

    # Satellite: body, booms and two solar panels, rotated.
    cx, cy, a = 0.60, 0.40, np.radians(-35)
    xr = np.cos(a) * (u - cx) + np.sin(a) * (v - cy)
    yr = -np.sin(a) * (u - cx) + np.cos(a) * (v - cy)
    paint((np.abs(xr) < 0.105) & (np.abs(yr) < 0.012), "#e5e7eb")           # boom
    for side in (-1, 1):
        px = xr - side * 0.225
        panel = (np.abs(px) < 0.12) & (np.abs(yr) < 0.065)
        paint(panel, "#38bdf8")
        cells = panel & ((np.abs(((px + 0.12) % 0.06) - 0.03) > 0.024) | (np.abs(yr) < 0.006))
        paint(cells, "#0c4a6e", 0.9)                                          # cell grid
    body = (np.abs(xr) < 0.075) & (np.abs(yr) < 0.075)
    paint(body, "#f8fafc")
    paint(body & (np.abs(xr) < 0.045) & (np.abs(yr) < 0.045), "#cbd5e1")
    return img


def downsample(img: np.ndarray, size: int) -> np.ndarray:
    f = HI // size
    small = img[: size * f, : size * f].reshape(size, f, size, f, 4).mean(axis=(1, 3))
    # un-premultiply colour so anti-aliased edges are not darkened
    a = small[..., 3:4]
    rgb = np.where(a > 0, small[..., :3] / np.maximum(a, 1e-6), 0)
    out = np.concatenate([np.clip(rgb, 0, 1), a], axis=-1)
    return (out * 255 + 0.5).astype(np.uint8)


def png_bytes(rgba: np.ndarray) -> bytes:
    h, w, _ = rgba.shape
    raw = b"".join(b"\x00" + rgba[y].tobytes() for y in range(h))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def ico_bytes(pngs: dict[int, bytes]) -> bytes:
    """ICO container with PNG-encoded images (supported since Windows Vista)."""
    head = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries, blobs = b"", b""
    for size, data in pngs.items():
        s = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", s, s, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    return head + entries + blobs


def main() -> None:
    img = render()
    OUT.mkdir(parents=True, exist_ok=True)
    pngs = {s: png_bytes(downsample(img, s)) for s in SIZES}
    (OUT / "sattrack.png").write_bytes(pngs[256])
    (OUT / "sattrack.ico").write_bytes(ico_bytes(pngs))
    print("written", OUT / "sattrack.ico", OUT / "sattrack.png")


if __name__ == "__main__":
    main()
