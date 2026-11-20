import json
import time

import pytest

from conftest import ISS_TLE
from sattrack import catalog
from sattrack.config import ConfigError


def test_save_value_writes_only_that_key(cfg):
    cfg.save_value(["passes", "min_elevation_deg"], 20)
    data = json.loads(cfg.source.read_text(encoding="utf-8"))
    assert data["passes"] == {"min_elevation_deg": 20}
    assert "radio" not in data                      # без стойностите по подразбиране
    assert data["satellites"]["overrides"][0]["norad_id"] == 25544  # старото остава
    assert cfg.section("passes")["min_elevation_deg"] == 20


def test_save_value_rejects_invalid(cfg):
    with pytest.raises(ConfigError):
        cfg.save_value(["passes", "min_elevation_deg"], 120)
    assert cfg.section("passes")["min_elevation_deg"] == 30   # непроменено


@pytest.fixture
def app(cfg):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("няма графична среда")
    root.withdraw()
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    (cfg.cache_dir / "amateur.tle").write_text(ISS_TLE, encoding="utf-8")
    (cfg.cache_dir / "transmitters.json").write_text("[]", encoding="utf-8")
    catalog.update_selection(cfg, [], {}, ISS_TLE)
    from sattrack.gui import App
    a = App(root, cfg)
    yield a
    root.destroy()


def pump(app, seconds=3.0):
    end = time.time() + seconds
    while time.time() < end and app.busy:
        app.root.update()
        time.sleep(0.02)
    app.root.update()


def test_gui_builds_and_shows_data(app):
    pump(app)
    assert len(app.nb.tabs()) == 5
    assert app.sat_tree.get_children() == ("25544",)
    assert app.state() == "stopped"
    app._update_status()
    assert "спряна" in app.state_lbl.cget("text")


def test_gui_refresh_shows_result(app):
    from sattrack.sources import RefreshReport
    app.store.refresh = lambda: RefreshReport(
        ok={"tle": True, "transmitters": False, "activity": True},
        errors={"transmitters": "Connection to db.satnogs.org timed out."})
    pump(app)
    app.refresh_data()
    pump(app, 5)
    text = app.out_txt.get("1.0", "end")
    assert "✓ Орбити (CelesTrak): обновено" in text
    assert "✗ Честоти (SatNOGS DB): сървърът не отговаря" in text
    assert "непълна" in text
    assert "disabled" not in app.btn_refresh.state()


def test_gui_toggle_satellite_persists(app, cfg):
    app.sat_tree.selection_set("25544")
    app.toggle_sat()
    data = json.loads(cfg.source.read_text(encoding="utf-8"))
    iss = next(o for o in data["satellites"]["overrides"] if o["norad_id"] == 25544)
    assert iss["enabled"] is False
    assert app.sat_tree.set("25544", "on") == "—"
