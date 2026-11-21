"""Списък на сателитите за слушане (FR-02, FR-05, FR-16).

Автоматичният избор се прави от SatNOGS DB (активни предаватели в разрешения
обхват), подреден по брой доклади „чут“ в AMSAT status за последните 24 часа.
Ръчните настройки от config.json (satellites.overrides) имат предимство.
Резултатът се пази в data/satellites.json (четим, извън Git).
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sattrack.fsutil import atomic_write_json, read_json

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


@dataclass
class SatEntry:
    norad_id: int
    name: str
    frequency_hz: int
    mode: str
    filter: str = "normal"
    cw_pitch_hz: int | None = None
    enabled: bool = True
    source: str = "auto"            # auto | manual
    transmitter: str | None = None  # описание на предавателя в SatNOGS
    transmitter_uuid: str | None = None
    activity: int | None = None     # доклади „чут“ в AMSAT; None = AMSAT не го следи


def satnogs_mode_to_radio(mode: str | None) -> str:
    """Преобразува режима от SatNOGS в режим на радиото (за запис на аудио)."""
    m = (mode or "").upper()
    if "CW" in m and "FM" not in m:
        return "CW"
    if re.search(r"\b(USB|LSB|SSB|BPSK|PSK|QPSK)\b", m) or m.startswith(("BPSK", "PSK")):
        return "LSB" if "LSB" in m else "USB"
    if m.startswith("AM"):
        return "AM"
    return "FM"  # FM, AFSK, GFSK, GMSK, FSK, APRS, SSTV и др.


def tle_names(tle_text: str) -> dict[int, str]:
    """NORAD -> име от 3-редов TLE."""
    names: dict[int, str] = {}
    lines = [l.rstrip() for l in tle_text.splitlines() if l.strip()]
    for i in range(len(lines) - 1):
        if lines[i + 1].startswith("1 ") and not lines[i].startswith(("1 ", "2 ")):
            try:
                names[int(lines[i + 1][2:7])] = lines[i].strip()
            except ValueError:
                pass
    return names


def candidates(cfg, transmitters: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    """Активни предаватели с downlink в разрешения обхват, групирани по NORAD."""
    out: dict[int, list[dict[str, Any]]] = {}
    for t in transmitters:
        norad = t.get("norad_cat_id")
        f = t.get("downlink_low")
        if not norad or not f:
            continue
        if t.get("status") != "active" or t.get("alive") is False:
            continue
        if not cfg.in_band(f):
            continue
        out.setdefault(int(norad), []).append(t)
    return out


def _pick_transmitter(txs: list[dict[str, Any]], cfg=None) -> dict[str, Any]:
    """Предпочитаме по-ниския обхват (2 m, антената е за него), после FM
    (лесно се чува), после CW, после останалото."""
    def score(t):
        rm = satnogs_mode_to_radio(t.get("mode"))
        band = cfg.band_index(t.get("downlink_low") or 0) if cfg else 0
        return (band if band is not None else 99, {"FM": 0, "CW": 1}.get(rm, 2), t.get("downlink_low") or 0)
    return sorted(txs, key=score)[0]


def auto_selection(cfg, transmitters, activity: dict[int, int], names: dict[int, str]) -> list[SatEntry]:
    cands = candidates(cfg, transmitters)
    entries = []
    for norad, txs in cands.items():
        if names and norad not in names:
            continue  # без орбитни данни не може да се прогнозира
        t = _pick_transmitter(txs, cfg)
        f = int(t["downlink_low"])
        if t.get("downlink_high") and t.get("type") == "Transponder":
            f = int((t["downlink_low"] + t["downlink_high"]) / 2)
        entries.append(SatEntry(
            norad_id=norad, name=names.get(norad, f"NORAD {norad}"), frequency_hz=f,
            mode=satnogs_mode_to_radio(t.get("mode")),
            transmitter=f"{t.get('description', '')} [{t.get('mode', '?')}]".strip(),
            transmitter_uuid=t.get("uuid"), activity=activity.get(norad)))
    entries.sort(key=lambda e: (activity_rank(e.activity), -(e.activity or 0),
                                -len(cands[e.norad_id]), e.name))
    # По auto_count сателита за всеки разрешен обхват, за да не се изместват
    # (напр. 70 cm да не избута сателитите на 2 m). Редът е по обхватите в конфигурацията.
    n = cfg.section("selection")["auto_count"]
    out: list[SatEntry] = []
    for band in range(len(cfg.section("band")["downlink_ranges_mhz"])):
        out += [e for e in entries if cfg.band_index(e.frequency_hz) == band][:n]
    return out


def activity_rank(count: int | None) -> int:
    """0 = чут наскоро в AMSAT, 1 = AMSAT не го следи, 2 = следи се, но не е чут."""
    if count is None:
        return 1
    return 0 if count > 0 else 2


def apply_overrides(auto: list[SatEntry], overrides: list[dict[str, Any]],
                    names: dict[int, str]) -> list[SatEntry]:
    """Ръчните настройки имат предимство (FR-05)."""
    by_id = {e.norad_id: e for e in auto}
    order = [e.norad_id for e in auto]
    for o in overrides:
        nid = o["norad_id"]
        base = by_id.get(nid)
        if base is None:
            if "frequency_hz" not in o:
                log.warning("Ръчен сателит %s няма frequency_hz и не е в автоматичния списък — пропуска се", nid)
                continue
            base = SatEntry(norad_id=nid, name=names.get(nid, f"NORAD {nid}"),
                            frequency_hz=o["frequency_hz"], mode=o.get("mode", "FM"))
            order.append(nid)
        fields = {k: v for k, v in o.items() if k in SatEntry.__dataclass_fields__}
        merged = SatEntry(**{**asdict(base), **fields, "source": "manual"})
        by_id[nid] = merged
    return [by_id[n] for n in order]


def build_selection(cfg, transmitters, activity, tle_text) -> list[SatEntry]:
    names = tle_names(tle_text)
    auto = auto_selection(cfg, transmitters, activity, names)
    return apply_overrides(auto, cfg.section("satellites").get("overrides", []), names)


# --- съхранение и засичане на промени (FR-16) ---

def selection_path(cfg) -> Path:
    return cfg.data_dir / "satellites.json"


def _candidate_summary(cfg, transmitters, names: dict[int, str] | None = None) -> dict[str, list[dict[str, Any]]]:
    """Кандидатите за засичане на промени — само тези с орбитни данни (без TLE не
    могат да се следят, а SatNOGS има много временни номера 98xxx/99xxx)."""
    return {str(n): sorted(({"uuid": t.get("uuid"), "downlink_hz": t.get("downlink_low"),
                             "mode": t.get("mode")} for t in txs), key=lambda x: str(x["uuid"]))
            for n, txs in candidates(cfg, transmitters).items() if not names or n in names}


MAX_CHANGE_LINES = 15


def detect_changes(old: dict[str, Any] | None, new_cands: dict[str, Any],
                   new_sel: list[SatEntry], names: dict[int, str]) -> list[str]:
    if not old:
        return []
    msgs: list[str] = []
    old_c = old.get("candidates", {})

    def nm(k):
        return names.get(int(k), f"NORAD {k}")

    cand_msgs: list[str] = []
    for k in sorted(set(new_cands) - set(old_c)):
        cand_msgs.append(f"{nm(k)} ({k}): отново/ново активен в обхвата")
    for k in sorted(set(old_c) - set(new_cands)):
        cand_msgs.append(f"{nm(k)} ({k}): вече няма активен предавател в обхвата")
    for k in sorted(set(old_c) & set(new_cands)):
        if old_c[k] != new_cands[k]:
            cand_msgs.append(f"{nm(k)} ({k}): променени честоти/режими: {old_c[k]} -> {new_cands[k]}")
    if len(cand_msgs) > MAX_CHANGE_LINES:
        # напр. при включване на нов обхват — обобщение вместо стотици редове
        added = len(set(new_cands) - set(old_c))
        removed = len(set(old_c) - set(new_cands))
        changed = len(cand_msgs) - added - removed
        msgs.append(f"Кандидати: +{added} нови, −{removed} отпаднали, {changed} с променени честоти "
                    f"(общо {len(new_cands)})")
    else:
        msgs += cand_msgs

    old_sel = {s["norad_id"]: s for s in old.get("selection", [])}
    new_by = {e.norad_id: e for e in new_sel}
    for n in sorted(set(new_by) - set(old_sel)):
        msgs.append(f"Добавен в списъка за слушане: {new_by[n].name} ({n})")
    for n in sorted(set(old_sel) - set(new_by)):
        msgs.append(f"Премахнат от списъка за слушане: {old_sel[n].get('name')} ({n})")
    for n in sorted(set(old_sel) & set(new_by)):
        o, e = old_sel[n], new_by[n]
        if (o.get("frequency_hz"), o.get("mode")) != (e.frequency_hz, e.mode):
            msgs.append(f"{e.name} ({n}): {o.get('frequency_hz')} {o.get('mode')} -> {e.frequency_hz} {e.mode}")
    return msgs


def update_selection(cfg, transmitters, activity, tle_text) -> tuple[list[SatEntry], list[str]]:
    """Съставя списъка, записва го и връща промените спрямо предишния."""
    names = tle_names(tle_text)
    sel = build_selection(cfg, transmitters, activity, tle_text)
    cands = _candidate_summary(cfg, transmitters, names)
    path = selection_path(cfg)
    old = read_json(path)
    changes = detect_changes(old, cands, sel, names)
    for c in changes:
        log.warning("Промяна: %s", c)
    atomic_write_json(path, {
        "schema_version": SCHEMA_VERSION,
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "selection": [asdict(e) for e in sel],
        "candidates": cands,
    })
    return sel, changes


def load_selection(cfg) -> list[SatEntry]:
    d = read_json(selection_path(cfg), {}) or {}
    return [SatEntry(**s) for s in d.get("selection", [])]
