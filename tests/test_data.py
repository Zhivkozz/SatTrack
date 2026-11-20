import json
import struct
import wave
from datetime import datetime, timezone

from conftest import ISS_TLE
from sattrack import catalog
from sattrack.config import ConfigError, load_config
from sattrack.passlog import STATUS_INCOMPLETE, STATUS_IN_PROGRESS, PassLog
from sattrack.recorder import repair_wav

import pytest


def tx(norad, f, mode="FM", status="active", uuid=None):
    return {"norad_cat_id": norad, "downlink_low": f, "mode": mode, "status": status,
            "alive": True, "uuid": uuid or f"u{norad}-{f}", "description": "test", "type": "Transmitter"}


TLE = ISS_TLE + """SAT2
1 11111U 98067A   14020.93268519  .00009878  00000-0  18200-3 0  5082
2 11111  51.6498 109.4756 0003572  55.9686 274.8005 15.49815350868473
SAT3
1 22222U 98067A   14020.93268519  .00009878  00000-0  18200-3 0  5082
2 22222  51.6498 109.4756 0003572  55.9686 274.8005 15.49815350868473
"""


def test_config_validation(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"radio": {"baudrate": 1234}, "passes": {"min_elevation_deg": 120}}))
    with pytest.raises(ConfigError) as e:
        load_config(p)
    assert "baudrate" in str(e.value) and "min_elevation_deg" in str(e.value)


def test_band_and_selection(cfg):
    txs = [tx(25544, 145_800_000), tx(11111, 145_900_000, "CW"), tx(22222, 437_000_000),
           tx(33333, 145_000_000, status="inactive")]
    sel = catalog.build_selection(cfg, txs, {11111: 9, 25544: 3}, TLE)
    ids = [s.norad_id for s in sel]
    assert 22222 not in ids and 33333 not in ids        # 70 cm и неактивни — извън
    assert ids[0] == 11111                               # по-активен е първи
    iss = next(s for s in sel if s.norad_id == 25544)
    assert iss.source == "manual"


def test_70cm_selected_per_band(cfg):
    cfg.raw["band"]["downlink_ranges_mhz"] = [[144.0, 146.0], [430.0, 440.0]]
    cfg.raw["selection"]["auto_count"] = 1
    txs = [tx(25544, 145_800_000), tx(25544, 437_800_000),   # МКС: и 2 m, и 70 cm
           tx(11111, 145_900_000), tx(22222, 437_000_000)]
    sel = catalog.build_selection(cfg, txs, {11111: 9, 22222: 1, 25544: 0}, TLE)
    by = {s.norad_id: s for s in sel}
    assert by[11111].frequency_hz == 145_900_000      # най-активният на 2 m
    assert by[22222].frequency_hz == 437_000_000      # плюс един на 70 cm
    assert by[25544].frequency_hz == 145_800_000      # ръчният ISS остава на 2 m
    assert cfg.amateur_bands() == ("vhf", "uhf")


def test_change_detection(cfg):
    txs = [tx(25544, 145_800_000), tx(11111, 145_900_000)]
    catalog.update_selection(cfg, txs, {}, TLE)
    _, changes = catalog.update_selection(cfg, [tx(25544, 145_800_000), tx(11111, 145_950_000, uuid="u11111-145900000")], {}, TLE)
    assert any("11111" in c for c in changes)
    _, changes = catalog.update_selection(cfg, [tx(25544, 145_800_000)], {}, TLE)
    assert any("вече няма" in c for c in changes)


def test_mode_mapping():
    assert catalog.satnogs_mode_to_radio("FM") == "FM"
    assert catalog.satnogs_mode_to_radio("AFSK") == "FM"
    assert catalog.satnogs_mode_to_radio("CW") == "CW"
    assert catalog.satnogs_mode_to_radio("BPSK1k2") == "USB"
    assert catalog.satnogs_mode_to_radio("USB") == "USB"


def test_passlog_atomic_index_and_recovery(cfg, tmp_path):
    pl = PassLog(cfg)
    start = datetime(2026, 11, 21, 2, 12, tzinfo=timezone.utc)
    stem = pl.new_stem(start, "ISS")
    assert stem == "2026-11-21T04-12_ISS"
    wav_path = cfg.recordings_dir / f"{stem}.wav"
    wav_path.parent.mkdir(parents=True)
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
        w.writeframes(b"\0\0" * 100)
    with open(wav_path, "ab") as f:  # симулира срив: данни след последния ъпдейт на заглавката
        f.write(b"\1\0" * 50)
    pl.write(stem, {"status": STATUS_IN_PROGRESS, "satellite": {"name": "ISS", "norad_id": 25544},
                    "start": pl.local_iso(start), "audio_file": pl.rel(wav_path)})
    assert pl.new_stem(start, "ISS") == "2026-11-21T04-12_ISS_2"
    assert not list(pl.dir.glob("*.tmp"))
    fixed = pl.recover_incomplete()
    assert len(fixed) == 1
    rec = json.loads((pl.dir / f"{stem}.json").read_text(encoding="utf-8"))
    assert rec["status"] == STATUS_INCOMPLETE and rec["schema_version"] == 1
    with wave.open(str(wav_path), "rb") as w:
        assert w.getnframes() == 150
    idx = json.loads(pl.index_path.read_text(encoding="utf-8"))
    assert idx["passes"][0]["status"] == STATUS_INCOMPLETE
    pl.index_path.unlink()
    assert pl.rebuild_index() == 1


def test_repair_wav_rejects_non_wav(tmp_path):
    p = tmp_path / "x.wav"
    p.write_bytes(b"not a wav")
    assert repair_wav(p) is False
