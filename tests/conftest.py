import json
from pathlib import Path

import pytest

from sattrack.config import load_config

# TLE от документацията на Skyfield (МКС, януари 2014)
ISS_TLE = """ISS (ZARYA)
1 25544U 98067A   14020.93268519  .00009878  00000-0  18200-3 0  5082
2 25544  51.6498 109.4756 0003572  55.9686 274.8005 15.49815350868473
"""


@pytest.fixture
def cfg(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "satellites": {"overrides": [
            {"norad_id": 25544, "name": "ISS", "frequency_hz": 145800000, "mode": "FM"}]},
    }), encoding="utf-8")
    return load_config(p)
