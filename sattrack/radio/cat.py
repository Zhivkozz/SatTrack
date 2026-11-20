"""CAT команди за FT-991A: форматиране, парсване и разрешен списък (FR-21, FR-26).

ВАЖНО: програмата е само за приемане. Всяка команда, преди да стигне до
серийния порт, минава през `check_allowed()`. В списъка няма команди за
предаване (TX, MX, VX, AC, ...), а проверката е по пълен шаблон, не по префикс.

Източник: Yaesu "FT-991A CAT Operation Reference Manual".
Кодовете на режимите трябва да се сверят на живо (`sattrack radio-test --modes`).
"""

from __future__ import annotations

import re

# P2 на командата MD0 (FT-991A). Сверено по CAT ръководството и на живо с
# `radio-test --modes` и по дисплея на радиото (2026-11-20, всички 11 режима OK).
MODE_CODES: dict[str, str] = {
    "LSB": "1",
    "USB": "2",
    "CW": "3",        # CW-U
    "FM": "4",
    "AM": "5",
    "CW-L": "7",
    "DATA-LSB": "8",
    "DATA-FM": "A",
    "FM-N": "B",
    "DATA-USB": "C",
    "AM-N": "D",
}
CODE_TO_MODE = {v: k for k, v in MODE_CODES.items()}
CODE_TO_MODE.update({"6": "RTTY-LSB", "9": "RTTY-USB", "E": "C4FM"})  # само за четене

FREQ_MIN_HZ = 30_000
FREQ_MAX_HZ = 470_000_000

# Пълни шаблони на разрешените команди (без терминатора ';').
_ALLOWED = [
    re.compile(p) for p in (
        r"ID",              # идентификация на радиото (четене)
        r"IF",              # информация за състоянието (четене)
        r"FA",              # четене на VFO-A
        r"FA\d{9}",         # задаване на VFO-A
        r"MD0",             # четене на режим
        r"MD0[1-9A-E]",     # задаване на режим
        r"SM0",             # четене на S-метъра
        r"NA0",             # четене на тесен филтър
        r"NA0[01]",         # тесен филтър изкл./вкл. (FR-04)
        r"KP",              # четене на CW тон
        r"KP\d{2}",         # CW тон 00-75 → 300-1050 Hz (FR-04)
        r"SQ0",             # четене на нивото на шумоподтискането
        r"SQ0\d{3}",        # шумоподтискане 000-100 (отваря се за целия пасаж, FR-41)
    )
]


class ForbiddenCommandError(RuntimeError):
    """Опит за изпращане на команда извън разрешения списък."""


def check_allowed(cmd: str) -> str:
    """Връща командата с ';' или вдига ForbiddenCommandError."""
    body = cmd[:-1] if cmd.endswith(";") else cmd
    if ";" in body or not any(p.fullmatch(body) for p in _ALLOWED):
        raise ForbiddenCommandError(f"Командата {cmd!r} не е в разрешения списък (само RX).")
    return body + ";"


# Построяване на команди ---------------------------------------------------

def cmd_set_freq(hz: int) -> str:
    hz = int(round(hz))
    if not FREQ_MIN_HZ <= hz <= FREQ_MAX_HZ:
        raise ValueError(f"Честота {hz} Hz е извън обхвата на радиото")
    return f"FA{hz:09d};"


def cmd_set_mode(mode: str) -> str:
    try:
        return f"MD0{MODE_CODES[mode.upper()]};"
    except KeyError:
        raise ValueError(f"Непознат режим {mode!r}") from None


def cmd_set_narrow(narrow: bool) -> str:
    return f"NA0{1 if narrow else 0};"


def cmd_set_cw_pitch(hz: int) -> str:
    step = round((hz - 300) / 10)
    if not 0 <= step <= 75:
        raise ValueError("CW тонът трябва да е между 300 и 1050 Hz")
    return f"KP{step:02d};"


def cmd_set_squelch(level: int) -> str:
    if not 0 <= level <= 100:
        raise ValueError("Шумоподтискането трябва да е 0-100")
    return f"SQ0{level:03d};"


# Парсване на отговори -----------------------------------------------------

def parse_squelch(resp: str) -> int:
    m = re.fullmatch(r"SQ0(\d{3});", resp)
    if not m:
        raise ValueError(f"Неочакван отговор за шумоподтискане: {resp!r}")
    return int(m.group(1))



def parse_freq(resp: str) -> int:
    m = re.fullmatch(r"FA(\d{9});", resp)
    if not m:
        raise ValueError(f"Неочакван отговор за честота: {resp!r}")
    return int(m.group(1))


def parse_mode(resp: str) -> str:
    m = re.fullmatch(r"MD0([0-9A-F]);", resp)
    if not m:
        raise ValueError(f"Неочакван отговор за режим: {resp!r}")
    return CODE_TO_MODE.get(m.group(1), f"?{m.group(1)}")


def parse_smeter(resp: str) -> int:
    m = re.fullmatch(r"SM0(\d{3});", resp)
    if not m:
        raise ValueError(f"Неочакван отговор за S-метър: {resp!r}")
    return int(m.group(1))


def parse_id(resp: str) -> str:
    m = re.fullmatch(r"ID(\d{4});", resp)
    if not m:
        raise ValueError(f"Неочакван отговор за ID: {resp!r}")
    return m.group(1)


FT991A_ID = "0670"
