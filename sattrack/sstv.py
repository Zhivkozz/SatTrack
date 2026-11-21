"""SSTV decoder for pass recordings (Robot36), numpy only.

The recording is FM-demodulated receiver audio, so SSTV tones appear at their
nominal frequencies (1200 Hz sync, 1500-2300 Hz black-white). Steps:

1. Instantaneous frequency of the 1000-2500 Hz band, decimated to 12 kHz.
2. Sync pulses (9 ms of 1200 Hz) are found and chained into transmissions:
   pulses whose spacing is a whole number of 150 ms lines belong together.
3. Line timing is fitted by least squares (removes slant from sound card
   clock error), so lines lost to fading or a missing VIS header do not
   break the picture: such lines stay grey and the image continues.
4. Robot36: Y 88 ms + one chroma 44 ms per line (R-Y on even, B-Y on odd
   lines, told apart by the 1500/2300 Hz separator), converted to RGB.
"""

from __future__ import annotations

import logging
import struct
import wave
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

RATE = 12000            # Hz of the frequency track
F_SYNC, F_BLACK, F_WHITE = 1200.0, 1500.0, 2300.0

# Robot36 line layout (seconds from the start of the sync pulse)
R36_LINE = 0.150
R36_SYNC = 0.009
R36_Y = (0.012, 0.100)       # after 3 ms porch
R36_SEP = (0.1005, 0.1040)   # 4.5 ms separator: 1500 Hz -> R-Y, 2300 Hz -> B-Y
R36_C = (0.1060, 0.1495)     # after 1.5 ms porch (stop before the next sync)
R36_W, R36_H = 320, 240


@dataclass
class Image:
    start_s: float               # time of the first line in the recording
    lines: int                   # lines actually received (with sync)
    rgb: np.ndarray = field(repr=False)
    mode: str = "Robot36"



# ---------------------------------------------------------------- audio -> frequency
def freq_track(path: Path, chunk_s: float = 20.0) -> np.ndarray:
    """Instantaneous frequency (Hz) of the 1000-2500 Hz band at RATE samples/s."""
    with wave.open(str(path)) as w:
        sr, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
        if w.getsampwidth() != 2:
            raise ValueError("поддържат се само 16-битови WAV файлове")
        dec = sr / RATE
        if dec != int(dec):
            raise ValueError(f"честота на дискретизация {sr} Hz не се поддържа")
        dec = int(dec)
        margin = int(0.05 * sr)
        step = int(chunk_s * sr)
        out = []
        pos = 0
        while pos < n:
            a = max(0, pos - margin)
            w.setpos(a)
            raw = w.readframes(min(n, pos + step + margin) - a)
            x = np.frombuffer(raw, np.int16).astype(np.float32)
            if ch > 1:
                x = x.reshape(-1, ch)[:, 0]
            fr = _inst_freq(x, sr)
            keep = fr[pos - a: pos - a + min(step, n - pos)]
            keep = keep[: len(keep) // dec * dec].reshape(-1, dec).mean(axis=1)
            out.append(keep.astype(np.float32))
            pos += step
    return np.concatenate(out) if out else np.zeros(0, np.float32)


def _inst_freq(x: np.ndarray, sr: int) -> np.ndarray:
    n = len(x)
    if n < 16:
        return np.full(n, F_BLACK, np.float32)
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(n, 1 / sr)
    X[(f < 1000) | (f > 2500)] = 0
    A = np.zeros(n, complex)
    A[: len(X)] = X
    A[1: len(X)] *= 2
    a = np.fft.ifft(A)
    d = a[1:] * np.conj(a[:-1])               # phase step per sample
    fr = np.angle(d) * sr / (2 * np.pi)
    return np.concatenate([fr, fr[-1:]]).astype(np.float32)


# ---------------------------------------------------------------- sync and line timing
def _movmean(x: np.ndarray, n: int) -> np.ndarray:
    c = np.concatenate([[0.0], np.cumsum(x, dtype=np.float64)])
    m = (c[n:] - c[:-n]) / n
    return np.concatenate([m, np.full(n - 1, m[-1] if len(m) else 0.0)])


def find_syncs(fr: np.ndarray) -> np.ndarray:
    """Start times (s) of 9 ms 1200 Hz sync pulses."""
    w = int(R36_SYNC * RATE)
    near = (np.abs(fr - F_SYNC) < 120).astype(np.float32)
    score = _movmean(near, w)                  # fraction of the next 9 ms at ~1200 Hz
    cand = np.flatnonzero(score > 0.6)
    syncs, last = [], -10 ** 9
    for i in cand:
        if i - last < int(0.1 * RATE):         # one pulse per line
            if score[i] > score[syncs[-1]]:
                syncs[-1] = i
            continue
        syncs.append(i)
        last = i
    return np.array(syncs, float) / RATE


def chain_lines(syncs: np.ndarray, period: float = R36_LINE, tol: float = 0.004,
                max_gap: float = 3.0, min_lines: int = 20) -> list[tuple[float, float, np.ndarray]]:
    """Group sync pulses on a common line grid; return [(t0, line period, line indices)]."""
    groups: list[list[float]] = []
    for t in syncs:
        if groups:
            g = groups[-1]
            d = t - g[-1]
            k = round(d / period)
            if k >= 1 and abs(d - k * period) < tol * max(1, k ** 0.5) and d < max_gap:
                g.append(t)
                continue
        groups.append([t])
    out = []
    for g in groups:
        if len(g) < min_lines:
            continue
        t = np.array(g)
        k = np.round((t - t[0]) / period)
        for _ in range(2):                      # fit, drop outliers, refit
            b, a = np.polyfit(k, t, 1)
            ok = np.abs(t - (a + b * k)) < 0.002
            t, k = t[ok], k[ok]
        b, a = np.polyfit(k, t, 1)
        out.append((a, b, k.astype(int)))
    return out


# ---------------------------------------------------------------- pixels
def _sample(fr: np.ndarray, t0: float, t1: float, n: int) -> np.ndarray:
    """Mean frequency over n equal pixel intervals between t0 and t1 (s)."""
    edges = np.linspace(t0 * RATE, t1 * RATE, n + 1)
    lo = np.clip(np.floor(edges[:-1]).astype(int), 0, len(fr) - 1)
    hi = np.clip(np.maximum(lo + 1, np.ceil(edges[1:]).astype(int)), 1, len(fr))
    c = np.concatenate([[0.0], np.cumsum(fr, dtype=np.float64)])
    return (c[hi] - c[lo]) / (hi - lo)


def _level(f: np.ndarray) -> np.ndarray:
    return np.clip((f - F_BLACK) / (F_WHITE - F_BLACK) * 255.0, 0, 255)


def decode_robot36(fr: np.ndarray, t0: float, period: float, lines: np.ndarray) -> Image:
    """Decode one Robot36 transmission whose line k starts at t0 + k*period."""
    first = int(lines.min())
    n_lines = min(R36_H, int(lines.max()) - first + 1)
    have = np.zeros(R36_H, bool)
    have[np.clip(lines - first, 0, R36_H - 1)] = True
    Y = np.full((R36_H, R36_W), 128.0)
    C = np.full((R36_H, R36_W), 128.0)
    is_v = np.zeros(R36_H, bool)
    for j in range(n_lines):
        ts = t0 + (first + j) * period
        if ts + R36_LINE > len(fr) / RATE:
            break
        Y[j] = _level(_sample(fr, ts + R36_Y[0], ts + R36_Y[1], R36_W))
        C[j] = _level(_sample(fr, ts + R36_C[0], ts + R36_C[1], R36_W))
        s = fr[int((ts + R36_SEP[0]) * RATE): int((ts + R36_SEP[1]) * RATE)]
        is_v[j] = float(np.median(s)) < 1900 if len(s) else False
    # parity: separator votes, but lines strictly alternate -> pick the better of the two phases
    even_v = np.sum(is_v[0:n_lines:2]) + np.sum(~is_v[1:n_lines:2])
    odd_v = np.sum(~is_v[0:n_lines:2]) + np.sum(is_v[1:n_lines:2])
    v_line = (np.arange(R36_H) % 2 == 0) if even_v >= odd_v else (np.arange(R36_H) % 2 == 1)
    V = np.where(v_line[:, None], C, np.roll(C, 1, axis=0))
    U = np.where(~v_line[:, None], C, np.roll(C, 1, axis=0))
    V[0], U[0] = (C[0], C[1]) if v_line[0] else (C[1], C[0])
    R = Y + 1.402 * (V - 128)
    G = Y - 0.344 * (U - 128) - 0.714 * (V - 128)
    B = Y + 1.772 * (U - 128)
    rgb = np.clip(np.stack([R, G, B], -1), 0, 255).astype(np.uint8)
    rgb = rgb[:n_lines]                     # a fragment keeps only the rows that were received
    return Image(start_s=t0 + first * period, lines=int(have[:n_lines].sum()), rgb=rgb)


def decode_file(path: Path, min_lines: int = 40) -> list[Image]:
    """All Robot36 pictures in a recording (at least min_lines received lines each)."""
    fr = freq_track(Path(path))
    images = []
    for t0, period, lines in chain_lines(find_syncs(fr)):
        if abs(period - R36_LINE) > 0.0015:     # not Robot36 (sound card error is < 1 %)
            continue
        # a chain longer than one picture: split into 240-line frames
        rel = lines - lines.min()
        for start in range(0, int(rel.max()) + 1, R36_H):
            part = lines[(rel >= start) & (rel < start + R36_H)]
            if len(part) >= min_lines:
                images.append(decode_robot36(fr, t0, period, part))
    log.info("SSTV в %s: %d изображения", Path(path).name, len(images))
    return images


# ---------------------------------------------------------------- PNG output
def write_png(path: Path, rgb: np.ndarray, scale: int = 1) -> Path:
    """Write an RGB uint8 image as PNG (no Pillow needed; Tk can show it)."""
    if scale > 1:
        rgb = rgb.repeat(scale, axis=0).repeat(scale, axis=1)
    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(png)
    return Path(path)


# ---------------------------------------------------------------- pass records
VERSION = 1


def decode_record(cfg, rec_path: Path, force: bool = False) -> dict | None:
    """Decode the SSTV pictures of a pass, save them as PNG next to the WAV and
    cache the list in the pass JSON (only that file is written, not index.json)."""
    from sattrack.fsutil import atomic_write_json, read_json
    rec = read_json(rec_path, {}) or {}
    if rec.get("status") == "in_progress" or not rec.get("audio_file"):
        return None
    audio = cfg.path(rec["audio_file"])
    if not audio.exists():
        return None
    old = rec.get("sstv")
    if (not force and old and old.get("version") == VERSION and old.get("audio_size") == audio.stat().st_size
            and all(cfg.path(i["file"]).exists() for i in old.get("images", []))):
        return old
    for i in (old or {}).get("images", []):     # stale pictures of an older run
        cfg.path(i["file"]).unlink(missing_ok=True)
    images = []
    for n, im in enumerate(decode_file(audio), 1):
        png = write_png(audio.with_name(f"{audio.stem}_sstv{n}.png"), im.rgb)
        images.append({"file": _rel(cfg, png), "mode": im.mode, "start_s": round(im.start_s, 2),
                       "lines": im.lines, "rows": int(im.rgb.shape[0])})
    result = {"version": VERSION, "audio_size": audio.stat().st_size, "images": images}
    rec = read_json(rec_path, {}) or rec
    rec["sstv"] = result
    atomic_write_json(rec_path, rec)
    return result


def _rel(cfg, p: Path) -> str:
    try:
        return p.resolve().relative_to(cfg.base_dir.resolve()).as_posix()
    except ValueError:
        return str(p)
