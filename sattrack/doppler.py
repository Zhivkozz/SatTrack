"""Доплерова корекция за приемане (раздел 4.3, FR-23)."""

from __future__ import annotations

C_M_S = 299_792_458.0


def rx_frequency(f0_hz: float, range_rate_m_s: float) -> float:
    """f = f0 · (1 − v/c); v > 0 при отдалечаване."""
    return f0_hz * (1.0 - range_rate_m_s / C_M_S)


class DopplerGate:
    """Решава кога да се прати нова честота: само при промяна над прага (FR-23)."""

    def __init__(self, threshold_hz: float):
        self.threshold_hz = threshold_hz
        self.last_sent: int | None = None
        self.updates = 0

    def should_send(self, freq_hz: float) -> int | None:
        f = int(round(freq_hz))
        if self.last_sent is None or abs(f - self.last_sent) >= self.threshold_hz:
            self.last_sent = f
            self.updates += 1
            return f
        return None
