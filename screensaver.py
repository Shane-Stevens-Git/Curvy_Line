"""Flowing Curve Generator -- screensaver (Windows only).

Reuses wallpaper_engine.py's curve generation, daily preset rotation, and
crawl-animation rendering (load_or_generate_path/current_preset/
crawl_bands/build_gradient_palette -- the exact same pipeline, same
wallpaper_config.json, same wallpaper_cache/ curve cache), but as an
actual Windows screensaver instead of a live desktop wallpaper: fullscreen
and on top of everything, and -- the entire point of a screensaver --
closes itself instantly on the first real mouse movement, a click, or a
keypress. That's the opposite of wallpaper_engine.py's WallpaperWindow,
which spends real effort making itself invisible to input (WS_EX_NOACTIVATE,
_make_input_safe, the activation guard, WorkerW reparenting) specifically
so it never intercepts any of that -- so this is its own separate, much
simpler window class, not a WallpaperWindow subclass: none of that
input-safety machinery is reused, because a screensaver needs the exact
opposite behavior from it.

Since a screensaver only shows while the user's away, it also doesn't try
to advance or save the daily rotation itself (see current_preset/
_is_new_day in wallpaper_engine.py) -- it's a read-only viewer of whatever
wallpaper_config.json currently says, sharing state with -- but never
writing to -- the live wallpaper engine, so there's no risk of the two
racing to roll over the same rotation_index. In practice this means: if
the live wallpaper (or a previous screensaver run) already rolled over
today's preset, the screensaver shows that; if nothing has rolled it over
yet today, it shows yesterday's preset's curve, freshly generated for
today's cache slot -- never literally wrong, just not independently
responsible for advancing the schedule.

Windows launches a .scr the same way it launches any other .exe, always
with one of a few standard command-line flags:
  (no args)      Default/preview mode -- treated the same as /s here.
  /s             Run full-screen -- the real "screensaver is now active" mode.
  /c             Show a configuration dialog. Reuses gui.py's Configure
                 Wallpaper dialog (same presets/settings, same config file)
                 rather than building a second, separate settings UI.
  /p <hwnd>      Preview mode: render embedded inside the small monitor
                 thumbnail in Windows' Screen Saver Settings dialog, hosted
                 in the window handle Windows passes as <hwnd>.

Packaging this as an actual .scr (a renamed .exe, built with PyInstaller)
and installing it via the registry
(HKEY_CURRENT_USER\\Control Panel\\Desktop, SCRNSAVE.EXE) needs to happen
on a real Windows machine -- see build_screensaver.bat/
install_screensaver.bat, and this file's own "hands-on, not blind" note
in FUTURE_IDEAS.md item #5.

Known limitation once frozen: /c's dialog is gui.py's real Configure
Wallpaper dialog, and its "Start Wallpaper" button launches
wallpaper_engine.py as a *separate* `sys.executable wallpaper_engine.py
--monitor <key>` subprocess (see gui.py's WALLPAPER_ENGINE_PATH). Inside
the frozen screensaver.exe, sys.executable is the screensaver .scr itself,
not a python.exe that can be handed a .py path -- so clicking "Start
Wallpaper" from Settings while running as the installed screensaver isn't
expected to work. Viewing/editing presets and settings from there is fine
(it's the same wallpaper_config.json either way); to actually start the
live wallpaper, use the normal desktop app (run.bat / the unfrozen
gui.py) instead -- the screensaver will pick up whatever that starts,
same as always, since it only ever reads that shared config.

Not Windows? Runs anyway in a normal (still animated, still closes on
input) window, same as wallpaper_engine.py off-Windows -- useful for
testing the rendering/idle-close logic without a real screensaver host.
"""
import ctypes
import sys


def _make_dpi_aware():
    """Same call, same reasoning, as wallpaper_engine.py's
    _make_dpi_aware() -- must run before tkinter is imported. Duplicated
    here (rather than importing wallpaper_engine first and calling its
    copy) specifically so *this* file controls import order from its own
    first line; wallpaper_engine.py is still imported for everything else
    a few lines down, well before any Tk() is created."""
    if sys.platform != "win32":
        return "not windows"
    try:
        ok = ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        if ok:
            return "per-monitor-v2"
    except (AttributeError, OSError):
        pass
    try:
        ok = ctypes.windll.user32.SetProcessDPIAware()
        return "system-dpi-aware" if ok else "SetProcessDPIAware returned failure"
    except Exception as exc:  # noqa: BLE001 -- diagnostic only
        return f"both calls failed: {exc!r}"


DPI_AWARENESS_RESULT = _make_dpi_aware()

import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path

from wallpaper_engine import (
    load_config, current_preset, load_or_generate_path, crawl_bands,
    build_gradient_palette, DEFAULT_PRESETS, _virtual_screen_bounds,
)

# See wallpaper_engine.py's own BASE_DIR comment: once this script is
# bundled into a .scr by PyInstaller (build_screensaver.bat), __file__ no
# longer points at the real project folder, so this falls back to the
# built executable's own directory when frozen. The debug log below lands
# next to the .scr in that case, which is also where install_screensaver.bat
# expects to find it.
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent
DEBUG_LOG_PATH = BASE_DIR / "screensaver_debug.log"

# Same smoothness-vs-cost tradeoff as wallpaper_engine.py's FRAME_MS --
# see that constant's comment. A screensaver only runs while the user is
# away, so there's less reason to be as conservative as the wallpaper (no
# battery-throttling here, for instance -- a screensaver is a short-lived,
# occasional cost, not an all-day one), but no reason to redraw faster
# than the wallpaper does either.
FRAME_MS = 50

# Windows (and X11/most other window systems) sends at least one <Motion>
# event immediately on activation even with the mouse sitting perfectly
# still -- a well-known screensaver-development gotcha. Comparing against
# the cursor's position *when this screensaver started* rather than
# reacting to the raw first event, and requiring it to have moved more
# than this many pixels, avoids closing itself the instant it opens.
IDLE_MOVE_THRESHOLD_PX = 8

_debug_log_started = False


def _debug_log(msg):
    """Same truncate-once-then-append pattern as wallpaper_engine.py's
    _debug_log -- a separate file/flag though, since this is a distinct
    process with its own short lifetime, not something that shares
    wallpaper_engine.py's own (possibly per-monitor-suffixed) log."""
    global _debug_log_started
    try:
        mode = "a" if _debug_log_started else "w"
        _debug_log_started = True
        with open(DEBUG_LOG_PATH, mode, encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat(timespec='milliseconds')}] {msg}\n")
    except OSError:
        pass


class ScreensaverWindow:
    """The real /s (and default) mode: fullscreen across every monitor
    (see _virtual_screen_bounds(..., 'all')), topmost, closes on the first
    real mouse move/click/keypress. Reuses the exact same crawl-drawing
    logic as WallpaperWindow.tick() (same crawl_bands/build_gradient_palette
    calls, same math) but trimmed down -- no reparent-health watchdog, no
    battery throttling, no regen-request polling, no day-rollover writes
    (see the module docstring) -- since none of those matter for a window
    that only lives for as long as the user is away from the keyboard."""

    def __init__(self):
        self.cfg = load_config()
        self.preset = current_preset(self.cfg)

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        try:
            self.root.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.x, self.y, self.w, self.h = _virtual_screen_bounds(self.root, "all")
        self.root.geometry(f"{self.w}x{self.h}+{self.x}+{self.y}")
        bg = self.cfg.get("bg_color", "#0b1220")
        self.root.configure(bg=bg)
        try:
            self.root.config(cursor="none")  # a screensaver hides the cursor while active
        except tk.TclError:
            pass

        self.canvas = tk.Canvas(self.root, width=self.w, height=self.h,
                                 highlightthickness=0, bd=0, bg=bg)
        self.canvas.pack(fill="both", expand=True)

        self.path = None
        self.display_scale = 1.0
        self._phase = 0.0
        self._spinner_angle = 0.0
        self._generating = False
        self._result_queue = queue.Queue()

        # See IDLE_MOVE_THRESHOLD_PX -- recorded once, here, before any
        # bindings are installed, so the very first (often spurious)
        # <Motion> event has something real to compare against.
        try:
            self._start_x, self._start_y = self.root.winfo_pointerxy()
        except tk.TclError:
            self._start_x, self._start_y = 0, 0
        self.root.bind("<Motion>", self._on_motion)
        self.root.bind("<Button>", lambda _e: self._close("mouse click"))
        self.root.bind("<Key>", lambda _e: self._close("keypress"))
        self.root.focus_force()

        self._last_tick = time.time()
        self._start_generation()
        _debug_log(f"screensaver window up: {self.w}x{self.h}+{self.x}+{self.y}, "
                   f"preset={self.preset.get('fill_shape')}")

    def _on_motion(self, event):
        dx = event.x_root - self._start_x
        dy = event.y_root - self._start_y
        if dx * dx + dy * dy > IDLE_MOVE_THRESHOLD_PX * IDLE_MOVE_THRESHOLD_PX:
            self._close("mouse movement")

    def _close(self, reason):
        _debug_log(f"closing: {reason}")
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _start_generation(self):
        """Unlike WallpaperWindow._start_generation, never advances or
        saves rotation_index/last_update -- see the module docstring for
        why. Just generates (or loads a same-day cached) curve for
        whatever current_preset(self.cfg) already says right now."""
        if self._generating or self.path is not None:
            return
        self._generating = True
        cfg_snapshot = dict(self.cfg)
        w, h = self.w, self.h

        def worker():
            try:
                path, display_scale = load_or_generate_path(cfg_snapshot, w, h)
                self._result_queue.put(("ok", path, display_scale))
            except Exception as exc:  # noqa: BLE001 -- report, don't crash the daemon thread
                self._result_queue.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_generation(self):
        try:
            result = self._result_queue.get_nowait()
        except queue.Empty:
            return
        self._generating = False
        if result[0] == "error":
            _debug_log(f"generation failed: {result[1]}")
            return
        _, path, display_scale = result
        self.path = path
        self.display_scale = display_scale
        self._phase = 0.0

    def _draw_spinner(self, cx, cy, radius, width, color="#ffffff"):
        self.canvas.create_arc(
            cx - radius, cy - radius, cx + radius, cy + radius,
            start=self._spinner_angle, extent=100,
            style=tk.ARC, outline=color, width=max(1.0, width))

    def tick(self):
        self._poll_generation()
        now = time.time()
        dt = max(0.0, min(0.25, now - self._last_tick))
        self._last_tick = now
        self._spinner_angle = (self._spinner_angle - 220.0 * dt) % 360.0

        self.canvas.delete("all")
        if self.path is not None:
            crawler_len = max(1.0, float(self.preset.get("crawler_size", 40.0)))
            gap_len = max(0.0, float(self.preset.get("gap", 20.0)))
            speed = max(0.0, float(self.preset.get("speed", 200.0)))
            colors = self.preset.get("colors", DEFAULT_PRESETS[0]["colors"])
            blend_steps = max(1, int(self.preset.get("blend_steps", 8.0)))
            palette = build_gradient_palette(colors, steps=blend_steps)
            period = len(palette) * (crawler_len + gap_len)
            self._phase = (self._phase + speed * dt) % period if period > 0 else 0.0

            stroke = self.cfg.get("stroke", 6.0) * self.display_scale
            for color_idx, pts in crawl_bands(self.path, crawler_len, gap_len, self._phase, n_colors=len(palette)):
                coords = (pts * self.display_scale).flatten().tolist()
                self.canvas.create_line(*coords, fill=palette[color_idx],
                                         width=max(1.0, stroke),
                                         capstyle=tk.ROUND, joinstyle=tk.ROUND)
        else:
            cx, cy = self.w / 2, self.h / 2
            radius = max(24, int(36 * self.display_scale))
            self._draw_spinner(cx, cy, radius, max(2.0, 3.0 * self.display_scale))
            self.canvas.create_text(
                cx, cy + radius + max(14, int(20 * self.display_scale)),
                text="Generating your curve... this can take a few minutes",
                fill="#ffffff",
                font=("Segoe UI", max(10, int(14 * self.display_scale))),
                anchor="n")

        self.root.after(FRAME_MS, self.tick)

    def run(self):
        self.tick()
        self.root.mainloop()


def run_fullscreen():
    _debug_log(f"=== screensaver.py starting fullscreen, pid={sys.argv}, "
               f"DPI awareness: {DPI_AWARENESS_RESULT} ===")
    window = ScreensaverWindow()
    window.run()


def run_config():
    """/c -- reuses gui.py's own Configure Wallpaper dialog wholesale
    (same wallpaper_config.json, same preset editor) rather than building
    a second, separate settings UI just for the screensaver -- see the
    module docstring."""
    _debug_log("=== screensaver.py starting in config mode (/c) ===")
    import gui
    app = gui.CurveApp()
    app._open_wallpaper_dialog()
    app.mainloop()


def run_preview(hwnd_arg):
    """/p <hwnd> -- embeds a small live preview inside the thumbnail
    monitor icon in Windows' Screen Saver Settings dialog. Best-effort:
    Windows-only (SetParent/GetClientRect), and generates at the tiny
    thumbnail resolution synchronously rather than on a background thread
    -- small enough that it should be fast, unlike a real screensaver run,
    and there's no sensible "loading spinner" to show in a thumbnail this
    small anyway. Any failure here (bad hwnd, non-Windows, an API call
    failing) exits quietly rather than crashing or popping an error --
    Windows shows a blank thumbnail rather than nothing at all either way,
    and this is only ever a decorative nicety, never the real screensaver
    run that actually matters."""
    _debug_log(f"=== screensaver.py starting preview mode (/p {hwnd_arg}) ===")
    if sys.platform != "win32":
        return
    try:
        target_hwnd = int(str(hwnd_arg).lstrip(":"))
    except ValueError:
        _debug_log(f"preview: could not parse hwnd from {hwnd_arg!r}")
        return

    try:
        from ctypes import wintypes
        user32 = ctypes.windll.user32

        root = tk.Tk()
        root.overrideredirect(True)
        root.update_idletasks()
        hwnd = root.winfo_id()

        user32.SetParent.restype = wintypes.HWND
        user32.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
        user32.SetParent(hwnd, target_hwnd)

        GWL_STYLE = -16
        WS_CHILD = 0x40000000
        WS_VISIBLE = 0x10000000
        user32.GetWindowLongW.restype = ctypes.c_long
        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.SetWindowLongW.restype = ctypes.c_long
        user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
        style = user32.GetWindowLongW(hwnd, GWL_STYLE)
        user32.SetWindowLongW(hwnd, GWL_STYLE, style | WS_CHILD | WS_VISIBLE)

        rect = wintypes.RECT()
        user32.GetClientRect.restype = wintypes.BOOL
        user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetClientRect(wintypes.HWND(target_hwnd), ctypes.byref(rect))
        w = max(1, rect.right - rect.left)
        h = max(1, rect.bottom - rect.top)
        root.geometry(f"{w}x{h}+0+0")

        cfg = load_config()
        preset = current_preset(cfg)
        bg = cfg.get("bg_color", "#0b1220")
        root.configure(bg=bg)
        canvas = tk.Canvas(root, width=w, height=h, highlightthickness=0, bd=0, bg=bg)
        canvas.pack(fill="both", expand=True)

        path, display_scale = load_or_generate_path(dict(cfg), w, h)
        colors = preset.get("colors", DEFAULT_PRESETS[0]["colors"])
        blend_steps = max(1, int(preset.get("blend_steps", 8.0)))
        palette = build_gradient_palette(colors, steps=blend_steps)
        crawler_len = max(1.0, float(preset.get("crawler_size", 40.0)))
        gap_len = max(0.0, float(preset.get("gap", 20.0)))
        speed = max(0.0, float(preset.get("speed", 200.0)))
        stroke = cfg.get("stroke", 6.0) * display_scale
        period = len(palette) * (crawler_len + gap_len)
        state = {"phase": 0.0, "last": time.time()}

        def preview_tick():
            now = time.time()
            dt = max(0.0, min(0.25, now - state["last"]))
            state["last"] = now
            state["phase"] = (state["phase"] + speed * dt) % period if period > 0 else 0.0
            canvas.delete("all")
            for color_idx, pts in crawl_bands(path, crawler_len, gap_len, state["phase"], n_colors=len(palette)):
                coords = (pts * display_scale).flatten().tolist()
                canvas.create_line(*coords, fill=palette[color_idx],
                                    width=max(1.0, stroke), capstyle=tk.ROUND, joinstyle=tk.ROUND)
            root.after(FRAME_MS, preview_tick)

        preview_tick()
        root.mainloop()
    except Exception as exc:  # noqa: BLE001 -- decorative preview only, must never crash/hang the settings dialog
        _debug_log(f"preview mode FAILED: {exc!r}")


def main():
    args = sys.argv[1:]
    # Windows passes flags case-loosely and sometimes glued to a leading
    # '/' or '-' depending on how the .scr was invoked -- normalize before
    # matching rather than requiring an exact '/c'/'/s'/'/p'.
    normalized = [a.lower().lstrip("/-") for a in args]

    if "c" in normalized or "config" in normalized:
        run_config()
        return
    if "p" in normalized:
        idx = normalized.index("p")
        hwnd_arg = args[idx + 1] if idx + 1 < len(args) else None
        if hwnd_arg is not None:
            run_preview(hwnd_arg)
            return
        _debug_log("preview mode (/p) requested but no hwnd argument was given -- falling back to fullscreen")
    # '/s', no args at all, or anything else unrecognized: run for real.
    run_fullscreen()


if __name__ == "__main__":
    main()
