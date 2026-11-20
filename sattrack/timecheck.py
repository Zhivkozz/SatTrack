"""Проверка на синхронизацията на часовника (важна за доплера при SSB/CW).

Само чете състоянието чрез `w32tm /query /status`; нищо не променя.
"""

from __future__ import annotations

import subprocess
import sys


def clock_warning() -> str | None:
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(["w32tm", "/query", "/status"], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:
        return "Не може да се провери синхронизацията на часовника (w32tm)."
    if not out.strip():
        return ("Услугата Windows Time не отговаря — часовникът може да не е синхронизиран. "
                "За SSB/CW доплерът изисква точно време.")
    low = out.lower()
    if "local cmos clock" in low or "free-running" in low:
        return ("Часовникът на Windows не се синхронизира с интернет (източник: локален часовник). "
                "За SSB/CW доплерът изисква точно време.")
    return None
