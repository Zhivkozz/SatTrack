"""Общ интерфейс към радиото. Всички реализации изпращат само през cat.check_allowed()."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from sattrack.radio import cat

log = logging.getLogger(__name__)


class RadioError(RuntimeError):
    """Радиото не отговаря или връща грешка (FR-33)."""


class Radio(ABC):
    """Високо ниво: настройка на честота/режим и четене на S-метъра."""

    @abstractmethod
    def _transact(self, cmd: str, expect_reply: bool) -> str | None:
        """Изпраща вече проверена команда. Реализира се от наследниците."""

    def open(self) -> None:  # pragma: no cover - по подразбиране нищо
        pass

    def close(self) -> None:  # pragma: no cover
        pass

    def send(self, cmd: str, expect_reply: bool = False) -> str | None:
        return self._transact(cat.check_allowed(cmd), expect_reply)

    # --- операции ---
    def identify(self) -> str:
        return cat.parse_id(self.send("ID;", True))

    def get_freq(self) -> int:
        return cat.parse_freq(self.send("FA;", True))

    def set_freq(self, hz: int) -> None:
        self.send(cat.cmd_set_freq(hz))

    def get_mode(self) -> str:
        return cat.parse_mode(self.send("MD0;", True))

    def set_mode(self, mode: str) -> None:
        self.send(cat.cmd_set_mode(mode))

    def set_narrow(self, narrow: bool) -> None:
        self.send(cat.cmd_set_narrow(narrow))

    def set_cw_pitch(self, hz: int) -> None:
        self.send(cat.cmd_set_cw_pitch(hz))

    def get_squelch(self) -> int:
        return cat.parse_squelch(self.send("SQ0;", True))

    def set_squelch(self, level: int) -> None:
        self.send(cat.cmd_set_squelch(level))

    def read_smeter(self) -> int:
        return cat.parse_smeter(self.send("SM0;", True))

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()
