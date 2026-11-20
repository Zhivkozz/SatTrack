from datetime import datetime, timezone

from conftest import ISS_TLE
from sattrack.catalog import SatEntry
from sattrack.doppler import DopplerGate, rx_frequency
from sattrack.predict import Pass, Predictor, choose_non_overlapping

ISS = SatEntry(norad_id=25544, name="ISS", frequency_hz=145_800_000, mode="FM")
STATION = {"latitude_deg": 42.89, "longitude_deg": 24.71, "elevation_m": 400}
T0 = datetime(2014, 1, 21, 0, 0, tzinfo=timezone.utc)


def test_doppler_formula():
    assert rx_frequency(145.8e6, 0) == 145.8e6
    # приближаване със 7 км/с -> около +3.4 kHz
    df = rx_frequency(145.8e6, -7000) - 145.8e6
    assert 3300 < df < 3500
    assert rx_frequency(145.8e6, 7000) < 145.8e6


def test_gate_threshold():
    g = DopplerGate(500)
    assert g.should_send(145_803_000) == 145_803_000
    assert g.should_send(145_802_700) is None
    assert g.should_send(145_802_500) == 145_802_500
    assert g.updates == 2


def test_passes_and_doppler_sign():
    pr = Predictor(STATION, ISS_TLE)
    passes = pr.passes([ISS], T0, 24, 0)
    assert passes, "МКС трябва да има преминавания за 24 часа"
    for p in passes:
        assert p.aos < p.tca < p.los
        assert 0 < p.max_elevation_deg <= 90
        assert 60 < p.duration_s < 15 * 60
        # преди TCA се приближава (честотата е над номинала), след това — под
        _, _, rr_before = pr.look(25544, p.aos)
        _, _, rr_after = pr.look(25544, p.los)
        assert rr_before < 0 < rr_after
    high = pr.passes([ISS], T0, 24, 30)
    assert all(p.max_elevation_deg >= 30 for p in high)
    assert len(high) <= len(passes)


def test_overlap_rule():
    a = Pass(ISS, datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc), datetime(2026, 1, 1, 10, 5, tzinfo=timezone.utc),
             datetime(2026, 1, 1, 10, 10, tzinfo=timezone.utc), 40)
    b = Pass(ISS, datetime(2026, 1, 1, 10, 8, tzinfo=timezone.utc), datetime(2026, 1, 1, 10, 12, tzinfo=timezone.utc),
             datetime(2026, 1, 1, 10, 16, tzinfo=timezone.utc), 60)
    assert choose_non_overlapping([a, b], "max_elevation") == [b]
    assert choose_non_overlapping([a, b], "first") == [a]
