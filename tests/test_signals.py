import json
import wave

import numpy as np
import pytest

from sattrack import signals
from sattrack.player import Player

SR = 48000


def make_wav(path, seconds=60.0, quiet=None, tone=None, seed=1):
    """White noise like an open FM receiver.

    quiet=(t0, t1): the noise drops 20 dB and a 1 kHz tone appears (FM signal).
    tone=(t0, t1): a weak carrier drifting 700 -> 760 Hz on top of the noise (CW/SSB).
    """
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    t = np.arange(n) / SR
    x = rng.normal(0, 0.1, n)
    if quiet:
        m = (t >= quiet[0]) & (t < quiet[1])
        x[m] = x[m] * 0.1 + 0.2 * np.sin(2 * np.pi * 1000 * t[m])
    if tone:
        m = (t >= tone[0]) & (t < tone[1])
        f = 700 + 60 * (t[m] - tone[0]) / (tone[1] - tone[0])
        x[m] += 0.02 * np.sin(2 * np.pi * np.cumsum(f) / SR)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())
    return path


def test_noise_only_has_no_signals(tmp_path):
    r = signals.analyze(make_wav(tmp_path / "n.wav"), "FM")
    assert r["segments"] == []
    assert r["duration_s"] == pytest.approx(60, abs=0.2)
    assert len(r["activity"]) == 60


def test_fm_quieting_burst_is_found(tmp_path):
    r = signals.analyze(make_wav(tmp_path / "fm.wav", quiet=(20.0, 21.5)), "FM")
    assert len(r["segments"]) == 1
    s = r["segments"][0]
    assert s["kind"] == "FM"
    assert s["start"] == pytest.approx(20.0, abs=0.5)
    assert s["end"] == pytest.approx(21.5, abs=0.5)


def test_weak_drifting_carrier_is_found(tmp_path):
    r = signals.analyze(make_wav(tmp_path / "cw.wav", tone=(20.0, 45.0)), "CW")
    assert len(r["segments"]) == 1
    s = r["segments"][0]
    assert s["kind"] == "tone"
    assert s["start"] == pytest.approx(20.0, abs=1.0)
    assert s["end"] == pytest.approx(45.0, abs=1.0)


def test_quieting_is_ignored_outside_fm(tmp_path):
    # on SSB/CW a noise-level drop is just gain/AGC, not a signal
    p = tmp_path / "q.wav"
    make_wav(p, quiet=(20.0, 21.5))
    r = signals.analyze(p, "CW")
    assert all(s["kind"] == "tone" for s in r["segments"])
    assert signals.analyze(p, "FM")["segments"][0]["kind"] == "FM"


def test_analyze_record_caches_in_pass_json(cfg):
    from sattrack.passlog import PassLog
    pl = PassLog(cfg)
    cfg.path("recordings").mkdir(parents=True, exist_ok=True)
    wav = make_wav(cfg.path("recordings/x.wav"), quiet=(10.0, 11.0))
    path = pl.write("2026-11-21T04-12_ISS", {"status": "complete", "mode": "FM", "start": "2026-11-21T04:12:00+02:00",
                                            "satellite": {"name": "ISS", "norad_id": 25544},
                                            "audio_file": "recordings/x.wav"})
    sig = signals.analyze_record(cfg, path)
    assert signals.summary(sig)[0] == 1
    rec = json.loads(path.read_text(encoding="utf-8"))
    assert rec["signals"]["segments"] == sig["segments"]
    assert signals.cached(rec, wav) == rec["signals"]
    # a changed file invalidates the cache
    make_wav(wav, seconds=30.0)
    assert signals.cached(rec, wav) is None


def test_analyze_record_skips_running_pass(cfg):
    from sattrack.passlog import PassLog
    cfg.path("recordings").mkdir(parents=True, exist_ok=True)
    make_wav(cfg.path("recordings/y.wav"), seconds=5)
    path = PassLog(cfg).write("2026-11-21T05-00_ISS", {"status": "in_progress", "audio_file": "recordings/y.wav",
                                                        "satellite": {"name": "ISS", "norad_id": 25544}})
    assert signals.analyze_record(cfg, path) is None
    assert "signals" not in json.loads(path.read_text(encoding="utf-8"))


def test_player_load_and_seek(tmp_path):
    p = Player()
    p.load(make_wav(tmp_path / "p.wav", seconds=10))
    assert p.duration == pytest.approx(10)
    p.seek(4.5)
    assert p.position == pytest.approx(4.5)
    p.seek(99)
    assert p.position == pytest.approx(10)
    assert not p.playing
    p.unload()
    (tmp_path / "p.wav").unlink()      # the file is not held open
