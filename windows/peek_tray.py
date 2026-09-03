#!/usr/bin/env python3
"""
peek_tray.py - PeekESP agent with a system-tray icon and a settings window.

Runs the same telemetry agent as peek_agent_win.py, but in the background with
a tray icon for control and a configuration window instead of command-line
flags. Settings are stored in %APPDATA%\\PeekESP\\config.json, which you are
equally welcome to edit by hand - the tray app reloads it on demand.

    python peek_tray.py

The window uses the Windows 11 acrylic backdrop where the OS provides it and
degrades to a flat dark theme where it does not, so it looks deliberate on
Windows 10 rather than broken.
"""

import ctypes
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import peek_agent_win as agent
import peek_config as cfgmod
import peek_pair as pair

# Frozen, the icon is bundled beside the exe's temp root; from source it lives
# in the repo's img/ folder. One file, no duplicated copy to drift.
if getattr(sys, "frozen", False):
    ICON = Path(sys._MEIPASS) / "logo.ico"
else:
    ICON = Path(__file__).resolve().parent.parent / "img" / "logo.ico"

# ---- palette, matching the device's own UI -------------------------------
BG = "#0B0F17"
CARD = "#121A28"
EDGE = "#1E2A3C"
TEXT = "#E6EDF7"
DIM = "#7C8CA3"
CYAN = "#00E5FF"
MAGENTA = "#FF2E7E"
GREEN = "#35F2A0"
AMBER = "#FFC145"
RED = "#FF4D6D"


# ==========================================================================
#  Windows 11 "liquid glass"
# ==========================================================================
def apply_glass(hwnd: int) -> bool:
    """
    Acrylic backdrop + dark title bar via DWM.

    DWMWA_SYSTEMBACKDROP_TYPE arrived in Windows 11 22H2; on anything older
    the call simply fails and we keep the flat dark theme. Returns whether
    the acrylic actually took, so the UI can say which it is rather than
    leaving you wondering if it is broken.
    """
    try:
        dwm = ctypes.WinDLL("dwmapi")
    except OSError:
        return False

    DWMWA_USE_IMMERSIVE_DARK_MODE = 20
    DWMWA_SYSTEMBACKDROP_TYPE = 38
    DWMSBT_TRANSIENTWINDOW = 3          # acrylic
    DWMWA_WINDOW_CORNER_PREFERENCE = 33
    DWMCP_ROUND = 2

    def seti(attr, value):
        v = ctypes.c_int(value)
        return dwm.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd), ctypes.c_uint(attr),
            ctypes.byref(v), ctypes.sizeof(v)) == 0

    seti(DWMWA_USE_IMMERSIVE_DARK_MODE, 1)
    seti(DWMWA_WINDOW_CORNER_PREFERENCE, DWMCP_ROUND)
    return seti(DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_TRANSIENTWINDOW)


# ==========================================================================
#  Agent supervision
# ==========================================================================
class Runner:
    """Owns the push loop and the local server, and can restart them when
    settings change without tearing down the whole process."""

    def __init__(self):
        self.cfg = cfgmod.load()
        self.status = "idle"
        self.detail = ""
        self.last_ok = 0.0
        self._stop = threading.Event()
        self._httpd = None
        self._lock = threading.Lock()
        threading.Thread(target=agent._sampler, daemon=True).start()

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        self._stop.clear()
        if self.cfg["mode"] in ("push", "both"):
            threading.Thread(target=self._push_forever, daemon=True).start()
        if self.cfg["mode"] in ("serve", "both"):
            threading.Thread(target=self._serve_forever, daemon=True).start()
        if self.cfg["mode"] == "serve":
            self._set("serving", f"port {self.cfg['serve_port']}")

    def stop(self):
        self._stop.set()
        with self._lock:
            if self._httpd is not None:
                try:
                    self._httpd.shutdown()
                    self._httpd.server_close()
                except Exception:
                    pass
                self._httpd = None
        self._set("stopped", "")

    def restart(self, cfg=None):
        if cfg is not None:
            self.cfg = cfgmod.validate(dict(cfg))
        self.stop()
        time.sleep(0.35)          # let the server socket close before rebinding
        self.start()

    # -- workers -----------------------------------------------------------
    def _set(self, status, detail):
        self.status, self.detail = status, detail

    def _push_forever(self):
        problems = cfgmod.problems(self.cfg)
        if problems:
            self._set("not configured", "; ".join(problems))
            return
        url, token = self.cfg["relay_url"], self.cfg["token"]
        fails = 0
        while not self._stop.is_set():
            code, msg = push_once(url, token)
            if code == 200:
                fails = 0
                self.last_ok = time.time()
                self._set("pushing", "last push OK")
            else:
                fails += 1
                self._set("error", msg)
            self._stop.wait(self.cfg["interval"])

    def _serve_forever(self):
        from http.server import ThreadingHTTPServer
        try:
            httpd = ThreadingHTTPServer(("0.0.0.0", self.cfg["serve_port"]), agent.Handler)
        except OSError as e:
            self._set("error", f"port {self.cfg['serve_port']}: {e}")
            return
        with self._lock:
            self._httpd = httpd
        try:
            httpd.serve_forever(poll_interval=0.4)
        except Exception:
            pass


def push_once(url, token, timeout=10):
    """(status_code, message). Used by both the loop and 'Test connection'."""
    import json
    body = json.dumps(agent.snapshot()).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + token,
                 "User-Agent": agent.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
            return 200, "OK"
    except urllib.error.HTTPError as e:
        hint = {401: "token rejected", 404: "wrong URL or stream",
                500: "worker secrets not set"}.get(e.code, "")
        return e.code, f"HTTP {e.code} {e.reason}" + (f" - {hint}" if hint else "")
    except Exception as e:
        return 0, str(e)


# ==========================================================================
#  Autostart via Task Scheduler (no admin, no service install)
# ==========================================================================
TASK = "PeekESP"


def _exe_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{Path(__file__).resolve()}"'


def autostart_enabled():
    r = subprocess.run(["schtasks", "/query", "/tn", TASK],
                       capture_output=True, text=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.returncode == 0


def set_autostart(on: bool):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if on:
        subprocess.run(["schtasks", "/create", "/f", "/tn", TASK,
                        "/sc", "onlogon", "/tr", _exe_command()],
                       capture_output=True, text=True, creationflags=flags)
    else:
        subprocess.run(["schtasks", "/delete", "/f", "/tn", TASK],
                       capture_output=True, text=True, creationflags=flags)
    return autostart_enabled()


# ==========================================================================
#  Settings window
# ==========================================================================
def open_settings(runner):
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("PeekESP")
    root.configure(bg=BG)
    root.resizable(False, False)
    try:
        root.iconbitmap(str(ICON))
    except Exception:
        pass

    root.update_idletasks()
    glass = apply_glass(int(root.wm_frame(), 16) if root.wm_frame() else 0)
    if not glass:
        # Without acrylic, a touch of uniform transparency still reads as glass
        # rather than looking like a plain grey box that failed at something.
        try:
            root.attributes("-alpha", 0.97)
        except Exception:
            pass

    pad = {"padx": 18}

    def label(parent, text, color=DIM, size=9, weight="normal", **kw):
        return tk.Label(parent, text=text, bg=parent["bg"], fg=color,
                        font=("Segoe UI", size, weight), **kw)

    def card(parent):
        f = tk.Frame(parent, bg=CARD, highlightbackground=EDGE,
                     highlightthickness=1, bd=0)
        return f

    def entry(parent, var, show=None, width=46):
        e = tk.Entry(parent, textvariable=var, bg=BG, fg=TEXT, width=width,
                     insertbackground=CYAN, relief="flat",
                     highlightbackground=EDGE, highlightcolor=CYAN,
                     highlightthickness=1, font=("Consolas", 9), show=show)
        return e

    # ---- header ----
    head = tk.Frame(root, bg=BG)
    head.pack(fill="x", pady=(16, 6), **pad)
    label(head, "PeekESP", CYAN, 15, "bold").pack(side="left")
    label(head, "  agent", DIM, 10).pack(side="left", pady=(5, 0))
    glass_lbl = label(head, "acrylic" if glass else "flat theme", DIM, 8)
    glass_lbl.pack(side="right", pady=(6, 0))

    # ---- status ----
    status_card = card(root)
    status_card.pack(fill="x", pady=6, **pad)
    status_dot = label(status_card, "●", AMBER, 11)
    status_dot.pack(side="left", padx=(12, 8), pady=10)
    status_text = label(status_card, "starting", TEXT, 9)
    status_text.pack(side="left", pady=10)

    cfg = dict(runner.cfg)
    v_mode = tk.StringVar(value=cfg["mode"])
    v_url = tk.StringVar(value=cfg["relay_url"])
    v_token = tk.StringVar(value=cfg["token"])
    v_interval = tk.StringVar(value=str(cfg["interval"]))
    v_port = tk.StringVar(value=str(cfg["serve_port"]))
    v_auto = tk.BooleanVar(value=autostart_enabled())
    v_show = tk.BooleanVar(value=False)

    body = tk.Frame(root, bg=BG)
    body.pack(fill="x", **pad)

    # ---- pairing: the only thing most people ever touch --------------------
    # Everything below this is derived from the code, so it is deliberately
    # first and everything else is labelled as automatic.
    label(body, "PAIR A DEVICE", DIM, 8).pack(anchor="w", pady=(8, 4))

    pair_card = card(body)
    pair_card.pack(fill="x")
    pin = tk.Frame(pair_card, bg=CARD)
    pin.pack(fill="x", padx=12, pady=10)

    label(pin, "Code shown on the device's screen", DIM, 8).pack(anchor="w")
    prow = tk.Frame(pin, bg=CARD)
    prow.pack(anchor="w", fill="x", pady=(3, 0))
    v_code = tk.StringVar(value=cfgmod.format_pair(cfg.get("pair_code", "")))
    code_entry = tk.Entry(prow, textvariable=v_code, bg=BG, fg=CYAN,
                          insertbackground=CYAN, relief="flat",
                          highlightbackground=EDGE, highlightcolor=CYAN,
                          highlightthickness=1, font=("Consolas", 13),
                          justify="center")
    code_entry.pack(side="left", fill="x", expand=True, ipady=5)

    def do_pair():
        raw = v_code.get()
        try:
            d = pair.derive(raw)
        except ValueError as e:
            set_status(RED, str(e))
            return
        base = v_base.get().strip().rstrip("/")
        if not base.lower().startswith(("http://", "https://")):
            set_status(RED, "relay must start with https://")
            return
        u = pair.urls(base, d["stream"])
        v_url.set(u["ingest"])
        v_token.set(d["push"])
        v_devtok.set(d["read"])
        v_mode.set("push")
        v_code.set(pair.format_code(d["code"]))
        set_status(GREEN, f"paired to stream {d['stream']} - press Save")

    tk.Button(prow, text="Pair", command=do_pair, bg=CYAN, fg=BG,
              activebackground=CYAN, activeforeground=BG, relief="flat", bd=0,
              font=("Segoe UI", 9, "bold"), padx=14, pady=5,
              cursor="hand2").pack(side="left", padx=(8, 0))

    # The relay the code is resolved against. Baked in so nobody has to type
    # it, but visible and editable here - otherwise anyone running their own
    # Worker would have to hand-edit config.json to point at it, and everyone
    # else would be looking at an empty URL field with no idea what it will use.
    label(pin, "Relay", DIM, 8).pack(anchor="w", pady=(9, 0))
    brow = tk.Frame(pin, bg=CARD)
    brow.pack(anchor="w", fill="x", pady=(2, 0))
    v_base = tk.StringVar(value=cfg.get("relay_base", cfgmod.DEFAULT_RELAY_BASE))
    tk.Entry(brow, textvariable=v_base, bg=BG, fg=TEXT, insertbackground=CYAN,
             relief="flat", highlightbackground=EDGE, highlightcolor=CYAN,
             highlightthickness=1, font=("Consolas", 8)
             ).pack(side="left", fill="x", expand=True, ipady=3)

    def reset_base():
        v_base.set(cfgmod.DEFAULT_RELAY_BASE)
        set_status(AMBER, "relay reset to the default - press Pair again")

    tk.Button(brow, text="Default", command=reset_base, bg=EDGE, fg=TEXT,
              activebackground=CYAN, activeforeground=BG, relief="flat", bd=0,
              font=("Segoe UI", 8), padx=8, pady=3,
              cursor="hand2").pack(side="left", padx=(6, 0))

    label(pin, "Everything below is filled in from the code and this relay.",
          DIM, 8).pack(anchor="w", pady=(7, 0))

    # ---- mode ----
    label(body, "HOW THIS PC REPORTS", DIM, 8).pack(anchor="w", pady=(14, 4))
    modes = tk.Frame(body, bg=BG)
    modes.pack(anchor="w", fill="x")
    for val, txt in (("push", "Push to relay"),
                     ("serve", "Serve on LAN"),
                     ("both", "Both")):
        tk.Radiobutton(modes, text=txt, value=val, variable=v_mode,
                       bg=BG, fg=TEXT, selectcolor=CARD, activebackground=BG,
                       activeforeground=CYAN, font=("Segoe UI", 9),
                       highlightthickness=0, bd=0).pack(side="left", padx=(0, 14))

    label(body, "RELAY  (set by pairing - edit only if you know why)", DIM, 8).pack(anchor="w", pady=(12, 4))
    label(body, "Ingest URL", DIM, 8).pack(anchor="w")
    entry(body, v_url).pack(anchor="w", fill="x", pady=(2, 6), ipady=4)

    label(body, "Push token", DIM, 8).pack(anchor="w")
    tok_row = tk.Frame(body, bg=BG)
    tok_row.pack(anchor="w", fill="x", pady=(2, 6))
    tok = entry(tok_row, v_token, show="•")
    tok.pack(side="left", fill="x", expand=True, ipady=4)

    def toggle_show():
        tok.config(show="" if v_show.get() else "•")

    tk.Checkbutton(tok_row, text="show", variable=v_show, command=toggle_show,
                   bg=BG, fg=DIM, selectcolor=CARD, activebackground=BG,
                   activeforeground=CYAN, font=("Segoe UI", 8),
                   highlightthickness=0, bd=0).pack(side="left", padx=(8, 0))

    nums = tk.Frame(body, bg=BG)
    nums.pack(anchor="w", fill="x", pady=(4, 0))
    left = tk.Frame(nums, bg=BG); left.pack(side="left")
    label(left, "Interval (s)", DIM, 8).pack(anchor="w")
    entry(left, v_interval, width=10).pack(anchor="w", ipady=3)
    right = tk.Frame(nums, bg=BG); right.pack(side="left", padx=(24, 0))
    label(right, "Serve port", DIM, 8).pack(anchor="w")
    entry(right, v_port, width=10).pack(anchor="w", ipady=3)

    # ---- what to type into the ESP32 ----
    # The device needs the READ token and the /telemetry URL; this agent uses
    # the PUSH token and /ingest. Keeping the pair visible in one place is the
    # difference between a two-minute setup and hunting for a value you wrote
    # down somewhere during the Cloudflare step.
    label(body, "ON THE DEVICE, AFTER FLASHING", DIM, 8).pack(anchor="w", pady=(16, 4))

    dev = card(body)
    dev.pack(fill="x")
    inner = tk.Frame(dev, bg=CARD)
    inner.pack(fill="x", padx=12, pady=10)

    label(inner, "Worker URL", DIM, 8).pack(anchor="w")
    v_devurl = tk.StringVar(value=cfgmod.device_url(cfg["relay_url"]))
    dev_url_lbl = tk.Label(inner, textvariable=v_devurl, bg=CARD, fg=CYAN,
                           font=("Consolas", 8), anchor="w", justify="left",
                           wraplength=380)
    dev_url_lbl.pack(anchor="w", fill="x", pady=(1, 8))

    label(inner, "Read token", DIM, 8).pack(anchor="w")
    v_devtok = tk.StringVar(value=cfg.get("device_token", ""))
    devrow = tk.Frame(inner, bg=CARD)
    devrow.pack(anchor="w", fill="x", pady=(1, 0))
    dev_tok = tk.Entry(devrow, textvariable=v_devtok, bg=BG, fg=MAGENTA,
                       insertbackground=CYAN, relief="flat",
                       highlightbackground=EDGE, highlightcolor=CYAN,
                       highlightthickness=1, font=("Consolas", 8))
    dev_tok.pack(side="left", fill="x", expand=True, ipady=3)

    def gen_device_token():
        v_devtok.set(cfgmod.new_token())
        set_status(AMBER, "generated - set it as READ_TOKEN in Cloudflare too")

    def copy_device():
        url, tok = v_devurl.get(), v_devtok.get()
        if not url or not tok:
            set_status(AMBER, "nothing to copy yet")
            return
        root.clipboard_clear()
        root.clipboard_append(f"Worker URL: {url}\nRead token: {tok}")
        root.update()
        set_status(GREEN, "device settings copied to clipboard")

    tk.Button(devrow, text="New", command=gen_device_token, bg=EDGE, fg=TEXT,
              activebackground=CYAN, activeforeground=BG, relief="flat", bd=0,
              font=("Segoe UI", 8), padx=8, pady=3,
              cursor="hand2").pack(side="left", padx=(6, 0))
    tk.Button(devrow, text="Copy", command=copy_device, bg=EDGE, fg=TEXT,
              activebackground=CYAN, activeforeground=BG, relief="flat", bd=0,
              font=("Segoe UI", 8), padx=8, pady=3,
              cursor="hand2").pack(side="left", padx=(4, 0))

    label(inner, "Enter these in the device's setup portal (192.168.4.1).",
          DIM, 8).pack(anchor="w", pady=(8, 0))

    # Keep the device URL in step with the ingest URL as it is typed.
    def sync_device_url(*_):
        v_devurl.set(cfgmod.device_url(v_url.get()) or "—")
    v_url.trace_add("write", sync_device_url)
    sync_device_url()

    tk.Checkbutton(body, text="Start automatically when I sign in",
                   variable=v_auto, bg=BG, fg=TEXT, selectcolor=CARD,
                   activebackground=BG, activeforeground=CYAN,
                   font=("Segoe UI", 9), highlightthickness=0,
                   bd=0).pack(anchor="w", pady=(14, 2))

    hint = label(body, f"config file: {cfgmod.config_path()}", DIM, 8)
    hint.pack(anchor="w", pady=(6, 0))

    # ---- buttons ----
    def button(parent, text, cmd, accent=False):
        return tk.Button(parent, text=text, command=cmd,
                         bg=CYAN if accent else CARD,
                         fg=BG if accent else TEXT,
                         activebackground=CYAN if accent else EDGE,
                         activeforeground=BG if accent else CYAN,
                         relief="flat", bd=0, font=("Segoe UI", 9, "bold"),
                         padx=16, pady=7, cursor="hand2")

    def collect():
        c = dict(runner.cfg)
        c.update({"mode": v_mode.get(),
                  "pair_code": pair.normalise(v_code.get()),
                  "relay_base": v_base.get().strip().rstrip("/"),
                  "relay_url": v_url.get().strip(),
                  "token": v_token.get().strip(),
                  "device_token": v_devtok.get().strip(),
                  "interval": v_interval.get(),
                  "serve_port": v_port.get(), "autostart": v_auto.get()})
        return cfgmod.validate(c)

    def set_status(color, text):
        status_dot.config(fg=color)
        status_text.config(text=text)

    def do_test():
        c = collect()
        probs = cfgmod.problems(c)
        if probs:
            set_status(AMBER, "; ".join(probs))
            return
        set_status(AMBER, "testing...")
        root.update_idletasks()

        def work():
            code, msg = push_once(c["relay_url"], c["token"])
            root.after(0, lambda: set_status(GREEN if code == 200 else RED,
                                             "relay accepted the push" if code == 200 else msg))
        threading.Thread(target=work, daemon=True).start()

    def do_save():
        c = collect()
        try:
            cfgmod.save(c)
        except OSError as e:
            set_status(RED, f"could not save: {e}")
            return
        if v_auto.get() != autostart_enabled():
            set_autostart(v_auto.get())
        runner.restart(c)
        probs = cfgmod.problems(c)
        set_status(AMBER if probs else GREEN,
                   "; ".join(probs) if probs else "saved and restarted")

    def do_open_folder():
        d = cfgmod.config_dir()
        d.mkdir(parents=True, exist_ok=True)
        os.startfile(str(d))

    btns = tk.Frame(root, bg=BG)
    btns.pack(fill="x", pady=(14, 18), **pad)
    button(btns, "Save", do_save, accent=True).pack(side="right")
    button(btns, "Test connection", do_test).pack(side="right", padx=(0, 8))
    button(btns, "Open config folder", do_open_folder).pack(side="left")

    # live status from the runner
    def tick():
        colors = {"pushing": GREEN, "serving": GREEN, "idle": DIM,
                  "stopped": DIM, "error": RED, "not configured": AMBER}
        if status_text.cget("text") in ("starting",) or runner.status in colors:
            txt = runner.status + (f" - {runner.detail}" if runner.detail else "")
            if not getattr(root, "_sticky", False):
                set_status(colors.get(runner.status, DIM), txt)
        root.after(1000, tick)

    tick()
    root.eval("tk::PlaceWindow . center")
    root.mainloop()


# ==========================================================================
#  Tray
# ==========================================================================
def main():
    import pystray
    from PIL import Image

    runner = Runner()
    runner.start()

    try:
        image = Image.open(ICON)
    except Exception:
        image = Image.new("RGBA", (32, 32), (0, 229, 255, 255))

    ui_lock = threading.Lock()

    def settings(icon=None, item=None):
        # tkinter must own its thread; opening twice would deadlock.
        if not ui_lock.acquire(blocking=False):
            return

        def run():
            try:
                open_settings(runner)
            finally:
                ui_lock.release()
        threading.Thread(target=run, daemon=True).start()

    def status_text(item=None):
        s = runner.status + (f" - {runner.detail}" if runner.detail else "")
        return s[:60]

    def toggle(icon=None, item=None):
        if runner.status in ("stopped", "idle"):
            runner.start()
        else:
            runner.stop()

    def reload_cfg(icon=None, item=None):
        runner.restart(cfgmod.load())

    def quit_app(icon, item=None):
        runner.stop()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Settings...", settings, default=True),
        pystray.MenuItem("Reload config file", reload_cfg),
        pystray.MenuItem("Start / stop", toggle),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_app),
    )

    pystray.Icon("PeekESP", image, "PeekESP agent", menu).run()


if __name__ == "__main__":
    main()
