# Future ideas / roadmap

Captured from a brainstorm on 2026-09-15. Nothing here is built yet — these
are notes on what each idea would actually take, so we can pick them up
later without re-deriving the approach.

## 1. Fill other simple shapes (circle, triangle), not just the square (DONE)

The current algorithm scatters points inside the square canvas (minus edge
margin), connects them with a Euclidean minimum spanning tree, inflates
that tree into a blob polygon (Shapely buffer), and traces its smoothed
boundary as the drawn path. Everything that currently treats the fill
region as "the square" would generalize to "an arbitrary polygon":

- Point scatter: sample inside the target polygon instead of the square.
- Edge clearance: Shapely already computes distance-to-boundary for any
  polygon, not just a square, so this part is mostly a plug-in swap rather
  than new math.
- The relaxation / empty-space sampling grid: needs to sample only inside
  the polygon (mask out points outside it) instead of the square minus
  margin.
- Coverage fraction: denominator becomes the polygon's area instead of the
  square's.

The real risk isn't the geometry, it's spacing: a triangle's corners are
much tighter than a square's, so gap/edge-clearance defaults tuned for a
square may starve valid candidates near acute corners. Might need
per-shape defaults or a corner-aware relaxation term. Net: a contained,
medium-sized refactor of organic_curve.py's boundary logic plus a new
"Fill shape" control in the GUI — not a rewrite of the core approach.

## 2. "Replay animation" button ****(DONE)****

Trivial. The draw-in animation code already exists and the GUI already
holds the last generated path, colors, and stroke. Just needs a button
(enabled once something's been generated) that re-runs `_animate_draw`
against the current state without regenerating anything.

## 3. Color strobe / traveling color effect (after draw animation) (DONE)

A second, independent animation: once the curve is fully on screen, cycle
a color gradient along it continuously (like a chase-light effect) until
stopped.

Approach: resample the path to a moderate number of segments (a few
hundred, not every raw point — full resolution would likely be too many
canvas items to recolor smoothly every frame), draw each as its own canvas
line item, and on a timer (`self.after(...)`, following the same polling
pattern already used elsewhere) recompute each segment's color from its
position along the arc plus a phase that advances each frame, via
`canvas.itemconfig(seg_id, fill=color)`. Needs a start/stop toggle (this
one runs indefinitely, unlike the one-shot draw-in animation) and should
reuse the existing generation-token pattern so it cleanly cancels if a new
curve is generated while it's running. Might want a 2-3 color "strobe
palette" picker rather than a single color. Moderate effort, mostly new
code but following patterns already in the codebase.

## 4. Live wallpaper: auto-generate a new curve daily

This isn't really a GUI feature — it's a small standalone companion, since
it has to run unattended once a day outside of any interactive session.
Pieces:

- Reuse the existing CLI (`organic_curve.py`) to generate to a fixed
  output path, seeding from the date (e.g. `seed = int(date.today()
  .strftime('%Y%m%d'))`) so it's deterministic and different each day.
- Set it as the desktop wallpaper via the standard Windows API call:
  `ctypes.windll.user32.SystemParametersInfoW(20, 0, path, 3)`.
- Schedule it with Windows Task Scheduler (`schtasks /create ...`) to run
  once a day, pointed at the right python.exe and script path on this
  machine.

Open questions to settle when we build this: render at actual screen
resolution (or square + letterbox/center), whether colors vary day to day
or stay fixed, and whether it picks up the shape-fill revamp (#1) once
that exists. Code-wise it's small; the fiddly part is the Task Scheduler
setup on the real machine, which is worth doing hands-on rather than
blind.

## 5. Screensaver mode

Windows screensavers are just `.scr` files (a renamed `.exe` implementing a
few command-line flags: `/s` to run full-screen, `/c` to show the config
dialog, `/p <hwnd>` to preview embedded in the small monitor thumbnail in
Windows' screensaver settings, no args to run in default/preview mode)
that Windows launches on idle timeout and kills the instant there's input.
Almost all of `WallpaperWindow`'s rendering code is reusable as-is — same
canvas, same `crawl_bands` loop, same gradient palette, even the
background-thread-generation-with-a-spinner pattern we just built for the
regen button would work nicely here (regenerate fresh each time the
screensaver kicks in, or just keep showing whatever the wallpaper is
already on).

What's genuinely different from the wallpaper path: a screensaver must
close itself the instant it sees mouse movement/clicks or a keypress
(Windows expects this, and anyone testing it will assume it if it
doesn't); it needs packaging as an actual `.scr` (built via PyInstaller,
since this can't run as a bare `.py` the way the wallpaper engine can from
Task Scheduler) and installing via the registry
(`HKEY_CURRENT_USER\Control Panel\Desktop`, `SCRNSAVE.EXE` key) — doable
following install.bat/install.sh's existing pattern, but worth doing
hands-on on the real machine rather than blind; and it should skip the
WorkerW desktop-icon reparenting trick (`attempt_worker_reparent`)
entirely — a screensaver is expected to cover the whole screen on top of
everything, which sidesteps the current wallpaper's core headache (that
reparenting-behind-icons is exactly what caused the click-freeze bug that
made "render behind desktop icons" default off in Configure Wallpaper...).
Worth calling out: since a screensaver only shows while the user's away,
it doesn't need input passthrough or click-safety at all — so it's
legitimately a simpler window to get right than the live wallpaper, not
just a reskin of it. Medium effort: most of the rendering code already
exists: the new work is the `.scr` packaging, the idle-close behavior, and
the install/registry step.

## 6. SVG export for pen plotters

`organic_curve.py` already has `render_svg()` for the "export static
image" path in gui.py, and the line data itself is already exactly what a
plotter wants — a single continuous stroke, not a filled shape (that's the
whole "flowing curve" premise). What plotter export needs on top of that:

- No fill at all, ever — just `stroke`, `fill="none"`, one consistent
  stroke-width. Worth double-checking today's export never has a filled
  boundary/background rectangle sneaking in behind the line.
- As few pen lifts as possible. The current single-strand path is already
  one continuous line, so this is mostly free today — but if multi-strand
  mode (a separate idea from this same brainstorm) ships first, it'll need
  path-ordering logic to minimize travel between strands.
- Real-world units (mm/inches tied to actual paper size) instead of
  arbitrary pixel coordinates, since plotter software (AxiDraw's workflow
  via Inkscape, etc.) wants the SVG's viewBox/width/height mapped straight
  to the plot bed.
- Optionally, orient the path so it starts/ends somewhere sensible (e.g.
  near a corner) rather than mid-canvas, since some plotter drivers home
  from a fixed start position.

Low-to-medium effort — mostly a new export function alongside `render_svg()`
with plotter-specific defaults (no fill, mm units, a stroke width entered
in real units) rather than new generation logic; the geometry to export
already exists.

## 7. Battery-aware performance throttling (live wallpaper)

The wallpaper engine currently redraws every `FRAME_MS` (50ms, ~20fps)
nonstop, all day, regardless of power source — fine on a desktop, wasteful
on a laptop running on battery. Windows exposes power status via
`GetSystemPowerStatus` (`ctypes.windll.kernel32`), which reports whether AC
power is connected and the battery percentage. `wallpaper_engine.py` could
poll this occasionally (every few seconds is plenty — no need to check
every tick) and respond a couple of ways:

- Drop the frame rate on battery (e.g. `FRAME_MS` 50 → 150 — still smooth
  enough for a slow chase-light effect, a fraction of the CPU/GPU work).
- Pause the crawl animation entirely below some battery threshold (e.g.
  <20%), freezing on the current frame instead of continuing to redraw,
  and resuming automatically once charging or back above the threshold.
- Tie it into the existing config (`wallpaper_config.json`) with a simple
  toggle in Configure Wallpaper... — something like "Reduce animation on
  battery" next to the existing Rendering section — so it's not a surprise
  behavior change.

Small-to-medium effort: the power-status check is a few lines (Windows
-only, same `ctypes` pattern already used for `_make_input_safe`/
`reparent_behind_desktop_icons`), the throttling itself is just varying
the `self.root.after(FRAME_MS, self.tick)` delay and/or skipping the
`crawl_bands` redraw work, and it composes cleanly with everything already
in `tick()` rather than needing a rewrite.
