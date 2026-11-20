"""Сглобяване на планировчика — общо за командния ред и графичния интерфейс."""

from __future__ import annotations

from sattrack.controls import Controls
from sattrack.passlog import PassLog
from sattrack.radio import make_radio
from sattrack.recorder import NullRecorder, WavRecorder
from sattrack.scheduler import Scheduler
from sattrack.sources import DataStore


def build_scheduler(cfg, dry_run: bool = False, clock=None, only_norad: int | None = None,
                    keyboard: bool = True) -> Scheduler:
    """dry-run: без аудио, а записите отиват в cache/dryrun (извън Git)."""
    r = cfg.section("recording")
    recorder = NullRecorder() if dry_run else WavRecorder(r["device"], r["samplerate"], r["channels"],
                                                          r["host_api"])
    passlog = PassLog(cfg, cfg.cache_dir / "dryrun") if dry_run else PassLog(cfg)
    controls = Controls(cfg.data_dir / "PAUSE", keyboard=keyboard)
    return Scheduler(cfg, DataStore(cfg), make_radio(cfg, dry_run), recorder, passlog,
                     controls, clock, dry_run=dry_run, only_norad=only_norad)
