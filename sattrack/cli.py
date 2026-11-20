"""Команден ред на SatTrack."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone

from sattrack import __version__, catalog
from sattrack.config import ConfigError, is_narrowband, load_config
from sattrack.fmt import fmt_mhz
from sattrack.logsetup import setup_logging

log = logging.getLogger("sattrack")


def _store(cfg):
    from sattrack.sources import DataStore
    return DataStore(cfg)


def cmd_update(cfg, args) -> int:
    store = _store(cfg)
    rep = store.refresh()
    for k in ("tle", "transmitters", "activity"):
        st = "OK" if rep.ok.get(k) else f"ГРЕШКА: {rep.errors.get(k)}"
        age = store.age_days(k)
        print(f"  {k:13} {st}" + (f"  (възраст {age * 24:.1f} ч)" if age is not None else ""))
    sel, changes = catalog.update_selection(cfg, store.load_transmitters(), store.load_activity(),
                                            store.load_tle_text())
    print(f"\nСписък за слушане: {len(sel)} сателита" + (f", промени: {len(changes)}" if changes else ""))
    for c in changes:
        print("  * " + c)
    for w in store.freshness_warnings(narrow=any(is_narrowband(s.mode) for s in sel)):
        print("ВНИМАНИЕ: " + w)
    return 0 if rep.ok.get("tle") and rep.ok.get("transmitters") else 1


def cmd_satellites(cfg, args) -> int:
    store = _store(cfg)
    if not store.load_transmitters():
        print("Няма данни. Изпълнете първо `sattrack update`.")
        return 1
    sel, _ = catalog.update_selection(cfg, store.load_transmitters(), store.load_activity(),
                                      store.load_tle_text())
    matches = store.load_activity_matches()
    print(f"{'NORAD':>6}  {'Име':20} {'Честота MHz':>11} {'Режим':6} {'Чут':>4} Източник  "
          f"{'Предавател':40} AMSAT")
    for s in sel:
        print(f"{s.norad_id:>6}  {s.name[:20]:20} {fmt_mhz(s.frequency_hz):>11} {s.mode:6} "
              f"{s.activity if s.activity is not None else '-':>4} {s.source:8}  "
              f"{(s.transmitter or '')[:40]:40} {', '.join(matches.get(s.norad_id, []))}")
    hrs = cfg.section("data")["activity_hours"]
    print(f"\n„Чут“ = доклади в AMSAT status за последните {hrs} ч; „-“ = AMSAT не следи сателита.\n"
          "Промени се правят в config.json → satellites.overrides (или в прозореца, раздел „Сателити“).")
    return 0


def cmd_passes(cfg, args) -> int:
    from sattrack.predict import Predictor, choose_non_overlapping, format_passes
    store = _store(cfg)
    tle = store.load_tle_text()
    if not tle:
        print("Няма TLE. Изпълнете първо `sattrack update`.")
        return 1
    sel = catalog.load_selection(cfg)
    if not sel:
        sel, _ = catalog.update_selection(cfg, store.load_transmitters(), store.load_activity(), tle)
    pred = Predictor(cfg.station, tle)
    p = cfg.section("passes")
    min_el = 0.0 if args.all else (args.min_el if args.min_el is not None else p["min_elevation_deg"])
    allp = pred.passes(sel, datetime.now(timezone.utc), args.hours or p["horizon_hours"], min_el)
    chosen = choose_non_overlapping(allp, p["overlap_rule"], p["prestart_s"])
    print(f"Станция: {cfg.station['name']} ({cfg.station['latitude_deg']}, {cfg.station['longitude_deg']}), "
          f"мин. височина {min_el}°\n")
    print(format_passes(allp, cfg.station["timezone"], chosen))
    for w in store.freshness_warnings():
        print("ВНИМАНИЕ: " + w)
    return 0


def cmd_radio_test(cfg, args) -> int:
    from sattrack.radio import RadioError, make_radio
    from sattrack.radio.cat import FT991A_ID, MODE_CODES
    radio = make_radio(cfg, args.dry_run)
    try:
        with radio:
            rid = radio.identify()
            print(f"ID: {rid}" + ("  (FT-991A)" if rid == FT991A_ID else "  (НЕ е FT-991A!)"))
            f0, m0 = radio.get_freq(), radio.get_mode()
            print(f"VFO-A: {fmt_mhz(f0)} MHz, режим {m0}, S-метър {radio.read_smeter()}")
            if args.set_freq:
                target = int(round(args.set_freq * 1e6))
                radio.set_freq(target)
                time.sleep(0.3)
                got = radio.get_freq()
                print(f"Зададено {target} Hz, прочетено {got} Hz -> {'OK' if got == target else 'РАЗЛИКА'}")
            if args.modes:
                print("Проверка на кодовете за режими (сравнете с дисплея на радиото):")
                for name in MODE_CODES:
                    radio.set_mode(name)
                    time.sleep(0.5)
                    got = radio.get_mode()
                    print(f"  {name:9} -> радиото отчита {got:9} {'OK' if got == name else 'РАЗЛИКА'}")
            if args.set_freq or args.modes:
                if m0 in MODE_CODES:
                    radio.set_mode(m0)
                radio.set_freq(f0)
                print("Върнати са началните честота и режим.")
    except (RadioError, ValueError) as e:
        print(f"ГРЕШКА: {e}")
        return 1
    return 0


def cmd_audio_devices(cfg, args) -> int:
    from sattrack.recorder import list_input_devices
    want = cfg.section("recording")["device"].lower()
    for i, name, api, sr in list_input_devices():
        mark = "*" if want in name.lower() else " "
        print(f"{mark} [{i:2}] {name}  ({api}, {sr} Hz)")
    print(f"\n* = съвпада с recording.device = {cfg.section('recording')['device']!r}")
    return 0


def _run(cfg, args, only_norad=None) -> int:
    from sattrack.app import build_scheduler
    from sattrack.scheduler import RealClock, SimClock
    from sattrack.timecheck import clock_warning

    if (w := clock_warning()):
        log.warning(w)
    clock = RealClock()
    if args.sim_start or args.speed != 1:
        start = datetime.fromisoformat(args.sim_start) if args.sim_start else datetime.now(timezone.utc)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if not args.dry_run:
            print("Симулирано време е позволено само с --dry-run.")
            return 2
        clock = SimClock(start, args.speed)
    sch = build_scheduler(cfg, args.dry_run, clock, only_norad)
    print("Клавиши: p = пауза, r = продължи, s = прогноза, q = изход" + ("   [DRY-RUN]" if args.dry_run else ""))
    try:
        sch.run(max_passes=args.max_passes)
    except KeyboardInterrupt:
        log.info("Спиране (Ctrl+C).")
    finally:
        sch.radio.close()
    return 0


def cmd_run(cfg, args) -> int:
    return _run(cfg, args)


def cmd_track(cfg, args) -> int:
    return _run(cfg, args, only_norad=args.norad)


def cmd_pause(cfg, args) -> int:
    f = cfg.data_dir / "PAUSE"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(datetime.now().isoformat(timespec="seconds") + "\n", encoding="utf-8")
    print("Пауза включена. Автоматиката няма да ползва радиото.")
    return 0


def cmd_resume(cfg, args) -> int:
    f = cfg.data_dir / "PAUSE"
    f.unlink(missing_ok=True)
    print("Пауза изключена (ако е включена с клавиш p, натиснете r в прозореца на програмата).")
    return 0


def cmd_gui(cfg, args) -> int:
    from sattrack.gui import main as gui_main
    return gui_main(["--config", str(cfg.source)] if cfg.source else [])


def cmd_rebuild_index(cfg, args) -> int:
    from sattrack.passlog import PassLog
    n = PassLog(cfg).rebuild_index()
    print(f"Индексът е възстановен: {n} преминавания.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="sattrack", description="Автоматично приемане на сателити с FT-991A (само RX).")
    ap.add_argument("--config", "-c", help="път до config.json (по подразбиране ./config.json)")
    ap.add_argument("--version", action="version", version=f"sattrack {__version__}")
    ap.add_argument("-v", "--verbose", action="store_true", help="подробен журнал (DEBUG)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("update", help="сверка на TLE, честоти и активност").set_defaults(fn=cmd_update)

    sub.add_parser("satellites", help="списък за слушане").set_defaults(fn=cmd_satellites)

    p = sub.add_parser("passes", help="прогноза за преминавания")
    p.add_argument("--hours", type=float)
    p.add_argument("--min-el", type=float, help="минимална височина за този показ")
    p.add_argument("--all", action="store_true", help="всички преминавания над хоризонта")
    p.set_defaults(fn=cmd_passes)

    p = sub.add_parser("radio-test", help="проверка на CAT връзката")
    p.add_argument("--set-freq", type=float, metavar="MHZ", help="задава и проверява честота, после връща")
    p.add_argument("--modes", action="store_true", help="проверява кодовете за режими")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_radio_test)

    sub.add_parser("audio-devices", help="аудио входове").set_defaults(fn=cmd_audio_devices)

    for name, fn, hlp in (("run", cmd_run, "автоматичен режим"), ("track", cmd_track, "ръчно следене на един сателит")):
        p = sub.add_parser(name, help=hlp)
        if name == "track":
            p.add_argument("norad", type=int)
        p.add_argument("--dry-run", action="store_true", help="без радио и запис, само показва командите")
        p.add_argument("--sim-start", help="(dry-run) начално UTC време, напр. 2026-11-21T04:00")
        p.add_argument("--speed", type=float, default=1.0, help="(dry-run) ускорение на времето")
        p.add_argument("--max-passes", type=int, help="спира след N преминавания")
        p.set_defaults(fn=fn)

    sub.add_parser("pause", help="пауза на автоматиката (FR-35)").set_defaults(fn=cmd_pause)
    sub.add_parser("resume", help="край на паузата").set_defaults(fn=cmd_resume)
    sub.add_parser("rebuild-index", help="възстановява data/index.json").set_defaults(fn=cmd_rebuild_index)
    sub.add_parser("gui", help="графичен интерфейс").set_defaults(fn=cmd_gui)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    setup_logging(cfg.log_dir, "DEBUG" if args.verbose else cfg.section("log")["level"])
    return args.fn(cfg, args)
