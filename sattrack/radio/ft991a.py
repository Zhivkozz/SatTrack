"""Директна серийна CAT връзка с FT-991A през pyserial (FR-20, FR-33)."""

from __future__ import annotations

import logging
import threading

import serial

from sattrack.radio import cat
from sattrack.radio.base import Radio, RadioError

log = logging.getLogger(__name__)


class FT991A(Radio):
    def __init__(self, port: str, baudrate: int = 38400, timeout_s: float = 1.0,
                 stopbits: int = 1):
        self.port = port
        self.baudrate = baudrate
        self.timeout_s = timeout_s
        self.stopbits = stopbits
        self._ser: serial.Serial | None = None
        self._lock = threading.Lock()

    def open(self) -> None:
        if self._ser and self._ser.is_open:
            return
        try:
            # DTR и RTS се свалят ПРЕДИ отварянето: в менюто на радиото те могат
            # да са настроени като PTT/KEY, а pyserial по подразбиране ги вдига.
            s = serial.Serial()
            s.port = self.port
            s.baudrate = self.baudrate
            s.bytesize = 8
            s.parity = "N"
            s.stopbits = self.stopbits
            s.timeout = self.timeout_s
            s.write_timeout = self.timeout_s
            s.rtscts = False
            s.dsrdtr = False
            s.dtr = False
            s.rts = False
            s.open()
            self._ser = s
        except serial.SerialException as e:
            self._ser = None
            raise RadioError(f"Не може да се отвори {self.port}: {e}") from e

    def close(self) -> None:
        if self._ser:
            try:
                self._ser.close()
            finally:
                self._ser = None

    def _transact(self, cmd: str, expect_reply: bool) -> str | None:
        cat.check_allowed(cmd)  # втора защита на най-ниското ниво
        with self._lock:
            if not self._ser:
                self.open()
            assert self._ser is not None
            try:
                self._ser.reset_input_buffer()
                self._ser.write(cmd.encode("ascii"))
                if not expect_reply:
                    return None
                raw = self._ser.read_until(b";")
            except serial.SerialException as e:
                self.close()
                raise RadioError(f"Грешка в серийната връзка: {e}") from e
        resp = raw.decode("ascii", errors="replace")
        if not resp.endswith(";"):
            raise RadioError(f"Няма отговор от радиото на {cmd!r} (получено {resp!r})")
        if resp == "?;":
            raise RadioError(f"Радиото отхвърли команда {cmd!r}")
        log.debug("CAT %s -> %s", cmd, resp)
        return resp
