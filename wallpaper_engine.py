"""Flowing Curve Generator -- live desktop wallpaper engine (Windows only).

Runs a continuously animated "chasing lights" curve behind the desktop
icons, using the exact same crawl_bands() pattern-logic that gui.py's
"Color crawl" preview uses -- organic_curve.py's crawl_bands() and
generate() were deliberately written as pure functions (no Tkinter, no
file I/O) specifically so this file could reuse them unchanged.

What it does, at a glance:
  1. Opens a borderless Tkinter window and, on Windows, reparents it behind
     the desktop icons via the well-known (if undocumented) WorkerW trick,
     so it renders like a real live wallpaper instead of floating on top
     of everything.
  2. Generates (or loads a same-day cached) curve for a preset each day,
     picking the next preset from wallpaper_config.json in rotation --
     always on a background thread (see WallpaperWindow), so the window
     stays responsive/visible instead of looking hung while a curve with
     tens of thousands of points is being laid out.
  3. Redraws the crawl animation on a timer, forever, and silently starts
     the next day's preset when the date rolls over -- no restart needed.

Not Windows? The WorkerW reparenting step is skipped and you get a normal
(still animated) window instead, which is useful for testing the rendering
and rotation logic on macOS/Linux even though it isn't a real wallpaper
there.

gui.py's "Configure Wallpaper..." dialog edits wallpaper_config.json for
you (colors, presets, edge, monitor_mode, and so on) and can start/stop
this script directly -- hand-editing the file is only needed for anything
that dialog doesn't cover yet.

Refuses to start a second instance (see _acquire_lock): two of these
running at once would both try to reparent into WorkerW and redraw the
screen, which can make the whole desktop sluggish -- whether the earlier
one was started from gui.py, a previous run of this script, or the test
.bat file.

Run directly with:  python wallpaper_engine.py
(same .venv / dependencies as gui.py -- see run.bat/run.sh)
"""
import ctypes
import sys


def _make_dpi_aware():
    """Tell Windows this process handles its own DPI scaling, *before*
    anything else touches a window or GetSystemMetrics.

    Order matters: if this runs after tk.Tk()/geometry() (as it used to,
    inside WallpaperWindow.__init__), Windows has already handed out
    DPI-virtualized (scaled) coordinates for monitor geometry and window
    placement, based on whatever monitor the process *happened* to be
    considered "on" at that point. On a mixed-DPI multi-monitor setup this
    can put the window's real on-screen rectangle somewhere other than
    where the math intended.

    Called here, at true import time -- before tkinter (or anything else)
    is even imported, let alone before a Tk() is created -- rather than
    just "early in main()", on the theory that a runtime DPI-awareness
    call needs to happen before *anything* Windows might consider
    DPI-relevant, and importing tkinter is the earliest such thing in this
    process. (A first attempt moved this to the top of main(), which did
    not resolve the click-blocking bug this exists to fix -- so this is a
    belt-and-suspenders tightening of that fix, not a confirmed additional
    root cause on its own; see wallpaper_debug.log for what's actually
    happening on a given machine.)

    Prefers per-monitor-v2 DPI awareness (correct for mixed-DPI multi-monitor
    rigs) and falls back to the older system-DPI-only API on Windows
    versions that don't have it, or to doing nothing at all if both fail --
    a slightly blurry wallpaper beats a crash.

    Returns a short string describing what happened (stashed in the
    DPI_AWARENESS_RESULT global below since this runs before the debug-log
    helper exists to record it directly) -- purely diagnostic."""
    if sys.platform != "win32":
        return "not windows"
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == -4. Needs an
        # explicit c_void_p so ctypes doesn't try to pass -4 as a 32-bit
        # int on 64-bit Windows.
        ok = ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        if ok:
            return "per-monitor-v2"
    except (AttributeError, OSError):
        pass  # older Windows without per-monitor-v2 support
    try:
        ok = ctypes.windll.user32.SetProcessDPIAware()
        return "system-dpi-aware" if ok else "SetProcessDPIAware returned failure"
    except Exception as exc:  # noqa: BLE001 -- diagnostic only
        return f"both calls failed: {exc!r}"


# Must run before tkinter (or anything else) is imported.
DPI_AWARENESS_RESULT = _make_dpi_aware()

import json
import os
import queue
import random
import threading
import time
import tkinter as tk
from datetime import date, datetime
from pathlib import Path

import numpy as np

from organic_curve import generate, crawl_bands, build_gradient_palette, GenerationError, FILL_SHAPES

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "wallpaper_config.json"
CACHE_DIR = BASE_DIR / "wallpaper_cache"
LOCK_PATH = BASE_DIR / "wallpaper.lock"
DEBUG_LOG_PATH = BASE_DIR / "wallpaper_debug.log"

# gui.py's "New random curve for today" button doesn't manage the running
# engine process at all (it may not even know one is running -- it could
# have been started in an earlier app session). Instead it just clears
# today's cache and touches this file; whichever wallpaper_engine.py
# instance is actually running notices it on the next tick (same cheap
# every-frame check already used for the day-rollover), consumes it, and
# regenerates in place -- no restart, no close/reopen, and the old curve
# keeps crawling on screen the whole time, exactly like an ordinary
# midnight rollover.
REGEN_REQUEST_PATH = BASE_DIR / "wallpaper_regen.request"

# Set True only while actively chasing the "clicks blocked on the GUI's
# monitor" bug -- writes timestamped diagnostics (monitor geometry, window
# handles/rects before and after the WorkerW reparent, whether SetParent
# actually succeeded) to wallpaper_debug.log next to this file. Cheap
# (a handful of short writes at startup, none in the animation loop) and
# gitignored, but there's no reason to leave it on once this is resolved.
DEBUG_LOGGING = True

_debug_log_started = False


def _debug_log(msg):
    """Best-effort diagnostic logging -- never let a logging failure take
    down the wallpaper. Truncates at the start of each run (so the file
    always reflects the most recent run, not an unbounded history) and
    appends for the rest of that run."""
    if not DEBUG_LOGGING:
        return
    global _debug_log_started
    try:
        mode = "a" if _debug_log_started else "w"
        _debug_log_started = True
        with open(DEBUG_LOG_PATH, mode, encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat(timespec='milliseconds')}] {msg}\n")
    except OSError:
        pass


def _get_window_rect(hwnd):
    """Returns (left, top, right, bottom) for hwnd, or None on failure.
    Diagnostic helper only -- used to see where a window actually ended up
    on screen, before/after reparenting."""
    if sys.platform != "win32":
        return None
    from ctypes import wintypes
    rect = wintypes.RECT()
    ok = ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    if not ok:
        return None
    return (rect.left, rect.top, rect.right, rect.bottom)


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
        "blend_steps": 8.0,
    },
    {
        "fill_shape": "square",
        "colors": ["#5eb0ff", "#c792ff", "#7fe7c4"],
        "crawler_size": 45.0,
        "gap": 22.0,
        "speed": 260.0,
        "blend_steps": 8.0,
    },
    {
        "fill_shape": "square",
        "colors": ["#ffd166", "#ff8fa3", "#5eb0ff"],
        "crawler_size": 70.0,
        "gap": 35.0,
        "speed": 180.0,
        "blend_steps": 8.0,
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
    # Off by default: the WorkerW "behind the desktop icons" reparenting
    # trick attaches this window as a child of a window owned by
    # explorer.exe (a different process) -- an unusual, poorly-documented
    # operation that was confirmed, via extensive testing on a real
    # 2-monitor machine, to cause the monitor the GUI is on to stop
    # accepting *any* clicks (including the taskbar and unrelated apps)
    # until the GUI was minimized. Disabling it (the default) uses a
    # plain positioned/lowered/click-through window instead, which loses
    # the "rendered behind your icons" look (icons are visually covered
    # while it runs, though clicks should still reach them) but is not
    # known to freeze anything. Turn this on from the "Configure
    # Wallpaper..." dialog only if you want to try the behind-icons look
    # again and are prepared for that same freeze to come back --
    # gui.py's checkbox for this carries that warning.
    "attempt_worker_reparent": False,
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


# --- single-instance lock -----------------------------------------------

def _pid_alive(pid):
    """True if a process with this PID currently exists. Used to tell a
    stale lock file (left behind by a crash or a forceful kill, where
    nothing had the chance to clean it up) from a real still-running
    instance."""
    if sys.platform == "win32":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_lock():
    """Refuse to start a second instance -- two of these running at once
    would both try to reparent into WorkerW and redraw the screen, which
    can make the whole desktop sluggish. Returns True if it's safe to
    proceed (no other live instance holds the lock), False otherwise.
    Best-effort: a lock file we can't read/write doesn't block startup,
    since running unprotected beats not running at all."""
    if LOCK_PATH.exists():
        try:
            other_pid = int(LOCK_PATH.read_text().strip())
        except (OSError, ValueError):
            other_pid = None
        if other_pid is not None and other_pid != os.getpid() and _pid_alive(other_pid):
            return False
    try:
        LOCK_PATH.write_text(str(os.getpid()))
    except OSError:
        pass
    return True


def release_lock():
    try:
        if LOCK_PATH.exists() and LOCK_PATH.read_text().strip() == str(os.getpid()):
            LOCK_PATH.unlink()
    except OSError:
        pass


def current_preset(cfg):
    presets = cfg["presets"]
    idx = cfg.get("rotation_index", 0) % len(presets)
    return presets[idx]


def _is_new_day(cfg):
    return cfg.get("last_update") != date.today().isoformat()


def _next_rotation(cfg):
    """Pure (no mutation, no file I/O): what rotation_index/preset *would*
    be used if the day were advanced right now. Generation runs against
    this candidate on a background thread -- the config is only actually
    updated (see WallpaperWindow._on_generation_done) once that generation
    has succeeded, so a failed attempt gets retried on the next tick
    instead of silently burning a day's rotation slot."""
    presets = cfg["presets"]
    idx = cfg.get("rotation_index", 0) % len(presets)
    if cfg.get("last_update") is not None:  # don't skip preset 0 on first-ever run
        idx = (idx + 1) % len(presets)
    return idx, presets[idx]


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


def _make_input_safe(hwnd):
    """Apply the standard Win32 recipe for "a window that's purely a
    visual background layer: it never takes keyboard/mouse focus, and
    mouse input passes straight through it to whatever's underneath" --
    WS_EX_NOACTIVATE (never becomes the active/foreground window, whether
    from being created, clicked, or Alt-Tabbed to) plus WS_EX_TRANSPARENT
    (excluded from mouse hit-testing entirely, so clicks land on whatever
    is actually beneath it) plus WS_EX_LAYERED (required for
    WS_EX_TRANSPARENT to take full effect, and for SetLayeredWindowAttributes
    below to apply).

    Why this exists: wallpaper_debug.log confirmed the WorkerW reparenting
    itself was working correctly (right monitor, right position, low CPU)
    -- yet clicks kept failing across the *entire* monitor, including the
    taskbar and unrelated apps, and the fix turned out to be: minimize
    gui.py. That points at a focus/activation conflict between gui.py's
    window (open and active on that monitor) and this new top-level
    window being created on top of/alongside it -- not at anything to do
    with WorkerW, z-order, or CPU load. WS_EX_NOACTIVATE stops this window
    from ever contesting activation with gui.py in the first place; called
    while still withdrawn (see WallpaperWindow.__init__), before this
    window is ever shown, so there's no window of time where it could
    grab focus before the style takes effect.

    Best-effort: failure here should never crash the wallpaper, since a
    visible-but-occasionally-input-grabby window still beats no wallpaper
    at all."""
    if sys.platform != "win32":
        return
    try:
        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        WS_EX_TRANSPARENT = 0x00000020
        WS_EX_NOACTIVATE = 0x08000000
        LWA_ALPHA = 0x2

        user32 = ctypes.windll.user32
        user32.GetWindowLongW.restype = ctypes.c_long
        user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetWindowLongW.restype = ctypes.c_long
        user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]

        ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        new_style = ex_style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new_style)
        # WS_EX_LAYERED windows need an explicit layering call to actually
        # composite (fully opaque here -- this isn't about transparency of
        # the *pixels*, only of *input*), or some Windows versions render
        # them blank.
        user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)
        _debug_log(f"input-safe: hwnd={hwnd}, old ex_style={ex_style:#x}, new ex_style={new_style:#x} "
                   f"(added WS_EX_LAYERED|WS_EX_TRANSPARENT|WS_EX_NOACTIVATE)")
    except Exception as exc:  # noqa: BLE001 -- best-effort safety net only
        _debug_log(f"input-safe: FAILED: {exc!r}")


def _rect_overlap_area(a, b):
    """Intersection area of two (left, top, right, bottom) rects, or 0 if
    they don't overlap (or either is missing/degenerate)."""
    if not a or not b:
        return 0
    left = max(a[0], b[0])
    top = max(a[1], b[1])
    right = min(a[2], b[2])
    bottom = min(a[3], b[3])
    if right <= left or bottom <= top:
        return 0
    return (right - left) * (bottom - top)


def reparent_behind_desktop_icons(hwnd, target_rect=None):
    """The WorkerW trick: ask Progman to spawn a WorkerW window behind the
    desktop icons (undocumented but stable since Windows 7/8, used by every
    "live wallpaper" tool that doesn't ship its own desktop replacement),
    find that specific WorkerW, and SetParent() our window into it.

    On a multi-monitor machine, Explorer can maintain *more than one*
    SHELLDLL_DefView-hosting top-level window, each with its own adjacent
    WorkerW scoped to a single monitor's rectangle rather than the whole
    virtual desktop -- confirmed via wallpaper_debug.log on a real 2-monitor
    machine (Progman -> Explorer's EnumWindows order surfaced 17 top-level
    "WorkerW"-classed windows total, most of them unrelated tiny helper
    windows that just happen to share the class name, and exactly one real
    desktop-icon WorkerW pair, scoped to the *other* monitor from the one
    requested). Blindly taking "whichever SHELLDLL_DefView-adjacent WorkerW
    EnumWindows happens to report last" -- the previous approach -- means
    on such a machine you can silently attach to the wrong monitor's
    WorkerW. SetParent() doesn't adjust the child's screen position for the
    new parent's origin, so the window then visibly *jumps* to wherever
    that WorkerW's monitor is, which is exactly what the log showed: a
    window created at (0,0,1920,1080) landed at (-1920,0,0,1080) after
    SetParent -- a clean one-monitor-width shift.

    target_rect, when given as (left, top, right, bottom) in screen
    coordinates, lets the caller say which monitor it actually wants: every
    SHELLDLL_DefView-adjacent WorkerW found is scored by how much it
    overlaps target_rect, and the best-overlapping one wins (falling back
    to "last one found" only if none overlap at all, so this still works
    if Explorer's WorkerW for the target monitor can't be found for some
    reason, or when target_rect isn't given).

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
    kernel32 = ctypes.windll.kernel32

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
    _debug_log(f"reparent: Progman hwnd={progman}")
    if not progman:
        return False

    def _find_target_workerw():
        """One EnumWindows pass to find every WorkerW that sits directly
        behind a desktop-icons layer (i.e. is the top-level sibling of some
        top-level window that hosts SHELLDLL_DefView -- there can be more
        than one such pair on a multi-monitor machine), plus a diagnostic
        list of every top-level WorkerW-classed window seen at all
        (including unrelated ones that just share the class name)."""
        candidates = []  # every SHELLDLL_DefView-adjacent WorkerW: (hwnd, rect)
        seen = []  # diagnostic only: every top-level WorkerW hwnd + its screen rect

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _enum_windows_cb(hwnd_top, _lparam):
            shell_view = user32.FindWindowExW(hwnd_top, None, "SHELLDLL_DefView", None)
            if shell_view:
                candidate = user32.FindWindowExW(None, hwnd_top, "WorkerW", None)
                if candidate:
                    candidates.append((candidate, _get_window_rect(candidate)))
            return True

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _enum_all_workerw_cb(hwnd_top, _lparam):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd_top, buf, 256)
            if buf.value == "WorkerW":
                seen.append((hwnd_top, _get_window_rect(hwnd_top)))
            return True

        user32.EnumWindows(_enum_windows_cb, 0)
        user32.EnumWindows(_enum_all_workerw_cb, 0)
        return candidates, seen

    def _pick_best(candidates):
        """Prefer whichever candidate's rect overlaps target_rect the most
        (the monitor we actually want to render on); if target_rect wasn't
        given, or nothing overlaps it, fall back to the last candidate
        found (the previous behavior) so this still degrades gracefully."""
        if not candidates:
            return None, False
        if target_rect is not None:
            scored = [(cand, _rect_overlap_area(rect, target_rect)) for cand, rect in candidates]
            best_cand, best_score = max(scored, key=lambda pair: pair[1])
            if best_score > 0:
                return best_cand, True
        # Either target_rect wasn't given, or nothing overlaps it at all --
        # fall back to the last one found (the previous behavior), but
        # flag it as an unconfirmed match so the caller can decide whether
        # it's safe to attach to (see matched_target_monitor below).
        return candidates[-1][0], False

    # Check for an already-existing target WorkerW *before* asking Progman
    # to spawn a new one. The 0x052C message is not documented to be
    # idempotent, and several other WorkerW-trick implementations report
    # that sending it again in a session that already has one (e.g. a
    # second Start after a Stop, in the same Explorer session) can create
    # an *additional* WorkerW rather than reusing the existing one.
    # Reusing whatever's already there when possible avoids ever piling
    # these up.
    candidates, all_workerw_seen = _find_target_workerw()
    if candidates:
        _debug_log(f"reparent: found {len(candidates)} existing SHELLDLL_DefView-adjacent "
                   f"WorkerW candidate(s) without spawning a new one: {candidates} "
                   f"(all WorkerW seen: {all_workerw_seen})")
    else:
        # Ask Progman to spawn a WorkerW behind the icons. The response
        # value doesn't matter -- what matters is the side effect of a new
        # WorkerW appearing, which we then have to go find via EnumWindows.
        result = ctypes.c_void_p(0)
        user32.SendMessageTimeoutW(progman, 0x052C, 0, 0, 0x0, 1000, ctypes.byref(result))
        candidates, all_workerw_seen = _find_target_workerw()
        _debug_log(f"reparent: no existing candidates, sent spawn message, "
                   f"now see {len(candidates)} candidate(s): {candidates}")

    target_workerw, matched_target_monitor = _pick_best(candidates)
    _debug_log(f"reparent: target_rect={target_rect}, candidates={candidates}, "
               f"chosen target_workerw={target_workerw}, matched_target_monitor={matched_target_monitor}")

    if not target_workerw:
        return False

    if target_rect is not None and not matched_target_monitor:
        # We were asked to render on a specific monitor rectangle, but no
        # SHELLDLL_DefView-adjacent WorkerW we could find overlaps it at
        # all -- every real candidate is scoped to some other monitor.
        # Attaching anyway would mean either (a) leaving the window at its
        # old screen position, now silently reinterpreted relative to a
        # parent whose client area doesn't cover it (likely clipped to
        # invisible, and definitely on the wrong monitor if it renders at
        # all), or (b) explicitly repositioning it outside that parent's
        # own bounds, which risks the same clipping. Neither is better
        # than the normal-window fallback the caller already has for
        # exactly this situation, so refuse cleanly here instead of
        # guessing which risk to take.
        _debug_log(f"reparent: no candidate WorkerW overlaps target_rect={target_rect} -- "
                   f"refusing to attach to an unrelated monitor's WorkerW; falling back "
                   f"to a normal window instead.")
        return False

    rect_before = _get_window_rect(hwnd)
    kernel32.SetLastError(0)
    prev_parent = user32.SetParent(hwnd, target_workerw)
    if not prev_parent:
        err = kernel32.GetLastError()
        _debug_log(f"reparent: SetParent FAILED, GetLastError={err}, "
                   f"hwnd={hwnd}, target={target_workerw}, rect_before={rect_before}")
        return False

    rect_after = _get_window_rect(hwnd)
    _debug_log(f"reparent: SetParent OK, prev_parent={prev_parent}, "
               f"rect_before={rect_before}, rect_after={rect_after}")

    # SetParent() does not adjust the child's position for the new parent's
    # screen origin -- it reinterprets the *same* x/y as parent-relative
    # instead of screen-relative, which is exactly what produced the
    # one-monitor-width jump seen in testing. Explicitly re-anchor to the
    # intended absolute screen rectangle now that we know the chosen
    # parent's own on-screen position, so the final result is correct even
    # if the parent we attached to isn't pixel-for-pixel where target_rect
    # says it should be.
    if target_rect is not None:
        parent_rect = _get_window_rect(target_workerw)
        if parent_rect:
            user32.SetWindowPos.restype = wintypes.BOOL
            user32.SetWindowPos.argtypes = [
                wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, wintypes.UINT,
            ]
            rel_x = target_rect[0] - parent_rect[0]
            rel_y = target_rect[1] - parent_rect[1]
            want_w = target_rect[2] - target_rect[0]
            want_h = target_rect[3] - target_rect[1]
            SWP_NOZORDER = 0x0004
            SWP_NOACTIVATE = 0x0010
            ok = user32.SetWindowPos(hwnd, None, rel_x, rel_y, want_w, want_h,
                                      SWP_NOZORDER | SWP_NOACTIVATE)
            _debug_log(f"reparent: SetWindowPos(rel_x={rel_x}, rel_y={rel_y}, "
                       f"w={want_w}, h={want_h}) -> ok={ok}, "
                       f"rect_after_reposition={_get_window_rect(hwnd)}")
    return True


# --- Tkinter rendering ---------------------------------------------------

class WallpaperWindow:
    """Owns the Tk root/canvas and the animation loop. Handles both the
    initial draw and, mid-run, a clean handoff to a new day's curve/preset
    with no restart needed.

    Curve generation (organic_curve.generate(), which can easily take tens
    of seconds) always runs on a background thread, never on the Tk main
    thread. Blocking the main thread means Tk never pumps its event/paint
    queue, so the window sits there unpainted and Windows flags it "Not
    Responding" -- this bit us in testing (a real window, reparented or
    not, that never gets a chance to draw itself before mainloop() starts
    looks exactly like a hung app). Keeping generation off the main thread
    means the window is responsive from the instant it appears, and the
    daily preset rollover no longer freezes the animation either -- the
    previous day's curve just keeps crawling until the new one is ready."""

    def __init__(self):
        self.cfg = load_config()
        self._result_queue = queue.Queue()
        self._generating = False
        self.path = None
        self.display_scale = 1.0
        self.preset = current_preset(self.cfg)
        self._loading_message = None
        # Clear out any stale regen request left over from a previous run
        # (e.g. the engine crashed or was killed between the file being
        # written and being consumed) so it doesn't trigger an unwanted
        # regeneration the instant this new run starts up.
        try:
            REGEN_REQUEST_PATH.unlink()
        except OSError:
            pass

        self.root = tk.Tk()
        # Hide immediately, before this window is ever mapped to the screen
        # -- everything below (geometry, extended input-safety styles,
        # reparenting) happens while it's still invisible, so there's no
        # window of time where a not-yet-configured top-level window could
        # flash up and contest focus/activation with gui.py. Shown for
        # real only at the very end, via deiconify().
        self.root.withdraw()
        monitor_mode = self.cfg.get("monitor_mode", "primary")
        if monitor_mode not in ("primary", "all"):
            monitor_mode = "primary"
        self.x, self.y, self.w, self.h = _virtual_screen_bounds(self.root, monitor_mode)
        if sys.platform == "win32":
            u32 = ctypes.windll.user32
            _debug_log(
                f"monitor_mode={monitor_mode}, chosen bounds=({self.x},{self.y},{self.w},{self.h}); "
                f"for comparison -- SM_CXSCREEN/CYSCREEN (primary monitor)="
                f"({u32.GetSystemMetrics(0)},{u32.GetSystemMetrics(1)}), "
                f"SM_X/YVIRTUALSCREEN+CX/CYVIRTUALSCREEN (all monitors)="
                f"({u32.GetSystemMetrics(76)},{u32.GetSystemMetrics(77)},"
                f"{u32.GetSystemMetrics(78)},{u32.GetSystemMetrics(79)}), "
                f"SM_CMONITORS (monitor count)={u32.GetSystemMetrics(80)}"
            )
        self.root.overrideredirect(True)
        self.root.geometry(f"{self.w}x{self.h}+{self.x}+{self.y}")
        self.root.configure(bg=self.cfg.get("bg_color", "#0b1220"))
        # A live wallpaper should never grab focus or show in taskbar/alt-tab.
        try:
            self.root.attributes("-topmost", False)
        except tk.TclError:
            pass

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
        _debug_log(f"root_hwnd={root_hwnd}, canvas_hwnd={hwnd}, "
                   f"root rect before reparent={_get_window_rect(root_hwnd)}")

        # Applied while still withdrawn (invisible): never take
        # keyboard/mouse focus, and let clicks pass straight through to
        # whatever's actually underneath. This is what actually fixes the
        # "GUI's monitor stops accepting clicks until you minimize GUI"
        # bug -- see _make_input_safe()'s docstring. It's independent of
        # (and a stronger guarantee than) the WorkerW reparenting below,
        # so it applies whether or not that succeeds.
        _make_input_safe(root_hwnd)

        target_rect = (self.x, self.y, self.x + self.w, self.y + self.h)
        attempt_reparent = bool(self.cfg.get("attempt_worker_reparent", False))
        if attempt_reparent:
            reparented = reparent_behind_desktop_icons(root_hwnd, target_rect=target_rect)
        else:
            _debug_log("attempt_worker_reparent is off (config) -- skipping the WorkerW "
                       "trick entirely this run and going straight to the plain "
                       "positioned/lowered/click-through window below.")
            reparented = False
        if not reparented:
            if attempt_reparent:
                print("Could not attach behind the desktop icons "
                      "(WorkerW trick did not find its target) -- "
                      "falling back to a normal window instead.")
            else:
                print("'Behind desktop icons' is turned off in Configure Wallpaper... "
                      "-- using a normal window instead.")
            _debug_log("reparenting not in effect -- falling back to a normal top-level window. "
                       "Lowering it in the normal z-order as a safety net so it can't "
                       "sit on top of (and block clicks to) other apps like gui.py.")
            # Reparenting failed, so this stayed a normal top-level window
            # instead of becoming a desktop-layer child -- a new top-level
            # window can otherwise land above other apps in z-order and
            # (being borderless and full-monitor) silently eat their
            # clicks. lower() pushes it to the bottom of the *normal*
            # z-order as a safety net; it's not the real "behind icons"
            # look, but it can no longer block input to anything else.
            try:
                self.root.lower()
            except tk.TclError:
                pass
        else:
            _debug_log(f"root rect after reparent={_get_window_rect(root_hwnd)}")

        # Finally make it visible -- everything above (geometry, the
        # input-safety styles, reparenting) is already in place, so there's
        # no gap where an unconfigured window could grab focus.
        self.root.deiconify()

        self._phase = 0.0
        self._last_tick = time.time()

    def _start_generation(self):
        """Kick off generate() on a background thread for whichever
        preset today's rotation points to (advancing to the next preset
        first, without saving it yet, if the day has rolled over). Safe to
        call repeatedly -- a no-op while a generation is already in
        flight, and self.cfg is only ever mutated back on the main thread
        in _poll_generation once the result is in hand."""
        if self._generating:
            return
        self._generating = True
        is_new_day = _is_new_day(self.cfg)
        idx, _preset = _next_rotation(self.cfg) if is_new_day else \
            (self.cfg.get("rotation_index", 0) % len(self.cfg["presets"]), current_preset(self.cfg))
        cfg_snapshot = dict(self.cfg)
        cfg_snapshot["rotation_index"] = idx
        w, h = self.w, self.h

        def worker():
            try:
                path, display_scale = load_or_generate_path(cfg_snapshot, w, h)
                self._result_queue.put(("ok", is_new_day, idx, path, display_scale))
            except Exception as exc:  # noqa: BLE001 -- report, don't crash the daemon thread
                self._result_queue.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_generation(self):
        """Non-blocking: called every tick. Swaps in a freshly generated
        curve as soon as the background thread finishes, and commits the
        day/rotation-index change to disk only on success."""
        try:
            result = self._result_queue.get_nowait()
        except queue.Empty:
            return
        self._generating = False
        self._loading_message = None
        if result[0] == "error":
            print(f"Wallpaper curve generation failed, will retry next tick: {result[1]}")
            return
        _, is_new_day, idx, path, display_scale = result
        if is_new_day:
            self.cfg["rotation_index"] = idx
            self.cfg["last_update"] = date.today().isoformat()
            save_config(self.cfg)
        self.preset = current_preset(self.cfg)
        self.path = path
        self.display_scale = display_scale
        self._phase = 0.0

    def tick(self):
        self._poll_generation()
        # Mid-run day rollover: check every tick (cheap: one string
        # compare) so the engine never needs restarting at midnight --
        # _start_generation() itself is a no-op while one is in flight.
        if _is_new_day(self.cfg):
            self._start_generation()
        elif REGEN_REQUEST_PATH.exists():
            # gui.py's "New random curve for today" button already cleared
            # today's cache before touching this file, so the regeneration
            # below is guaranteed to pick a fresh random seed instead of
            # reloading what was cached -- and since is_new_day is False
            # here, it stays on today's preset/rotation_index rather than
            # advancing to tomorrow's.
            try:
                REGEN_REQUEST_PATH.unlink()
            except OSError:
                pass
            self._loading_message = "Generating a new curve..."
            self._start_generation()

        now = time.time()
        dt = max(0.0, min(0.25, now - self._last_tick))  # clamp a stall/lag spike
        self._last_tick = now

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
        # else: first curve is still generating in the background -- leave
        # the plain background color showing instead of erroring, since
        # there's nothing to draw yet.

        if self._loading_message:
            # Only shown for an explicit user-requested regen (see tick()
            # above) -- not on ordinary silent daily rollover, so the
            # wallpaper doesn't pop up unprompted text every night. Drawn
            # on top of the still-crawling old curve, bottom-right corner
            # so it doesn't sit under any desktop icons up top.
            margin = max(16, int(24 * self.display_scale))
            self.canvas.create_text(
                self.w - margin, self.h - margin,
                text=self._loading_message, fill="#ffffff",
                font=("Segoe UI", max(10, int(14 * self.display_scale))),
                anchor="se")

        self.root.after(FRAME_MS, self.tick)

    def run(self, test_seconds=None):
        self._start_generation()  # kick off the very first curve
        self.tick()
        if test_seconds:
            # Verification-only: auto-close after N seconds instead of
            # running forever, so a live test can be launched and checked
            # (e.g. by double-clicking a .bat) without needing to type into
            # or otherwise control the window to end it.
            self.root.after(int(test_seconds * 1000), self.root.destroy)
        self.root.mainloop()


def main():
    _debug_log(f"=== wallpaper_engine.py starting, pid={os.getpid()} ===")
    _debug_log(f"DPI awareness result: {DPI_AWARENESS_RESULT}")

    test_seconds = None
    for i, arg in enumerate(sys.argv):
        if arg == "--test-seconds" and i + 1 < len(sys.argv):
            try:
                test_seconds = float(sys.argv[i + 1])
            except ValueError:
                pass

    if not acquire_lock():
        print("Another wallpaper_engine.py instance already appears to be running "
              "-- exiting instead of starting a second one on top of it.")
        return

    if sys.platform != "win32":
        print("wallpaper_engine.py's desktop-icon reparenting only works on "
              "Windows. Running anyway in a normal (non-wallpaper) window, "
              "which is fine for testing the animation and rotation logic.")
    try:
        window = WallpaperWindow()
        window.run(test_seconds=test_seconds)
    finally:
        release_lock()


if __name__ == "__main__":
    main()
