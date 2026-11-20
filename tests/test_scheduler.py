"""Планировчик с фалшив часовник, dry-run радио и без мрежа."""

import json
from datetime import datetime, timedelta, timezone

from conftest import ISS_TLE
from sattrack.controls import Controls
from sattrack.passlog import PassLog
from sattrack.radio.base import RadioError
from sattrack.radio.dryrun import DryRunRadio
from sattrack.recorder import NullRecorder
from sattrack.scheduler import Scheduler
from sattrack.sources import DataStore, RefreshReport


class FakeClock:
    def __init__(self, t):
        self.t = t

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += timedelta(seconds=max(s, 0.5))


class OfflineStore(DataStore):
    def refresh(self, candidates_norad=None):
        return RefreshReport(ok={"tle": False}, errors={"tle": "offline"})


def make(cfg, t0, radio=None):
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    (cfg.cache_dir / "amateur.tle").write_text(ISS_TLE, encoding="utf-8")
    (cfg.cache_dir / "transmitters.json").write_text("[]", encoding="utf-8")
    clock = FakeClock(t0)
    store = OfflineStore(cfg, now=clock.now)
    controls = Controls(cfg.data_dir / "PAUSE", keyboard=False)
    radio = radio or DryRunRadio(echo=False)
    return Scheduler(cfg, store, radio, NullRecorder(), PassLog(cfg), controls, clock, dry_run=True), radio


T0 = datetime(2014, 1, 21, 0, 0, tzinfo=timezone.utc)


def test_full_pass_dry_run(cfg):
    cfg.raw["passes"]["min_elevation_deg"] = 10
    sch, radio = make(cfg, T0)
    sch.run(max_passes=1)
    freqs = [int(c[2:-1]) for c in radio.sent if c.startswith("FA") and len(c) > 4]
    assert "MD04;" in radio.sent
    # шумоподтискането се отваря преди пасажа и се връща (40) след него
    assert radio.sent.index("SQ0000;") < radio.sent.index("MD04;")
    assert radio.sent[-1] == "SQ0040;"
    assert len(freqs) >= 3
    assert freqs[0] > 145_800_000 > freqs[-1]          # доплерът пада монотонно
    assert all(a >= b for a, b in zip(freqs, freqs[1:]))
    assert all(abs(a - b) >= 500 for a, b in zip(freqs, freqs[1:]))
    recs = [json.loads(p.read_text(encoding="utf-8")) for p in (cfg.data_dir / "passes").glob("*.json")]
    assert len(recs) == 1 and recs[0]["status"] == "complete"
    assert recs[0]["s_meter"]["samples"] > 0
    assert (cfg.data_dir / "index.json").exists()


def test_mode_change_shift_is_overwritten(cfg):
    """FT-991A мести честотата при смяна AM->USB; след настройка тя трябва да е точна."""
    from sattrack.catalog import SatEntry
    sch, radio = make(cfg, T0)
    radio.set_mode("AM")
    sch._configure_radio(SatEntry(25544, "X", 145_800_000, "USB"), 145_803_357)
    assert radio.get_freq() == 145_803_357
    assert radio.get_mode() == "USB"


def test_pause_aborts_and_blocks(cfg):
    cfg.raw["passes"]["min_elevation_deg"] = 10
    sch, radio = make(cfg, T0)
    sch.startup()
    p = sch.schedule[0]
    sch.clock.t = p.aos
    (cfg.data_dir / "PAUSE").write_text("x")
    sch.run_pass(p)  # паузата е активна -> спира веднага
    rec = json.loads(next((cfg.data_dir / "passes").glob("*.json")).read_text(encoding="utf-8"))
    assert rec["status"] == "aborted"
    assert radio.sent[-1] == "SQ0040;"  # и при пауза нивото се връща


class DeadRadio(DryRunRadio):
    def _transact(self, cmd, expect_reply):
        raise RadioError("няма връзка")


def test_dead_radio_skips_pass(cfg):
    cfg.raw["passes"]["min_elevation_deg"] = 10
    sch, _ = make(cfg, T0, radio=DeadRadio(echo=False))
    sch.run(max_passes=1)  # не трябва да хвърля изключение
    assert not list((cfg.data_dir / "passes").glob("*.json"))
