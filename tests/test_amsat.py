from datetime import datetime, timezone

from sattrack import amsat, catalog

PAGE = """<select><option value="">Select Satellite
<option value="AO-73_[U/v]">AO-73 [U/v]
<option value="ISS_[FM]">ISS [FM]
<option value="ISS_[UHF_Digi]">ISS [UHF Digi]
<option value="NO-44_[VHF_Digi]">NO-44 [VHF Digi]
<option value="RS-44_[V/u]">RS-44 [V/u]
<option value="SONATE-2_[VHF_Digi]">SONATE-2 [VHF Digi]
<option value=11>Nov</select>"""


def test_parse_names():
    names = amsat.parse_names(PAGE)
    assert "ISS_[FM]" in names and "NO-44_[VHF_Digi]" in names
    assert all("_[" in n for n in names)


def test_match_by_tle_name():
    names = amsat.parse_names(PAGE)
    assert amsat.match(names, "ISS (ZARYA)") == ["ISS_[FM]"]          # UHF режимът отпада
    assert amsat.match(names, "PCSAT (NO-44)") == ["NO-44_[VHF_Digi]"]
    assert amsat.match(names, "FUNCUBE-1 (AO-73)") == ["AO-73_[U/v]"]
    assert amsat.match(names, "SONATE-2") == ["SONATE-2_[VHF_Digi]"]
    assert amsat.match(names, "RS-44") == []                          # downlink на 70 cm
    assert amsat.match(names, "TIANYAN 01") == []


def test_match_with_70cm_enabled():
    names = amsat.parse_names(PAGE)
    both = ("vhf", "uhf")
    assert amsat.match(names, "ISS (ZARYA)", both) == ["ISS_[FM]", "ISS_[UHF_Digi]"]
    assert amsat.match(names, "RS-44", both) == ["RS-44_[V/u]"]
    assert amsat.label_band("QO-100_[NB]") == "other"
    assert amsat.match(["QO-100_[NB]"], "QO-100", both) == []


def test_count_heard():
    since = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
    reports = [
        {"report": "Heard", "reported_time": "2026-11-20T10:41:42Z"},
        {"report": "Telemetry Only", "reported_time": "2026-11-20T09:00:00Z"},
        {"report": "Not Heard", "reported_time": "2026-11-20T08:00:00Z"},
        {"report": "Heard", "reported_time": "2026-11-18T08:00:00Z"},   # твърде старо
        {"report": "Heard"},                                             # без време
    ]
    assert amsat.count_heard(reports, since) == 2


def test_activity_rank_order():
    order = sorted([None, 0, 5, 1], key=lambda a: (catalog.activity_rank(a), -(a or 0)))
    assert order == [5, 1, None, 0]
