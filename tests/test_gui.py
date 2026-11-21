import gc
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
    # collect Tk objects here, on the main thread: if the garbage collector runs them in a
    # worker thread later, Tcl fails and the next tk.Tk() of another test is skipped
    gc.collect()


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

    def slow_refresh():
        time.sleep(1.2)
        return RefreshReport(ok={"tle": True, "transmitters": False, "activity": True},
                             errors={"transmitters": "Connection to db.satnogs.org timed out."})

    app.store.refresh = slow_refresh
    pump(app)
    app.refresh_data()
    app.root.update()
    assert "сверява се" in app.refresh_lbl.cget("text")      # индикаторът тече
    assert app.refresh_pb.winfo_manager() == "pack"
    assert "disabled" in app.btn_refresh.state()
    pump(app, 5)
    assert "Готово" in app.refresh_lbl.cget("text")          # и показва края
    assert app.refresh_pb.winfo_manager() == ""
    text = app.out_txt.get("1.0", "end")
    assert "✓ Орбити (CelesTrak): обновено" in text
    assert "✗ Честоти (SatNOGS DB): сървърът не отговаря" in text
    assert "непълна" in text
    assert "disabled" not in app.btn_refresh.state()


def test_gui_delete_record(app, cfg):
    from sattrack.passlog import PassLog
    pl = PassLog(cfg)
    pl.write("2026-11-21T04-12_ISS", {"status": "complete", "satellite": {"name": "ISS", "norad_id": 25544},
                                      "start": "2026-11-21T04:12:00+02:00", "audio_file": None})
    app.refresh_records()
    assert len(app.rec_tree.get_children()) == 1
    app.rec_tree.selection_set("0")
    app.delete_record(confirm=False)
    assert app.rec_tree.get_children() == ()
    assert not (pl.dir / "2026-11-21T04-12_ISS.json").exists()


def test_gui_record_timeline_shows_signals(app, cfg):
    from test_signals import make_wav
    from sattrack.passlog import PassLog
    cfg.path("recordings").mkdir(parents=True, exist_ok=True)
    make_wav(cfg.path("recordings/s.wav"), quiet=(30.0, 31.0))
    PassLog(cfg).write("2026-11-21T04-12_ISS", {"status": "complete", "mode": "FM",
                                                "satellite": {"name": "ISS", "norad_id": 25544},
                                                "start": "2026-11-21T04:12:00+02:00", "audio_file": "recordings/s.wav"})
    app.refresh_records()                      # starts the background analysis
    app.rec_tree.selection_set("0")
    app._on_rec_select()
    pump(app, 10)
    assert app.rec_tree.set("0", "sig").startswith("1 · ")
    assert app.rec_sig and app.rec_sig["segments"][0]["kind"] == "FM"
    assert app.timeline.duration == pytest.approx(60)
    assert "Сигнали: 1" in app.tl_info.cget("text")
    started = []                               # playback needs an audio device; check the target only
    app._start_playback = lambda start=None: started.append(start)
    app.player.seek(5)
    app.jump_signal(1)
    assert started[-1] == pytest.approx(29, abs=0.5)   # 1 s before the signal
    app.delete_record(confirm=False)           # the selected WAV is not locked
    assert not cfg.path("recordings/s.wav").exists()


def test_gui_sstv_decodes_and_shows(app, cfg, monkeypatch):
    from test_sstv import make_pattern, robot36_audio, save
    from sattrack.passlog import PassLog
    cfg.path("recordings").mkdir(parents=True, exist_ok=True)
    save(cfg.path("recordings/v.wav"), robot36_audio(make_pattern()))
    PassLog(cfg).write("2026-11-21T11-17_VIZARD", {"status": "complete", "mode": "FM",
                                                   "satellite": {"name": "VIZARD", "norad_id": 1},
                                                   "start": "2026-11-21T11:17:00+02:00", "audio_file": "recordings/v.wav"})
    shown = []
    real = app.open_sstv_viewer
    monkeypatch.setattr(app, "open_sstv_viewer", lambda rec, ims: shown.append(real(rec, ims)))
    app.refresh_records()
    app.rec_tree.selection_set("0")
    app._on_rec_select()
    app.show_sstv()
    end = time.time() + 20
    while not shown and time.time() < end:
        app.root.update()
        time.sleep(0.02)
    assert shown, "viewer did not open"
    win = shown[0]
    assert len(win.photos) == 1 and win.photos[0][1].width() == 640
    win.destroy()
    app.show_sstv()                    # second time: from the cache, no decoding
    assert len(shown) == 2
    shown[1].destroy()


def test_gui_toggle_satellite_persists(app, cfg):
    app.sat_tree.selection_set("25544")
    app.toggle_sat()
    data = json.loads(cfg.source.read_text(encoding="utf-8"))
    iss = next(o for o in data["satellites"]["overrides"] if o["norad_id"] == 25544)
    assert iss["enabled"] is False
    assert app.sat_tree.set("25544", "on") == "—"
