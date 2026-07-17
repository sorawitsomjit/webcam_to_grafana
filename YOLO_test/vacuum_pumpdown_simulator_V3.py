#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Vacuum Pump-down Simulator (GUI) — LCD look + CSV Playback (no time limit in Sim mode)
Author: ChatGPT

Features
- LCD-style GUI to mimic:
    "340: Pressure"
    "  6.3E-6 mbar"
- Two modes:
    1) Simulate (two-stage exponential + noise/flicker), runs without time limit
    2) Playback CSV (columns: second, pressure_mbar, display)
- Start / Pause / Reset
- Speed control (x1–x240) affects both modes
- Export CSV (1 h @1s) from the current simulation model
"""
import math
import csv
import random
from dataclasses import dataclass, field
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ---------- Formatting ----------

def format_sci_mbar(value: float) -> str:
    s = f"{value:.1E}".replace("E+0","E+").replace("E-0","E-")
    return f"{s} mbar"

# ---------- Pump model ----------

@dataclass
class PumpDownParams:
    P0: float = 4.0e0
    Pbase: float = 8.6e-6
    t1: float = 60.0
    t2: float = 1200.0
    frac_fast: float = 0.96
    noise_sigma_rel: float = 0.015
    dt: float = 1.0
    flicker_strength: float = 0.02

@dataclass
class PumpDownModel:
    params: PumpDownParams = field(default_factory=PumpDownParams)
    seed: int = 42
    def __post_init__(self):
        self.reset()
    def reset(self):
        random.seed(self.seed)
        self.prev_noise = 0.0
    def pressure_at(self, t: float) -> float:
        p = self.params
        A_fast = p.frac_fast * (p.P0 - p.Pbase)
        A_slow = (1.0 - p.frac_fast) * (p.P0 - p.Pbase)
        val = A_fast*math.exp(-t/p.t1) + A_slow*math.exp(-t/p.t2) + p.Pbase
        white = random.gauss(0.0, p.noise_sigma_rel)
        self.prev_noise = 0.85*self.prev_noise + white
        val *= (1.0 + self.prev_noise)
        if random.random() < (p.flicker_strength * p.dt / 10.0):
            val *= random.uniform(1.05, 1.15)
        return max(min(val, p.P0), p.Pbase)

# ---------- App ----------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Vacuum Pump-down — LCD Mockup (Simulate / CSV Playback)")
        self.resizable(False, False)

        self.model = PumpDownModel()
        self.duration_s = 3600.0  # used only for export_csv
        self.sim_time = 0.0
        self.running = False
        self.update_interval_ms = 100
        self.speed = 60.0

        self.csv_rows = None
        self.csv_len = 0

        self._build_ui()
        self._reset_display_initial()

    def _build_ui(self):
        root = ttk.Frame(self, padding=16)
        root.grid(row=0, column=0, sticky="nsew")

        lcd_bg = "#e7f8ff"
        bezel = tk.Frame(root, bg="#d9d9d9", bd=2, relief="ridge", padx=12, pady=12)
        bezel.grid(row=0, column=0, columnspan=4, sticky="ew")

        lcd = tk.Frame(bezel, bg=lcd_bg, bd=2, relief="sunken", padx=16, pady=10)
        lcd.pack(fill="x")

        font_line1 = ("Consolas", 20, "bold")
        font_line2 = ("Consolas", 30, "bold")
        font_line3 = ("Consolas", 26)

        self.line1 = tk.Label(lcd, text="340:  Pressure", font=font_line1, bg=lcd_bg, fg="#0a2a6b")
        self.line1.pack(anchor="w")
        self.line2 = tk.Label(lcd, text="6.3E-6 mbar", font=font_line2, bg=lcd_bg, fg="#0a2a6b")
        self.line2.pack(anchor="w", pady=(4, 0))
        self.line3 = tk.Label(lcd, text="▼       ▼", font=font_line3, bg=lcd_bg, fg="#0a2a6b")
        self.line3.pack(anchor="w", pady=(2, 0))

        self.start_btn = ttk.Button(root, text="Start", command=self.start)
        self.pause_btn = ttk.Button(root, text="Pause", command=self.pause, state="disabled")
        self.reset_btn = ttk.Button(root, text="Reset", command=self.reset)
        self.export_btn = ttk.Button(root, text="Export CSV (1h @1s)", command=self.export_csv)
        self.start_btn.grid(row=1, column=0, padx=4, pady=(12, 0), sticky="ew")
        self.pause_btn.grid(row=1, column=1, padx=4, pady=(12, 0), sticky="ew")
        self.reset_btn.grid(row=1, column=2, padx=4, pady=(12, 0), sticky="ew")
        self.export_btn.grid(row=1, column=3, padx=4, pady=(12, 0), sticky="ew")

        self.mode_var = tk.StringVar(value="simulate")
        ttk.Label(root, text="Mode").grid(row=2, column=0, sticky="w", pady=(10,0))
        self.rb_sim = ttk.Radiobutton(root, text="Simulate", variable=self.mode_var, value="simulate", command=self._on_mode_change)
        self.rb_csv = ttk.Radiobutton(root, text="Playback CSV", variable=self.mode_var, value="csv", command=self._on_mode_change)
        self.rb_sim.grid(row=2, column=1, sticky="w", pady=(10,0))
        self.rb_csv.grid(row=2, column=2, sticky="w", pady=(10,0))
        self.load_btn = ttk.Button(root, text="Load CSV…", command=self.load_csv, state="disabled")
        self.load_btn.grid(row=2, column=3, sticky="ew", padx=4, pady=(10,0))

        ttk.Label(root, text="Speed (x)").grid(row=3, column=0, sticky="w", pady=(10,0))
        self.speed_var = tk.DoubleVar(value=self.speed)
        self.speed_scale = ttk.Scale(root, from_=1.0, to=240.0, orient="horizontal",
                                     command=self._on_speed_change, variable=self.speed_var)
        self.speed_scale.grid(row=3, column=1, columnspan=2, sticky="ew", padx=4, pady=(10,0))
        self.speed_label = ttk.Label(root, text=f"x{int(self.speed)}")
        self.speed_label.grid(row=3, column=3, sticky="e", pady=(10,0))

        self.status = ttk.Label(root, text="Ready", anchor="w")
        self.status.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(8,0))

        for i in range(4):
            root.columnconfigure(i, weight=1)

    def _on_mode_change(self):
        mode = self.mode_var.get()
        self.load_btn.config(state=("normal" if mode == "csv" else "disabled"))
        self.reset()

    def _on_speed_change(self, _=None):
        self.speed = max(1.0, float(self.speed_var.get()))
        self.speed_label.config(text=f"x{int(self.speed)}")

    def _reset_display_initial(self):
        if self.mode_var.get() == "csv" and self.csv_rows:
            first = self.csv_rows[0]
            disp = first.get("display") or format_sci_mbar(float(first["pressure_mbar"]))
            self._set_display(disp)
        else:
            self._set_display(format_sci_mbar(self.model.params.P0))

    def _set_display(self, text):
        self.line2.config(text=text)

    def _tick(self):
        if not self.running:
            return
        self.sim_time += (self.speed * self.update_interval_ms / 1000.0)

        if self.mode_var.get() == "simulate":
            # Unlimited time: keep running; pressure will asymptotically
            # approach Pbase (8.6E-6 mbar) and stay fluctuating around it.
            p = self.model.pressure_at(self.sim_time)
            self._set_display(format_sci_mbar(p))
            self.status.config(text=f"Sim t={int(self.sim_time)} s")
        else:
            if not self.csv_rows:
                self._halt("No CSV loaded")
                return
            idx = int(round(self.sim_time))
            idx = max(0, min(idx, self.csv_len - 1))
            row = self.csv_rows[idx]
            disp = row.get("display") or format_sci_mbar(float(row["pressure_mbar"]))
            self._set_display(disp)
            self.status.config(text=f"CSV t={idx}s / {self.csv_len-1}s")
            if idx >= self.csv_len - 1:
                self._halt("CSV end")
                return

        self.after(self.update_interval_ms, self._tick)

    def _halt(self, msg):
        self.running = False
        self.start_btn.config(state="normal")
        self.pause_btn.config(state="disabled")
        self.status.config(text=msg)

    def start(self):
        if self.running:
            return
        self.running = True
        self.start_btn.config(state="disabled")
        self.pause_btn.config(state="normal")
        self.after(self.update_interval_ms, self._tick)

    def pause(self):
        if not self.running:
            return
        self._halt("Paused")

    def reset(self):
        self.running = False
        self.sim_time = 0.0
        self.model.reset()
        self._reset_display_initial()
        self.start_btn.config(state="normal")
        self.pause_btn.config(state="disabled")
        self.status.config(text="Ready")

    def export_csv(self):
        path = filedialog.asksaveasfilename(
            title="Save CSV",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
            initialfile="simulated_pumpdown_1h.csv",
        )
        if not path:
            return
        mdl = PumpDownModel(seed=random.randint(1, 10_000))
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["second", "pressure_mbar", "display"])
            for t in range(0, int(self.duration_s)+1):
                p = mdl.pressure_at(t)
                w.writerow([t, f"{p:.8e}", format_sci_mbar(p)])
        messagebox.showinfo("Export complete", f"Saved: {path}")

    def load_csv(self):
        path = filedialog.askopenfilename(
            title="Open CSV",
            filetypes=[("CSV files", "*.csv")],
        )
        if not path:
            return
        try:
            rows = []
            with open(path, "r", newline="") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    if "pressure_mbar" not in r and "display" not in r:
                        continue
                    rows.append(r)
            if not rows:
                raise ValueError("CSV has no usable rows")
            self.csv_rows = rows
            self.csv_len = len(rows)
            self.sim_time = 0.0
            self._reset_display_initial()
            self.status.config(text=f"Loaded CSV ({self.csv_len} rows)")
        except Exception as e:
            messagebox.showerror("Load CSV failed", str(e))

if __name__ == "__main__":
    App().mainloop()
