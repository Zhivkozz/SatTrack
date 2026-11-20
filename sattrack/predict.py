"""Прогноза за преминавания със Skyfield (FR-11..13, FR-31)."""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from skyfield.api import EarthSatellite, load, wgs84
from skyfield.iokit import parse_tle_file

from sattrack.catalog import SatEntry

_ts = load.timescale(builtin=True)

# Колко назад се търси, за да се хване текущо преминаване (LEO преминаване < 25 мин).
LOOKBACK = timedelta(minutes=30)


def timescale():
    return _ts


@dataclass
class Pass:
    sat: SatEntry
    aos: datetime          # UTC
    tca: datetime
    los: datetime
    max_elevation_deg: float
    aos_azimuth_deg: float = 0.0
    los_azimuth_deg: float = 0.0

    @property
    def duration_s(self) -> float:
        return (self.los - self.aos).total_seconds()

    def in_progress(self, now: datetime) -> bool:
        return self.aos <= now < self.los

    def overlaps(self, other: "Pass", margin_s: float = 0) -> bool:
        m = timedelta(seconds=margin_s)
        return self.aos - m < other.los and other.aos - m < self.los


class Predictor:
    def __init__(self, station: dict, tle_text: str, aos_elevation_deg: float = 0.0):
        self.observer = wgs84.latlon(station["latitude_deg"], station["longitude_deg"],
                                     elevation_m=station["elevation_m"])
        self.aos_el = aos_elevation_deg
        self.sats: dict[int, EarthSatellite] = {}
        f = io.BytesIO(tle_text.encode("ascii", errors="ignore"))
        for s in parse_tle_file(f, _ts):
            self.sats[s.model.satnum] = s

    def has(self, norad: int) -> bool:
        return norad in self.sats

    def tle_epoch(self, norad: int) -> datetime:
        return self.sats[norad].epoch.utc_datetime()

    def passes(self, entries: list[SatEntry], start: datetime, hours: float,
               min_max_el: float) -> list[Pass]:
        """Преминавания, които не са приключили до `start`, включително текущото.

        Търсенето започва LOOKBACK по-рано, за да се намерят истинските начало,
        връх и максимална височина на преминаване, което вече тече.
        """
        t0 = _ts.from_datetime(start - LOOKBACK)
        t1 = _ts.from_datetime(start + timedelta(hours=hours))
        out: list[Pass] = []
        for e in entries:
            sat = self.sats.get(e.norad_id)
            if sat is None or not e.enabled:
                continue
            times, events = sat.find_events(self.observer, t0, t1, altitude_degrees=self.aos_el)
            cur: dict = {}
            for t, ev in zip(times, events):
                if ev == 0:
                    cur = {"aos": t}
                elif ev == 1 and "aos" in cur:
                    # при няколко кулминации се пази най-високата
                    el = (sat - self.observer).at(t).altaz()[0].degrees
                    if el > cur.get("el", -90):
                        cur.update(tca=t, el=el)
                elif ev == 2 and "tca" in cur:
                    if cur["el"] >= min_max_el and t.utc_datetime() > start:
                        out.append(Pass(
                            sat=e, aos=cur["aos"].utc_datetime(), tca=cur["tca"].utc_datetime(),
                            los=t.utc_datetime(), max_elevation_deg=round(float(cur["el"]), 1),
                            aos_azimuth_deg=self.azimuth(e.norad_id, cur["aos"].utc_datetime()),
                            los_azimuth_deg=self.azimuth(e.norad_id, t.utc_datetime())))
                    cur = {}
        out.sort(key=lambda p: p.aos)
        return out

    def look(self, norad: int, when: datetime) -> tuple[float, float, float]:
        """(височина°, азимут°, скорост на промяна на разстоянието м/с)."""
        sat = self.sats[norad]
        t = _ts.from_datetime(when)
        topo = (sat - self.observer).at(t)
        alt, az, _ = topo.altaz()
        rr = topo.frame_latlon_and_rates(self.observer)[5].km_per_s * 1000.0
        return float(alt.degrees), float(az.degrees), float(rr)

    def azimuth(self, norad: int, when: datetime) -> float:
        return round(self.look(norad, when)[1], 0)


def choose_non_overlapping(passes: list[Pass], rule: str = "max_elevation",
                           margin_s: float = 0) -> list[Pass]:
    """FR-31: при припокриване остава преминаването с по-висок приоритет."""
    keys = {
        "max_elevation": lambda p: (-p.max_elevation_deg, p.aos),
        "first": lambda p: (p.aos,),
        "longest": lambda p: (-p.duration_s, p.aos),
    }
    chosen: list[Pass] = []
    for p in sorted(passes, key=keys[rule]):
        if not any(p.overlaps(c, margin_s) for c in chosen):
            chosen.append(p)
    return sorted(chosen, key=lambda p: p.aos)


def format_passes(passes: list[Pass], tz: str, chosen: list[Pass] | None = None) -> str:
    z = ZoneInfo(tz)
    now = datetime.now(timezone.utc)
    chosen_ids = {id(p) for p in chosen} if chosen is not None else None
    rows = [f"{'':1} {'Дата':10} {'AOS':8} {'TCA':8} {'LOS':8} {'Макс':>5} {'Мин':>4}  "
            f"{'Сателит':18} {'Честота':>11} Режим"]
    for p in passes:
        mark = " " if chosen_ids is None else ("*" if id(p) in chosen_ids else "-")
        if p.in_progress(now):
            mark = "▶" if mark != "-" else mark
        a = p.aos.astimezone(z)
        rows.append(
            f"{mark} {a:%Y-%m-%d} {a:%H:%M:%S} {p.tca.astimezone(z):%H:%M:%S} "
            f"{p.los.astimezone(z):%H:%M:%S} {p.max_elevation_deg:5.1f} "
            f"{p.duration_s / 60:4.1f}  {p.sat.name[:18]:18} "
            f"{p.sat.frequency_hz / 1e6:11.4f} {p.sat.mode}")
    if chosen_ids is not None:
        rows.append("* = ще се записва, ▶ = тече в момента, - = пропуска се заради припокриване")
    return "\n".join(rows)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
