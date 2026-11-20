"""AMSAT Live OSCAR Satellite Status — източник за скорошна активност (FR-05).

Имената в AMSAT са с вида "NO-44_[VHF_Digi]", "ISS_[FM]", "JO-97_[U/v]".
Свързват се с NORAD номерата чрез имената от TLE на CelesTrak, напр.
"PCSAT (NO-44)" -> NO-44. Ако автоматичното свързване не стига, в
конфигурацията има ръчна таблица data.amsat_aliases: {"25544": ["ISS_[FM]"]}.
"""

from __future__ import annotations

import html
import re
from datetime import datetime

# Доклади, които означават, че сателитът е чут.
HEARD_REPORTS = ("heard", "telemetry only", "crew active")

# Етикети на режими с downlink извън 2 m — не се броят.
_NOT_VHF = ("uhf", "/u", "/a", "/s", "/x", "5.8g", "nb]", "wb]", "dvb", "datv", "c/x")


def parse_names(page: str) -> list[str]:
    """Имената от падащото меню на status.amsat.org."""
    vals = re.findall(r'<option[^>]*value="([^"]+_\[[^"]+\])"', page, flags=re.I)
    return sorted({html.unescape(v) for v in vals})


def _norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def aliases(tle_name: str) -> set[str]:
    """'FUNCUBE-1 (AO-73)' -> {'FUNCUBE1', 'AO73'}."""
    out = {_norm(tle_name)}
    m = re.match(r"\s*(.*?)\s*\((.*?)\)\s*$", tle_name)
    if m:
        out |= {_norm(m.group(1)), _norm(m.group(2))}
    return {a for a in out if a}


def base_name(amsat_name: str) -> str:
    return amsat_name.split("_[", 1)[0]


def is_vhf_downlink(amsat_name: str) -> bool:
    label = amsat_name[len(base_name(amsat_name)):].lower()
    return not any(x in label for x in _NOT_VHF)


def match(amsat_names: list[str], tle_name: str) -> list[str]:
    al = aliases(tle_name)
    return [n for n in amsat_names if _norm(base_name(n)) in al and is_vhf_downlink(n)]


def count_heard(reports: list[dict], since: datetime) -> int:
    n = 0
    for r in reports:
        if str(r.get("report", "")).lower() not in HEARD_REPORTS:
            continue
        try:
            t = datetime.fromisoformat(str(r["reported_time"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if t >= since:
            n += 1
    return n
