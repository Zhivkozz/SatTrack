"""Find the few seconds of signal in a long, mostly-noise pass recording.

Two independent cues are computed on a 50 ms grid:

* FM quieting: in FM mode the receiver outputs loud hiss with no signal; when a
  carrier arrives the noise in the 3-4.5 kHz band drops. Catches short packets.
* Narrowband line: a steady or slowly drifting tone (CW/SSB carrier, beacon,
  telemetry tone) stands out of a whitened spectrum averaged over ~1 s.

The spectrum is whitened against a per-bin noise reference that is local in
time (5th percentile per 10 s block, median over +-2 min), so the radio's
passband shape, birdies and gain/filter steps are not mistaken for signals.
Thresholds are absolute dB values tuned on real recordings (2026-11-21).
"""

from __future__ import annotations

import logging
import math
import wave
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

VERSION = 1             # bump when the algorithm changes: cached results are recomputed
FR = 20                 # analysis frames per second (50 ms hop)
SKIP_S = 0.5            # every recording starts with a short silent glitch
ACTIVITY_STEP_S = 1.0   # resolution of the stored activity envelope

QUIET_ON, QUIET_OFF = 3.5, 2.0   # dB below the noise reference (FM only)
TONE_ON, TONE_OFF = 4.5, 3.5     # dB above the frame's spectral floor
TONE_MIN_S, TONE_MIN_DB = 1.0, 5.0
MIN_SEG_S, MERGE_GAP_S = 0.25, 2.0


def _movavg(x: np.ndarray, n: int) -> np.ndarray:
    """Centered moving average along axis 0 (edges padded with edge values)."""
    if n <= 1 or len(x) == 0:
        return x
    lo, hi = n // 2, n - 1 - n // 2
    pad = [(lo, hi)] + [(0, 0)] * (x.ndim - 1)
    c = np.cumsum(np.pad(x, pad, mode="edge"), axis=0, dtype=np.float64)
    c = np.concatenate([np.zeros((1,) + x.shape[1:]), c])
    return ((c[n:] - c[:-n]) / n).astype(np.float32)


def _local_ref(db: np.ndarray, block_s: float = 10.0, half_blocks: int = 12, pct: float = 5) -> np.ndarray:
    B = int(block_s * FR)
    nb = max(1, math.ceil(len(db) / B))
    blk = np.stack([np.percentile(db[i * B:(i + 1) * B], pct, axis=0) for i in range(nb)])
    sm = np.stack([np.median(blk[max(0, i - half_blocks):i + half_blocks + 1], axis=0) for i in range(nb)])
    return np.repeat(sm, B, axis=0)[: len(db)]


def _hysteresis(x: np.ndarray, on: float, off: float) -> np.ndarray:
    out = np.zeros(len(x), bool)
    state = False
    for i, v in enumerate(x):
        state = bool(v > off) if state else bool(v > on)
        out[i] = state
    return out


def _runs(active: np.ndarray, merge_gap: int) -> list[list[int]]:
    d = np.diff(np.concatenate([[0], active.astype(np.int8), [0]]))
    runs: list[list[int]] = []
    for s, e in zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)):
        if runs and s - runs[-1][1] <= merge_gap:
            runs[-1][1] = int(e)
        else:
            runs.append([int(s), int(e)])
    return runs


def _spectra(path: Path) -> tuple[int, np.ndarray, np.ndarray]:
    """Read the WAV in chunks; return (sample rate, hf band dB per frame, dB of 200-4500 Hz bins)."""
    with wave.open(str(path)) as w:
        sr, ch, width, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        if width != 2:
            raise ValueError(f"unsupported sample width {width * 8} bit")
        hop = sr // FR
        nfft = 1 << max(8, round(math.log2(sr * 0.0427)))   # 2048 at 44.1/48 kHz
        f = np.fft.rfftfreq(nfft, 1 / sr)
        hf_m = (f >= 3000) & (f < 4500)
        ln_m = (f >= 200) & (f < 4500)
        win = np.hanning(nfft).astype(np.float32)
        hf_out, ln_out = [], []
        tail = np.zeros(0, np.float32)
        chunk = hop * 1000
        read = 0
        while read < n:
            raw = w.readframes(min(chunk, n - read))
            if not raw:
                break
            x = np.frombuffer(raw, np.int16)
            read += len(x) // ch
            if ch > 1:
                x = x.reshape(-1, ch)[:, 0]
            x = np.concatenate([tail, x.astype(np.float32) / 32768.0])
            nfr = (len(x) - nfft) // hop + 1
            if nfr <= 0:
                tail = x
                continue
            idx = (np.arange(nfr) * hop)[:, None] + np.arange(nfft)[None, :]
            P = np.abs(np.fft.rfft(x[idx] * win, axis=1)) ** 2
            hf_out.append(10 * np.log10(P[:, hf_m].mean(axis=1) + 1e-20))
            ln_out.append((10 * np.log10(P[:, ln_m] + 1e-20)).astype(np.float32))
            tail = x[nfr * hop:]
    if not hf_out:
        return sr, np.zeros(0, np.float32), np.zeros((0, int(ln_m.sum())), np.float32)
    return sr, np.concatenate(hf_out).astype(np.float32), np.concatenate(ln_out)


def analyze(path: Path, mode: str = "FM") -> dict[str, Any]:
    """Analyse one recording; the result is JSON-serialisable and is cached in the pass record."""
    path = Path(path)
    sr, hf, db = _spectra(path)
    skip = int(SKIP_S * FR)
    hf, db = hf[skip:], db[skip:]
    nfr = len(hf)
    result: dict[str, Any] = {"version": VERSION, "audio_size": path.stat().st_size,
                              "duration_s": round((nfr + skip) / FR, 2), "segments": [],
                              "activity_step_s": ACTIVITY_STEP_S, "activity": ""}
    if nfr < FR * 2:
        return result

    quiet = np.percentile(hf, 60) - _movavg(hf, 3)
    db -= _local_ref(db)
    db -= np.median(db, axis=1, keepdims=True)
    sm = _movavg(db, FR)
    mx = sm.copy()
    for s in (1, 2):   # tolerate Doppler drift of a couple of bins
        mx[:, s:] = np.maximum(mx[:, s:], sm[:, :-s])
        mx[:, :-s] = np.maximum(mx[:, :-s], sm[:, s:])
    tone = mx.max(axis=1) - np.median(mx, axis=1)
    del db, sm, mx

    fm = (mode or "FM").upper() == "FM"
    q_act = _hysteresis(quiet, QUIET_ON, QUIET_OFF) if fm else np.zeros(nfr, bool)
    t_act = _hysteresis(tone, TONE_ON, TONE_OFF)
    for s, e in _runs(q_act | t_act, int(MERGE_GAP_S * FR)):
        if e - s < MIN_SEG_S * FR:
            continue
        q_any = bool(q_act[s:e].any())
        # a real carrier lasts; short tone-only blips are noise peaks
        if not q_any and (e - s < TONE_MIN_S * FR or tone[s:e].max() < TONE_MIN_DB):
            continue
        result["segments"].append({
            "start": round((s + skip) / FR, 2), "end": round((e + skip) / FR, 2),
            "kind": "FM" if q_any else "tone",   # quieting = a carrier in the FM receiver
            "quiet_db": round(float(quiet[s:e].max()), 1) if fm else None,
            "tone_db": round(float(tone[s:e].max()), 1),
        })

    # activity envelope 0-9 per second, for drawing the timeline
    score = np.maximum(np.clip(quiet / 10, 0, 1) if fm else 0, np.clip((tone - 2) / 10, 0, 1))
    step = int(ACTIVITY_STEP_S * FR)
    score = np.concatenate([np.zeros(skip), score])
    nb = math.ceil(len(score) / step)
    env = np.pad(score, (0, nb * step - len(score))).reshape(nb, step).max(axis=1)
    result["activity"] = "".join(str(int(v)) for v in np.round(env * 9))
    return result


def summary(sig: dict[str, Any] | None) -> tuple[int, float]:
    """(number of segments, total seconds)."""
    segs = (sig or {}).get("segments") or []
    return len(segs), sum(s["end"] - s["start"] for s in segs)


def cached(rec: dict[str, Any], audio: Path) -> dict[str, Any] | None:
    """The cached analysis if it still matches the audio file and algorithm."""
    sig = rec.get("signals")
    try:
        if sig and sig.get("version") == VERSION and sig.get("audio_size") == audio.stat().st_size:
            return sig
    except OSError:
        pass
    return None


def analyze_record(cfg, rec_path: Path) -> dict[str, Any] | None:
    """Analyse a pass record's audio if needed and store the result in its JSON.

    Writes only the pass JSON (not index.json), so it is safe next to a running scheduler.
    Skips passes that are still being recorded.
    """
    from sattrack.fsutil import atomic_write_json, read_json
    rec = read_json(rec_path, {}) or {}
    if rec.get("status") == "in_progress" or not rec.get("audio_file"):
        return None
    audio = cfg.path(rec["audio_file"])
    if not audio.exists():
        return None
    sig = cached(rec, audio)
    if sig is not None:
        return sig
    sig = analyze(audio, rec.get("mode") or "FM")
    rec = read_json(rec_path, {}) or rec      # re-read: keep any change made meanwhile
    rec["signals"] = sig
    atomic_write_json(rec_path, rec)
    n, total = summary(sig)
    log.info("Анализ на %s: %d сигнала (%.0f s)", audio.name, n, total)
    return sig
