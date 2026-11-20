"""JSON дневник на преминаванията (FR-42, FR-50..55) и възстановяване (FR-34)."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sattrack import __version__
from sattrack.fsutil import atomic_write_json, read_json

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETE = "complete"
STATUS_INCOMPLETE = "incomplete"      # прекъснат от рестарт/срив (FR-34)
STATUS_ABORTED = "aborted"            # спрян с пауза или изход


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "SAT"


class PassLog:
    def __init__(self, cfg, root: Path | None = None):
        """root: друга папка вместо data/ (dry-run пише в cache/dryrun, извън Git)."""
        self.cfg = cfg
        root = root or cfg.data_dir
        self.dir = root / "passes"
        self.index_path = root / "index.json"
        self.tz = ZoneInfo(cfg.station["timezone"])

    def new_stem(self, start_utc: datetime, sat_name: str) -> str:
        """Име по местно време и сателит; уникално и при смяна на лятно/зимно време."""
        local = start_utc.astimezone(self.tz)
        stem = f"{local:%Y-%m-%dT%H-%M}_{safe_name(sat_name)}"
        cand, n = stem, 2
        while (self.dir / f"{cand}.json").exists():
            cand = f"{stem}_{n}"
            n += 1
        return cand

    def local_iso(self, t: datetime) -> str:
        return t.astimezone(self.tz).isoformat(timespec="seconds")

    def rel(self, p: Path | None) -> str | None:
        if p is None:
            return None
        try:
            return p.resolve().relative_to(self.cfg.base_dir.resolve()).as_posix()
        except ValueError:
            return str(p)

    def write(self, stem: str, record: dict[str, Any]) -> Path:
        ordered = {"schema_version": SCHEMA_VERSION, "program_version": __version__}
        ordered.update(record)
        path = self.dir / f"{stem}.json"
        atomic_write_json(path, ordered)
        self._update_index(path, record)
        return path

    # --- индекс (FR-53) ---
    @staticmethod
    def _summary(path: Path, r: dict[str, Any]) -> dict[str, Any]:
        return {
            "file": f"passes/{path.name}",
            "satellite": r.get("satellite", {}).get("name"),
            "norad_id": r.get("satellite", {}).get("norad_id"),
            "start": r.get("start"),
            "max_elevation_deg": r.get("max_elevation_deg"),
            "mode": r.get("mode"),
            "status": r.get("status"),
            "s_meter_max": (r.get("s_meter") or {}).get("max"),
        }

    def _update_index(self, path: Path, record: dict[str, Any]) -> None:
        idx = read_json(self.index_path) or {"schema_version": SCHEMA_VERSION, "passes": []}
        entry = self._summary(path, record)
        idx["passes"] = [p for p in idx["passes"] if p["file"] != entry["file"]] + [entry]
        idx["passes"].sort(key=lambda p: p.get("start") or "")
        atomic_write_json(self.index_path, idx)

    def rebuild_index(self) -> int:
        entries = []
        for p in sorted(self.dir.glob("*.json")):
            r = read_json(p)
            if isinstance(r, dict):
                entries.append(self._summary(p, r))
        entries.sort(key=lambda p: p.get("start") or "")
        atomic_write_json(self.index_path, {"schema_version": SCHEMA_VERSION, "passes": entries})
        return len(entries)

    # --- FR-34 ---
    def recover_incomplete(self) -> list[Path]:
        from sattrack.recorder import repair_wav
        fixed = []
        for p in sorted(self.dir.glob("*.json")):
            r = read_json(p)
            if not isinstance(r, dict) or r.get("status") != STATUS_IN_PROGRESS:
                continue
            audio = r.get("audio_file")
            if audio:
                ap = self.cfg.path(audio)
                if ap.exists():
                    repair_wav(ap)
            r["status"] = STATUS_INCOMPLETE
            r.setdefault("events", []).append("прекъснат: програмата е рестартирана по време на записа")
            self.write(p.stem, r)
            log.warning("Непълен запис от предишно стартиране: %s", p.name)
            fixed.append(p)
        return fixed
