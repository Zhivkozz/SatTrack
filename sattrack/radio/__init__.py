"""Слой за радиото — само приемане."""

from sattrack.radio.base import Radio, RadioError


def make_radio(cfg, dry_run: bool) -> Radio:
    if dry_run:
        from sattrack.radio.dryrun import DryRunRadio
        return DryRunRadio()
    from sattrack.radio.ft991a import FT991A
    r = cfg.section("radio")
    return FT991A(r["port"], r["baudrate"], r["timeout_s"], r["stopbits"])


__all__ = ["Radio", "RadioError", "make_radio"]
