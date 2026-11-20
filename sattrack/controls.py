"""Управление от потребителя по време на работа (FR-35).

Клавиши в конзолата (Windows): p = пауза, r = продължи, s = прогноза, q = изход.
Пауза и отдалечено: файлът data/PAUSE (`sattrack pause` / `sattrack resume`).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)

try:
    import msvcrt  # type: ignore
except ImportError:  # не-Windows: само файлов флаг
    msvcrt = None


class Controls:
    def __init__(self, pause_file: Path, keyboard: bool = True):
        self.pause_file = pause_file
        self.keyboard = keyboard and msvcrt is not None and sys.stdin is not None and sys.stdin.isatty()
        self._key_paused = False
        self.quit = False
        self.show_forecast = False
        self._last_state = False

    @property
    def paused(self) -> bool:
        return self._key_paused or self.pause_file.exists()

    def pause(self) -> None:
        self._key_paused = True

    def resume(self) -> None:
        self._key_paused = False
        try:
            self.pause_file.unlink()
        except FileNotFoundError:
            pass

    def poll(self) -> None:
        if self.keyboard:
            while msvcrt.kbhit():
                ch = msvcrt.getwch().lower()
                if ch in ("p", "з"):
                    self.pause()
                elif ch in ("r", "р"):
                    self.resume()
                elif ch in ("q", "я"):
                    self.quit = True
                elif ch in ("s", "с"):
                    self.show_forecast = True
        state = self.paused
        if state != self._last_state:
            if state:
                log.warning("ПАУЗА: автоматиката е спряна, радиото е свободно за ръчна работа. "
                            "Натиснете r или `sattrack resume` за продължаване.")
            else:
                log.info("Автоматиката е възобновена.")
            self._last_state = state
