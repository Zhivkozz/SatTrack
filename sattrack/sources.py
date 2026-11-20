"""Изтегляне и кеш на TLE, предаватели и активност (FR-10, FR-14, FR-15, FR-17).

Кеш файлове в cache/:
  amateur.tle         - TLE от CelesTrak
  transmitters.json   - предаватели от SatNOGS DB
  activity.json       - брой доклади „чут“ в AMSAT status за последните часове
  meta.json           - кога и откъде е изтеглено всяко
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from sattrack import __version__
from sattrack.fsutil import atomic_write_json, atomic_write_text, read_json

log = logging.getLogger(__name__)

USER_AGENT = f"SatTrack/{__version__} (amateur radio receive-only tracker)"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class RefreshReport:
    ok: dict[str, bool] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def all_ok(self) -> bool:
        return all(self.ok.values())


class DataStore:
    def __init__(self, cfg, session: requests.Session | None = None,
                 now: Callable[[], datetime] = utcnow):
        self.cfg = cfg
        self.d = cfg.section("data")
        self.cache = cfg.cache_dir
        self.now = now
        self.http = session or requests.Session()
        self.http.headers["User-Agent"] = USER_AGENT

    # --- пътища ---
    @property
    def tle_path(self) -> Path:
        return self.cache / "amateur.tle"

    @property
    def tx_path(self) -> Path:
        return self.cache / "transmitters.json"

    @property
    def activity_path(self) -> Path:
        return self.cache / "activity.json"

    @property
    def meta_path(self) -> Path:
        return self.cache / "meta.json"

    def meta(self) -> dict[str, Any]:
        return read_json(self.meta_path, {}) or {}

    def _mark(self, key: str, url: str) -> None:
        m = self.meta()
        m[key] = {"fetched": self.now().isoformat(timespec="seconds"), "url": url}
        atomic_write_json(self.meta_path, m)

    # --- възраст на данните (FR-15) ---
    def fetched_at(self, key: str) -> datetime | None:
        e = self.meta().get(key)
        return datetime.fromisoformat(e["fetched"]) if e else None

    def age_days(self, key: str) -> float | None:
        t = self.fetched_at(key)
        return None if t is None else (self.now() - t).total_seconds() / 86400

    def needs_refresh(self) -> bool:
        """FR-14: веднъж дневно, или ако е пропуснато, докато програмата е била спряна."""
        limit = self.d["refresh_hours"] / 24
        for key in ("tle", "transmitters"):
            a = self.age_days(key)
            if a is None or a >= limit:
                return True
        return False

    def freshness_warnings(self, narrow: bool = False) -> list[str]:
        limit = self.d["max_age_days_narrow" if narrow else "max_age_days"]
        out = []
        for key, label in (("tle", "орбитните данни (TLE)"), ("transmitters", "данните за честоти")):
            a = self.age_days(key)
            if a is None:
                out.append(f"Няма кеширани {label}. Нужна е връзка с интернет.")
            elif a > limit:
                out.append(f"{label.capitalize()} са на {a:.1f} дни (праг {limit} дни"
                           f"{' за SSB/CW' if narrow else ''}).")
        return out

    def data_version(self) -> dict[str, str | None]:
        """Записва се в JSON на всяко преминаване (FR-52)."""
        m = self.meta()
        return {k: (m.get(k) or {}).get("fetched") for k in ("tle", "transmitters", "activity")}

    # --- изтегляне ---
    def _get(self, url: str, **params) -> requests.Response:
        r = self.http.get(url, params=params or None, timeout=self.d["http_timeout_s"])
        r.raise_for_status()
        return r

    def refresh(self, candidates_norad: list[int] | None = None) -> RefreshReport:
        """Изтегля всичко. При грешка старият кеш остава (FR-10, FR-17)."""
        rep = RefreshReport()

        def attempt(key: str, fn: Callable[[], None]) -> None:
            try:
                fn()
                rep.ok[key] = True
            except Exception as e:  # мрежа, HTTP, лош формат
                rep.ok[key] = False
                rep.errors[key] = str(e)
                log.warning("Неуспешно обновяване на %s: %s. Ползва се кешът.", key, e)

        attempt("tle", self._refresh_tle)
        attempt("transmitters", self._refresh_transmitters)
        if candidates_norad is None:
            from sattrack.catalog import candidates, tle_names
            with_tle = tle_names(self.load_tle_text())
            candidates_norad = sorted(n for n in candidates(self.cfg, self.load_transmitters())
                                      if n in with_tle)
        attempt("activity", lambda: self._refresh_activity(candidates_norad))
        return rep

    def _refresh_tle(self) -> None:
        url = self.d["tle_url"]
        text = self._get(url).text
        if text.count("\n1 ") < 1:
            raise ValueError("отговорът не прилича на TLE")
        atomic_write_text(self.tle_path, text)
        self._mark("tle", url)
        log.info("TLE обновени: %d обекта", text.count("\n1 ") + text.startswith("1 "))

    def _refresh_transmitters(self) -> None:
        url = self.d["satnogs_transmitters_url"]
        data = self._get(url).json()
        if not isinstance(data, list):
            raise ValueError("неочакван формат от SatNOGS DB")
        atomic_write_json(self.tx_path, data)
        self._mark("transmitters", url)
        log.info("Предаватели от SatNOGS DB: %d", len(data))

    def _refresh_activity(self, norads: list[int]) -> None:
        """Брой доклади „чут“ в AMSAT status за последните N часа (FR-05).

        В counts влизат само сателити, които AMSAT следи; липсващ NORAD значи
        „няма данни“, а 0 — „следи се, но не е чут“.
        """
        from sattrack import amsat
        from sattrack.catalog import tle_names

        hours = self.d["activity_hours"]
        since_dt = self.now() - timedelta(hours=hours)
        timeout = (10, 30)
        names = amsat.parse_names(self.http.get(self.d["amsat_status_url"], timeout=timeout).text)
        if not names:
            raise ValueError("не са намерени имена на сателити в страницата на AMSAT")
        tle = tle_names(self.load_tle_text())
        manual = {int(k): v for k, v in self.d.get("amsat_aliases", {}).items()}

        counts: dict[str, int] = {}
        matches: dict[str, list[str]] = {}
        failures = 0
        for norad in norads:
            found = manual.get(norad) or amsat.match(names, tle.get(norad, ""))
            if not found:
                continue
            total = 0
            for nm in found:
                try:
                    r = self.http.get(self.d["amsat_api_url"], params={"name": nm, "hours": hours},
                                      timeout=timeout)
                    r.raise_for_status()
                    reports = r.json()
                    total += amsat.count_heard(reports if isinstance(reports, list) else [], since_dt)
                except Exception as e:
                    failures += 1
                    log.debug("AMSAT %s: %s", nm, e)
                    if failures >= 3 and not counts:
                        raise RuntimeError(f"AMSAT status не отговаря ({e.__class__.__name__})") from e
                time.sleep(0.3)  # учтиво към сървъра
            counts[str(norad)] = total
            matches[str(norad)] = found
        atomic_write_json(self.activity_path, {
            "source": "AMSAT status", "since": since_dt.isoformat(timespec="seconds"),
            "counts": counts, "matches": matches})
        self._mark("activity", self.d["amsat_api_url"])
        log.info("Активност от AMSAT: %d от %d кандидата се следят, %d чути",
                 len(counts), len(norads), sum(1 for v in counts.values() if v > 0))

    # --- четене от кеша ---
    def load_transmitters(self) -> list[dict[str, Any]]:
        return read_json(self.tx_path, []) or []

    def load_activity(self) -> dict[int, int]:
        d = read_json(self.activity_path, {}) or {}
        return {int(k): v for k, v in d.get("counts", {}).items()}

    def load_activity_matches(self) -> dict[int, list[str]]:
        d = read_json(self.activity_path, {}) or {}
        return {int(k): v for k, v in d.get("matches", {}).items()}

    def load_tle_text(self) -> str:
        try:
            return self.tle_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""
