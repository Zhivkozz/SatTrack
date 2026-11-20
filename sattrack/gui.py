"""Графичен интерфейс на SatTrack (Tkinter).

Планировчикът работи във фонова нишка; прозорецът само чете `Scheduler.status`
и праща команди (старт, стоп, пауза, сверка, нов график). Всички бавни операции
(мрежа, радио, прогноза) вървят във фонови нишки, а резултатите се връщат през
опашка, за да не замръзва прозорецът.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import queue
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from zoneinfo import ZoneInfo

from sattrack import __version__, catalog
from sattrack.config import ConfigError, load_config
from sattrack.fsutil import read_json
from sattrack.logsetup import setup_logging
from sattrack.predict import Pass, Predictor, choose_non_overlapping
from sattrack.sources import DataStore

log = logging.getLogger("sattrack.gui")

STATE_TEXT = {
    "stopped": ("Автоматиката е спряна", "#6b7280"),
    "starting": ("Стартира…", "#2563eb"),
    "waiting": ("Изчаква следващото преминаване", "#15803d"),
    "paused": ("ПАУЗА — радиото е свободно", "#c2410c"),
    "tracking": ("ЗАПИСВА", "#b91c1c"),
}

FONT = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI", 10, "bold")
FONT_BIG = ("Segoe UI", 22, "bold")
FONT_MID = ("Segoe UI", 13)
FONT_MONO = ("Consolas", 9)


def fmt_td(td: timedelta) -> str:
    s = max(0, int(td.total_seconds()))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class QueueHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put(("log", record.levelno, self.format(record)))
        except Exception:
            pass


# --------------------------------------------------------------------------
# Небесна карта: хоризонт по края, зенит в центъра
# --------------------------------------------------------------------------

class SkyPlot(tk.Canvas):
    def __init__(self, master, size: int = 300):
        super().__init__(master, width=size, height=size, bg="white", highlightthickness=0)
        self.track: list[tuple[float, float]] = []
        self.pos: tuple[float, float] | None = None
        self.min_el: float | None = None
        self.title = ""
        self.bind("<Configure>", lambda e: self.redraw())

    def show(self, track=None, pos=None, min_el=None, title="") -> None:
        self.track, self.pos, self.min_el, self.title = track or [], pos, min_el, title
        self.redraw()

    def _geom(self):
        w, h = self.winfo_width(), self.winfo_height()
        top = 26  # място за заглавието
        return w / 2, top + (h - top) / 2, max(10, min(w, h - top) / 2 - 22)

    def _xy(self, el: float, az: float):
        cx, cy, R = self._geom()
        r = R * (90 - max(el, 0)) / 90
        a = math.radians(az)
        return cx + r * math.sin(a), cy - r * math.cos(a)

    def redraw(self) -> None:
        self.delete("all")
        cx, cy, R = self._geom()
        for el, dash in ((0, None), (30, (2, 3)), (60, (2, 3))):
            r = R * (90 - el) / 90
            self.create_oval(cx - r, cy - r, cx + r, cy + r, outline="#9ca3af", dash=dash)
            if el:
                self.create_text(cx + 3, cy - r + 7, text=f"{el}°", fill="#9ca3af", anchor="w",
                                 font=("Segoe UI", 7))
        if self.min_el:
            r = R * (90 - self.min_el) / 90
            self.create_oval(cx - r, cy - r, cx + r, cy + r, outline="#f59e0b", dash=(4, 2))
        self.create_line(cx - R, cy, cx + R, cy, fill="#e5e7eb")
        self.create_line(cx, cy - R, cx, cy + R, fill="#e5e7eb")
        for txt, dx, dy in (("С", 0, -R - 10), ("И", R + 10, 0), ("Ю", 0, R + 10), ("З", -R - 10, 0)):
            self.create_text(cx + dx, cy + dy, text=txt, fill="#374151", font=FONT_BOLD)
        if self.title:
            self.create_text(6, 4, text=self.title, anchor="nw", fill="#111827", font=FONT_BOLD)
        if len(self.track) >= 2:
            pts = [c for el, az in self.track for c in self._xy(el, az)]
            self.create_line(*pts, fill="#2563eb", width=2, smooth=True)
            for (el, az), col in ((self.track[0], "#16a34a"), (self.track[-1], "#dc2626")):
                x, y = self._xy(el, az)
                self.create_oval(x - 4, y - 4, x + 4, y + 4, fill=col, outline="")
        if self.pos and self.pos[0] > -5:
            x, y = self._xy(*self.pos)
            self.create_oval(x - 7, y - 7, x + 7, y + 7, fill="#dc2626", outline="white", width=2)


# --------------------------------------------------------------------------
# Главен прозорец
# --------------------------------------------------------------------------

class App:
    def __init__(self, root: tk.Tk, cfg):
        self.root = root
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.station["timezone"])
        self.q: queue.Queue = queue.Queue()
        self.store = DataStore(cfg)
        self.sched = None
        self.thread: threading.Thread | None = None
        self.busy: set[str] = set()
        self.predictor: Predictor | None = None
        self.forecast: list[Pass] = []
        self.chosen: list[Pass] = []
        self.track_cache: dict[tuple[int, datetime], list[tuple[float, float]]] = {}
        self.records: list[dict[str, Any]] = []

        handler = QueueHandler(self.q)
        handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(handler)

        root.title(f"SatTrack {__version__} — {cfg.station['name']}")
        self.sc = max(1.0, root.winfo_fpixels("1i") / 96.0)  # мащабиране на Windows (125 %, 150 %…)
        w = min(self.px(1120), root.winfo_screenwidth() - 40)
        h = min(self.px(860), int(root.winfo_screenheight() * 0.85))  # да не влиза под лентата на задачите
        root.geometry(f"{w}x{h}+20+10")
        root.minsize(min(self.px(900), w), min(self.px(640), h))
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        style = ttk.Style()
        from tkinter import font as tkfont
        row_h = tkfont.Font(root=root, font=FONT).metrics("linespace") + 10  # отчита мащабирането
        style.configure("Treeview", rowheight=row_h, font=FONT)
        style.configure("Treeview.Heading", font=FONT_BOLD)
        style.configure("TButton", padding=(10, 4))
        style.configure("Big.TButton", font=FONT_BOLD, padding=(14, 6))

        self._build()
        self.refresh_forecast()
        self.refresh_satellites()
        self.refresh_records()
        self.refresh_data_info()
        self.root.after(300, self._tick)
        log.info("SatTrack е готов. Натиснете „Старт“ за автоматичен режим.")

    # ---------------- общи помощни ----------------
    def px(self, n: float) -> int:
        """Пиксели, съобразени с мащабирането на екрана."""
        return int(n * getattr(self, "sc", 1.0))

    def _columns(self, tree: ttk.Treeview, spec, stretch=()) -> None:
        for c, title, w in spec:
            tree.heading(c, text=title)
            tree.column(c, width=self.px(w), minwidth=self.px(min(w, 50)), anchor="w", stretch=c in stretch)

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def state(self) -> str:
        return self.sched.status.get("state", "stopped") if self.running and self.sched else "stopped"

    def local(self, t: datetime, fmt: str = "%H:%M:%S") -> str:
        lt = t.astimezone(self.tz)
        if "%a" in fmt:  # дните на български, независимо от езика на Windows
            fmt = fmt.replace("%a", ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд")[lt.weekday()])
        return lt.strftime(fmt)

    def bg(self, name: str, fn: Callable[[], Any], done: Callable[[Any, Exception | None], None] | None = None):
        """Изпълнява fn във фонова нишка; done(резултат, грешка) се вика в главната нишка."""
        if name in self.busy:
            return
        self.busy.add(name)

        def run():
            try:
                res, err = fn(), None
            except Exception as e:  # noqa: BLE001 — показва се на потребителя
                res, err = None, e
                log.error("%s: %s", name, e)
            self.q.put(("done", name, done, res, err))

        threading.Thread(target=run, daemon=True).start()

    def _tick(self) -> None:
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "log":
                    self._append_log(msg[1], msg[2])
                elif msg[0] == "done":
                    _, name, done, res, err = msg
                    self.busy.discard(name)
                    if done:
                        done(res, err)
                elif msg[0] == "out":
                    self._out(msg[1])
                elif msg[0] == "stopped":
                    self.refresh_records()
                    self.refresh_forecast()
        except queue.Empty:
            pass
        try:
            self._update_status()
        except Exception as e:  # интерфейсът не бива да спира заради грешка в показването
            log.debug("status: %s", e)
        self.root.after(500, self._tick)

    # ---------------- изграждане ----------------
    def _build(self) -> None:
        top = ttk.Frame(self.root, padding=(10, 8))
        top.pack(fill="x")
        self.dry_var = tk.BooleanVar(value=False)
        self.btn_close = ttk.Button(top, text="Изход", command=self.on_close)
        self.btn_close.pack(side="right", padx=(6, 0))
        self.btn_pause = ttk.Button(top, text="Пауза", command=self.toggle_pause)
        self.btn_pause.pack(side="right", padx=4)
        self.btn_stop = ttk.Button(top, text="■ Стоп", command=self.stop)
        self.btn_stop.pack(side="right", padx=4)
        self.btn_start = ttk.Button(top, text="▶ Старт", style="Big.TButton", command=self.start)
        self.btn_start.pack(side="right", padx=4)
        ttk.Checkbutton(top, text="Тест без радио", variable=self.dry_var).pack(side="right", padx=12)
        # статусът се подрежда последен, за да се свие той, а не бутоните
        self.state_lbl = tk.Label(top, text="", font=("Segoe UI", 13, "bold"), fg="#6b7280", anchor="w")
        self.state_lbl.pack(side="left", fill="x", expand=True)

        paned = ttk.PanedWindow(self.root, orient="vertical")
        paned.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.nb = ttk.Notebook(paned)
        paned.add(self.nb, weight=4)

        logf = ttk.LabelFrame(paned, text="Журнал на събитията", padding=4)
        paned.add(logf, weight=1)
        self.log_txt = ScrolledText(logf, height=5, font=FONT_MONO, state="disabled", wrap="word")
        self.log_txt.pack(fill="both", expand=True)
        self.log_txt.tag_configure("warn", foreground="#b45309")
        self.log_txt.tag_configure("err", foreground="#b91c1c")

        self._build_status_tab()
        self._build_forecast_tab()
        self._build_sats_tab()
        self._build_records_tab()
        self._build_data_tab()
        self.nb.enable_traversal()  # Ctrl+Tab / Ctrl+Shift+Tab сменят разделите

    def _append_log(self, level: int, text: str) -> None:
        tag = "err" if level >= logging.ERROR else "warn" if level >= logging.WARNING else ""
        self.log_txt.configure(state="normal")
        self.log_txt.insert("end", text + "\n", tag)
        lines = int(self.log_txt.index("end-1c").split(".")[0])
        if lines > 1500:
            self.log_txt.delete("1.0", f"{lines - 1500}.0")
        self.log_txt.see("end")
        self.log_txt.configure(state="disabled")

    # ---------------- Статус ----------------
    def _build_status_tab(self) -> None:
        tab = ttk.Frame(self.nb, padding=12)
        self.nb.add(tab, text="  Статус  ")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        left = ttk.Frame(tab)
        left.grid(row=0, column=0, sticky="nsew")
        self.sky = SkyPlot(tab, self.px(270))
        self.sky.grid(row=0, column=1, sticky="ne", padx=(12, 0))
        bottom = ttk.Frame(tab)
        bottom.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(12, 0))

        self.st_title = tk.Label(left, text="—", font=FONT_BIG, anchor="w")
        self.st_title.pack(fill="x")
        self.st_sub = tk.Label(left, text="", font=FONT_MID, anchor="w", fg="#374151")
        self.st_sub.pack(fill="x", pady=(0, 6))

        grid = ttk.Frame(left)
        grid.pack(fill="x")
        self.st_vals: dict[str, tk.Label] = {}
        rows = [("freq", "На радиото"), ("calc", "Изчислена"), ("offset", "Доплер"), ("el", "Височина"),
                ("az", "Азимут"), ("mode", "Режим"), ("audio", "Аудио файл")]
        for i, (key, name) in enumerate(rows):
            ttk.Label(grid, text=name + ":", font=FONT).grid(row=i, column=0, sticky="w", pady=1)
            lbl = tk.Label(grid, text="—", font=("Segoe UI", 12), anchor="w")
            lbl.grid(row=i, column=1, sticky="w", padx=10)
            self.st_vals[key] = lbl
        r = len(rows)
        ttk.Label(grid, text="S-метър:", font=FONT).grid(row=r, column=0, sticky="w", pady=2)
        self.sm_bar = ttk.Progressbar(grid, maximum=255, length=260)
        self.sm_bar.grid(row=r, column=1, sticky="w", padx=10)
        self.sm_lbl = ttk.Label(grid, text="—")
        self.sm_lbl.grid(row=r, column=2, sticky="w")
        ttk.Label(grid, text="Преминаване:", font=FONT).grid(row=r + 1, column=0, sticky="w", pady=2)
        self.pass_bar = ttk.Progressbar(grid, maximum=1000, length=260)
        self.pass_bar.grid(row=r + 1, column=1, sticky="w", padx=10)
        self.pass_lbl = ttk.Label(grid, text="—")
        self.pass_lbl.grid(row=r + 1, column=2, sticky="w")

        ttk.Label(bottom, text="Следващи записи", font=FONT_BOLD).pack(anchor="w", pady=(0, 4))
        cols = ("when", "sat", "el", "mode", "freq")
        self.next_tree = ttk.Treeview(bottom, columns=cols, show="headings", height=5)
        self._columns(self.next_tree, (("when", "Кога", 170), ("sat", "Сателит", 200), ("el", "Макс.", 70),
                                       ("mode", "Режим", 70), ("freq", "Честота MHz", 110)), stretch=("sat",))
        self.next_tree.pack(fill="both", expand=True)

    def _pass_track(self, p: Pass) -> list[tuple[float, float]]:
        key = (p.sat.norad_id, p.aos)
        if key not in self.track_cache:
            pred = (self.sched.predictor if self.running and self.sched and self.sched.predictor
                    else self.predictor)
            if pred is None or not pred.has(p.sat.norad_id):
                return []
            pts, t = [], p.aos
            while t <= p.los:
                el, az, _ = pred.look(p.sat.norad_id, t)
                pts.append((el, az))
                t += timedelta(seconds=10)
            self.track_cache[key] = pts
        return self.track_cache[key]

    def _update_status(self) -> None:
        st = self.sched.status if self.running and self.sched else {"state": "stopped"}
        state = st.get("state", "stopped")
        text, color = STATE_TEXT.get(state, (state, "#000"))
        if self.running and self.sched and self.sched.dry_run:
            text += "  [dry-run]"
        self.state_lbl.configure(text=text, fg=color)

        self.btn_start.state(["disabled"] if self.running else ["!disabled"])
        self.btn_stop.state(["!disabled"] if self.running else ["disabled"])
        self.btn_pause.state(["!disabled"] if self.running else ["disabled"])
        paused = self.running and self.sched.controls.paused
        self.btn_pause.configure(text="▶ Продължи" if paused else "❚❚ Пауза")
        self.btn_modes.state(["disabled"] if self.running and not paused else ["!disabled"])
        self.btn_audio.state(["disabled"] if state == "tracking" else ["!disabled"])

        now = datetime.now(timezone.utc)
        min_el = self.cfg.section("passes")["min_elevation_deg"]
        v = self.st_vals
        if state == "tracking":
            p: Pass = st["pass"]
            live = st.get("live") or {}
            now = st.get("now", now)
            self.st_title.configure(text=p.sat.name, fg="#b91c1c")
            self.st_sub.configure(text=f"Записва до {self.local(p.los)}  •  макс. {p.max_elevation_deg:.0f}°")
            if live:
                sent = live.get("sent")
                thr = self.cfg.doppler_params(p.sat.mode)["threshold_hz"]
                if sent:
                    v["freq"].configure(text=f"{sent / 1e6:.6f} MHz")
                    v["calc"].configure(text=f"{live['freq'] / 1e6:.6f} MHz   (разлика {sent - live['freq']:+.0f} Hz,"
                                             f" нова честота се праща при > {thr} Hz)")
                else:
                    v["freq"].configure(text="—")
                    v["calc"].configure(text=f"{live['freq'] / 1e6:.6f} MHz")
                v["offset"].configure(text=f"{live['offset']:+.0f} Hz")
                v["el"].configure(text=f"{live['el']:.1f}°")
                v["az"].configure(text=f"{live['az']:.0f}°")
                sm = live.get("smeter")
                self.sm_bar["value"] = sm or 0
                self.sm_lbl.configure(text="—" if sm is None else str(sm))
                v["audio"].configure(text=Path(live["audio"]).name if live.get("audio") else "без запис")
            v["mode"].configure(text=f"{p.sat.mode}  ({p.sat.frequency_hz / 1e6:.4f} MHz номинално)")
            total = max(1.0, (p.los - p.aos).total_seconds())
            done = (now - p.aos).total_seconds()
            self.pass_bar["value"] = max(0, min(1000, 1000 * done / total))
            self.pass_lbl.configure(text=f"остават {fmt_td(p.los - now)}")
            pos = (live["el"], live["az"]) if live else None
            self.sky.show(self._pass_track(p), pos, min_el, f"{p.sat.name} — сега")
        else:
            nxt: Pass | None = st.get("next") if state in ("waiting", "paused") else None
            if nxt is None and not self.running:
                nxt = next((p for p in self.chosen if p.los > now), None)
            for lbl in v.values():
                lbl.configure(text="—")
            self.sm_bar["value"] = 0
            self.sm_lbl.configure(text="—")
            self.pass_bar["value"] = 0
            if nxt:
                self.st_title.configure(text=f"Следва: {nxt.sat.name}", fg="#111827")
                wait = nxt.aos - now
                self.st_sub.configure(text=f"в {self.local(nxt.aos, '%H:%M')} (след {fmt_td(wait)})  •  "
                                           f"макс. {nxt.max_elevation_deg:.0f}°  •  "
                                           f"{fmt_td(nxt.los - nxt.aos)} мин")
                v["mode"].configure(text=f"{nxt.sat.mode}  {nxt.sat.frequency_hz / 1e6:.4f} MHz")
                self.pass_lbl.configure(text="")
                self.sky.show(self._pass_track(nxt), None, min_el,
                              f"{nxt.sat.name} — {self.local(nxt.aos, '%H:%M')}")
            else:
                self.st_title.configure(text="Няма предстоящи записи", fg="#111827")
                self.st_sub.configure(text="Обновете данните или намалете минималната височина.")
                self.sky.show([], None, min_el)

        upcoming = (list(self.sched.schedule) if self.running and self.sched
                    else [p for p in self.chosen if p.aos > now])[:8]
        ids = [f"{p.sat.norad_id}-{p.aos.timestamp():.0f}" for p in upcoming]
        if ids != list(self.next_tree.get_children()):
            self.next_tree.delete(*self.next_tree.get_children())
            for iid, p in zip(ids, upcoming):
                self.next_tree.insert("", "end", iid=iid, values=(
                    self.local(p.aos, "%a %d.%m  %H:%M"), p.sat.name, f"{p.max_elevation_deg:.0f}°",
                    p.sat.mode, f"{p.sat.frequency_hz / 1e6:.4f}"))

    # ---------------- Управление ----------------
    def start(self) -> None:
        if self.running:
            return
        from sattrack.app import build_scheduler
        try:
            sched = build_scheduler(self.cfg, dry_run=self.dry_var.get(), keyboard=False)
        except Exception as e:
            messagebox.showerror("SatTrack", f"Не може да стартира: {e}")
            return
        self.sched = sched

        def run():
            try:
                sched.run()
            except Exception:
                log.exception("Автоматиката спря с грешка")
            finally:
                try:
                    sched.radio.close()
                except Exception:
                    pass
                self.q.put(("stopped",))

        self.thread = threading.Thread(target=run, name="scheduler", daemon=True)
        self.thread.start()
        log.info("Автоматиката е стартирана%s.", " (dry-run)" if self.dry_var.get() else "")

    def stop(self, wait: bool = False) -> None:
        if not self.running:
            return
        if self.state() == "tracking" and not wait and not messagebox.askyesno(
                "SatTrack", "В момента се записва преминаване. Да се спре ли записът?"):
            return
        self.sched.controls.quit = True
        log.info("Спиране на автоматиката…")
        if wait:
            self.thread.join(timeout=15)

    def toggle_pause(self) -> None:
        if not self.running:
            return
        c = self.sched.controls
        if c.paused:
            c.resume()
        else:
            if self.state() == "tracking" and not messagebox.askyesno(
                    "SatTrack", "Тече запис. Пауза ще го прекъсне. Продължаване?"):
                return
            c.pause()

    def on_close(self) -> None:
        if self.running:
            if not messagebox.askyesno("SatTrack", "Автоматиката работи. Да се спре ли и да се затвори?"):
                return
            self.stop(wait=True)
        try:
            import winsound
            winsound.PlaySound(None, 0)
        except Exception:
            pass
        self.root.destroy()

    def _after_settings_change(self) -> None:
        """Нов график: в работещия планировчик и в прогнозата на прозореца."""
        if self.running and self.sched:
            self.sched.request_reload()
        self.track_cache.clear()
        self.refresh_forecast()

    # ---------------- Прогноза ----------------
    def _build_forecast_tab(self) -> None:
        tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(tab, text="  Прогноза  ")
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="Мин. височина на запис:").pack(side="left")
        self.minel_var = tk.IntVar(value=int(self.cfg.section("passes")["min_elevation_deg"]))
        ttk.Spinbox(bar, from_=0, to=90, increment=5, width=5, textvariable=self.minel_var).pack(side="left", padx=4)
        ttk.Label(bar, text="°").pack(side="left")
        ttk.Button(bar, text="Запази", command=self.save_min_el).pack(side="left", padx=(6, 20))
        ttk.Label(bar, text="Часове напред:").pack(side="left")
        self.hours_var = tk.IntVar(value=int(self.cfg.section("passes")["horizon_hours"]))
        ttk.Spinbox(bar, from_=6, to=96, increment=6, width=5, textvariable=self.hours_var).pack(side="left", padx=4)
        self.showall_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Покажи и ниските", variable=self.showall_var,
                        command=self.refresh_forecast).pack(side="left", padx=16)
        ttk.Button(bar, text="⟳ Преизчисли", command=self.refresh_forecast).pack(side="left")
        self.fc_info = ttk.Label(tab, text="", foreground="#4b5563")
        self.fc_info.pack(side="bottom", anchor="w", pady=(6, 0))

        body = ttk.Frame(tab)
        body.pack(fill="both", expand=True)
        cols = ("date", "aos", "tca", "los", "el", "dur", "sat", "freq", "mode", "rec")
        self.fc_tree = ttk.Treeview(body, columns=cols, show="headings", selectmode="browse")
        self._columns(self.fc_tree, (("date", "Дата", 80), ("aos", "Начало", 62), ("tca", "Връх", 55),
                                     ("los", "Край", 55), ("el", "Макс.", 55), ("dur", "Мин.", 45),
                                     ("sat", "Сателит", 150), ("freq", "MHz", 80), ("mode", "Режим", 58),
                                     ("rec", "Запис", 115)), stretch=("sat",))
        sb = ttk.Scrollbar(body, orient="vertical", command=self.fc_tree.yview)
        self.fc_tree.configure(yscrollcommand=sb.set)
        self.fc_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.fc_tree.tag_configure("rec", foreground="#15803d")
        self.fc_tree.tag_configure("skip", foreground="#9ca3af")
        self.fc_tree.bind("<<TreeviewSelect>>", self._on_fc_select)
        self.fc_sky = SkyPlot(body, self.px(250))
        self.fc_sky.pack(side="left", fill="both", padx=(10, 0))

    def save_min_el(self) -> None:
        try:
            v = int(self.minel_var.get())
            self.cfg.save_value(["passes", "min_elevation_deg"], v)
        except (ValueError, tk.TclError, ConfigError) as e:
            messagebox.showerror("SatTrack", f"Невалидна стойност: {e}")
            return
        log.info("Минималната височина е %d° (записано в config.json).", v)
        self._after_settings_change()

    def refresh_forecast(self) -> None:
        cfg = self.cfg
        try:
            hours = int(self.hours_var.get())
        except (tk.TclError, ValueError):
            hours = cfg.section("passes")["horizon_hours"]
        show_all = self.showall_var.get()
        self.fc_info.configure(text="изчислява…")

        def work():
            tle = self.store.load_tle_text()
            if not tle:
                raise RuntimeError("няма TLE — обновете данните")
            sel = catalog.load_selection(cfg)
            pred = Predictor(cfg.station, tle)
            p = cfg.section("passes")
            allp = pred.passes(sel, datetime.now(timezone.utc), hours, 0 if show_all else p["min_elevation_deg"])
            eligible = [x for x in allp if x.max_elevation_deg >= p["min_elevation_deg"]]
            chosen = choose_non_overlapping(eligible, p["overlap_rule"], p["prestart_s"])
            return pred, allp, chosen

        def done(res, err):
            if err:
                self.fc_info.configure(text=f"Грешка: {err}")
                return
            self.predictor, self.forecast, self.chosen = res
            min_el = cfg.section("passes")["min_elevation_deg"]
            chosen_ids = {id(x) for x in self.chosen}
            self.fc_tree.delete(*self.fc_tree.get_children())
            for i, p in enumerate(self.forecast):
                if id(p) in chosen_ids:
                    rec, tag = "✓ запис", "rec"
                elif p.max_elevation_deg < min_el:
                    rec, tag = "под прага", "skip"
                else:
                    rec, tag = "припокриване", "skip"
                self.fc_tree.insert("", "end", iid=str(i), tags=(tag,), values=(
                    self.local(p.aos, "%a %d.%m"), self.local(p.aos, "%H:%M"), self.local(p.tca, "%H:%M"),
                    self.local(p.los, "%H:%M"), f"{p.max_elevation_deg:.0f}°", f"{p.duration_s / 60:.0f}",
                    p.sat.name, f"{p.sat.frequency_hz / 1e6:.4f}", p.sat.mode, rec))
            self.fc_info.configure(text=f"{len(self.chosen)} преминавания за запис от {len(self.forecast)} показани.  "
                                        "Изберете ред, за да видите траекторията.")
            first = next((str(i) for i, p in enumerate(self.forecast) if id(p) in chosen_ids), None)
            if first:
                self.fc_tree.selection_set(first)
                self.fc_tree.see(first)

        self.bg("Прогноза", work, done)

    def _on_fc_select(self, _e=None) -> None:
        sel = self.fc_tree.selection()
        if not sel:
            return
        p = self.forecast[int(sel[0])]
        self.fc_sky.show(self._pass_track(p), None, self.cfg.section("passes")["min_elevation_deg"],
                         f"{p.sat.name} {self.local(p.aos, '%d.%m %H:%M')}")

    # ---------------- Сателити ----------------
    def _build_sats_tab(self) -> None:
        tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(tab, text="  Сателити  ")
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Button(bar, text="Включи / изключи избрания", command=self.toggle_sat).pack(side="left")
        self.btn_confirm = ttk.Button(bar, text="✓ Потвърди списъка", command=self.confirm_sats)
        self.btn_confirm.pack(side="left", padx=8)
        self.sat_info = tk.Label(bar, text="", font=FONT_BOLD)
        self.sat_info.pack(side="left", padx=12)

        cols = ("on", "norad", "name", "freq", "mode", "heard", "src", "tx")
        self.sat_tree = ttk.Treeview(tab, columns=cols, show="headings", selectmode="browse")
        self._columns(self.sat_tree, (("on", "Вкл.", 45), ("norad", "NORAD", 65), ("name", "Сателит", 165),
                                      ("freq", "MHz", 80), ("mode", "Режим", 58), ("heard", "Чут 24 ч", 75),
                                      ("src", "Източник", 75), ("tx", "Предавател (SatNOGS)", 300)),
                      stretch=("tx",))
        self.sat_tree.pack(fill="both", expand=True)
        self.sat_tree.tag_configure("off", foreground="#9ca3af")
        self.sat_tree.bind("<Double-1>", lambda e: self.toggle_sat())
        ttk.Label(tab, foreground="#4b5563", wraplength=1000, justify="left", text=(
            "Списъкът се съставя автоматично: активни предаватели на 2 m от SatNOGS DB, подредени по "
            "доклади „чут“ в AMSAT status. Двоен клик включва или изключва сателит (записва се в "
            "config.json). Честота и режим на ръка се задават в config.json → satellites.overrides."
        )).pack(fill="x", pady=(8, 0))

    def refresh_satellites(self) -> None:
        data = read_json(catalog.selection_path(self.cfg), {}) or {}
        sel = [catalog.SatEntry(**s) for s in data.get("selection", [])]
        self.sat_tree.delete(*self.sat_tree.get_children())
        for s in sel:
            self.sat_tree.insert("", "end", iid=str(s.norad_id), tags=(() if s.enabled else ("off",)), values=(
                "✓" if s.enabled else "—", s.norad_id, s.name, f"{s.frequency_hz / 1e6:.4f}", s.mode,
                "-" if s.activity is None else s.activity,
                "ръчен" if s.source == "manual" else "авто", s.transmitter or ""))
        if data.get("confirmed"):
            self.sat_info.configure(text="Списъкът е потвърден.", fg="#15803d")
        else:
            self.sat_info.configure(text="Списъкът не е потвърден — прегледайте го.", fg="#c2410c")

    def _rebuild_selection(self) -> None:
        catalog.update_selection(self.cfg, self.store.load_transmitters(), self.store.load_activity(),
                                 self.store.load_tle_text())

    def toggle_sat(self) -> None:
        sel = self.sat_tree.selection()
        if not sel:
            return
        norad = int(sel[0])
        currently_on = self.sat_tree.set(sel[0], "on") == "✓"
        overrides = [dict(o) for o in self.cfg.section("satellites").get("overrides", [])]
        o = next((x for x in overrides if x["norad_id"] == norad), None)
        if o is None:
            overrides.append({"norad_id": norad, "enabled": not currently_on})
        else:
            o["enabled"] = not currently_on
            if set(o) == {"norad_id", "enabled"} and o["enabled"]:
                overrides.remove(o)  # само „включен“ — не е нужна ръчна настройка
        try:
            self.cfg.save_value(["satellites", "overrides"], overrides)
            self._rebuild_selection()
        except Exception as e:
            messagebox.showerror("SatTrack", str(e))
            return
        log.info("%s е %s.", self.sat_tree.set(sel[0], "name"), "изключен" if currently_on else "включен")
        self.refresh_satellites()
        self.sat_tree.selection_set(str(norad))
        self._after_settings_change()

    def confirm_sats(self) -> None:
        from sattrack.fsutil import atomic_write_json
        path = catalog.selection_path(self.cfg)
        data = read_json(path, {}) or {}
        if not data:
            return
        data["confirmed"] = True
        atomic_write_json(path, data)
        log.info("Списъкът със сателити е потвърден.")
        self.refresh_satellites()

    # ---------------- Записи ----------------
    def _build_records_tab(self) -> None:
        tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(tab, text="  Записи  ")
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Button(bar, text="▶ Пусни", command=self.play).pack(side="left")
        ttk.Button(bar, text="■ Спри", command=self.stop_play).pack(side="left", padx=4)
        ttk.Button(bar, text="Отвори в плейър", command=self.open_player).pack(side="left", padx=4)
        ttk.Button(bar, text="Папка със записите", command=self.open_folder).pack(side="left", padx=4)
        ttk.Button(bar, text="⟳ Обнови", command=self.refresh_records).pack(side="right")

        paned = ttk.PanedWindow(tab, orient="horizontal")
        paned.pack(fill="both", expand=True)
        cols = ("when", "sat", "el", "mode", "smax", "status", "audio")
        self.rec_tree = ttk.Treeview(paned, columns=cols, show="headings", selectmode="browse")
        self._columns(self.rec_tree, (("when", "Начало", 130), ("sat", "Сателит", 140), ("el", "Макс.", 55),
                                      ("mode", "Режим", 58), ("smax", "S макс.", 65), ("status", "Статус", 90),
                                      ("audio", "Аудио", 55)), stretch=("sat",))
        self.rec_tree.tag_configure("bad", foreground="#c2410c")
        self.rec_tree.bind("<<TreeviewSelect>>", self._on_rec_select)
        self.rec_tree.bind("<Double-1>", lambda e: self.play())
        paned.add(self.rec_tree, weight=3)
        self.rec_detail = ScrolledText(paned, font=FONT_MONO, width=48, state="disabled")
        paned.add(self.rec_detail, weight=2)

    def refresh_records(self) -> None:
        idx = read_json(self.cfg.data_dir / "index.json", {}) or {}
        self.records = list(reversed(idx.get("passes", [])))
        self.rec_tree.delete(*self.rec_tree.get_children())
        status_bg = {"complete": "завършен", "incomplete": "непълен", "aborted": "прекъснат",
                     "in_progress": "записва"}
        for i, r in enumerate(self.records):
            rec = read_json(self.cfg.data_dir / r["file"], {}) or {}
            audio = rec.get("audio_file")
            has_audio = bool(audio) and self.cfg.path(audio).exists()
            start = r.get("start") or ""
            when = start[:16].replace("T", "  ") if start else "?"
            self.rec_tree.insert("", "end", iid=str(i), tags=(() if r.get("status") == "complete" else ("bad",)),
                                 values=(when, r.get("satellite"), f"{r.get('max_elevation_deg') or 0:.0f}°",
                                         r.get("mode"), r.get("s_meter_max") if r.get("s_meter_max") is not None else "—",
                                         status_bg.get(r.get("status"), r.get("status")),
                                         "✓" if has_audio else "—"))

    def _selected_record(self) -> dict | None:
        sel = self.rec_tree.selection()
        if not sel:
            return None
        return read_json(self.cfg.data_dir / self.records[int(sel[0])]["file"], {}) or {}

    def _selected_audio(self) -> Path | None:
        rec = self._selected_record()
        if not rec or not rec.get("audio_file"):
            messagebox.showinfo("SatTrack", "Този запис няма аудио файл.")
            return None
        p = self.cfg.path(rec["audio_file"])
        if not p.exists():
            messagebox.showinfo("SatTrack", f"Файлът липсва: {p}")
            return None
        return p

    def _on_rec_select(self, _e=None) -> None:
        rec = self._selected_record()
        self.rec_detail.configure(state="normal")
        self.rec_detail.delete("1.0", "end")
        if rec:
            self.rec_detail.insert("end", json.dumps(rec, ensure_ascii=False, indent=2))
        self.rec_detail.configure(state="disabled")

    def play(self) -> None:
        p = self._selected_audio()
        if p:
            try:
                import winsound
                winsound.PlaySound(str(p), winsound.SND_FILENAME | winsound.SND_ASYNC)
                log.info("Възпроизвежда се %s", p.name)
            except Exception as e:
                messagebox.showerror("SatTrack", f"Не може да се пусне: {e}")

    def stop_play(self) -> None:
        try:
            import winsound
            winsound.PlaySound(None, 0)
        except Exception:
            pass

    def open_player(self) -> None:
        p = self._selected_audio()
        if p:
            os.startfile(p)  # type: ignore[attr-defined]

    def open_folder(self) -> None:
        d = self.cfg.recordings_dir
        d.mkdir(parents=True, exist_ok=True)
        os.startfile(d)  # type: ignore[attr-defined]

    # ---------------- Данни и радио ----------------
    def _build_data_tab(self) -> None:
        tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(tab, text="  Данни и радио  ")
        top = ttk.Frame(tab)
        top.pack(fill="x")

        df = ttk.LabelFrame(top, text="Данни (TLE, честоти, активност)", padding=10)
        df.pack(side="left", fill="both", expand=True)
        self.data_lbls: dict[str, ttk.Label] = {}
        for i, (k, name) in enumerate((("tle", "Орбити (CelesTrak)"), ("transmitters", "Честоти (SatNOGS DB)"),
                                       ("activity", "Активност (AMSAT)"))):
            ttk.Label(df, text=name + ":").grid(row=i, column=0, sticky="w", pady=2)
            lbl = ttk.Label(df, text="—")
            lbl.grid(row=i, column=1, sticky="w", padx=8)
            self.data_lbls[k] = lbl
        self.data_warn = tk.Label(df, text="", fg="#b45309", justify="left", wraplength=380)
        self.data_warn.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.btn_refresh = ttk.Button(df, text="⟳ Обнови данните сега", command=self.refresh_data)
        self.btn_refresh.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))

        rf = ttk.LabelFrame(top, text="Радио и аудио", padding=10)
        rf.pack(side="left", fill="both", expand=True, padx=(10, 0))
        r = self.cfg.section("radio")
        ttk.Label(rf, text=f"FT-991A на {r['port']}, {r['baudrate']} бода  •  "
                           f"шумоподтискане при запис: {r.get('squelch')}").pack(anchor="w")
        ttk.Label(rf, text=f"Аудио вход: „{self.cfg.section('recording')['device']}“").pack(anchor="w", pady=(2, 8))
        btns = ttk.Frame(rf)
        btns.pack(anchor="w")
        ttk.Button(btns, text="Провери радиото", command=self.radio_check).pack(side="left")
        self.btn_modes = ttk.Button(btns, text="Тест на режимите", command=self.radio_modes)
        self.btn_modes.pack(side="left", padx=4)
        self.btn_audio = ttk.Button(btns, text="Тест на аудиото (5 s)", command=self.audio_test)
        self.btn_audio.pack(side="left", padx=4)
        ttk.Label(rf, foreground="#4b5563", wraplength=420, justify="left", text=(
            "Тестът на режимите и на аудиото е достъпен, когато автоматиката е спряна или на пауза. "
            "Радиото се връща в началното състояние след теста.")).pack(anchor="w", pady=(8, 0))

        ttk.Label(tab, text="Резултат:", font=FONT_BOLD).pack(anchor="w", pady=(12, 4))
        self.out_txt = ScrolledText(tab, font=FONT_MONO, height=12, state="disabled")
        self.out_txt.pack(fill="both", expand=True)

    def _out(self, text: str, clear: bool = False) -> None:
        self.out_txt.configure(state="normal")
        if clear:
            self.out_txt.delete("1.0", "end")
        self.out_txt.insert("end", text + "\n")
        self.out_txt.see("end")
        self.out_txt.configure(state="disabled")

    def refresh_data_info(self) -> None:
        for k, lbl in self.data_lbls.items():
            t = self.store.fetched_at(k)
            if t is None:
                lbl.configure(text="няма данни")
            else:
                age = self.store.age_days(k) or 0
                age_txt = f"{age * 24:.0f} ч" if age < 2 else f"{age:.1f} дни"
                lbl.configure(text=f"{self.local(t, '%d.%m.%Y %H:%M')}  (преди {age_txt})")
        warns = self.store.freshness_warnings()
        self.data_warn.configure(text="\n".join("⚠ " + w for w in warns))

    def refresh_data(self) -> None:
        if self.running and self.sched:
            self.sched.request_refresh()
            log.info("Сверката е заявена; ще се изпълни между преминаванията.")
            self.root.after(15000, self._after_refresh)
            return
        self.btn_refresh.state(["disabled"])
        log.info("Сверка на данните…")

        def work():
            rep = self.store.refresh()
            self._rebuild_selection()
            return rep

        def done(rep, err):
            self.btn_refresh.state(["!disabled"])
            if rep is not None:
                for k, ok in rep.ok.items():
                    if not ok:
                        log.warning("%s: %s (ползва се кешът)", k, rep.errors.get(k))
                log.info("Сверката приключи.")
            self._after_refresh()

        self.bg("Сверка", work, done)

    def _after_refresh(self) -> None:
        self.refresh_data_info()
        self.refresh_satellites()
        self.track_cache.clear()
        self.refresh_forecast()

    def _radio(self):
        """Ползва радиото на работещия планировчик (същата серийна връзка) или временна."""
        if self.running and self.sched:
            return self.sched.radio, False
        from sattrack.radio import make_radio
        return make_radio(self.cfg, self.dry_var.get()), True

    def radio_check(self) -> None:
        self._out("Проверка на радиото…", clear=True)

        def work():
            radio, own = self._radio()
            try:
                rid = radio.identify()
                return (f"ID: {rid}{'  (FT-991A)' if rid == '0670' else '  (НЕ е FT-991A!)'}\n"
                        f"VFO-A: {radio.get_freq() / 1e6:.6f} MHz, режим {radio.get_mode()}\n"
                        f"S-метър: {radio.read_smeter()}, шумоподтискане: {radio.get_squelch()}")
            finally:
                if own:
                    radio.close()

        self.bg("Проверка на радиото", work,
                lambda res, err: self._out(res if err is None else f"ГРЕШКА: {err}\n"
                                           "Проверете кабела, COM порта и менюто 033 CAT RTS = DISABLE."))

    def radio_modes(self) -> None:
        from sattrack.radio.cat import MODE_CODES
        self._out("Тест на режимите (сравнете с дисплея на радиото)…", clear=True)

        def work():
            radio, own = self._radio()
            lines = []
            try:
                f0, m0 = radio.get_freq(), radio.get_mode()
                try:
                    radio.set_freq(145_800_000)
                    for name in MODE_CODES:
                        radio.set_mode(name)
                        time.sleep(0.8)
                        got = radio.get_mode()
                        msg = f"  {name:9} -> {got:9} {'OK' if got == name else 'РАЗЛИКА'}"
                        lines.append(msg)
                        self.q.put(("out", msg))  # показва се веднага, докато тестът тече
                finally:
                    if m0 in MODE_CODES:
                        radio.set_mode(m0)
                    radio.set_freq(f0)
                return f"Върнато: {f0 / 1e6:.6f} MHz {m0}"
            finally:
                if own:
                    radio.close()

        self.bg("Тест на режимите", work,
                lambda res, err: self._out(res if err is None else f"ГРЕШКА: {err}"))

    def audio_test(self) -> None:
        self._out("Запис 5 секунди от аудио входа на радиото…", clear=True)
        r = self.cfg.section("recording")

        def work():
            import wave

            import numpy as np

            from sattrack.recorder import WavRecorder
            radio, own = self._radio()
            sq_old = None
            try:
                try:
                    sq_old = radio.get_squelch()
                    radio.set_squelch(0)
                except Exception:
                    sq_old = None
                path = self.cfg.cache_dir / "audiotest.wav"
                rec = WavRecorder(r["device"], r["samplerate"], r["channels"], r["host_api"])
                rec.start(path)
                time.sleep(5)
                rec.stop()
            finally:
                if sq_old is not None:
                    radio.set_squelch(sq_old)
                if own:
                    radio.close()
            with wave.open(str(path)) as w:
                a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(float)
            rms = math.sqrt(float(np.mean(a ** 2))) if len(a) else 0.0
            peak = float(np.abs(a).max()) if len(a) else 0.0
            db = lambda x: 20 * math.log10(max(x, 1) / 32767)  # noqa: E731
            advice = ("Нивото е добро." if -30 <= db(peak) <= -3 else
                      "Твърде силно — има риск от изкривяване. Намалете нивото." if db(peak) > -3 else
                      "Слабо. Увеличете нивото на „USB Audio CODEC“ в Windows (Звук → Вход) "
                      "или изходното ниво за USB/DATA в менюто на радиото.")
            # шумопотискането на Windows: шум в началото, после почти тишина
            secs = np.array_split(a, 5)
            first, rest = (math.sqrt(float(np.mean(s ** 2))) for s in (secs[0], np.concatenate(secs[1:])))
            if db(first) - db(rest) > 25:
                advice = ("Звукът изчезва след първата секунда — Windows потиска шума. "
                          "Затворете програмите, ползващи входа на радиото, или изключете "
                          "„Audio enhancements“ за USB Audio CODEC.")
            return (f"Аудио път: {rec.used_path}\n"
                    f"Пик: {db(peak):.1f} dBFS, средно (RMS): {db(rms):.1f} dBFS\n{advice}\n"
                    f"Файл: {path}")

        self.bg("Тест на аудиото", work, lambda res, err: self._out(res if err is None else f"ГРЕШКА: {err}"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sattrack-gui")
    ap.add_argument("--config", "-c")
    args = ap.parse_args(argv)
    path = args.config
    if path is None and not Path("config.json").exists():
        # стартиране от пряк път: config.json до пакета (редактируема инсталация)
        cand = Path(__file__).resolve().parents[1] / "config.json"
        path = str(cand) if cand.exists() else None
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # ясен текст при мащабиране
    except Exception:
        pass
    root = tk.Tk()
    try:
        cfg = load_config(path)
    except ConfigError as e:
        root.withdraw()
        messagebox.showerror("SatTrack — грешка в конфигурацията", str(e))
        return 2
    setup_logging(cfg.log_dir, cfg.section("log")["level"], console=False)
    App(root, cfg)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
