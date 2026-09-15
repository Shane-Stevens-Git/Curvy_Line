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

Line color, background color, and "Display stroke" (after a generation) are
pure presentation -- they redraw the already-computed, already-validated
path instantly instead of re-running the ~seconds-to-a-minute generation.
Display stroke is capped so it can never thicken the line enough to make it
touch itself; see organic_curve.max_safe_render_stroke().

Run with the same interpreter you used for organic_curve.py, e.g.:
    .venv\\Scripts\\python.exe gui.py          (Windows)
    .venv/bin/python gui.py                    (macOS/Linux)
"""
import json
import queue
import random
import shutil
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import colorchooser, ttk, filedialog, messagebox

from PIL import ImageTk

from organic_curve import (generate, render_png, render_svg, GenerationError,
                            max_safe_render_stroke, fill_polygon, FILL_SHAPES)

OUTPUT_DIR = Path(__file__).parent / "outputs"
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

        controls = ttk.Frame(root)
        controls.grid(row=0, column=0, sticky="ns", padx=(0, 12))
        self._build_controls(controls)

        preview = ttk.Frame(root)
        preview.grid(row=0, column=1, sticky="nsew")
        self._build_preview(preview)

        self._update_shape_preview()  # show the default shape's border right away

    def _build_controls(self, parent):
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
            return scale

        ttk.Label(parent, text="Flowing Curve Generator", font=("", 13, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 6))
        row += 1

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
        shape_combo = ttk.Combobox(parent, textvariable=self.fill_shape_var, values=list(FILL_SHAPES),
                                    state="readonly", width=12)
        shape_combo.grid(row=row, column=0, columnspan=2, sticky="w")
        shape_combo.bind("<<ComboboxSelected>>", self._update_shape_preview)
        row += 1

        # --- Frame size (needs a regenerate: changes the actual geometry) ---
        self.size_var = tk.IntVar(value=1200)
        self.size_scale = add_slider("Frame size (px)", self.size_var, 200, 2000, 50, "{:.0f}")
        self.size_scale.bind("<ButtonRelease-1>", self._update_shape_preview)
        ttk.Label(parent, text="1200px takes ~30-60s to generate. Try\n500-600 while experimenting.",
                  foreground="#666").grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
        row += 1

        # --- Stroke (generation-time: affects path spacing) ---
        self.stroke_var = tk.DoubleVar(value=3.0)
        add_slider("Stroke width (px, at generation)", self.stroke_var, 1.0, 10.0, 0.5, "{:.1f}")

        # --- Smoothness ---
        self.smoothness_var = tk.DoubleVar(value=1.0)
        add_slider("Smoothness (0.25 tight → 3 round)", self.smoothness_var, 0.25, 3.0, 0.25, "{:.2f}")

        # --- Appearance (pure presentation -- instant re-render, no regenerate) ---
        ttk.Label(parent, text="Appearance", font=("", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(14, 2))
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

        # --- Generate button + progress ---
        self.generate_btn = ttk.Button(parent, text="Generate", command=self._start_generate)
        self.generate_btn.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(16, 4))
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

        # --- Replay / Save / cleanup ---
        self.replay_btn = ttk.Button(parent, text="Replay animation", command=self._replay_animation,
                                      state="disabled")
        self.replay_btn.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(8, 4))
        row += 1
        self.save_btn = ttk.Button(parent, text="Save As...", command=self._save_as, state="disabled")
        self.save_btn.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(8, 4))
        row += 1
        ttk.Button(parent, text="Clear saved copies", command=self._clear_outputs).grid(
            row=row, column=0, columnspan=2, sticky="ew")
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

        self.info_var = tk.StringVar(value="")
        ttk.Label(parent, textvariable=self.info_var, foreground="#444").grid(row=1, column=0, sticky="w", pady=(6, 0))

    def _update_shape_preview(self, *_args):
        """Draw just the outline of the currently selected fill shape (at
        the current frame size/edge clearance) on the canvas, so you can see
        what a Generate would fill before spending the time to run one.
        Whatever was previously generated no longer matches these settings,
        so this also resets the controls that depend on a live result."""
        if self.busy:
            return  # don't clobber a generation/animation in progress
        try:
            size = self.size_var.get()
            edge = self.edge_var.get()
            shape = self.fill_shape_var.get()
            region = fill_polygon(shape, size, edge)
        except (tk.TclError, GenerationError):
            return  # mid-edit / momentarily invalid combination -- leave the canvas as-is

        self.last_path = None
        self.last_report = None
        self.save_btn.config(state="disabled")
        self.replay_btn.config(state="disabled")
        self.display_stroke_scale.config(state="disabled")
        self.display_stroke_label.config(text="--")

        scale = PREVIEW_DISPLAY_SIZE / size
        coords = []
        for x, y in region.exterior.coords:
            coords.extend([x * scale, y * scale])

        self.preview_canvas.config(background=self.bg_color_var.get())
        self.preview_canvas.delete("all")
        self.preview_canvas.create_polygon(coords, outline="#888888", fill="", width=2, dash=(5, 3))
        self.status_var.set("Ready.")
        self.info_var.set(f"Shape preview: {shape}. Click Generate to fill it.")

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
        try:
            params = dict(
                size=self.size_var.get(),
                gap=self.gap_var.get(),
                preferred_gap=self.preferred_gap_var.get(),
                iterations=self.iterations_var.get(),
                smoothness=round(self.smoothness_var.get(), 4),
                edge_wave=self.edge_wave_var.get(),
                stroke=self.stroke_var.get(),
                edge=self.edge_var.get(),
                seed=self.seed_var.get(),
                attempts=self.attempts_var.get(),
                fill_shape=self.fill_shape_var.get(),
            )
        except tk.TclError:
            messagebox.showerror("Invalid input", "One of the fields isn't a valid number.")
            return

        self.busy = True
        self._gen_token += 1  # invalidate any still-running animation from a previous generate
        self._anim_skip = False
        self.generate_btn.config(state="disabled")
        self.save_btn.config(state="disabled")
        self.replay_btn.config(state="disabled")
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
        self.progress.config(value=100)
        self.status_var.set(f"Done in {elapsed:.1f}s (seed {report['seed']}).")

        self.last_path = p
        self.last_report = report
        self.last_size = params["size"]
        self.last_stroke = params["stroke"]
        self.last_generation_stroke = params["stroke"]

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

        self.info_var.set(
            f"seed={report['seed']}  shape={report.get('fill_shape', 'square')}  "
            f"min gap={report['minimum_nonlocal_ink_gap_px']}px  "
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
        fully computed and validated before this runs."""
        token = self._gen_token
        self.preview_canvas.config(background=self.bg_color_var.get())
        self.preview_canvas.delete("all")
        scale = PREVIEW_DISPLAY_SIZE / size
        coords = (p * scale).flatten().tolist()  # x0,y0,x1,y1,...
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
        self._animate_draw(p, size, stroke, img)

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
        is being generated."""
        if self.last_path is None or self.busy or self._rerendering:
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


if __name__ == "__main__":
    app = CurveApp()
    app.mainloop()
