"""Конфигурация: зареждане от JSON, стойности по подразбиране, валидиране (FR-01..04)."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Режими, които програмата познава. Кодовете за CAT са в sattrack.radio.cat.
MODES = ("FM", "FM-N", "AM", "AM-N", "USB", "LSB", "CW", "CW-L",
         "DATA-FM", "DATA-USB", "DATA-LSB")

DEFAULTS: dict[str, Any] = {
    "station": {
        "name": "Troyan",
        "latitude_deg": 42.89,
        "longitude_deg": 24.71,
        "elevation_m": 400,
        "timezone": "Europe/Sofia",
    },
    "radio": {
        "port": "COM3",
        "baudrate": 38400,
        "timeout_s": 1.0,
        "stopbits": 1,                  # при грешки в отговорите опитайте 2
        # Шумоподтискане по време на пасаж (0 = напълно отворено, FR-41).
        # След пасажа се връща предишното ниво. null = програмата не го пипа.
        "squelch": 0,
        "retry_interval_s": 30,
    },
    "band": {
        # FR-03: 70 cm се добавя тук, напр. [430.0, 440.0], без промяна на кода.
        "downlink_ranges_mhz": [[144.0, 146.0]],
    },
    "passes": {
        "min_elevation_deg": 30.0,      # FR-12
        "horizon_hours": 48,            # FR-11
        "overlap_rule": "max_elevation",  # FR-31: max_elevation | first | longest
        "prestart_s": 20,               # колко по-рано да се настрои радиото
    },
    "doppler": {
        # FR-23: интервал на обновяване и праг за изпращане на нова честота.
        "FM": {"interval_s": 5.0, "threshold_hz": 500},
        "AM": {"interval_s": 5.0, "threshold_hz": 500},
        "SSB": {"interval_s": 1.0, "threshold_hz": 50},
        "CW": {"interval_s": 1.0, "threshold_hz": 50},
    },
    "data": {
        "data_dir": "data",
        "cache_dir": "cache",
        "refresh_hours": 24,            # FR-14
        "retry_minutes": 60,            # FR-17
        "max_age_days": 3.0,            # FR-15
        "max_age_days_narrow": 1.5,     # FR-15, за SSB и CW
        "http_timeout_s": 30,
        "tle_url": "https://celestrak.org/NORAD/elements/gp.php?GROUP=amateur&FORMAT=tle",
        "satnogs_transmitters_url": "https://db.satnogs.org/api/transmitters/?format=json",
        # FR-05: активност от AMSAT status (активен = чут през последните 24 часа)
        "amsat_status_url": "https://status.amsat.org/",
        "amsat_api_url": "https://status.amsat.org/api/v1/sat_info.php",
        "activity_hours": 24,
        # Ръчно свързване NORAD -> имена в AMSAT, ако автоматичното не стига:
        # {"25544": ["ISS_[FM]"]}
        "amsat_aliases": {},
    },
    "selection": {
        "auto_count": 7,                # FR-05: първите 6-7 сателита
    },
    "satellites": {
        # FR-02 / FR-05: ръчните настройки имат предимство пред автоматичния списък.
        # Пример: {"norad_id": 25544, "name": "ISS", "frequency_hz": 145800000,
        #          "mode": "FM", "filter": "normal", "cw_pitch_hz": null, "enabled": true}
        "overrides": [],
    },
    "recording": {
        "dir": "recordings",
        "device": "USB Audio CODEC",    # част от името на аудио входа на радиото
        # auto = изключителен WASAPI, после WDM-KS, после MME. Първите два заобикалят
        # звуковите ефекти на Windows, които изтриват шума и слабите сигнали.
        "host_api": "auto",
        "samplerate": 48000,
        "channels": 1,
        "keep_days": None,              # null = не се трият стари записи
    },
    "log": {
        "dir": "logs",
        "level": "INFO",
    },
}


class ConfigError(ValueError):
    """Грешка в конфигурацията с ясно съобщение за потребителя."""


@dataclass
class Config:
    raw: dict[str, Any]
    base_dir: Path
    errors: list[str] = field(default_factory=list)
    source: Path | None = None      # файлът, от който е заредена (за запис от интерфейса)

    def save_value(self, keys: list[str], value: Any) -> None:
        """Записва една настройка в config.json (само нея, без стойностите по подразбиране)
        и я прилага в паметта."""
        from sattrack.fsutil import atomic_write_json, read_json
        def put(target: dict) -> None:
            d = target
            for k in keys[:-1]:
                d = d.setdefault(k, {})
            d[keys[-1]] = copy.deepcopy(value)

        trial = copy.deepcopy(self.raw)
        put(trial)
        errors = validate(trial)
        if errors:
            raise ConfigError("; ".join(errors))
        path = self.source or (self.base_dir / "config.json")
        data = read_json(path, {}) or {}
        put(data)
        atomic_write_json(path, data)
        put(self.raw)
        self.source = path

    # Удобни достъпи -------------------------------------------------------
    def section(self, name: str) -> dict[str, Any]:
        return self.raw[name]

    def path(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else self.base_dir / p

    @property
    def station(self) -> dict[str, Any]:
        return self.raw["station"]

    @property
    def data_dir(self) -> Path:
        return self.path(self.raw["data"]["data_dir"])

    @property
    def cache_dir(self) -> Path:
        return self.path(self.raw["data"]["cache_dir"])

    @property
    def recordings_dir(self) -> Path:
        return self.path(self.raw["recording"]["dir"])

    @property
    def log_dir(self) -> Path:
        return self.path(self.raw["log"]["dir"])

    def in_band(self, freq_hz: float) -> bool:
        mhz = freq_hz / 1e6
        return any(lo <= mhz <= hi for lo, hi in self.raw["band"]["downlink_ranges_mhz"])

    def doppler_params(self, mode: str) -> dict[str, float]:
        return self.raw["doppler"][mode_family(mode)]


def mode_family(mode: str) -> str:
    """Група на режима за настройките на доплера."""
    m = mode.upper()
    if m.startswith("CW"):
        return "CW"
    if m in ("USB", "LSB", "DATA-USB", "DATA-LSB"):
        return "SSB"
    if m.startswith("AM"):
        return "AM"
    return "FM"


def is_narrowband(mode: str) -> bool:
    return mode_family(mode) in ("SSB", "CW")


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(path: str | Path | None = None) -> Config:
    """Зарежда конфигурацията. Без файл се ползват стойностите по подразбиране."""
    if path is None:
        candidate = Path("config.json")
        path = candidate if candidate.exists() else None
    if path is None:
        cfg = Config(copy.deepcopy(DEFAULTS), Path.cwd())
    else:
        p = Path(path)
        try:
            user = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ConfigError(f"Конфигурационният файл {p} не съществува.") from None
        except json.JSONDecodeError as e:
            raise ConfigError(f"Грешен JSON в {p}, ред {e.lineno}, колона {e.colno}: {e.msg}") from None
        if not isinstance(user, dict):
            raise ConfigError(f"{p}: очаква се JSON обект на най-горно ниво.")
        cfg = Config(deep_merge(DEFAULTS, user), p.resolve().parent, source=p.resolve())
    errors = validate(cfg.raw)
    if errors:
        raise ConfigError("Грешки в конфигурацията:\n  - " + "\n  - ".join(errors))
    return cfg


def validate(raw: dict[str, Any]) -> list[str]:
    errs: list[str] = []

    def num(sec: str, key: str, lo: float | None = None, hi: float | None = None, allow_none=False):
        v = raw.get(sec, {}).get(key)
        if v is None and allow_none:
            return
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            errs.append(f"{sec}.{key} трябва да е число (сега: {v!r})")
            return
        if lo is not None and v < lo or hi is not None and v > hi:
            errs.append(f"{sec}.{key}={v} е извън допустимото [{lo}, {hi}]")

    num("station", "latitude_deg", -90, 90)
    num("station", "longitude_deg", -180, 180)
    num("station", "elevation_m", -500, 9000)
    num("radio", "baudrate", 4800, 38400)
    num("radio", "timeout_s", 0.05, 10)
    num("passes", "min_elevation_deg", 0, 90)
    num("passes", "horizon_hours", 1, 168)
    num("data", "max_age_days", 0.1, 60)
    num("data", "max_age_days_narrow", 0.1, 60)
    num("selection", "auto_count", 1, 50)
    num("data", "activity_hours", 1, 24 * 14)
    al = raw["data"].get("amsat_aliases")
    if not isinstance(al, dict) or not all(
            str(k).isdigit() and isinstance(v, list) and all(isinstance(x, str) for x in v)
            for k, v in al.items()):
        errs.append('data.amsat_aliases трябва да е обект {"NORAD": ["ИМЕ_[РЕЖИМ]", ...]}')

    if raw["radio"].get("baudrate") not in (4800, 9600, 19200, 38400):
        errs.append("radio.baudrate трябва да е 4800, 9600, 19200 или 38400 (менюто CAT RATE на радиото)")
    num("radio", "squelch", 0, 100, allow_none=True)
    if raw["recording"].get("host_api") not in ("auto", "wasapi-exclusive", "wdm-ks", "mme"):
        errs.append("recording.host_api трябва да е auto, wasapi-exclusive, wdm-ks или mme")
    if raw["radio"].get("stopbits") not in (1, 2):
        errs.append("radio.stopbits трябва да е 1 или 2")
    if raw["passes"].get("overlap_rule") not in ("max_elevation", "first", "longest"):
        errs.append("passes.overlap_rule трябва да е max_elevation, first или longest")

    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(raw["station"]["timezone"])
    except Exception:
        errs.append(f"station.timezone {raw['station'].get('timezone')!r} не е позната часова зона "
                    "(на Windows е нужен пакетът tzdata)")

    ranges = raw["band"].get("downlink_ranges_mhz")
    if not isinstance(ranges, list) or not ranges or not all(
            isinstance(r, list) and len(r) == 2 and r[0] < r[1] for r in ranges):
        errs.append("band.downlink_ranges_mhz трябва да е списък от двойки [от, до] в MHz")

    for fam in ("FM", "AM", "SSB", "CW"):
        d = raw["doppler"].get(fam)
        if not isinstance(d, dict) or "interval_s" not in d or "threshold_hz" not in d:
            errs.append(f"doppler.{fam} трябва да има interval_s и threshold_hz")

    for i, s in enumerate(raw["satellites"].get("overrides", [])):
        where = f"satellites.overrides[{i}]"
        if not isinstance(s, dict) or not isinstance(s.get("norad_id"), int):
            errs.append(f"{where}: липсва цяло число norad_id")
            continue
        if "mode" in s and s["mode"] not in MODES:
            errs.append(f"{where}: непознат режим {s['mode']!r}; позволени: {', '.join(MODES)}")
        if "frequency_hz" in s and not isinstance(s["frequency_hz"], int):
            errs.append(f"{where}: frequency_hz трябва да е цяло число в Hz")
        if s.get("filter") not in (None, "normal", "narrow"):
            errs.append(f"{where}: filter трябва да е normal или narrow")
    return errs
