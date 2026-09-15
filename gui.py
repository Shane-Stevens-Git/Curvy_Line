"""Desktop GUI for organic_curve.py.

Lets you tweak seed / stroke / smoothness (and the less common knobs, under
Advanced) with sliders and text boxes, hit Generate, and see the result
without touching the command line. Generation runs in a background thread
so the window stays responsive, with a progress bar driven by
organic_curve.generate()'s progress_callback.

Each Generate overwrites a single set of preview files (outputs/preview.png
/.svg/.validation.json), so nothing ever collides with a previous file and
there is no "file already exists" error to run into. When you land on a
result worth keeping, "Save As..." copies that preview out under a name you
choose; "Clear saved copies" empties the outputs folder if it's built up a
lot of keepers you no longer want.

With "Animate drawing" on, the finished curve is revealed stroke-by-stroke
on a canvas (like watching a pen trace it) instead of just popping in as a
static image; "Skip" jumps straight to the finished frame. The animation is
just a progressive reveal of the already-computed path for playback -- it
does not change what gets generated, saved, or validated.

"Color crawl" is a separate, looping animation: 3 colors chase along the
already-generated curve in repeating bands, like chasing lights. Crawler
size/gap/speed and the 3 colors all update live while it's running. The
banding math (organic_curve.crawl_bands) is a plain function of the path,
not tied to Tkinter, so the same logic can drive a static frame exporter or
a future live-wallpaper daemon later -- this GUI is just one consumer of it.

Line color, background color, and "Display stroke" (after a generation) are
pure presentation -- they redraw the already-computed, already-validated
path instantly instead of re-running the ~seconds-to-a-minute generation.
Display stroke is capped so it can never thicken the line enough to make it
touch itself; see organic_curve.max_safe_render_stroke().

Width/Height support a rectangular canvas (e.g. a 1920x1080 wallpaper),
filled edge-to-edge with no cropping; "Square" links them together for the
original single-size behavior. "Draw custom boundary..." lets you hand-draw
a closed shape on the preview before generating, which is then filled
instead of a preset shape.

Run with the same interpreter you used for organic_curve.py, e.g.:
    .venv\\Scripts\\python.exe gui.py          (Windows)
    .venv/bin/python gui.py                    (macOS/Linux)
"""
import json
import queue
import random
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import colorchooser, ttk, filedialog, messagebox

from PIL import ImageTk
from shapely.geometry import Polygon as ShapelyPolygon

from organic_curve import (generate, render_png, render_svg, GenerationError,
                            max_safe_render_stroke, fill_polygon, FILL_SHAPES, crawl_bands)
from wallpaper_engine import (load_config as load_wallpaper_config,
                               save_config as save_wallpaper_config,
                               DEFAULT_PRESETS as WALLPAPER_DEFAULT_PRESETS)

OUTPUT_DIR = Path(__file__).parent / "outputs"
WALLPAPER_ENGINE_PATH = Path(__file__).parent / "wallpaper_engine.py"
WALLPAPER_SHAPE_CHOICES = [s for s in FILL_SHAPES if s != "custom"]  # 'custom' needs a hand-drawn
                                                                       # region, not meaningful per-preset
PREVIEW_BASENAME = "preview"
PREVIEW_DISPLAY_SIZE = 560  # on-screen preview box, px


class CurveApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Flowing Curve Generator")
        self.minsize(880, 620)

        self.worker_queue = queue.Queue()
        self.busy = False
        self.last_path = None   # numpy path array of the most recent successful render
        self.last_report = None
        self.last_size = None
        self.last_stroke = None
        self.last_generation_stroke = None
        self.preview_photo = None  # keep a reference so Tk doesn't garbage-collect it
        self._gen_token = 0      # invalidates a stale/superseded animation when a new run starts
        self._anim_skip = False  # set by the Skip button to jump the running animation to the end
        self._rerendering = False  # guards against overlapping color/stroke re-renders

        # Custom drawn-boundary state (fill_shape='custom').
        self.custom_region = None   # finalized Shapely Polygon, in full generation-space coords
        self.custom_points = []     # raw (canvas_x, canvas_y) points while a stroke is in progress
        self._draw_mode = False     # armed: next drag on the preview canvas draws a boundary
        self._drawing = False       # a drag is currently in progress

        # Color crawl animation state (see crawl_bands()).
        self._crawl_running = False
        self._crawl_token = 0    # invalidates a stale/superseded crawl loop
        self._crawl_phase = 0.0  # px along the path the pattern has shifted so far
        self._crawl_last_tick = 0.0
        self._last_static_img = None  # the most recent crisp (non-animating) render, to restore on Stop

        # Live wallpaper dialog/process state (see _open_wallpaper_dialog).
        # Kept at the app level, not the dialog, so a running wallpaper
        # process and its status stay tracked even if the dialog is closed
        # and reopened -- the wallpaper itself is meant to outlive both the
        # dialog and this whole app once started.
        self._wallpaper_dialog = None
        self._wallpaper_proc = None
        self._wallpaper_status_var = tk.StringVar(value="Not running")

        OUTPUT_DIR.mkdir(exist_ok=True)

        self._build_layout()
        self._poll_queue()

    # ---------------------------------------------------------- layout ----

    def _build_layout(self):
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=0)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)

        controls = self._build_scrollable_controls(root)
        controls.grid(row=0, column=0, sticky="ns", padx=(0, 12))

        preview = ttk.Frame(root)
        preview.grid(row=0, column=1, sticky="nsew")
        self._build_preview(preview)

        self._update_shape_preview()  # show the default shape's border right away

    def _build_scrollable_controls(self, root):
        """Wrap the controls sidebar in a scrollable canvas so it always
        stays reachable -- the sidebar keeps growing as features get added
        (Color crawl was the one that first pushed Generate and everything
        below it out of view, with no way to reach it), and a fixed window
        height/screen size shouldn't ever be able to hide controls again.
        Returns the container frame to `.grid(...)` in place of the old
        plain `controls` frame; the actual widgets still go in a normal
        `ttk.Frame` (built by `_build_controls`), just embedded in a canvas."""
        container = ttk.Frame(root)
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)

        canvas = tk.Canvas(container, highlightthickness=0, borderwidth=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scrollbar.set,
                          yscrollincrement=24)  # ~one row per wheel notch, not a huge jump

        controls = ttk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=controls, anchor="nw")

        def sync_scrollregion(_evt=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def sync_inner_width(evt):
            # Keep the embedded frame exactly as wide as the canvas viewport
            # so "ew"-sticky widgets inside it (sliders, full-width buttons)
            # stretch correctly instead of clamping to their own reqwidth.
            canvas.itemconfig(window_id, width=evt.width)

        controls.bind("<Configure>", sync_scrollregion)
        canvas.bind("<Configure>", sync_inner_width)

        def scroll_units(n):
            if canvas.bbox("all") is not None and canvas.bbox("all")[3] > canvas.winfo_height():
                canvas.yview_scroll(n, "units")

        def on_mousewheel(evt):  # Windows / macOS
            scroll_units(int(-1 * (evt.delta / 120)) or (-1 if evt.delta > 0 else 1))

        def on_wheel_up(_evt):  # Linux
            scroll_units(-1)

        def on_wheel_down(_evt):  # Linux
            scroll_units(1)

        # Only capture the scroll wheel while the pointer is actually over
        # the sidebar, so scrolling the preview/canvas area elsewhere isn't
        # hijacked by this binding.
        def bind_wheel(_evt=None):
            canvas.bind_all("<MouseWheel>", on_mousewheel)
            canvas.bind_all("<Button-4>", on_wheel_up)
            canvas.bind_all("<Button-5>", on_wheel_down)

        def unbind_wheel(_evt=None):
            canvas.unbind_all("<MouseWheel>")
            canvas.unbind_all("<Button-4>")
            canvas.unbind_all("<Button-5>")

        canvas.bind("<Enter>", bind_wheel)
        canvas.bind("<Leave>", unbind_wheel)

        self._build_controls(controls)

        # Size the canvas to the controls' own natural width (a bare Canvas
        # has no opinion of its own and would otherwise squeeze the sidebar
        # to nothing) so the sidebar's width is unchanged from before it was
        # wrapped in a canvas; only its height is now allowed to scroll.
        controls.update_idletasks()
        canvas.configure(width=controls.winfo_reqwidth())
        return container

    def _build_controls(self, parent):
        # Layout order: everything that affects what Generate will produce
        # (or how the draw-in plays back) comes first, top to bottom, ending
        # in the Generate button itself; everything below the separator only
        # applies to a curve that's already been generated -- appearance,
        # color crawl, replay/save/cleanup -- since none of it means
        # anything until a result exists to present, replay, or save.
        row = 0

        def add_slider(label, var, frm, to, step, fmt="{:.2f}"):
            nonlocal row
            ttk.Label(parent, text=label).grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 0))
            row += 1
            val_label = ttk.Label(parent, text=fmt.format(var.get()), width=8)
            val_label.grid(row=row, column=1, sticky="e")

            def on_move(_evt=None, v=var, l=val_label, s=step, f=fmt):
                snapped = round(v.get() / s) * s
                v.set(snapped)
                l.config(text=f.format(snapped))

            scale = ttk.Scale(parent, from_=frm, to=to, variable=var, command=lambda _v: on_move())
            scale.grid(row=row, column=0, sticky="ew")
            row += 1
            # The scale's `command` (and hence the value label) only fires on
            # user drag, not on a programmatic var.set() -- expose a manual
            # refresh so code that sets the variable directly (presets, the
            # width/height link) can keep the label in sync too.
            scale.refresh_label = lambda v=var, l=val_label, f=fmt: l.config(text=f.format(v.get()))
            return scale

        ttk.Label(parent, text="Flowing Curve Generator", font=("", 13, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 6))
        row += 1

        # ================================================ before generate ====

        # --- Seed ---
        ttk.Label(parent, text="Seed").grid(row=row, column=0, sticky="w", pady=(8, 0))
        row += 1
        seed_frame = ttk.Frame(parent)
        seed_frame.grid(row=row, column=0, columnspan=2, sticky="ew")
        row += 1
        self.seed_var = tk.IntVar(value=17)
        ttk.Entry(seed_frame, textvariable=self.seed_var, width=10).pack(side="left")
        ttk.Button(seed_frame, text="Random", command=self._randomize_seed).pack(side="left", padx=(6, 0))

        # --- Fill shape (needs a regenerate: changes the actual geometry) ---
        ttk.Label(parent, text="Fill shape").grid(row=row, column=0, sticky="w", pady=(8, 0))
        row += 1
        self.fill_shape_var = tk.StringVar(value="square")
        self.shape_combo = ttk.Combobox(parent, textvariable=self.fill_shape_var, values=list(FILL_SHAPES),
                                         state="readonly", width=12)
        self.shape_combo.grid(row=row, column=0, columnspan=2, sticky="w")
        self.shape_combo.bind("<<ComboboxSelected>>", self._on_shape_selected)
        row += 1

        draw_row = ttk.Frame(parent)
        draw_row.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        row += 1
        self.draw_btn = ttk.Button(draw_row, text="Draw custom boundary...", command=self._toggle_draw_mode)
        self.draw_btn.pack(side="left")
        self.clear_draw_btn = ttk.Button(draw_row, text="Clear", command=self._clear_custom_boundary,
                                          state="disabled")
        self.clear_draw_btn.pack(side="left", padx=(6, 0))
        ttk.Label(parent, text="Draws on the preview, at the current width/height.",
                  foreground="#666").grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
        row += 1

        # --- Frame size (needs a regenerate: changes the actual geometry) ---
        self.link_wh_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(parent, text="Square (link width/height)", variable=self.link_wh_var,
                         command=self._on_link_toggle).grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 0))
        row += 1
        self.width_var = tk.IntVar(value=1200)
        self.height_var = tk.IntVar(value=1200)
        self.width_scale = add_slider("Width (px)", self.width_var, 200, 3000, 10, "{:.0f}")
        self.width_scale.bind("<ButtonRelease-1>", self._on_frame_size_changed)
        self.height_scale = add_slider("Height (px)", self.height_var, 200, 3000, 10, "{:.0f}")
        self.height_scale.bind("<ButtonRelease-1>", self._on_frame_size_changed)
        self.height_scale.config(state="disabled")  # linked by default; width drives both
        self.width_var.trace_add("write", self._on_width_changed)

        preset_row = ttk.Frame(parent)
        preset_row.grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1
        ttk.Button(preset_row, text="1920x1080", width=10,
                   command=lambda: self._set_wh(1920, 1080)).pack(side="left")
        ttk.Button(preset_row, text="Square 1200", width=10,
                   command=lambda: self._set_wh(1200, 1200)).pack(side="left", padx=(6, 0))
        ttk.Label(parent, text="1200px square takes ~30-60s. A full\n1920x1080 canvas can take a minute or two.",
                  foreground="#666").grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
        row += 1

        # --- Stroke (generation-time: affects path spacing) ---
        self.stroke_var = tk.DoubleVar(value=3.0)
        add_slider("Stroke width (px, at generation)", self.stroke_var, 1.0, 10.0, 0.5, "{:.1f}")

        # --- Smoothness ---
        self.smoothness_var = tk.DoubleVar(value=1.0)
        add_slider("Smoothness (0.25 tight → 3 round)", self.smoothness_var, 0.25, 3.0, 0.25, "{:.2f}")

        # --- Advanced (collapsible) ---
        self.advanced_visible = tk.BooleanVar(value=False)
        toggle = ttk.Checkbutton(parent, text="Advanced settings", variable=self.advanced_visible,
                                  command=self._toggle_advanced, style="Toolbutton")
        toggle.grid(row=row, column=0, columnspan=2, sticky="w", pady=(12, 0))
        row += 1

        self.advanced_frame = ttk.Frame(parent)
        self.advanced_frame.grid(row=row, column=0, columnspan=2, sticky="ew")
        self.advanced_frame.grid_remove()
        row += 1

        adv_row = 0
        self.gap_var = tk.DoubleVar(value=12.0)
        self.preferred_gap_var = tk.DoubleVar(value=18.0)
        self.edge_var = tk.DoubleVar(value=35.0)
        self.iterations_var = tk.IntVar(value=50)
        self.edge_wave_var = tk.DoubleVar(value=20.0)
        self.attempts_var = tk.IntVar(value=60)

        def add_entry(label, var):
            nonlocal adv_row
            ttk.Label(self.advanced_frame, text=label).grid(row=adv_row, column=0, sticky="w", pady=2)
            ttk.Entry(self.advanced_frame, textvariable=var, width=10).grid(row=adv_row, column=1, sticky="e", pady=2)
            adv_row += 1

        add_entry("Min ink gap (px)", self.gap_var)
        add_entry("Preferred gap (px)", self.preferred_gap_var)
        add_entry("Edge clearance (px)", self.edge_var)
        add_entry("Relax iterations", self.iterations_var)
        add_entry("Edge wave (px)", self.edge_wave_var)
        add_entry("Search attempts", self.attempts_var)
        # Edge clearance changes the shape's own inset, so keep the border
        # preview in sync with it too (not just shape/frame size).
        self.edge_var.trace_add("write", lambda *_a: self._update_shape_preview())

        # --- Animation ---
        self.animate_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(parent, text="Animate drawing", variable=self.animate_var).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(12, 0))
        row += 1
        self.anim_duration_var = tk.DoubleVar(value=2.5)
        add_slider("Draw duration (s)", self.anim_duration_var, 0.5, 15.0, 0.5, "{:.1f}")

        # Replay lives here (rather than down with the rest of the
        # after-generate controls) so it sits right next to the animation
        # settings it replays with -- it's disabled until a generation
        # exists, same as before.
        self.replay_btn = ttk.Button(parent, text="Replay animation", command=self._replay_animation,
                                      state="disabled")
        self.replay_btn.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(16, 4))
        row += 1

        # --- Generate button + progress ---
        self.generate_btn = ttk.Button(parent, text="Generate", command=self._start_generate)
        self.generate_btn.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(4, 4))
        row += 1

        self.progress = ttk.Progressbar(parent, mode="determinate", maximum=100)
        self.progress.grid(row=row, column=0, columnspan=2, sticky="ew")
        row += 1

        self.skip_btn = ttk.Button(parent, text="Skip animation", command=self._skip_animation, state="disabled")
        self.skip_btn.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        row += 1

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(parent, textvariable=self.status_var, foreground="#444", wraplength=240).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(4, 12))
        row += 1

        # ================================================= after generate ====
        # Nothing below here changes what Generate produces -- it presents,
        # animates, replays, or saves the curve that's already been made.
        ttk.Separator(parent, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=(4, 8))
        row += 1

        # --- Appearance (pure presentation -- instant re-render, no regenerate) ---
        ttk.Label(parent, text="Appearance", font=("", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(2, 2))
        row += 1

        color_row = ttk.Frame(parent)
        color_row.grid(row=row, column=0, columnspan=2, sticky="ew")
        row += 1
        self.line_color_var = tk.StringVar(value="#ffffff")
        self.bg_color_var = tk.StringVar(value="#000000")
        self.line_color_btn = tk.Button(color_row, text="Line", width=7,
                                         background=self.line_color_var.get(),
                                         activebackground=self.line_color_var.get(),
                                         foreground=self._contrast_text_color(self.line_color_var.get()),
                                         command=lambda: self._pick_color(self.line_color_var, self.line_color_btn, "Line color"))
        self.line_color_btn.pack(side="left")
        self.bg_color_btn = tk.Button(color_row, text="Background", width=10,
                                       background=self.bg_color_var.get(),
                                       activebackground=self.bg_color_var.get(),
                                       foreground=self._contrast_text_color(self.bg_color_var.get()),
                                       command=lambda: self._pick_color(self.bg_color_var, self.bg_color_btn, "Background color"))
        self.bg_color_btn.pack(side="left", padx=(6, 0))

        ttk.Label(parent, text="Display stroke (px, after generation)").grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(10, 0))
        row += 1
        self.display_stroke_label = ttk.Label(parent, text="--", width=8)
        self.display_stroke_label.grid(row=row, column=1, sticky="e")
        self.display_stroke_var = tk.DoubleVar(value=3.0)
        self.display_stroke_scale = ttk.Scale(parent, from_=1.0, to=3.0, variable=self.display_stroke_var,
                                               command=self._on_display_stroke_move, state="disabled")
        self.display_stroke_scale.grid(row=row, column=0, sticky="ew")
        self.display_stroke_scale.bind("<ButtonRelease-1>", self._on_display_stroke_release)
        row += 1
        ttk.Label(parent, text="Generate once to unlock. Capped so the line\ncan never thicken enough to touch itself.",
                  foreground="#666").grid(row=row, column=0, columnspan=2, sticky="w", pady=(2, 0))
        row += 1

        # --- Color crawl (looping chasing-lights animation, pure presentation) ---
        ttk.Label(parent, text="Color crawl", font=("", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(14, 2))
        row += 1

        crawl_color_row = ttk.Frame(parent)
        crawl_color_row.grid(row=row, column=0, columnspan=2, sticky="ew")
        row += 1
        self.crawl_color_vars = [tk.StringVar(value=c) for c in ("#ff595e", "#8ac926", "#1982c4")]
        for i, cvar in enumerate(self.crawl_color_vars):
            btn = tk.Button(crawl_color_row, text=f"Crawler {i + 1}", width=9,
                             background=cvar.get(), activebackground=cvar.get(),
                             foreground=self._contrast_text_color(cvar.get()))
            btn.config(command=lambda v=cvar, b=btn, n=i + 1: self._pick_crawl_color(v, b, f"Crawler {n} color"))
            btn.pack(side="left", padx=(0 if i == 0 else 4, 0))

        self.crawler_size_var = tk.DoubleVar(value=40.0)
        add_slider("Crawler size (px)", self.crawler_size_var, 4.0, 300.0, 2.0, "{:.0f}")
        self.crawl_gap_var = tk.DoubleVar(value=20.0)
        add_slider("Gap between crawlers (px)", self.crawl_gap_var, 0.0, 300.0, 2.0, "{:.0f}")
        self.crawl_speed_var = tk.DoubleVar(value=300.0)
        add_slider("Crawl speed (px/s)", self.crawl_speed_var, 10.0, 2000.0, 10.0, "{:.0f}")

        self.crawl_btn = ttk.Button(parent, text="Start crawl", command=self._toggle_crawl, state="disabled")
        self.crawl_btn.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(4, 4))
        row += 1
        ttk.Label(parent, text="Generate once to unlock. Colors/sliders\nupdate live while it's running.",
                  foreground="#666").grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
        row += 1

        # --- Save / cleanup ---
        self.save_btn = ttk.Button(parent, text="Save As...", command=self._save_as, state="disabled")
        self.save_btn.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(8, 4))
        row += 1
        ttk.Button(parent, text="Clear saved copies", command=self._clear_outputs).grid(
            row=row, column=0, columnspan=2, sticky="ew")
        row += 1

        # --- Live wallpaper (independent of everything above -- a ---
        # separate background process, see wallpaper_engine.py) ---
        ttk.Label(parent, text="Live wallpaper", font=("", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(14, 2))
        row += 1
        ttk.Button(parent, text="Configure Wallpaper...", command=self._open_wallpaper_dialog).grid(
            row=row, column=0, columnspan=2, sticky="ew")
        row += 1
        ttk.Label(parent, text="Windows only. A rotating set of colors/shapes\n"
                               "crawling behind your desktop icons, one per day.",
                  foreground="#666").grid(row=row, column=0, columnspan=2, sticky="w", pady=(2, 0))
        row += 1

    def _build_preview(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        self.preview_canvas = tk.Canvas(parent, width=PREVIEW_DISPLAY_SIZE, height=PREVIEW_DISPLAY_SIZE,
                                         background="#111", highlightthickness=0)
        self.preview_canvas.grid(row=0, column=0, sticky="nsew")
        self._placeholder_text_id = self.preview_canvas.create_text(
            PREVIEW_DISPLAY_SIZE / 2, PREVIEW_DISPLAY_SIZE / 2,
            text="Nothing generated yet", fill="#888")
        self.preview_canvas.bind("<ButtonPress-1>", self._on_canvas_press)
        self.preview_canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self.preview_canvas.bind("<ButtonRelease-1>", self._on_canvas_release)

        self.info_var = tk.StringVar(value="")
        ttk.Label(parent, textvariable=self.info_var, foreground="#444").grid(row=1, column=0, sticky="w", pady=(6, 0))

    @staticmethod
    def _preview_scale_offset(width, height):
        """Map full generation-space (width x height) coordinates onto the
        square on-screen preview canvas: uniformly scaled to fit (never
        distorted) and centered, so a wide/tall rectangle gets letterboxed
        within the box instead of stretched. On a square canvas this reduces
        to exactly the old `PREVIEW_DISPLAY_SIZE / size` with zero offset."""
        scale = min(PREVIEW_DISPLAY_SIZE / width, PREVIEW_DISPLAY_SIZE / height)
        disp_w, disp_h = width * scale, height * scale
        return scale, (PREVIEW_DISPLAY_SIZE - disp_w) / 2, (PREVIEW_DISPLAY_SIZE - disp_h) / 2

    def _on_shape_selected(self, _evt=None):
        # Picking a shape from the dropdown while a custom boundary exists
        # abandons that boundary -- the dropdown and the drawn boundary are
        # mutually exclusive ways of choosing what gets filled.
        if self.fill_shape_var.get() != "custom" and self.custom_region is not None:
            self.custom_region = None
            self.custom_points = []
            self.clear_draw_btn.config(state="disabled")
        self._update_shape_preview()

    def _update_shape_preview(self, *_args):
        """Draw just the outline of the currently selected fill shape (at
        the current width/height/edge clearance) on the canvas, so you can
        see what a Generate would fill before spending the time to run one.
        Whatever was previously generated no longer matches these settings,
        so this also resets the controls that depend on a live result."""
        if self.busy or self._drawing or self._draw_mode:
            return  # don't clobber a generation/animation/draw-in-progress
        try:
            width = self.width_var.get()
            height = self.height_var.get()
            edge = self.edge_var.get()
            shape = self.fill_shape_var.get()
        except tk.TclError:
            return  # mid-edit -- leave the canvas as-is

        self._stop_crawl(restore=False)  # about to redraw the canvas ourselves

        if shape == "custom":
            if self.custom_region is not None:
                self._redraw_custom_boundary_outline()
            else:
                self.preview_canvas.config(background=self.bg_color_var.get())
                self.preview_canvas.delete("all")
                self.preview_canvas.create_text(
                    PREVIEW_DISPLAY_SIZE / 2, PREVIEW_DISPLAY_SIZE / 2,
                    text='Click "Draw custom boundary..."\nand drag on this canvas',
                    fill="#888", justify="center")
                self.status_var.set("Ready.")
                self.info_var.set("No boundary drawn yet.")
            return

        try:
            region = fill_polygon(shape, (width, height), edge)
        except GenerationError:
            return  # momentarily invalid combination -- leave the canvas as-is

        self.last_path = None
        self.last_report = None
        self.save_btn.config(state="disabled")
        self.replay_btn.config(state="disabled")
        self.crawl_btn.config(state="disabled")
        self.display_stroke_scale.config(state="disabled")
        self.display_stroke_label.config(text="--")

        scale, off_x, off_y = self._preview_scale_offset(width, height)
        coords = []
        for x, y in region.exterior.coords:
            coords.extend([x * scale + off_x, y * scale + off_y])

        self.preview_canvas.config(background=self.bg_color_var.get())
        self.preview_canvas.delete("all")
        self.preview_canvas.create_polygon(coords, outline="#888888", fill="", width=2, dash=(5, 3))
        self.status_var.set("Ready.")
        self.info_var.set(f"Shape preview: {shape}. Click Generate to fill it.")

    # ------------------------------------------------------ width/height ----

    def _on_link_toggle(self):
        if self.link_wh_var.get():
            self.height_var.set(self.width_var.get())
            self.height_scale.config(state="disabled")
        else:
            self.height_scale.config(state="normal")
        self.height_scale.refresh_label()
        self._on_frame_size_changed()

    def _on_width_changed(self, *_args):
        if not self.link_wh_var.get():
            return
        try:
            w = self.width_var.get()
        except tk.TclError:
            return
        if self.height_var.get() != w:
            self.height_var.set(w)
        self.height_scale.refresh_label()

    def _set_wh(self, w, h):
        square = (w == h)
        self.link_wh_var.set(square)
        self.height_scale.config(state="disabled" if square else "normal")
        self.width_var.set(w)
        self.height_var.set(h)
        self.width_scale.refresh_label()
        self.height_scale.refresh_label()
        self._on_frame_size_changed()

    def _on_frame_size_changed(self, _evt=None):
        # A drawn boundary is tied to the width/height it was drawn at; once
        # the canvas size changes it no longer lines up, so drop it rather
        # than silently filling a stale/mismatched shape.
        if self.custom_region is not None:
            self._clear_custom_boundary()
        else:
            self._update_shape_preview()

    # --------------------------------------------------- draw custom shape ----

    def _toggle_draw_mode(self):
        if self.busy:
            return
        self._stop_crawl(restore=False)  # about to take over the canvas either way
        if self._draw_mode:
            self._draw_mode = False
            self._drawing = False
            self.draw_btn.config(text="Draw custom boundary...")
            if self.custom_region is None:
                self.shape_combo.config(state="readonly")
                self.fill_shape_var.set("square")
            self._update_shape_preview()
            return
        self._draw_mode = True
        self.custom_points = []
        self.fill_shape_var.set("custom")
        self.shape_combo.config(state="disabled")
        self.draw_btn.config(text="Drawing... (click to cancel)")
        self.status_var.set("Click and drag on the preview to draw your boundary; release to finish.")
        self.preview_canvas.config(background=self.bg_color_var.get())
        self.preview_canvas.delete("all")
        self.preview_canvas.create_text(PREVIEW_DISPLAY_SIZE / 2, PREVIEW_DISPLAY_SIZE / 2,
                                         text="Click and drag to draw a boundary", fill="#888", justify="center")

    def _on_canvas_press(self, event):
        if not self._draw_mode or self.busy:
            return
        self._drawing = True
        self.custom_points = [(event.x, event.y)]
        self.preview_canvas.delete("draw_stroke")

    def _on_canvas_drag(self, event):
        if not self._drawing:
            return
        last = self.custom_points[-1]
        if (event.x - last[0]) ** 2 + (event.y - last[1]) ** 2 >= 4:  # skip near-duplicate points
            self.preview_canvas.create_line(last[0], last[1], event.x, event.y,
                                             fill="#ffcc66", width=2, tags="draw_stroke")
            self.custom_points.append((event.x, event.y))

    def _on_canvas_release(self, _event):
        if not self._drawing:
            return
        self._drawing = False
        self._draw_mode = False
        self.draw_btn.config(text="Draw custom boundary...")
        if len(self.custom_points) < 3:
            self.status_var.set('Boundary too short -- click "Draw custom boundary..." and try a longer stroke.')
            self.custom_points = []
            self.shape_combo.config(state="readonly")
            self.fill_shape_var.set("square")
            self._update_shape_preview()
            return
        self._finalize_custom_boundary()

    def _finalize_custom_boundary(self):
        try:
            width, height = self.width_var.get(), self.height_var.get()
        except tk.TclError:
            width, height = 1200, 1200
        scale, off_x, off_y = self._preview_scale_offset(width, height)
        full_pts = [((cx - off_x) / scale, (cy - off_y) / scale) for cx, cy in self.custom_points]
        full_pts = [(min(max(x, 0), width), min(max(y, 0), height)) for x, y in full_pts]
        try:
            poly = ShapelyPolygon(full_pts)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.is_empty or poly.geom_type != "Polygon" or poly.area < 1000:
                raise ValueError("boundary too small/degenerate")
        except Exception:
            messagebox.showerror(
                "Boundary not usable",
                "That stroke didn't form a clean closed shape. Try drawing a simpler loop.")
            self.custom_points = []
            self.preview_canvas.delete("draw_stroke")
            self.shape_combo.config(state="readonly")
            self.fill_shape_var.set("square")
            self._update_shape_preview()
            return
        self.custom_region = poly
        self.custom_points = []
        self.clear_draw_btn.config(state="normal")
        self._redraw_custom_boundary_outline()

    def _redraw_custom_boundary_outline(self):
        self._stop_crawl(restore=False)  # about to redraw the canvas ourselves
        width, height = self.width_var.get(), self.height_var.get()
        scale, off_x, off_y = self._preview_scale_offset(width, height)
        coords = []
        for x, y in self.custom_region.exterior.coords:
            coords.extend([x * scale + off_x, y * scale + off_y])

        self.last_path = None
        self.last_report = None
        self.save_btn.config(state="disabled")
        self.replay_btn.config(state="disabled")
        self.crawl_btn.config(state="disabled")
        self.display_stroke_scale.config(state="disabled")
        self.display_stroke_label.config(text="--")

        self.preview_canvas.config(background=self.bg_color_var.get())
        self.preview_canvas.delete("all")
        self.preview_canvas.create_polygon(coords, outline="#ffcc66", fill="", width=2, dash=(5, 3))
        self.status_var.set("Ready.")
        self.info_var.set("Custom boundary ready. Click Generate to fill it.")

    def _clear_custom_boundary(self):
        if self.busy:
            return
        self.custom_region = None
        self.custom_points = []
        self.clear_draw_btn.config(state="disabled")
        self.shape_combo.config(state="readonly")
        if self.fill_shape_var.get() == "custom":
            self.fill_shape_var.set("square")
        self._update_shape_preview()

    def _toggle_advanced(self):
        if self.advanced_visible.get():
            self.advanced_frame.grid()
        else:
            self.advanced_frame.grid_remove()

    def _randomize_seed(self):
        self.seed_var.set(random.randint(0, 999_999))

    # ------------------------------------------------------- generation ----

    def _start_generate(self):
        if self.busy:
            return
        shape = self.fill_shape_var.get()
        if shape == "custom" and self.custom_region is None:
            messagebox.showerror(
                "No boundary drawn",
                'Fill shape is set to "custom" but no boundary has been drawn yet. '
                'Click "Draw custom boundary..." and drag on the preview, or pick a different shape.')
            return
        try:
            params = dict(
                size=(self.width_var.get(), self.height_var.get()),
                gap=self.gap_var.get(),
                preferred_gap=self.preferred_gap_var.get(),
                iterations=self.iterations_var.get(),
                smoothness=round(self.smoothness_var.get(), 4),
                edge_wave=self.edge_wave_var.get(),
                stroke=self.stroke_var.get(),
                edge=self.edge_var.get(),
                seed=self.seed_var.get(),
                attempts=self.attempts_var.get(),
                fill_shape=shape,
            )
            if shape == "custom":
                params["custom_region"] = self.custom_region
        except tk.TclError:
            messagebox.showerror("Invalid input", "One of the fields isn't a valid number.")
            return

        self._stop_crawl(restore=False)  # about to overwrite the canvas with a new run anyway
        self.busy = True
        self._gen_token += 1  # invalidate any still-running animation from a previous generate
        self._anim_skip = False
        self.generate_btn.config(state="disabled")
        self.save_btn.config(state="disabled")
        self.replay_btn.config(state="disabled")
        self.crawl_btn.config(state="disabled")
        self.skip_btn.config(state="disabled")
        self.progress.config(value=0)
        self.status_var.set("Starting...")

        thread = threading.Thread(target=self._worker, args=(params,), daemon=True)
        thread.start()

    def _worker(self, params):
        t0 = time.time()

        def on_progress(phase, current, total):
            pct = 100.0 * current / max(total, 1)
            if phase == "search":
                pct *= 0.15  # search is usually quick; give relax the bulk of the bar
            else:
                pct = 15 + pct * 0.75
            self.worker_queue.put(("progress", phase, pct))

        try:
            p, report = generate(progress_callback=on_progress, **params)
            self.worker_queue.put(("progress", "render", 92))
            line_color = self.line_color_var.get()
            bg_color = self.bg_color_var.get()
            img = render_png(p, params["size"], params["stroke"], line_color=line_color, bg_color=bg_color)
            svg_text = render_svg(p, params["size"], params["stroke"], line_color=line_color, bg_color=bg_color)
            elapsed = time.time() - t0
            self.worker_queue.put(("done", p, report, img, svg_text, params, elapsed))
        except GenerationError as e:
            self.worker_queue.put(("error", str(e)))
        except Exception as e:  # unexpected bug: still don't crash the GUI
            self.worker_queue.put(("error", f"Unexpected error: {e}"))

    def _poll_queue(self):
        try:
            while True:
                msg = self.worker_queue.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    _, phase, pct = msg
                    self.progress.config(value=pct)
                    self.status_var.set(f"Generating... ({phase})")
                elif kind == "done":
                    _, p, report, img, svg_text, params, elapsed = msg
                    self._on_generation_done(p, report, img, svg_text, params, elapsed)
                elif kind == "error":
                    self._on_generation_error(msg[1])
                elif kind == "rerendered":
                    _, img, svg_text = msg
                    self._on_rerendered(img, svg_text)
                elif kind == "replay_ready":
                    _, token, p, size, stroke, img = msg
                    self._on_replay_ready(token, p, size, stroke, img)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _on_generation_done(self, p, report, img, svg_text, params, elapsed):
        self.busy = False
        self.generate_btn.config(state="normal")
        self.save_btn.config(state="normal")
        self.replay_btn.config(state="normal")
        self.crawl_btn.config(state="normal")
        self.progress.config(value=100)
        self.status_var.set(f"Done in {elapsed:.1f}s (seed {report['seed']}).")

        self.last_path = p
        self.last_report = report
        self.last_size = params["size"]
        self.last_stroke = params["stroke"]
        self.last_generation_stroke = params["stroke"]
        self._last_static_img = img

        # Unlock "Display stroke": bounded so it can thin freely but can only
        # thicken up to the point the validated minimum gap would hit zero.
        gen_stroke = params["stroke"]
        max_stroke = max_safe_render_stroke(report, gen_stroke)
        min_stroke = max(0.5, gen_stroke * 0.2)
        self.display_stroke_scale.config(from_=min_stroke, to=max_stroke, state="normal")
        self.display_stroke_var.set(gen_stroke)
        self.display_stroke_label.config(text=f"{gen_stroke:.1f}")

        # Overwrite the single preview file set -- never a naming conflict.
        png_path = OUTPUT_DIR / f"{PREVIEW_BASENAME}.png"
        svg_path = OUTPUT_DIR / f"{PREVIEW_BASENAME}.svg"
        json_path = OUTPUT_DIR / f"{PREVIEW_BASENAME}.validation.json"
        img.save(png_path)
        svg_path.write_text(svg_text)
        json_path.write_text(json.dumps(report, indent=2))

        canvas_w = report.get("canvas_width", img.size[0])
        canvas_h = report.get("canvas_height", img.size[1])
        self.info_var.set(
            f"seed={report['seed']}  shape={report.get('fill_shape', 'square')}  "
            f"{canvas_w:.0f}x{canvas_h:.0f}px  min gap={report['minimum_nonlocal_ink_gap_px']}px  "
            f"coverage={report['covered_fraction']*100:.0f}%  saved to outputs/{PREVIEW_BASENAME}.png"
        )

        if self.animate_var.get():
            self._animate_draw(p, params["size"], params["stroke"], img)
        else:
            self._show_preview(img)

    # ----------------------------------------------------------- drawing ----

    def _animate_draw(self, p, size, stroke, final_img):
        """Progressively reveal the already-computed path on the canvas, like
        watching it get drawn. Purely playback -- the path/image were already
        fully computed and validated before this runs. `size` is a
        (width, height) pair; the path is fit-to-box and centered exactly
        like the shape-border preview, so it lines up with it."""
        token = self._gen_token
        width, height = size
        self.preview_canvas.config(background=self.bg_color_var.get())
        self.preview_canvas.delete("all")
        scale, off_x, off_y = self._preview_scale_offset(width, height)
        coords = ((p * scale) + [off_x, off_y]).flatten().tolist()  # x0,y0,x1,y1,...
        n_points = len(coords) // 2
        if n_points < 2:
            self._show_preview(final_img)
            return

        line_id = self.preview_canvas.create_line(
            *coords[:4], fill=self.line_color_var.get(), width=max(1.0, stroke * scale),
            capstyle=tk.ROUND, joinstyle=tk.ROUND)

        duration_ms = max(200, self.anim_duration_var.get() * 1000)
        frame_ms = 30
        n_frames = max(1, int(duration_ms / frame_ms))
        points_per_frame = max(1, n_points // n_frames)

        self.skip_btn.config(state="normal")
        self.replay_btn.config(state="disabled")  # avoid overlapping animations on the same canvas

        def step(next_point=2):
            if token != self._gen_token:
                return  # a newer generation/replay started; abandon this animation
            if self._anim_skip:
                next_point = n_points
            end = min(n_points, next_point + points_per_frame)
            self.preview_canvas.coords(line_id, *coords[: end * 2])
            if end >= n_points:
                self.skip_btn.config(state="disabled")
                if not self.busy:
                    self.replay_btn.config(state="normal")
                self._show_preview(final_img)  # swap in the crisp anti-aliased render to finish
                return
            self.after(frame_ms, lambda: step(end))

        step()

    def _skip_animation(self):
        self._anim_skip = True

    def _replay_animation(self):
        """Re-play the draw-in animation for the already-generated curve,
        using whatever colors/stroke are currently selected -- no
        regeneration, just a fresh render + playback of the existing path."""
        if self.last_path is None or self.busy:
            return
        self._stop_crawl(restore=False)  # about to take over the canvas anyway
        self._gen_token += 1  # cancel any animation still running (generate or a prior replay)
        self._anim_skip = False
        token = self._gen_token
        self.replay_btn.config(state="disabled")

        p = self.last_path
        size = self.last_size
        stroke = self.display_stroke_var.get()
        line_color = self.line_color_var.get()
        bg_color = self.bg_color_var.get()

        def work():
            img = render_png(p, size, stroke, line_color=line_color, bg_color=bg_color)
            self.worker_queue.put(("replay_ready", token, p, size, stroke, img))

        threading.Thread(target=work, daemon=True).start()

    def _on_replay_ready(self, token, p, size, stroke, img):
        if token != self._gen_token:
            return  # superseded by a newer generate/replay while rendering
        self._last_static_img = img
        self._animate_draw(p, size, stroke, img)

    # ------------------------------------------------------- color crawl ----

    def _pick_crawl_color(self, var, swatch_btn, title):
        _rgb, hexval = colorchooser.askcolor(color=var.get(), title=title)
        if not hexval:
            return  # user cancelled
        var.set(hexval)
        swatch_btn.config(background=hexval, activebackground=hexval,
                           foreground=self._contrast_text_color(hexval))
        # Nothing else to do: a running crawl reads these vars fresh every
        # tick, and a stopped one just shows the new swatch until started.

    def _toggle_crawl(self):
        if self.last_path is None or self.busy:
            return
        if self._crawl_running:
            self._stop_crawl()
        else:
            self._start_crawl()

    def _start_crawl(self):
        self._gen_token += 1  # cancel any draw-in animation still running on this canvas
        self._anim_skip = False
        self.skip_btn.config(state="disabled")
        self._crawl_running = True
        self._crawl_token += 1
        self._crawl_phase = 0.0
        self._crawl_last_tick = time.time()
        self.crawl_btn.config(text="Stop crawl")
        self.replay_btn.config(state="disabled")
        self.save_btn.config(state="disabled")  # "Save As" saves the static render, not a crawl frame
        self._crawl_tick(self._crawl_token)

    def _stop_crawl(self, restore=True):
        """Stop the crawl loop. `restore=False` skips redrawing the static
        image, for callers that are about to draw something else on the
        canvas themselves right after (a new generate, a shape-preview
        redraw, starting to draw a custom boundary, etc.) -- avoids a
        pointless flash of the old static render in between."""
        was_running = self._crawl_running
        self._crawl_running = False
        self.crawl_btn.config(text="Start crawl")
        if self.last_path is not None and not self.busy:
            self.replay_btn.config(state="normal")
            self.save_btn.config(state="normal")
        if restore and was_running and self._last_static_img is not None:
            self._show_preview(self._last_static_img)

    def _crawl_tick(self, token):
        """One frame of the chasing-lights animation: recolor the whole
        curve as a sequence of Tkinter line segments per crawl_bands(), then
        schedule the next frame. Crawler size/gap/speed and all 3 colors are
        read fresh every tick, so they update live while this is running --
        no need to stop/restart to see a change. Runs until _stop_crawl()
        (or something else takes over the canvas and bumps the token)."""
        if not self._crawl_running or token != self._crawl_token or self.last_path is None:
            return
        now = time.time()
        dt = max(0.0, min(0.25, now - self._crawl_last_tick))  # clamp a stall/lag spike
        self._crawl_last_tick = now
        try:
            speed = max(0.0, self.crawl_speed_var.get())
            crawler_len = max(1.0, self.crawler_size_var.get())
            gap_len = max(0.0, self.crawl_gap_var.get())
        except tk.TclError:
            speed, crawler_len, gap_len = 300.0, 40.0, 20.0
        period = 3 * (crawler_len + gap_len)
        self._crawl_phase = (self._crawl_phase + speed * dt) % period

        width, height = self.last_size
        scale, off_x, off_y = self._preview_scale_offset(width, height)
        colors = [v.get() for v in self.crawl_color_vars]
        stroke = self.display_stroke_var.get()

        self.preview_canvas.config(background=self.bg_color_var.get())
        self.preview_canvas.delete("all")
        for color_idx, pts in crawl_bands(self.last_path, crawler_len, gap_len, self._crawl_phase, n_colors=3):
            coords = ((pts * scale) + [off_x, off_y]).flatten().tolist()
            self.preview_canvas.create_line(*coords, fill=colors[color_idx],
                                             width=max(1.0, stroke * scale),
                                             capstyle=tk.ROUND, joinstyle=tk.ROUND)

        self.after(30, lambda: self._crawl_tick(token))

    def _on_generation_error(self, message):
        self.busy = False
        self.generate_btn.config(state="normal")
        self.skip_btn.config(state="disabled")
        self.progress.config(value=0)
        self.status_var.set("Failed - see message.")
        messagebox.showerror("Generation failed", message)

    def _show_preview(self, img):
        w, h = img.size
        scale = min(PREVIEW_DISPLAY_SIZE / w, PREVIEW_DISPLAY_SIZE / h, 1.0)
        disp = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        self.preview_photo = ImageTk.PhotoImage(disp)
        self.preview_canvas.config(background=self.bg_color_var.get())
        self.preview_canvas.delete("all")
        self.preview_canvas.create_image(PREVIEW_DISPLAY_SIZE / 2, PREVIEW_DISPLAY_SIZE / 2,
                                          anchor="center", image=self.preview_photo)

    # ------------------------------------------------- appearance / re-render ----

    @staticmethod
    def _contrast_text_color(hex_color):
        """Black or white text, whichever reads better against hex_color."""
        h = hex_color.lstrip("#")
        try:
            r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        except (ValueError, IndexError):
            return "#000000"
        luminance = 0.299 * r + 0.587 * g + 0.114 * b
        return "#000000" if luminance > 140 else "#ffffff"

    def _pick_color(self, var, swatch_btn, title):
        _rgb, hexval = colorchooser.askcolor(color=var.get(), title=title)
        if not hexval:
            return  # user cancelled
        var.set(hexval)
        swatch_btn.config(background=hexval, activebackground=hexval,
                           foreground=self._contrast_text_color(hexval))
        self._maybe_rerender()

    def _on_display_stroke_move(self, _val):
        self.display_stroke_label.config(text=f"{self.display_stroke_var.get():.1f}")

    def _on_display_stroke_release(self, _evt):
        self._maybe_rerender()

    def _maybe_rerender(self):
        """Redraw the already-computed path with the current color/stroke
        choices. Pure presentation: does not touch the underlying geometry,
        so this is a fast re-render, not a regenerate, and is safe to fire
        from a color pick or a stroke-slider release even while nothing new
        is being generated. Skipped while the crawl animation is running --
        it already redraws every tick and reads bg_color live on its own, so
        there's nothing for this to usefully update."""
        if self.last_path is None or self.busy or self._rerendering or self._crawl_running:
            return
        self._rerendering = True
        p = self.last_path
        size = self.last_size
        stroke = self.display_stroke_var.get()
        line_color = self.line_color_var.get()
        bg_color = self.bg_color_var.get()

        def work():
            img = render_png(p, size, stroke, line_color=line_color, bg_color=bg_color)
            svg_text = render_svg(p, size, stroke, line_color=line_color, bg_color=bg_color)
            self.worker_queue.put(("rerendered", img, svg_text))

        threading.Thread(target=work, daemon=True).start()

    def _on_rerendered(self, img, svg_text):
        self._rerendering = False
        self.last_stroke = self.display_stroke_var.get()

        png_path = OUTPUT_DIR / f"{PREVIEW_BASENAME}.png"
        svg_path = OUTPUT_DIR / f"{PREVIEW_BASENAME}.svg"
        img.save(png_path)
        svg_path.write_text(svg_text)

        self._last_static_img = img
        self._show_preview(img)
        gen_stroke = self.last_generation_stroke
        disp_stroke = self.last_stroke
        note = "" if abs(disp_stroke - gen_stroke) < 0.05 else f"  (generated at {gen_stroke:.1f}px)"
        self.info_var.set(
            f"seed={self.last_report['seed']}  display stroke={disp_stroke:.1f}px{note}  "
            f"saved to outputs/{PREVIEW_BASENAME}.png"
        )

    # ------------------------------------------------------------ saving ----

    def _save_as(self):
        if self.last_path is None:
            return
        dest = filedialog.asksaveasfilename(
            title="Save curve as...",
            defaultextension=".png",
            filetypes=[("PNG image", "*.png"), ("SVG image", "*.svg"), ("All files", "*.*")],
            initialdir=str(OUTPUT_DIR),
            initialfile=f"curve_seed{self.last_report['seed']}.png",
        )
        if not dest:
            return
        dest = Path(dest)
        src_png = OUTPUT_DIR / f"{PREVIEW_BASENAME}.png"
        src_svg = OUTPUT_DIR / f"{PREVIEW_BASENAME}.svg"
        src_json = OUTPUT_DIR / f"{PREVIEW_BASENAME}.validation.json"
        try:
            if dest.suffix.lower() == ".svg":
                shutil.copyfile(src_svg, dest)
            else:
                shutil.copyfile(src_png, dest)
            # Also drop the matching SVG + report next to it for convenience.
            shutil.copyfile(src_svg, dest.with_suffix(".svg"))
            shutil.copyfile(src_json, dest.with_suffix(".validation.json"))
        except OSError as e:
            messagebox.showerror("Save failed", str(e))
            return
        self.status_var.set(f"Saved to {dest.name}")

    def _clear_outputs(self):
        keep = {f"{PREVIEW_BASENAME}.png", f"{PREVIEW_BASENAME}.svg", f"{PREVIEW_BASENAME}.validation.json"}
        removed = 0
        for f in OUTPUT_DIR.iterdir():
            if f.is_file() and f.name not in keep:
                f.unlink()
                removed += 1
        messagebox.showinfo("Cleared", f"Removed {removed} saved file(s). The current preview was kept.")

    # ------------------------------------------------------ live wallpaper ----
    # A "Wallpaper" dialog for configuring wallpaper_engine.py's daily-rotating
    # preset list (see that file for the engine itself). Deliberately a
    # separate Toplevel rather than more sidebar controls: unlike everything
    # above, none of this depends on a curve having been generated here, and
    # the wallpaper process it can start keeps running independently of this
    # window (and of the app) once launched.

    def _open_wallpaper_dialog(self):
        if self._wallpaper_dialog is not None and self._wallpaper_dialog.winfo_exists():
            self._wallpaper_dialog.lift()
            self._wallpaper_dialog.focus_force()
            return

        self._wp_cfg = load_wallpaper_config()
        self._wp_saved_snapshot = json.dumps(self._wp_cfg, sort_keys=True)
        self._wp_selected = 0 if self._wp_cfg["presets"] else -1

        win = tk.Toplevel(self)
        win.title("Live Wallpaper")
        win.transient(self)
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", self._close_wallpaper_dialog)
        self._wallpaper_dialog = win

        self._build_wallpaper_dialog(win)
        self._wp_update_run_buttons()

    def _close_wallpaper_dialog(self):
        if json.dumps(self._wp_cfg, sort_keys=True) != self._wp_saved_snapshot:
            if not messagebox.askyesno("Unsaved changes", "Discard unsaved wallpaper changes?",
                                        parent=self._wallpaper_dialog):
                return
        self._wallpaper_dialog.destroy()
        self._wallpaper_dialog = None

    def _build_wallpaper_dialog(self, win):
        pad = ttk.Frame(win, padding=12)
        pad.grid(row=0, column=0, sticky="nsew")

        # --- preset list (left column) ---
        list_frame = ttk.Frame(pad)
        list_frame.grid(row=0, column=0, sticky="n", padx=(0, 14))
        ttk.Label(list_frame, text="Daily rotation", font=("", 10, "bold")).pack(anchor="w")
        ttk.Label(list_frame, text="One per day, in order,\nwrapping back to the top.",
                  foreground="#666").pack(anchor="w", pady=(0, 4))
        self._wp_listbox = tk.Listbox(list_frame, height=10, width=10, exportselection=False)
        self._wp_listbox.pack(fill="y")
        self._wp_listbox.bind("<<ListboxSelect>>", self._on_wallpaper_preset_selected)

        list_btn_row = ttk.Frame(list_frame)
        list_btn_row.pack(fill="x", pady=(4, 0))
        ttk.Button(list_btn_row, text="Add", width=6, command=self._wp_add_preset).grid(
            row=0, column=0, padx=1, pady=1)
        self._wp_remove_btn = ttk.Button(list_btn_row, text="Remove", width=8, command=self._wp_remove_preset)
        self._wp_remove_btn.grid(row=0, column=1, padx=1, pady=1)
        ttk.Button(list_btn_row, text="▲", width=3, command=lambda: self._wp_move_preset(-1)).grid(
            row=1, column=0, padx=1, pady=1)
        ttk.Button(list_btn_row, text="▼", width=3, command=lambda: self._wp_move_preset(1)).grid(
            row=1, column=1, padx=1, pady=1)

        # --- selected-preset editor (right column, top) ---
        editor = ttk.Frame(pad)
        editor.grid(row=0, column=1, sticky="n")
        erow = 0

        ttk.Label(editor, text="Selected preset", font=("", 10, "bold")).grid(
            row=erow, column=0, columnspan=2, sticky="w")
        erow += 1

        ttk.Label(editor, text="Fill shape").grid(row=erow, column=0, sticky="w", pady=(8, 0))
        erow += 1
        self._wp_shape_var = tk.StringVar(value="square")
        shape_combo = ttk.Combobox(editor, textvariable=self._wp_shape_var, values=WALLPAPER_SHAPE_CHOICES,
                                    state="readonly", width=12)
        shape_combo.grid(row=erow, column=0, columnspan=2, sticky="w")
        shape_combo.bind("<<ComboboxSelected>>", lambda _e: self._wp_editor_changed())
        erow += 1

        color_row = ttk.Frame(editor)
        color_row.grid(row=erow, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        erow += 1
        self._wp_color_vars = [tk.StringVar(value=c) for c in ("#ff595e", "#ffd166", "#7fe7c4")]
        self._wp_color_btns = []
        for i, cvar in enumerate(self._wp_color_vars):
            btn = tk.Button(color_row, text=f"Color {i + 1}", width=9,
                             background=cvar.get(), activebackground=cvar.get(),
                             foreground=self._contrast_text_color(cvar.get()))
            btn.config(command=lambda v=cvar, b=btn, n=i + 1: self._wp_pick_color(v, b, f"Color {n}"))
            btn.pack(side="left", padx=(0 if i == 0 else 4, 0))
            self._wp_color_btns.append(btn)

        def wp_slider(label, frm, to, step, fmt="{:.0f}"):
            nonlocal erow
            ttk.Label(editor, text=label).grid(row=erow, column=0, columnspan=2, sticky="w", pady=(8, 0))
            erow += 1
            val_label = ttk.Label(editor, text=fmt.format(frm), width=8)
            val_label.grid(row=erow, column=1, sticky="e")
            var = tk.DoubleVar(value=frm)

            def on_move(_evt=None, v=var, l=val_label, s=step, f=fmt):
                snapped = round(v.get() / s) * s
                v.set(snapped)
                l.config(text=f.format(snapped))
                self._wp_editor_changed()

            scale = ttk.Scale(editor, from_=frm, to=to, variable=var, command=lambda _v: on_move())
            scale.grid(row=erow, column=0, sticky="ew")
            erow += 1
            scale.refresh_label = lambda v=var, l=val_label, f=fmt: l.config(text=f.format(v.get()))
            return var, scale

        self._wp_size_var, self._wp_size_scale = wp_slider("Crawler size (px)", 4.0, 300.0, 2.0)
        self._wp_gap_var, self._wp_gap_scale = wp_slider("Gap between crawlers (px)", 0.0, 300.0, 2.0)
        self._wp_speed_var, self._wp_speed_scale = wp_slider("Crawl speed (px/s)", 10.0, 2000.0, 10.0)

        ttk.Button(editor, text="Copy from current Color crawl settings",
                   command=self._wp_copy_from_generator).grid(
            row=erow, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        erow += 1
        ttk.Label(editor, text="Grabs the colors/crawler size/gap/speed\n"
                               "set in the main Color crawl controls.",
                  foreground="#666").grid(row=erow, column=0, columnspan=2, sticky="w", pady=(2, 0))

        # --- global display settings (bottom, full width) ---
        ttk.Separator(pad, orient="horizontal").grid(row=1, column=0, columnspan=2, sticky="ew", pady=(14, 10))
        grow = 2
        ttk.Label(pad, text="Display settings", font=("", 10, "bold")).grid(
            row=grow, column=0, columnspan=2, sticky="w")
        grow += 1

        bg_row = ttk.Frame(pad)
        bg_row.grid(row=grow, column=0, columnspan=2, sticky="w", pady=(6, 0))
        grow += 1
        ttk.Label(bg_row, text="Background:").pack(side="left")
        self._wp_bg_var = tk.StringVar(value=self._wp_cfg.get("bg_color", "#0b1220"))
        self._wp_bg_btn = tk.Button(bg_row, text="Background", width=12,
                                     background=self._wp_bg_var.get(), activebackground=self._wp_bg_var.get(),
                                     foreground=self._contrast_text_color(self._wp_bg_var.get()),
                                     command=self._wp_pick_bg_color)
        self._wp_bg_btn.pack(side="left", padx=(6, 0))

        def wp_global_slider(label, key, frm, to, step, fmt="{:.1f}"):
            nonlocal grow
            ttk.Label(pad, text=label).grid(row=grow, column=0, columnspan=2, sticky="w", pady=(8, 0))
            grow += 1
            val_label = ttk.Label(pad, text=fmt.format(self._wp_cfg.get(key, frm)), width=8)
            val_label.grid(row=grow, column=1, sticky="e")
            var = tk.DoubleVar(value=self._wp_cfg.get(key, frm))

            def on_move(_evt=None, v=var, l=val_label, s=step, f=fmt, k=key):
                snapped = round(v.get() / s) * s
                v.set(snapped)
                l.config(text=f.format(snapped))
                self._wp_cfg[k] = snapped

            scale = ttk.Scale(pad, from_=frm, to=to, variable=var, command=lambda _v: on_move())
            scale.grid(row=grow, column=0, sticky="ew")
            grow += 1
            return var

        self._wp_stroke_var = wp_global_slider("Stroke width (px)", "stroke", 1.0, 20.0, 0.5)
        self._wp_edge_var = wp_global_slider("Edge inset (px)", "edge", 0.5, 60.0, 0.5)
        ttk.Label(pad, text="Keep this small so the pattern reaches\nessentially edge-to-edge.",
                  foreground="#666").grid(row=grow, column=0, columnspan=2, sticky="w", pady=(0, 4))
        grow += 1

        ttk.Label(pad, text="Monitors").grid(row=grow, column=0, columnspan=2, sticky="w", pady=(8, 0))
        grow += 1
        self._wp_monitor_var = tk.StringVar(value=self._wp_cfg.get("monitor_mode", "primary"))
        mon_row = ttk.Frame(pad)
        mon_row.grid(row=grow, column=0, columnspan=2, sticky="w")
        grow += 1
        ttk.Radiobutton(mon_row, text="This monitor only", value="primary", variable=self._wp_monitor_var,
                         command=self._wp_monitor_changed).pack(anchor="w")
        ttk.Radiobutton(mon_row, text="Stretch across all monitors", value="all", variable=self._wp_monitor_var,
                         command=self._wp_monitor_changed).pack(anchor="w")

        # --- run controls ---
        ttk.Separator(pad, orient="horizontal").grid(row=grow, column=0, columnspan=2, sticky="ew", pady=(14, 10))
        grow += 1
        status_row = ttk.Frame(pad)
        status_row.grid(row=grow, column=0, columnspan=2, sticky="ew")
        grow += 1
        ttk.Label(status_row, text="Status:").pack(side="left")
        ttk.Label(status_row, textvariable=self._wallpaper_status_var).pack(side="left", padx=(4, 0))

        run_row = ttk.Frame(pad)
        run_row.grid(row=grow, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        grow += 1
        self._wp_start_btn = ttk.Button(run_row, text="Start Wallpaper Now", command=self._wp_start)
        self._wp_start_btn.pack(side="left", fill="x", expand=True)
        self._wp_stop_btn = ttk.Button(run_row, text="Stop Wallpaper", command=self._wp_stop)
        self._wp_stop_btn.pack(side="left", fill="x", expand=True, padx=(6, 0))
        ttk.Label(pad, text="Starting saves your changes first, then runs\n"
                            "wallpaper_engine.py in the background -- it keeps\n"
                            "going even after you close this window or the app.",
                  foreground="#666").grid(row=grow, column=0, columnspan=2, sticky="w", pady=(4, 0))
        grow += 1

        bottom_row = ttk.Frame(pad)
        bottom_row.grid(row=grow, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        ttk.Button(bottom_row, text="Save", command=self._wp_save).pack(side="left", fill="x", expand=True)
        ttk.Button(bottom_row, text="Close", command=self._close_wallpaper_dialog).pack(
            side="left", fill="x", expand=True, padx=(6, 0))

        self._wp_refresh_listbox()

    def _wp_refresh_listbox(self):
        self._wp_listbox.delete(0, tk.END)
        for i, preset in enumerate(self._wp_cfg["presets"]):
            self._wp_listbox.insert(tk.END, f"Day {i + 1}")
            color0 = preset.get("colors", ["#333333"])[0]
            self._wp_listbox.itemconfig(i, background=color0, foreground=self._contrast_text_color(color0))
        if self._wp_cfg["presets"]:
            self._wp_selected = max(0, min(self._wp_selected, len(self._wp_cfg["presets"]) - 1))
            self._wp_listbox.selection_set(self._wp_selected)
            self._wp_load_editor(self._wp_selected)
        else:
            self._wp_selected = -1
        self._wp_remove_btn.config(state="normal" if len(self._wp_cfg["presets"]) > 1 else "disabled")

    def _wp_load_editor(self, idx):
        preset = self._wp_cfg["presets"][idx]
        self._wp_shape_var.set(preset.get("fill_shape", "square"))
        colors = preset.get("colors") or ["#ff595e", "#ffd166", "#7fe7c4"]
        for i, cvar in enumerate(self._wp_color_vars):
            hexval = colors[i] if i < len(colors) else "#888888"
            cvar.set(hexval)
            btn = self._wp_color_btns[i]
            btn.config(background=hexval, activebackground=hexval, foreground=self._contrast_text_color(hexval))
        self._wp_size_var.set(preset.get("crawler_size", 40.0))
        self._wp_size_scale.refresh_label()
        self._wp_gap_var.set(preset.get("gap", 20.0))
        self._wp_gap_scale.refresh_label()
        self._wp_speed_var.set(preset.get("speed", 200.0))
        self._wp_speed_scale.refresh_label()

    def _on_wallpaper_preset_selected(self, _evt=None):
        sel = self._wp_listbox.curselection()
        if not sel:
            return
        self._wp_selected = sel[0]
        self._wp_load_editor(self._wp_selected)

    def _wp_editor_changed(self, *_args):
        """Live-syncs the editor widgets into the selected preset's dict --
        colors are written directly in _wp_pick_color, this handles shape/
        slider changes. Nothing here touches disk; only Save (_wp_save)
        does, so switching presets or closing without saving is safe."""
        if not (0 <= self._wp_selected < len(self._wp_cfg["presets"])):
            return
        preset = self._wp_cfg["presets"][self._wp_selected]
        preset["fill_shape"] = self._wp_shape_var.get()
        preset["crawler_size"] = self._wp_size_var.get()
        preset["gap"] = self._wp_gap_var.get()
        preset["speed"] = self._wp_speed_var.get()

    def _wp_pick_color(self, var, btn, title):
        _rgb, hexval = colorchooser.askcolor(color=var.get(), title=title, parent=self._wallpaper_dialog)
        if not hexval:
            return  # user cancelled
        var.set(hexval)
        btn.config(background=hexval, activebackground=hexval, foreground=self._contrast_text_color(hexval))
        if 0 <= self._wp_selected < len(self._wp_cfg["presets"]):
            preset = self._wp_cfg["presets"][self._wp_selected]
            preset["colors"] = [v.get() for v in self._wp_color_vars]
            self._wp_listbox.itemconfig(self._wp_selected, background=preset["colors"][0],
                                         foreground=self._contrast_text_color(preset["colors"][0]))

    def _wp_pick_bg_color(self):
        _rgb, hexval = colorchooser.askcolor(color=self._wp_bg_var.get(), title="Background color",
                                              parent=self._wallpaper_dialog)
        if not hexval:
            return
        self._wp_bg_var.set(hexval)
        self._wp_bg_btn.config(background=hexval, activebackground=hexval,
                                foreground=self._contrast_text_color(hexval))
        self._wp_cfg["bg_color"] = hexval

    def _wp_monitor_changed(self):
        self._wp_cfg["monitor_mode"] = self._wp_monitor_var.get()

    def _wp_add_preset(self):
        base = WALLPAPER_DEFAULT_PRESETS[len(self._wp_cfg["presets"]) % len(WALLPAPER_DEFAULT_PRESETS)]
        new_preset = dict(base)
        new_preset["colors"] = list(base["colors"])
        self._wp_cfg["presets"].append(new_preset)
        self._wp_selected = len(self._wp_cfg["presets"]) - 1
        self._wp_refresh_listbox()

    def _wp_remove_preset(self):
        presets = self._wp_cfg["presets"]
        if len(presets) <= 1 or self._wp_selected < 0:
            return  # always keep at least one preset -- the rotation can't be empty
        del presets[self._wp_selected]
        self._wp_selected = min(self._wp_selected, len(presets) - 1)
        self._wp_cfg["rotation_index"] = min(self._wp_cfg.get("rotation_index", 0), len(presets) - 1)
        self._wp_refresh_listbox()

    def _wp_move_preset(self, direction):
        presets = self._wp_cfg["presets"]
        i = self._wp_selected
        j = i + direction
        if i < 0 or not (0 <= j < len(presets)):
            return
        presets[i], presets[j] = presets[j], presets[i]
        self._wp_selected = j
        self._wp_refresh_listbox()

    def _wp_copy_from_generator(self):
        """Grabs whatever is currently dialed in on the main Color crawl
        controls (colors, crawler size/gap/speed) plus the main Fill shape
        picker, and writes it into the selected wallpaper preset -- the
        easiest way to turn a look you already like into a wallpaper day."""
        if not (0 <= self._wp_selected < len(self._wp_cfg["presets"])):
            return
        shape = self.fill_shape_var.get()
        preset = self._wp_cfg["presets"][self._wp_selected]
        preset["fill_shape"] = shape if shape in WALLPAPER_SHAPE_CHOICES else "square"
        preset["colors"] = [v.get() for v in self.crawl_color_vars]
        preset["crawler_size"] = self.crawler_size_var.get()
        preset["gap"] = self.crawl_gap_var.get()
        preset["speed"] = self.crawl_speed_var.get()
        self._wp_load_editor(self._wp_selected)
        self._wp_listbox.itemconfig(self._wp_selected, background=preset["colors"][0],
                                     foreground=self._contrast_text_color(preset["colors"][0]))

    def _wp_save(self, silent=False):
        save_wallpaper_config(self._wp_cfg)
        self._wp_saved_snapshot = json.dumps(self._wp_cfg, sort_keys=True)
        if not silent:
            messagebox.showinfo("Saved", "Wallpaper settings saved.", parent=self._wallpaper_dialog)

    def _wp_start(self):
        self._wp_save(silent=True)  # what starts should match what's shown, not a stale on-disk
        # copy -- silent because a blocking confirmation here would just be
        # an unwanted interruption between clicking Start and it launching
        if self._wallpaper_proc is not None and self._wallpaper_proc.poll() is None:
            messagebox.showinfo("Already running",
                                 "A wallpaper process started from here is already running -- "
                                 "stop it first if you want to restart with new settings.",
                                 parent=self._wallpaper_dialog)
            return
        try:
            kwargs = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            self._wallpaper_proc = subprocess.Popen(
                [sys.executable, str(WALLPAPER_ENGINE_PATH)],
                cwd=str(WALLPAPER_ENGINE_PATH.parent), **kwargs,
            )
        except OSError as e:
            messagebox.showerror("Could not start", str(e), parent=self._wallpaper_dialog)
            return
        self._wp_update_run_buttons()
        self._wp_poll_process()

    def _wp_stop(self):
        if self._wallpaper_proc is None or self._wallpaper_proc.poll() is not None:
            self._wallpaper_proc = None
            self._wp_update_run_buttons()
            return
        self._wallpaper_proc.terminate()
        try:
            self._wallpaper_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._wallpaper_proc.kill()
        self._wallpaper_proc = None
        self._wp_update_run_buttons()

    def _wp_poll_process(self):
        """Reschedules itself only while a process we started is still
        alive, so it notices (and reflects in the Status label/buttons) if
        the wallpaper process crashes or is closed some other way."""
        if self._wallpaper_proc is not None and self._wallpaper_proc.poll() is None:
            self.after(2000, self._wp_poll_process)
        else:
            self._wallpaper_proc = None
            self._wp_update_run_buttons()

    def _wp_update_run_buttons(self):
        running = self._wallpaper_proc is not None and self._wallpaper_proc.poll() is None
        self._wallpaper_status_var.set(f"Running (pid {self._wallpaper_proc.pid})" if running else "Not running")
        if self._wallpaper_dialog is not None and self._wallpaper_dialog.winfo_exists():
            self._wp_start_btn.config(state="disabled" if running else "normal")
            self._wp_stop_btn.config(state="normal" if running else "disabled")


if __name__ == "__main__":
    app = CurveApp()
    app.mainloop()
