"""Главен цикъл: изчакване, запис на преминавания, сверка на данните (FR-30..35, FR-17)."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean

from sattrack import catalog
from sattrack.config import is_narrowband
from sattrack.fmt import fmt_mhz
from sattrack.controls import Controls
from sattrack.doppler import DopplerGate, rx_frequency
from sattrack.passlog import (STATUS_ABORTED, STATUS_COMPLETE, STATUS_IN_PROGRESS, PassLog)
from sattrack.predict import Pass, Predictor, choose_non_overlapping, format_passes
from sattrack.radio import Radio, RadioError
from sattrack.recorder import RecorderError
from sattrack.sources import DataStore

log = logging.getLogger(__name__)

MIN_REMAINING_S = 60         # текущо преминаване се записва, ако остава поне толкова
SMETER_INTERVAL_S = 2.0
STATUS_INTERVAL_S = 15.0
SCHEDULE_REBUILD_H = 6


class RealClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, s: float) -> None:
        if s > 0:
            time.sleep(s)


class SimClock:
    """Ускорено време за dry-run проверки: speed пъти по-бързо от реалното."""

    def __init__(self, start: datetime, speed: float = 1.0):
        self.start = start
        self.speed = speed
        self._t0 = time.monotonic()

    def now(self) -> datetime:
        return self.start + timedelta(seconds=(time.monotonic() - self._t0) * self.speed)

    def sleep(self, s: float) -> None:
        if s > 0:
            time.sleep(s / self.speed)


class Scheduler:
    def __init__(self, cfg, store: DataStore, radio: Radio, recorder, passlog: PassLog,
                 controls: Controls, clock=None, dry_run: bool = False,
                 only_norad: int | None = None):
        self.cfg = cfg
        self.store = store
        self.radio = radio
        self.recorder = recorder
        self.passlog = passlog
        self.controls = controls
        self.clock = clock or RealClock()
        self.dry_run = dry_run
        self.only_norad = only_norad    # FR-32: ръчно следене на един сателит
        self.p = cfg.section("passes")
        self.selection: list[catalog.SatEntry] = []
        self.predictor: Predictor | None = None
        self.schedule: list[Pass] = []
        self.schedule_built: datetime | None = None
        self.next_refresh_try: datetime = datetime.min.replace(tzinfo=timezone.utc)
        self.passes_done = 0
        # Състояние за графичния интерфейс (подменя се цялото, затова е безопасно
        # за четене от друга нишка): state = starting|waiting|paused|tracking|stopped
        self.status: dict = {"state": "stopped"}
        self._refresh_req = False
        self._reload_req = False
        self.last_refresh: tuple[float, object] | None = None

    # --- заявки от интерфейса (изпълняват се между преминаванията, FR-17) ---
    def request_refresh(self) -> None:
        self._refresh_req = True

    def request_reload(self) -> None:
        """Списъкът или настройките са променени — нов график."""
        self._reload_req = True

    def _handle_requests(self) -> None:
        if self._refresh_req:
            self._refresh_req = False
            self.refresh_data(force=True)
        if self._reload_req:
            self._reload_req = False
            self.load_selection(update=False)
            self.build_schedule()
            log.info("Графикът е обновен: %d преминавания напред", len(self.schedule))

    # --- данни и прогноза ---
    def refresh_data(self, force: bool = False) -> None:
        """FR-14/16/17: сверка; при неуспех — кеш и нов опит по-късно."""
        now = self.clock.now()
        if not force and (now < self.next_refresh_try or not self.store.needs_refresh()):
            return
        log.info("Сверка на орбитните данни и честотите...")
        rep = self.store.refresh()
        self.last_refresh = (time.time(), rep)  # за интерфейса
        if not rep.all_ok:
            self.next_refresh_try = now + timedelta(minutes=self.cfg.section("data")["retry_minutes"])
            log.warning("Сверката е непълна; нов опит след %s мин.",
                        self.cfg.section("data")["retry_minutes"])
        else:
            self.next_refresh_try = now
        self.load_selection(update=True)
        for w in self.store.freshness_warnings(narrow=any(is_narrowband(s.mode) for s in self.selection)):
            log.warning("Остарели данни: %s", w)
        self.build_schedule()

    def load_selection(self, update: bool) -> None:
        tle = self.store.load_tle_text()
        if update or not catalog.selection_path(self.cfg).exists():
            self.selection, _ = catalog.update_selection(
                self.cfg, self.store.load_transmitters(), self.store.load_activity(), tle)
        else:
            self.selection = catalog.load_selection(self.cfg)
        if self.only_norad is not None:
            sel = [s for s in self.selection if s.norad_id == self.only_norad]
            if not sel:
                over = [o for o in self.cfg.section("satellites")["overrides"]
                        if o["norad_id"] == self.only_norad]
                sel = catalog.apply_overrides([], over, catalog.tle_names(tle))
            if not sel:
                raise SystemExit(f"Сателит {self.only_norad} не е в списъка и няма ръчна настройка "
                                 "с frequency_hz в config.json.")
            self.selection = [catalog.SatEntry(**{**s.__dict__, "enabled": True}) for s in sel]
        self.predictor = Predictor(self.cfg.station, tle)
        missing = [s.name for s in self.selection if not self.predictor.has(s.norad_id)]
        if missing:
            log.warning("Няма TLE за: %s", ", ".join(missing))

    def build_schedule(self) -> list[Pass]:
        assert self.predictor is not None
        now = self.clock.now()
        allp = self.predictor.passes(self.selection, now, self.p["horizon_hours"],
                                     self.p["min_elevation_deg"])
        self.schedule = choose_non_overlapping(allp, self.p["overlap_rule"], self.p["prestart_s"])
        self.schedule_built = now
        self._all_passes = allp
        return self.schedule

    def print_forecast(self) -> None:
        print()
        print(format_passes(self._all_passes, self.cfg.station["timezone"], self.schedule))
        print()

    # --- главен цикъл ---
    def startup(self) -> None:
        for p in self.passlog.recover_incomplete():
            log.info("Маркиран като непълен: %s", p.name)
        self.check_radio()
        if self.store.needs_refresh():
            self.refresh_data(force=True)   # FR-14: сверка при стартиране, ако е пропусната
        else:
            self.load_selection(update=False)
            self.build_schedule()
            for w in self.store.freshness_warnings(narrow=any(is_narrowband(s.mode) for s in self.selection)):
                log.warning("Остарели данни: %s", w)
        self.print_forecast()

    def check_radio(self) -> bool:
        try:
            rid = self.radio.identify()
            freq = self.radio.get_freq()
            log.info("Радиото отговаря: ID %s, VFO-A %s MHz", rid, fmt_mhz(freq))
            return True
        except (RadioError, ValueError) as e:
            log.warning("Радиото не отговаря: %s. Програмата продължава; ще опита отново.", e)
            return False

    def run(self, max_passes: int | None = None) -> None:
        self.status = {"state": "starting"}
        try:
            self.startup()
            self._loop(max_passes)
        finally:
            self.status = {"state": "stopped"}

    def _loop(self, max_passes: int | None) -> None:
        last_status = 0.0
        while not self.controls.quit:
            self.controls.poll()
            if self.controls.show_forecast:
                self.controls.show_forecast = False
                self.print_forecast()
            now = self.clock.now()

            self._handle_requests()
            self.refresh_data()
            if self.schedule_built is None or now - self.schedule_built > timedelta(hours=SCHEDULE_REBUILD_H):
                self.build_schedule()

            # Преминаване, което вече тече, се записва веднага (нов запис от този
            # момент), стига да остава поне MIN_REMAINING_S. Отпадат само приключилите.
            while self.schedule and now > self.schedule[0].los - timedelta(seconds=MIN_REMAINING_S):
                p = self.schedule.pop(0)
                if now < p.los:
                    log.info("Пропуснато (остават под %d s): %s", MIN_REMAINING_S, p.sat.name)
            if not self.schedule:
                self.build_schedule()
                if not self.schedule:
                    self.status = {"state": "paused" if self.controls.paused else "waiting", "next": None}
                    for _ in range(60):  # реагира на команди от интерфейса
                        if self.controls.quit or self._refresh_req or self._reload_req:
                            break
                        self.clock.sleep(1)
                        self.controls.poll()
                    continue

            nxt = self.schedule[0]
            start_at = nxt.aos - timedelta(seconds=self.p["prestart_s"])
            if self.controls.paused:
                self.status = {"state": "paused", "next": nxt}
                self.clock.sleep(1)
                continue
            self.status = {"state": "waiting", "next": nxt, "start_at": start_at}
            if now >= start_at:
                self.schedule.pop(0)
                self.run_pass(nxt)
                self.passes_done += 1
                if max_passes is not None and self.passes_done >= max_passes:
                    break
                continue

            if time.monotonic() - last_status > 300:
                last_status = time.monotonic()
                wait = start_at - now
                log.info("Следващо: %s в %s (след %s), макс. %.0f°", nxt.sat.name,
                         nxt.aos.astimezone(self.passlog.tz).strftime("%H:%M:%S"),
                         str(wait).split(".")[0], nxt.max_elevation_deg)
            self.clock.sleep(min(1.0, max(0.05, (start_at - now).total_seconds())))

    # --- едно преминаване ---
    def _configure_radio(self, s: catalog.SatEntry, f0: float) -> None:
        sq = self.cfg.section("radio").get("squelch")
        self._saved_squelch = None
        if sq is not None:
            self._saved_squelch = self.radio.get_squelch()
            self.radio.set_squelch(sq)
        self.radio.set_mode(s.mode)
        # FR-04 / FR-22: тесен филтър при поискване (за FM се ползва режимът FM-N)
        if s.filter == "narrow":
            self.radio.set_narrow(True)
        elif is_narrowband(s.mode):
            self.radio.set_narrow(False)
        if s.cw_pitch_hz and s.mode.startswith("CW"):
            self.radio.set_cw_pitch(s.cw_pitch_hz)
        # Честотата се задава СЛЕД режима: FT-991A мести VFO с ±700 Hz при смяна
        # между AM/USB/LSB (проверено на живо 2026-11-20). После се проверява.
        target = int(round(f0))
        self.radio.set_freq(target)
        got = self.radio.get_freq()
        if got != target:
            log.warning("Радиото отчита %d Hz вместо %d Hz — задава се отново", got, target)
            self.radio.set_freq(target)

    def run_pass(self, p: Pass) -> None:
        s = p.sat
        assert self.predictor is not None
        dp = self.cfg.doppler_params(s.mode)
        gate = DopplerGate(dp["threshold_hz"])
        log.info("=== %s: %s MHz %s, макс. %.1f°, до %s ===", s.name, fmt_mhz(s.frequency_hz),
                 s.mode, p.max_elevation_deg, p.los.astimezone(self.passlog.tz).strftime("%H:%M:%S"))

        now = self.clock.now()
        self.status = {"state": "tracking", "pass": p, "now": now, "live": None}
        _, _, rr = self.predictor.look(s.norad_id, now)
        f_now = rx_frequency(s.frequency_hz, rr)
        try:
            self._configure_radio(s, f_now)
            gate.should_send(f_now)
        except (RadioError, ValueError) as e:
            log.warning("Радиото не отговаря при старт на %s: %s. Пропуска се; чака следващото.", s.name, e)
            self._restore_squelch()
            return

        stem = self.passlog.new_stem(p.aos, s.name)
        wav = None if self.dry_run else self.cfg.recordings_dir / f"{stem}.wav"
        record = {
            "status": STATUS_IN_PROGRESS,
            "satellite": {"name": s.name, "norad_id": s.norad_id},
            "start": self.passlog.local_iso(now),
            "end": None,
            "predicted": {"aos": self.passlog.local_iso(p.aos), "tca": self.passlog.local_iso(p.tca),
                          "los": self.passlog.local_iso(p.los)},
            "max_elevation_deg": p.max_elevation_deg,
            "frequency_hz": s.frequency_hz,
            "mode": s.mode,
            "filter": s.filter,
            "s_meter": None,
            "audio_file": self.passlog.rel(wav),
            "dry_run": self.dry_run,
            "tle_epoch": self.passlog.local_iso(self.predictor.tle_epoch(s.norad_id)),
            "data_version": self.store.data_version(),
            "events": [],
        }
        late_s = (now - p.aos).total_seconds()
        if late_s > 0:
            el_now = self.predictor.look(s.norad_id, now)[0]
            msg = (f"започнат със закъснение: {late_s / 60:.1f} мин след AOS "
                   f"(сателитът вече е на {el_now:.0f}°)")
            record["events"].append(msg)
            record["late_start_s"] = round(late_s)
            log.info("%s е над хоризонта — записът започва веднага, %s", s.name, msg)
        self.passlog.write(stem, record)

        try:
            self.recorder.start(wav)
            record["audio_input"] = getattr(self.recorder, "used_path", None)
        except (RecorderError, Exception) as e:
            log.error("Аудио записът не стартира: %s", e)
            record["events"].append(f"аудио грешка: {e}")
            record["audio_file"] = None

        smeter: list[int] = []
        radio_errors = 0
        next_dop = next_sm = next_stat = 0.0
        status = STATUS_COMPLETE
        f_min = f_max = f_now
        try:
            while True:
                self.controls.poll()
                if self.controls.quit or self.controls.paused:
                    status = STATUS_ABORTED
                    record["events"].append("спрян с пауза" if self.controls.paused else "спрян с изход")
                    break
                now = self.clock.now()
                if now >= p.los:
                    break
                el, az, rr = self.predictor.look(s.norad_id, now)
                f = rx_frequency(s.frequency_hz, rr)
                f_min, f_max = min(f_min, f), max(f_max, f)
                self.status = {"state": "tracking", "pass": p, "now": now, "live": {
                    "el": el, "az": az, "freq": f, "offset": f - s.frequency_hz,
                    "sent": gate.last_sent, "smeter": smeter[-1] if smeter else None,
                    "radio_errors": radio_errors, "audio": record["audio_file"]}}
                mono = now.timestamp()
                try:
                    if mono >= next_dop:
                        next_dop = mono + dp["interval_s"]
                        fs = gate.should_send(f)
                        if fs is not None:
                            self.radio.set_freq(fs)
                    if mono >= next_sm:
                        next_sm = mono + SMETER_INTERVAL_S
                        smeter.append(self.radio.read_smeter())
                except (RadioError, ValueError) as e:
                    radio_errors += 1
                    if radio_errors in (1, 10) or radio_errors % 100 == 0:
                        log.warning("Грешка с радиото (%d): %s", radio_errors, e)
                if mono >= next_stat:
                    next_stat = mono + STATUS_INTERVAL_S
                    log.info("%s el %5.1f° az %5.1f° f %s MHz (%+.0f Hz) S %s", s.name, el, az,
                             fmt_mhz(f), f - s.frequency_hz, smeter[-1] if smeter else "-")
                self.clock.sleep(min(dp["interval_s"], SMETER_INTERVAL_S, 1.0))
        finally:
            try:
                self.recorder.stop()
            except Exception as e:
                log.error("Грешка при спиране на записа: %s", e)
            self._restore_squelch(record)

        record.update({
            "status": status,
            "end": self.passlog.local_iso(self.clock.now()),
            "s_meter": ({"mean": round(mean(smeter), 1), "max": max(smeter), "samples": len(smeter)}
                        if smeter else None),
            "doppler": {"min_hz": int(f_min), "max_hz": int(f_max), "updates": gate.updates,
                        "interval_s": dp["interval_s"], "threshold_hz": dp["threshold_hz"]},
            "radio_errors": radio_errors,
        })
        path = self.passlog.write(stem, record)
        log.info("=== Край на %s (%s): %s ===", s.name, status, path.name)
        self.cleanup_recordings()

    def _restore_squelch(self, record: dict | None = None) -> None:
        """Връща нивото на шумоподтискането отпреди пасажа."""
        if getattr(self, "_saved_squelch", None) is None:
            return
        try:
            self.radio.set_squelch(self._saved_squelch)
            log.info("Шумоподтискането е върнато на %d", self._saved_squelch)
        except (RadioError, ValueError) as e:
            log.warning("Не може да се върне шумоподтискането (%d): %s", self._saved_squelch, e)
            if record is not None:
                record["events"].append(f"шумоподтискането не е върнато на {self._saved_squelch}")
        self._saved_squelch = None

    def cleanup_recordings(self) -> None:
        days = self.cfg.section("recording").get("keep_days")
        if not days:
            return
        limit = time.time() - days * 86400
        for f in Path(self.cfg.recordings_dir).glob("*.wav"):
            try:
                if f.stat().st_mtime < limit:
                    f.unlink()
                    log.info("Изтрит стар запис: %s", f.name)
            except OSError as e:
                log.warning("Не може да се изтрие %s: %s", f, e)
