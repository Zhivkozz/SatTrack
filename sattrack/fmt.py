"""Display formatting helpers."""

from __future__ import annotations


def fmt_mhz(hz: float) -> str:
    """Format a frequency in Hz as 'MMM,kkk,hh' (MHz, kHz, tens of Hz), e.g. 145,800,00."""
    tens = int(round(hz / 10))
    mhz, rest = divmod(tens, 100_000)
    khz, h = divmod(rest, 100)
    return f"{mhz},{khz:03d},{h:02d}"
