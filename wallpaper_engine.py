"""Flowing Curve Generator -- live desktop wallpaper engine (Windows only).

Runs a continuously animated "chasing lights" curve behind the desktop
icons, using the exact same crawl_bands() pattern-logic that gui.py's
"Color crawl" preview uses -- organic_curve.py's crawl_bands() and
generate() were deliberately written as pure functions (no Tkinter, no
file I/O) specifically so this file could reuse them unchanged.

What it does, at a glance:
  1. Generates (or loads a same-day cached) curve for a preset each day,
     picking the next preset from wallpaper_config.json in rotation.
  2. Opens a borderless Tkinter window and, on Windows, reparents it behind
     the desktop icons via the well-known (if undocumented) WorkerW trick,
     so it renders like a real live wallpaper instead of floating on top
     of everything.
  3. Redraws the crawl animation on a timer, forever, and silently starts
     the next day's preset when the date rolls over -- no restart needed.

Not Windows? The WorkerW reparenting step is skipped and you get a normal
(still animated) window instead, which is useful for testing the rendering
and rotation logic on macOS/Linux even though it isn't a real wallpaper
there.

Two settings worth knowing about in wallpaper_config.json (no GUI for
these yet -- see DEFAULT_CONFIG below for every key):
  - "edge": how far the curve is inset from the screen's border. Small by
    default so the pattern fills essentially the whole monitor.
  - "monitor_mode": "primary" (default, just your main monitor) or "all"
    (stretches one curve across every monitor combined -- looks stretched
    rather than uniform per screen if your monitors don't match).

Run directly with:  python wallpaper_engine.py
(same .venv / dependencies as gui.py -- see run.bat/run.sh)
"""
import ctypes
import json
import random
import sys
import time
import tkinter as tk
from datetime import date
from pathlib import Path

import numpy as np

from organic_curve import generate, crawl_bands, GenerationError, FILL_SHAPES

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "wallpaper_config.json"
CACHE_DIR = BASE_DIR / "wallpaper_cache"

# Curves are generated at this resolution (on the long edge) no matter how
# big the real screen is, then scaled up when drawing -- redrawing tens or
# hundreds of thousands of points every frame at native 4K+ resolution
# would make the animation stutter for no visible benefit; the curve's
# organic wobble looks the same either way once scaled up.
MAX_GEN_DIMENSION = 1600

# A background wallpaper does not need gui.py's 30ms (~33fps) responsiveness
# -- 50ms (~20fps) is smooth enough for a chasing-lights effect and lighter
# on the CPU/GPU for something meant to run all day, every day.
FRAME_MS = 50

# fill_shape is 'square' for every default preset -- for a wallpaper (unlike
# the GUI's boxed preview) the whole point is to cover the monitor
# edge-to-edge, and 'square' is the shape that means "the whole inset
# canvas" per organic_curve.generate() -- a full rectangle, not a hexagon/
# circle/etc inscribed inside it with empty corners around it. Combined
# with the small `edge` config value below, this fills essentially the
# entire screen.
DEFAULT_PRESETS = [
    {
        "fill_shape": "square",
        "colors": ["#ff595e", "#ffd166", "#7fe7c4"],
        "crawler_size": 60.0,
        "gap": 30.0,
        "speed": 220.0,
    },
    {
        "fill_shape": "square",
        "colors": ["#5eb0ff", "#c792ff", "#7fe7c4"],
        "crawler_size": 45.0,
        "gap": 22.0,
        "speed": 260.0,
    },
    {
        "fill_shape": "square",
        "colors": ["#ffd166", "#ff8fa3", "#5eb0ff"],
        "crawler_size": 70.0,
        "gap": 35.0,
        "speed": 180.0,
    },
]

DEFAULT_CONFIG = {
    "enabled": True,
    "bg_color": "#0b1220",
    "stroke": 6.0,
    # How far the curve is inset from the screen's edge, in generated-curve
    # px -- small on purpose (organic_curve.generate() requires > 0) so the
    # pattern reaches essentially edge-to-edge instead of leaving a visible
    # border, which is what a wallpaper needs but the GUI's boxed preview
    # doesn't.
    "edge": 4.0,
    # 'primary' fills just your main monitor; 'all' stretches one curve
    # across the combined bounding box of every monitor. See
    # _virtual_screen_bounds() -- a real gui.py toggle for this is coming,
    # for now edit this value directly in wallpaper_config.json.
    "monitor_mode": "primary",
    "presets": DEFAULT_PRESETS,
    "rotation_index": 0,
    "last_update": None,
}


# --- config -----------------------------------------------------------

def load_config():
    """Read wallpaper_config.json, filling in any missing keys from
    DEFAULT_CONFIG so a partially hand-edited file (or one from an older
    version of this script) still works. Falls back to the full default
    config if the file is missing, empty, or not valid JSON, so a fresh
    install (or a corrupted file) never crashes the engine."""
    cfg = dict(DEFAULT_CONFIG)
    cfg["presets"] = [dict(p) for p in DEFAULT_PRESETS]
    if CONFIG_PATH.exists():
        try:
            on_disk = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(on_disk, dict):
                cfg.update(on_disk)
        except (json.JSONDecodeError, OSError):
            pass
    if not cfg.get("presets"):
        cfg["presets"] = [dict(p) for p in DEFAULT_PRESETS]
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def current_preset(cfg):
    presets = cfg["presets"]
    idx = cfg.get("rotation_index", 0) % len(presets)
    return presets[idx]


def advance_if_new_day(cfg):
    """If today's date differs from the config's last_update, move the
    rotation forward one preset (wrapping) and persist immediately, so the
    new choice survives a crash/reboot before the next check. Returns True
    if the day (and therefore the preset) changed."""
    today = date.today().isoformat()
    if cfg.get("last_update") == today:
        return False
    presets = cfg["presets"]
    if cfg.get("last_update") is not None:  # don't skip preset 0 on first-ever run
        cfg["rotation_index"] = (cfg.get("rotation_index", 0) + 1) % len(presets)
    cfg["last_update"] = today
    save_config(cfg)
    return True


# --- curve generation / per-day caching --------------------------------

def _gen_dimensions(screen_w, screen_h):
    """Scale (screen_w, screen_h) down so its long edge is MAX_GEN_DIMENSION,
    preserving aspect ratio, and return (gen_w, gen_h, display_scale) where
    display_scale maps generated-curve coordinates back up to screen pixels."""
    long_edge = max(screen_w, screen_h)
    if long_edge <= MAX_GEN_DIMENSION:
        return screen_w, screen_h, 1.0
    display_scale = long_edge / MAX_GEN_DIMENSION
    gen_w = max(1, round(screen_w / display_scale))
    gen_h = max(1, round(screen_h / display_scale))
    return gen_w, gen_h, display_scale


def _cache_path(today, rotation_index, gen_w, gen_h, edge):
    return CACHE_DIR / f"{today}_{rotation_index}_{gen_w}x{gen_h}_{edge:g}.npy"


def _prune_stale_cache(today):
    if not CACHE_DIR.exists():
        return
    for f in CACHE_DIR.glob("*.npy"):
        if not f.name.startswith(today):
            try:
                f.unlink()
            except OSError:
                pass


def load_or_generate_path(cfg, screen_w, screen_h):
    """Return (path, display_scale) for today's preset, at screen_w x
    screen_h. Reuses a cached .npy array for today's date/preset/resolution
    if one exists; otherwise generates a fresh curve and caches it. Retries
    with a new random seed a few times if a particular seed fails
    validation (rare, but generate() can refuse a seed it can't fit)."""
    CACHE_DIR.mkdir(exist_ok=True)
    today = date.today().isoformat()
    _prune_stale_cache(today)

    preset = current_preset(cfg)
    gen_w, gen_h, display_scale = _gen_dimensions(screen_w, screen_h)
    edge = max(0.1, float(cfg.get("edge", 4.0)))
    cache_file = _cache_path(today, cfg.get("rotation_index", 0), gen_w, gen_h, edge)

    if cache_file.exists():
        try:
            return np.load(cache_file), display_scale
        except (OSError, ValueError):
            pass  # fall through and regenerate

    last_error = None
    for attempt in range(5):
        seed = random.randint(0, 2**31 - 1)
        try:
            path, _report = generate(
                size=(gen_w, gen_h),
                stroke=cfg.get("stroke", 6.0),
                edge=edge,
                seed=seed,
                fill_shape=preset.get("fill_shape", "square")
                if preset.get("fill_shape") in FILL_SHAPES else "square",
            )
            np.save(cache_file, path)
            return path, display_scale
        except GenerationError as exc:
            last_error = exc
    raise RuntimeError(f"Could not generate a wallpaper curve after 5 attempts: {last_error}")


# --- Windows WorkerW desktop-icon-layer reparenting --------------------

def _virtual_screen_bounds(fallback_root=None, monitor_mode="primary"):
    """Screen bounds to cover, in one of two modes:

    'primary' (default) -- just the main monitor, via GetSystemMetrics'
    SM_CXSCREEN/SM_CYSCREEN, origin (0, 0).

    'all' -- every monitor combined, via SM_XVIRTUALSCREEN/
    SM_YVIRTUALSCREEN/SM_C{X,Y}VIRTUALSCREEN, so a single curve is
    stretched across the whole multi-monitor bounding box edge-to-edge
    (not the same as one wallpaper mirrored per monitor -- if your
    monitors differ in resolution or aspect ratio, "all" will look
    stretched rather than uniform on each screen).

    ctypes.windll only exists on Windows, so on any other platform (used
    here only for testing the animation/rotation logic, since the
    desktop-icon reparenting trick itself is Windows-only anyway) this
    falls back to the single display Tk already knows about, regardless
    of monitor_mode."""
    if sys.platform != "win32":
        if fallback_root is not None:
            return 0, 0, fallback_root.winfo_screenwidth(), fallback_root.winfo_screenheight()
        return 0, 0, 1920, 1080

    user32 = ctypes.windll.user32

    if monitor_mode == "all":
        x = user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
        y = user32.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
        w = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
        h = user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
        if w > 0 and h > 0:
            return x, y, w, h
        # fall through to primary-monitor metrics if the virtual-screen
        # ones came back empty, which shouldn't normally happen

    w = user32.GetSystemMetrics(0)  # SM_CXSCREEN
    h = user32.GetSystemMetrics(1)  # SM_CYSCREEN
    return 0, 0, w, h


def reparent_behind_desktop_icons(hwnd):
    """The WorkerW trick: ask Progman to spawn a WorkerW window behind the
    desktop icons (undocumented but stable since Windows 7/8, used by every
    "live wallpaper" tool that doesn't ship its own desktop replacement),
    find that specific WorkerW, and SetParent() our window into it.

    IMPORTANT: every one of these functions must have .restype/.argtypes
    explicitly set to wintypes.HWND. ctypes defaults an unannotated
    function's return type to a 32-bit c_int, which silently truncates a
    64-bit window handle on 64-bit Windows -- a well-known and easy-to-hit
    bug that produces a garbage handle instead of a clean failure.

    Returns True if reparenting succeeded, False if the trick did not work
    on this Windows build/setup (caller should fall back to a normal
    window rather than crash)."""
    if sys.platform != "win32":
        return False

    from ctypes import wintypes

    user32 = ctypes.windll.user32

    user32.FindWindowW.restype = wintypes.HWND
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowExW.restype = wintypes.HWND
    user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.SetParent.restype = wintypes.HWND
    user32.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
    user32.SendMessageTimeoutW.restype = ctypes.c_void_p
    user32.SendMessageTimeoutW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
    ]

    progman = user32.FindWindowW("Progman", None)
    if not progman:
        return False

    # Ask Progman to spawn a WorkerW behind the icons. The response value
    # doesn't matter -- what matters is the side effect of a new WorkerW
    # appearing, which we then have to go find via EnumWindows below.
    result = ctypes.c_void_p(0)
    user32.SendMessageTimeoutW(progman, 0x052C, 0, 0, 0x0, 1000, ctypes.byref(result))

    target_workerw = [None]

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum_windows_cb(hwnd_top, _lparam):
        shell_view = user32.FindWindowExW(hwnd_top, None, "SHELLDLL_DefView", None)
        if shell_view:
            # The WorkerW we want is the *next sibling* of the top-level
            # window that hosts SHELLDLL_DefView (the icon layer), not that
            # window itself.
            candidate = user32.FindWindowExW(None, hwnd_top, "WorkerW", None)
            if candidate:
                target_workerw[0] = candidate
        return True

    user32.EnumWindows(_enum_windows_cb, 0)

    if not target_workerw[0]:
        return False

    user32.SetParent(hwnd, target_workerw[0])
    return True


# --- Tkinter rendering ---------------------------------------------------

class WallpaperWindow:
    """Owns the Tk root/canvas and the animation loop. Handles both the
    initial draw and, mid-run, a clean handoff to a new day's curve/preset
    with no restart needed."""

    def __init__(self):
        self.cfg = load_config()

        self.root = tk.Tk()
        monitor_mode = self.cfg.get("monitor_mode", "primary")
        if monitor_mode not in ("primary", "all"):
            monitor_mode = "primary"
        self.x, self.y, self.w, self.h = _virtual_screen_bounds(self.root, monitor_mode)
        self.root.overrideredirect(True)
        self.root.geometry(f"{self.w}x{self.h}+{self.x}+{self.y}")
        self.root.configure(bg=self.cfg.get("bg_color", "#0b1220"))
        # A live wallpaper should never grab focus or show in taskbar/alt-tab.
        try:
            self.root.attributes("-topmost", False)
        except tk.TclError:
            pass

        if sys.platform == "win32":
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass  # best-effort; a slightly blurry wallpaper beats a crash

        self.canvas = tk.Canvas(self.root, width=self.w, height=self.h,
                                 highlightthickness=0, bd=0,
                                 bg=self.cfg.get("bg_color", "#0b1220"))
        self.canvas.pack(fill="both", expand=True)

        self.root.update_idletasks()
        hwnd = self.canvas.winfo_id()
        try:
            root_hwnd = self.root.winfo_id()
        except tk.TclError:
            root_hwnd = hwnd
        reparented = reparent_behind_desktop_icons(root_hwnd)
        if not reparented:
            print("Could not attach behind the desktop icons "
                  "(WorkerW trick did not find its target) -- "
                  "falling back to a normal window instead.")

        self._phase = 0.0
        self._last_tick = time.time()
        self._load_today()

    def _load_today(self):
        advance_if_new_day(self.cfg)
        self.preset = current_preset(self.cfg)
        self.path, self.display_scale = load_or_generate_path(self.cfg, self.w, self.h)
        self._phase = 0.0

    def tick(self):
        # Mid-run day rollover: check every tick (cheap: one string compare)
        # so the engine never needs restarting at midnight.
        if advance_if_new_day(self.cfg):
            self.preset = current_preset(self.cfg)
            self.path, self.display_scale = load_or_generate_path(self.cfg, self.w, self.h)
            self._phase = 0.0

        now = time.time()
        dt = max(0.0, min(0.25, now - self._last_tick))  # clamp a stall/lag spike
        self._last_tick = now

        crawler_len = max(1.0, float(self.preset.get("crawler_size", 40.0)))
        gap_len = max(0.0, float(self.preset.get("gap", 20.0)))
        speed = max(0.0, float(self.preset.get("speed", 200.0)))
        colors = self.preset.get("colors", DEFAULT_PRESETS[0]["colors"])
        period = len(colors) * (crawler_len + gap_len)
        self._phase = (self._phase + speed * dt) % period if period > 0 else 0.0

        stroke = self.cfg.get("stroke", 6.0) * self.display_scale
        self.canvas.delete("all")
        for color_idx, pts in crawl_bands(self.path, crawler_len, gap_len, self._phase, n_colors=len(colors)):
            coords = (pts * self.display_scale).flatten().tolist()
            self.canvas.create_line(*coords, fill=colors[color_idx],
                                     width=max(1.0, stroke),
                                     capstyle=tk.ROUND, joinstyle=tk.ROUND)

        self.root.after(FRAME_MS, self.tick)

    def run(self, test_seconds=None):
        self.tick()
        if test_seconds:
            # Verification-only: auto-close after N seconds instead of
            # running forever, so a live test can be launched and checked
            # (e.g. by double-clicking a .bat) without needing to type into
            # or otherwise control the window to end it.
            self.root.after(int(test_seconds * 1000), self.root.destroy)
        self.root.mainloop()


def main():
    test_seconds = None
    for i, arg in enumerate(sys.argv):
        if arg == "--test-seconds" and i + 1 < len(sys.argv):
            try:
                test_seconds = float(sys.argv[i + 1])
            except ValueError:
                pass

    if sys.platform != "win32":
        print("wallpaper_engine.py's desktop-icon reparenting only works on "
              "Windows. Running anyway in a normal (non-wallpaper) window, "
              "which is fine for testing the animation and rotation logic.")
    window = WallpaperWindow()
    window.run(test_seconds=test_seconds)


if __name__ == "__main__":
    main()
