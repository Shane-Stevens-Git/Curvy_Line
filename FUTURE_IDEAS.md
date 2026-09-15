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

## 3. Color strobe / traveling color effect (after draw animation)

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
