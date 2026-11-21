import json
import wave

import numpy as np
import pytest

from sattrack import sstv

SR = 48000


def robot36_audio(rgb: np.ndarray, lead_s=2.0, tail_s=2.0, noise=0.05, clock=1.0, seed=3) -> np.ndarray:
    """Encode an RGB 240x320 picture as Robot36 audio (no VIS), FM-receiver-like noise around it.

    clock != 1 simulates a sound card running slightly fast/slow (slanted picture).
    """
    rgb = rgb.astype(float)
    Y = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
    U = 128 + (rgb[..., 2] - Y) / 1.772
    V = 128 + (rgb[..., 0] - Y) / 1.402
    lvl = lambda v: 1500 + np.clip(v, 0, 255) / 255 * 800        # noqa: E731
    parts = []                                                   # (frequency, seconds)
    for j in range(240):
        c = V[j] if j % 2 == 0 else U[j]
        parts += [(1200, 0.009), (1500, 0.003)] + [(f, 0.088 / 320) for f in lvl(Y[j])]
        parts += [(1500 if j % 2 == 0 else 2300, 0.0045), (1900, 0.0015)]
        parts += [(f, 0.044 / 320) for f in lvl(c)]
    sr = SR * clock
    edges = np.cumsum([0.0] + [d for _, d in parts]) * sr
    n = int(edges[-1])
    freq = np.repeat([f for f, _ in parts], np.diff(np.round(edges).astype(int)))[:n]
    tone = 0.3 * np.sin(2 * np.pi * np.cumsum(freq) / sr)
    rng = np.random.default_rng(seed)
    x = np.concatenate([rng.normal(0, 0.1, int(lead_s * SR)), tone, rng.normal(0, 0.1, int(tail_s * SR))])
    return x + rng.normal(0, noise, len(x))


def save(path, x):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())
    return path


def make_pattern():
    img = np.zeros((240, 320, 3), np.uint8)
    img[:, :160] = (200, 60, 60)          # red left
    img[:, 160:] = (60, 60, 200)          # blue right
    img[100:140, :, :] = 230              # white band
    return img


@pytest.mark.parametrize("clock", [1.0, 1.0004])
def test_robot36_roundtrip(tmp_path, clock):
    img = make_pattern()
    p = save(tmp_path / "s.wav", robot36_audio(img, clock=clock))
    ims = sstv.decode_file(p)
    assert len(ims) == 1
    im = ims[0]
    assert im.mode == "Robot36"
    assert im.start_s == pytest.approx(2.0, abs=0.01)
    assert im.rgb.shape == (240, 320, 3)
    err = np.abs(im.rgb.astype(int) - img.astype(int))
    inner = err[4:-4, 8:-8]                # edges blur over a pixel or two
    assert np.median(inner) < 12
    # no slant: the red/blue border stays at the same column on top and bottom
    for row in (10, 230):
        reds = im.rgb[row, :, 0] > im.rgb[row, :, 2]
        assert 150 <= np.argmin(reds) <= 170


def test_noise_has_no_picture(tmp_path):
    rng = np.random.default_rng(1)
    p = save(tmp_path / "n.wav", rng.normal(0, 0.1, SR * 30))
    assert sstv.decode_file(p) == []


def test_decode_record_saves_pngs_and_delete_removes_them(cfg):
    from sattrack.passlog import PassLog
    cfg.path("recordings").mkdir(parents=True, exist_ok=True)
    save(cfg.path("recordings/v.wav"), robot36_audio(make_pattern()))
    pl = PassLog(cfg)
    path = pl.write("2026-11-21T11-17_VIZARD", {"status": "complete", "mode": "FM", "audio_file": "recordings/v.wav",
                                                "satellite": {"name": "VIZARD", "norad_id": 1}})
    res = sstv.decode_record(cfg, path)
    assert len(res["images"]) == 1
    png = cfg.path(res["images"][0]["file"])
    assert png.name == "v_sstv1.png" and png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert json.loads(path.read_text(encoding="utf-8"))["sstv"] == res
    assert sstv.decode_record(cfg, path) == res         # cached
    pl.delete(path.stem)
    assert not png.exists()
