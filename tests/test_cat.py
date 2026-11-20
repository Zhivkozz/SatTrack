import re
from pathlib import Path

import pytest

from sattrack.radio import cat
from sattrack.radio.dryrun import DryRunRadio


@pytest.mark.parametrize("cmd", ["FA;", "FA145800000;", "MD0;", "MD04;", "MD0C;", "SM0;",
                                 "ID;", "IF;", "NA01;", "KP30;", "SQ0;", "SQ0000;", "SQ0100;"])
def test_allowed(cmd):
    assert cat.check_allowed(cmd) == cmd


@pytest.mark.parametrize("cmd", [
    "TX1;", "TX;", "TX0;", "MX1;", "VX1;", "AC001;", "AC002;", "BI1;",  # предаване / тунер / break-in
    "FA145800000;TX1;", "FA14580000;", "MD0Z;", "SM1;", "PC100;", "EX0311;", "",
    "fa145800000;", "FA145800000X;", "SQ1000;", "SQ00000;",
])
def test_forbidden(cmd):
    with pytest.raises(cat.ForbiddenCommandError):
        cat.check_allowed(cmd)


def test_radio_layer_blocks_forbidden():
    r = DryRunRadio(echo=False)
    with pytest.raises(cat.ForbiddenCommandError):
        r.send("TX1;")
    assert r.sent == []


def test_builders_and_parsers():
    assert cat.cmd_set_freq(145_800_123) == "FA145800123;"
    assert cat.cmd_set_mode("FM") == "MD04;"
    assert cat.cmd_set_mode("usb") == "MD02;"
    assert cat.cmd_set_cw_pitch(600) == "KP30;"
    assert cat.parse_freq("FA145800000;") == 145_800_000
    assert cat.parse_mode("MD04;") == "FM"
    assert cat.parse_smeter("SM0123;") == 123
    assert cat.parse_id("ID0670;") == "0670"
    for m in cat.MODE_CODES:
        cat.check_allowed(cat.cmd_set_mode(m))
    with pytest.raises(ValueError):
        cat.cmd_set_freq(500_000_000)


def test_dryrun_roundtrip():
    r = DryRunRadio(echo=False)
    assert r.identify() == "0670"
    r.set_freq(145_801_000)
    r.set_mode("CW")
    assert r.get_freq() == 145_801_000
    assert r.get_mode() == "CW"


def test_no_tx_commands_in_source():
    """Критерий 6: в кода няма низ с команда за предаване."""
    root = Path(__file__).resolve().parents[1] / "sattrack"
    pat = re.compile(r"""["'](TX|MX|VX|AC)\d*;?["']""")
    hits = [f"{p.name}: {m.group(0)}" for p in root.rglob("*.py")
            for m in pat.finditer(p.read_text(encoding="utf-8"))]
    assert hits == []
