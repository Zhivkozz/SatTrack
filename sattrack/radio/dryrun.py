"""Режим без радио (FR-25): командите само се показват; четенията се симулират."""

from __future__ import annotations

import logging
import random

from sattrack.radio import cat
from sattrack.radio.base import Radio

log = logging.getLogger(__name__)


class DryRunRadio(Radio):
    def __init__(self, echo: bool = True):
        self.echo = echo
        self.sent: list[str] = []
        self._freq = 145_000_000
        self._mode = "4"

    def _transact(self, cmd: str, expect_reply: bool) -> str | None:
        cat.check_allowed(cmd)
        self.sent.append(cmd)
        if self.echo and not expect_reply:
            log.info("[dry-run] CAT %s", cmd)
        body = cmd[:-1]
        if body.startswith("FA") and len(body) > 2:
            self._freq = int(body[2:])
        elif body.startswith("MD0") and len(body) > 3:
            # като истинското FT-991A: смяната AM/USB/LSB мести VFO
            shift = {("5", "2"): -700, ("2", "1"): 1400}.get((self._mode, body[3]), 0)
            self._freq += shift
            self._mode = body[3]
        if not expect_reply:
            return None
        if body == "ID":
            return f"ID{cat.FT991A_ID};"
        if body == "FA":
            return f"FA{self._freq:09d};"
        if body == "MD0":
            return f"MD0{self._mode};"
        if body == "SQ0":
            return "SQ0040;"
        if body == "SM0":
            return f"SM0{random.randint(0, 40):03d};"
        return f"{body};"
