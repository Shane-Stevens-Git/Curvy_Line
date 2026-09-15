"""Organic single-line generator. Install: pip install numpy scipy shapely pillow

Randomly scattered sites -> Euclidean spanning tree -> rounded outline ->
smooth outline -> one opening -> empty-space/spacing relaxation -> polishing
-> smooth inward boundary bends.
The tree is only scaffolding, never drawn.
No grid, spiral, greedy walk, or unimplemented 'sensors'. A full path is planned
before drawing, so the pen cannot get trapped midway. Failed candidates are
rejected, not exported. All distances are in output pixels. Defaults: 12 px
hard ink gap, 18 px soft preferred ink gap, 3 px stroke, 35 px edge clearance.
Nearby portions of the same bend are excluded from self-spacing tests using
a documented arc-length neighborhood; intersections are never excluded.

Fills a square by default; --fill-shape circle/triangle inscribes the same
algorithm in a circle or triangle within the usual edge-inset area instead.
edge-wave boundary bending is currently square-only and is skipped (noted in
the report, not an error) for the other shapes.

CLI usage: python organic_curve.py --gap 12 --preferred-gap 18 --iterations 50
Use --smoothness 1.5 for rounder curves (1 is the original strength;
supported range 0.25 to 3). Higher values can reduce density. Smoothing is
reduced automatically if necessary to preserve the spacing constraints.
Use --edge-wave 20 to bend edge-adjacent runs inward (0 disables this step).
The soft target is not a promised maximum: space/shape constraints may prevent
reaching it everywhere. --iterations 0 disables relaxation (but not polishing).
Requires Shapely 2.x. The report measures empty space on a 6 px probe lattice;
those sampled distances are not exact global empty-circle bounds.

Library usage: import generate(), render_png(), render_svg() to drive this
from other Python code (e.g. a GUI) without going through the CLI or a
subprocess. generate() takes an optional progress_callback(phase, current,
total) so a caller can show a progress bar instead of the CLI's print output.
A bad parameter/size combination raises GenerationError with a clear message
instead of letting a ValueError traceback escape.
"""
import argparse
import json
from pathlib import Path
import numpy as np
from scipy.spatial.distance import cdist
from scipy.spatial import cKDTree
from scipy.sparse.csgraph import minimum_spanning_tree
from scipy.ndimage import gaussian_filter1d
from shapely.geometry import LineString, MultiLineString, Point, Polygon, box
from shapely import (STRtree, linestrings, distance as geometric_distance,
                      points as shapely_points, contains as geometric_contains,
                      covers as geometric_covers, prepare)
from PIL import Image, ImageDraw


class GenerationError(Exception):
    """Raised when no valid curve could be produced for the given parameters."""


FILL_SHAPES = ('square', 'circle', 'triangle', 'diamond', 'pentagon', 'hexagon', 'octagon', 'star', 'custom')


def _wh(size):
    """Normalize a `size` argument into an explicit (width, height) pair.

    `size` may be a single number, meaning a square canvas (the original,
    still-default behavior -- kept so every existing caller that passes a
    plain size keeps working unchanged), or a (width, height) pair for a
    non-square canvas. Always returns floats.
    """
    if isinstance(size, (tuple, list)):
        if len(size) != 2:
            raise GenerationError('size must be a single number or a (width, height) pair.')
        return float(size[0]), float(size[1])
    return float(size), float(size)


def _regular_polygon(size, edge, n_sides, rotation_deg=-90.0):
    """A regular n-sided polygon inscribed in the inset canvas rectangle's
    incircle/inellipse (the same inset area every fill shape uses).
    rotation_deg places the first vertex; -90 puts it at the top of the
    canvas. On a square canvas (width == height) this is a true regular
    polygon, bit-for-bit the same as before rectangle support existed; on a
    non-square canvas the vertices are stretched per-axis so the shape still
    reaches every inset edge instead of leaving letterboxed empty space.
    """
    width, height = _wh(size)
    lo_x, hi_x = edge, width - edge
    lo_y, hi_y = edge, height - edge
    cx, cy = width / 2.0, height / 2.0
    rx = (hi_x - lo_x) / 2.0
    ry = (hi_y - lo_y) / 2.0
    angles = np.deg2rad(rotation_deg) + np.arange(n_sides) * (2 * np.pi / n_sides)
    return Polygon(np.column_stack([cx + rx * np.cos(angles), cy + ry * np.sin(angles)]))


def _star_polygon(size, edge, n_points, inner_ratio=0.55, rotation_deg=-90.0):
    """A simple n-pointed star: vertices alternate between an outer radius
    (touching the inset rectangle's incircle/inellipse, like the other
    regular shapes) and an inner radius. inner_ratio is kept fairly high
    (fatter arms, less razor-thin points) since very thin points leave
    little room for the ink-spacing/edge-clearance requirements every shape
    shares. Stretched per-axis on a non-square canvas, same as
    _regular_polygon; bit-for-bit unchanged on a square one.
    """
    width, height = _wh(size)
    lo_x, hi_x = edge, width - edge
    lo_y, hi_y = edge, height - edge
    cx, cy = width / 2.0, height / 2.0
    rx_outer = (hi_x - lo_x) / 2.0
    ry_outer = (hi_y - lo_y) / 2.0
    rx_inner = rx_outer * inner_ratio
    ry_inner = ry_outer * inner_ratio
    step = np.pi / n_points
    angles = np.deg2rad(rotation_deg) + np.arange(2 * n_points) * step
    outer = np.arange(2 * n_points) % 2 == 0
    rx = np.where(outer, rx_outer, rx_inner)
    ry = np.where(outer, ry_outer, ry_inner)
    return Polygon(np.column_stack([cx + rx * np.cos(angles), cy + ry * np.sin(angles)]))


def fill_polygon(fill_shape, size, edge, custom_region=None):
    """The region the curve must stay within, already inset by `edge` from
    the canvas border. `size` is a single number for a square canvas or a
    (width, height) pair for a rectangle -- see _wh(). 'square' fills the
    whole inset canvas (a true rectangle when width != height, which is how
    a wallpaper-sized canvas gets filled edge-to-edge with no cropping
    needed). Every other preset shape is a simple alternative inscribed in
    that same inset area, so `edge`/`gap`/`stroke` all mean the same thing
    regardless of shape. 'custom' uses the caller-supplied `custom_region`
    (a Shapely Polygon in canvas pixel coordinates, e.g. a boundary the user
    hand-drew in the GUI) instead of a preset shape, inset by `edge` from
    both its own outline and the canvas border.
    """
    width, height = _wh(size)
    lo_x, hi_x = edge, width - edge
    lo_y, hi_y = edge, height - edge
    if hi_x <= lo_x or hi_y <= lo_y:
        raise GenerationError(f'edge={edge} leaves no room inside a {width:.0f}x{height:.0f}px canvas.')
    if fill_shape == 'custom':
        if custom_region is None:
            raise GenerationError("fill_shape='custom' requires custom_region (a Shapely Polygon).")
        canvas_box = box(0, 0, width, height)
        inset = custom_region.intersection(canvas_box).buffer(-edge)
        region = inset.intersection(box(lo_x, lo_y, hi_x, hi_y))
        if region.is_empty or region.geom_type != 'Polygon' or region.area < 9 * edge * edge:
            raise GenerationError(
                'The drawn boundary is too small or too thin once inset by the edge clearance; '
                'draw a larger/rounder shape or reduce --edge.'
            )
        return region
    if fill_shape == 'square':
        return box(lo_x, lo_y, hi_x, hi_y)
    cx, cy = width / 2.0, height / 2.0
    rx, ry = (hi_x - lo_x) / 2.0, (hi_y - lo_y) / 2.0
    if fill_shape == 'circle':
        if width == height:
            return Point(cx, cy).buffer(rx, quad_segs=128)
        theta = np.linspace(0, 2 * np.pi, 256, endpoint=False)
        return Polygon(np.column_stack([cx + rx * np.cos(theta), cy + ry * np.sin(theta)]))
    if fill_shape == 'triangle':
        # Upward-pointing triangle filling the inset rectangle: apex at top
        # center, base spanning the full inset width at the bottom.
        return Polygon([(cx, lo_y), (lo_x, hi_y), (hi_x, hi_y)])
    if fill_shape == 'diamond':
        return _regular_polygon(size, edge, 4)
    if fill_shape == 'pentagon':
        return _regular_polygon(size, edge, 5)
    if fill_shape == 'hexagon':
        return _regular_polygon(size, edge, 6)
    if fill_shape == 'octagon':
        return _regular_polygon(size, edge, 8, rotation_deg=-90 + 22.5)
    if fill_shape == 'star':
        return _star_polygon(size, edge, 5)
    raise GenerationError(f"Unknown fill_shape {fill_shape!r}; expected one of {FILL_SHAPES}.")


def probe_grid(region, step=6.0, inset=3.0):
    """A grid of points spanning region's bounding box, kept only where they
    fall inside region. Used to sense empty space without ever letting the
    path be pulled toward areas outside the target fill shape."""
    minx, miny, maxx, maxy = region.bounds
    axis_x = np.arange(minx + inset, maxx, step)
    axis_y = np.arange(miny + inset, maxy, step)
    probes = np.stack(np.meshgrid(axis_x, axis_y), axis=-1).reshape(-1, 2)
    if len(probes) == 0:
        return probes
    return probes[geometric_contains(region, shapely_points(probes))]


def resample(p, step=2):
    arc = np.r_[0, np.cumsum(np.linalg.norm(np.diff(p, axis=0), axis=1))]
    keep = np.r_[True, np.diff(arc) > 1e-9]
    p, arc = p[keep], arc[keep]
    s = np.linspace(0, arc[-1], int(arc[-1] / step) + 2)
    return np.column_stack([np.interp(s, arc, p[:, j]) for j in (0, 1)])


def candidate(size, gap, stroke, edge, seed, fill_shape, region):
    width, height = _wh(size)
    rng = np.random.default_rng(seed)
    pitch = (gap + stroke) * 3.2
    radius = pitch * 0.245

    if fill_shape == 'square':
        # Exact original fast path (no region/rejection-sampling machinery):
        # keeps the seed -> curve mapping identical to before this shape
        # generalization existed, for everyone already using the default
        # (still true bit-for-bit on a square canvas; a rectangular canvas
        # just uses independent x/y bounds instead of one shared lo/hi).
        margin = edge + stroke / 2 + radius + 8
        lo_x, hi_x = margin, width - margin
        lo_y, hi_y = margin, height - margin
        if hi_x - lo_x < 3 * pitch or hi_y - lo_y < 3 * pitch:
            raise GenerationError(
                f'Canvas is too small for this spacing: with gap={gap}, stroke={stroke}, '
                f'edge={edge}, both dimensions must be at least {int(3 * pitch + 2 * margin)} px '
                f'(got {width:.0f}x{height:.0f} px). Increase --size/--width/--height or reduce --gap/--edge.'
            )
        lo = np.array([lo_x, lo_y])
        hi = np.array([hi_x, hi_y])
        sites = [rng.uniform(lo, hi, 2)]
        # Best-candidate scattering fills empty areas without a lattice.
        for _ in range(int((hi_x-lo_x)*(hi_y-lo_y) / pitch**2 * 1.5)):
            choices = rng.uniform(lo, hi, (180, 2))
            d = cdist(choices, sites).min(axis=1)
            if d.max() < pitch * 0.80:
                break
            sites.append(choices[d.argmax()])
        sites = np.array(sites)
    else:
        # General path for circle/triangle/.../custom (any convex or
        # non-convex fill polygon): erode the region so the inflated tree
        # (buffer radius) plus stroke never crosses the shape's own
        # boundary, then scatter by rejection sampling within it instead of
        # a plain uniform range.
        site_region = region.buffer(-(stroke / 2 + radius + 8))
        if site_region.is_empty or site_region.area < 9 * pitch * pitch:
            raise GenerationError(
                f'Canvas/shape is too small for this spacing: with gap={gap}, stroke={stroke}, '
                f'edge={edge}, the fill area has too little room (got {width:.0f}x{height:.0f} px canvas, '
                f'fill_shape={fill_shape!r}). Increase the canvas size, reduce --gap/--edge, or pick '
                f'a less constrained --fill-shape.'
            )
        prepare(site_region)
        minx, miny, maxx, maxy = site_region.bounds

        def sample_inside(n):
            pts = rng.uniform([minx, miny], [maxx, maxy], (n, 2))
            return pts[geometric_contains(site_region, shapely_points(pts))]

        first = sample_inside(64)
        tries = 0
        while len(first) == 0 and tries < 40:
            first = sample_inside(64)
            tries += 1
        if len(first) == 0:
            return None
        sites = [first[0]]
        # Best-candidate scattering fills empty areas without a lattice.
        for _ in range(int(site_region.area / pitch**2 * 1.5)):
            choices = sample_inside(180)
            if len(choices) == 0:
                continue
            d = cdist(choices, sites).min(axis=1)
            if d.max() < pitch * 0.80:
                break
            sites.append(choices[d.argmax()])
        sites = np.array(sites)
        if len(sites) < 4:
            return None

    mst = minimum_spanning_tree(cdist(sites, sites)).tocoo()
    tree = MultiLineString([[sites[i], sites[j]] for i, j in zip(mst.row, mst.col)])
    shape = tree.buffer(radius, quad_segs=24)
    if shape.geom_type != 'Polygon' or len(shape.interiors):
        return None
    p = resample(np.array(shape.exterior.coords), 2)[:-1]
    # Smooth concave junctions as well as convex turns. Validation follows.
    p = gaussian_filter1d(p, sigma=radius * 0.75, axis=0, mode='wrap')
    # Open the loop by removing a short arc around its leftmost point.
    p = np.roll(p, -int(np.argmin(p[:, 0])), axis=0)
    lengths = np.r_[0, np.cumsum(np.linalg.norm(np.diff(p, axis=0), axis=1))]
    cut = (gap + stroke) * 1.8
    p = p[(lengths > cut / 2) & (lengths < lengths[-1] - cut / 2)]
    return resample(p), len(sites)


def validate(p, size, gap, stroke, edge, region=None, geometry_only=False):
    width, height = _wh(size)
    if region is None:
        region = box(edge, edge, width-edge, height-edge)  # original square-only default
    line = LineString(p)
    if not line.is_simple:
        return None
    # Distance to the raw canvas border (0/width or 0/height px per axis),
    # independent of fill shape -- lets every shape report a comparable "how
    # close to the image edge did the ink get" figure, exactly like the
    # original square-only check (bit-identical to it when width == height:
    # same min() over the same per-point, per-axis values, just grouped by
    # axis first instead of flattened all at once).
    dist_x = np.minimum(p[:, 0], width - p[:, 0])
    dist_y = np.minimum(p[:, 1], height - p[:, 1])
    edge_actual = float(np.min(np.minimum(dist_x, dist_y))) - stroke / 2
    if edge_actual < edge:
        return None
    # The path must also stay within the fill shape itself. For 'square'
    # this is implied by the check above (region is exactly that inset
    # box); for 'circle'/'triangle', region is smaller than the surrounding
    # inset square, so this is the check that actually constrains the fill
    # area to the chosen shape. covers() (not contains()) so a point that
    # lands exactly on the shape's boundary isn't spuriously rejected.
    if not geometric_covers(region, shapely_points(p)).all():
        return None
    segs = linestrings(np.stack([p[:-1], p[1:]], axis=1))
    arc = np.r_[0, np.cumsum(np.linalg.norm(np.diff(p, axis=0), axis=1))]
    index = STRtree(segs)
    required = gap + stroke
    # Local neighbors necessarily approach zero separation. Exclude only a
    # documented *arc-length* neighborhood, never arbitrary sample counts.
    local_arc = 3 * required
    i, j = index.query(segs, predicate='dwithin', distance=required*2)
    mask = (j > i) & (arc[j] - arc[i+1] > local_arc)
    gaps = geometric_distance(segs[i[mask]], segs[j[mask]]) - stroke
    minimum = float(gaps.min()) if len(gaps) else float('inf')
    if minimum < gap:
        return None
    if geometry_only:
        return True
    coverage = line.buffer((gap+stroke)*1.6).intersection(region).area / region.area
    if coverage < 0.70:
        return None
    return dict(single_path=True, endpoints=2, self_intersections=0,
                minimum_nonlocal_ink_gap_px=round(minimum, 3),
                excluded_local_arc_length_px=local_arc,
                ink_edge_clearance_px=round(edge_actual, 3),
                requested_ink_gap_px=gap, requested_edge_clearance_px=edge,
                coverage_neighborhood_radius_px=(gap+stroke)*1.6,
                covered_fraction=round(coverage, 4),
                validation='Exact segment tests on the exported polyline; local arc excluded as documented.')


def empty_space_stats(p, probes, stroke):
    d, _ = cKDTree(p).query(probes)
    d = np.maximum(0, d-stroke/2)
    return dict(mean_empty_distance_px=round(float(d.mean()), 3),
                p95_empty_distance_px=round(float(np.percentile(d, 95)), 3),
                max_sampled_empty_distance_px=round(float(d.max()), 3),
                path_length_px=round(float(LineString(p).length), 1))


def soften_boundary(p, size, gap, stroke, edge, seed, amplitude, region=None, fill_shape='square'):
    """Replace ruler-like boundary runs with smooth, nonperiodic-looking bends.

    Nearby sections move together in a smooth spatial field, rather than
    adding independent noisy wiggles to each vertex. Displacement points
    inward and fades into the interior. Every candidate is checked exactly.

    Only implemented for the 'square' fill shape so far -- the bending here
    is defined relative to 4 straight canvas-parallel edges, which doesn't
    generalize to a circle's curved boundary, a polygon's angled ones, or an
    arbitrary hand-drawn 'custom' boundary. For other shapes this is a no-op
    (edge_wave has no effect), noted in the returned report rather than
    failing. 'square' itself now means "fill the whole inset canvas", which
    can be a true rectangle when width != height -- the wave math below
    already works per-axis, so edge-wave still bends all 4 sides on a
    rectangular canvas exactly as it does on a square one, just with
    independent width/height instead of one shared size.
    """
    width, height = _wh(size)
    if amplitude == 0 or fill_shape != 'square':
        note = None if fill_shape == 'square' else (
            f"edge-wave boundary bending isn't implemented for fill_shape={fill_shape!r} yet; "
            f"skipped (no effect on this curve)."
        )
        return p, dict(requested_amplitude_px=amplitude, accepted_amplitude_px=0, note=note)
    rng = np.random.default_rng(seed+941)
    phases = rng.uniform(0, 2*np.pi, (4, 2))
    dims = (width, height)
    min_dim = min(width, height)
    wavelength = max(100., min_dim*0.14)
    band = max(70., min_dim*0.085)
    displacement = np.zeros_like(p)
    for side in range(4):
        axis = side//2
        sign = 1 if side%2 == 0 else -1
        dim = dims[axis]
        depth = p[:, axis]-edge if sign == 1 else dim-edge-p[:, axis]
        along = p[:, 1-axis]
        wave = (0.52 + 0.32*np.sin(2*np.pi*along/wavelength+phases[side,0])
                + 0.16*np.sin(2*np.pi*along/(wavelength*1.73)+phases[side,1]))
        influence = np.exp(-(np.maximum(0, depth)/band)**2)
        displacement[:, axis] += sign*influence*wave
    for fraction in (1., .8, .6, .4, .2):
        trial = np.round(resample(p+amplitude*fraction*displacement, step=.75), 4)
        if validate(trial, size, gap, stroke, edge, region, geometry_only=True):
            return trial, dict(requested_amplitude_px=amplitude,
                               accepted_amplitude_px=amplitude*fraction,
                               wavelength_px=wavelength, influence_band_px=band)
    raise GenerationError('Boundary rounding could not preserve clearance; reduce --edge-wave.')


def relax(p, size, gap, stroke, edge, preferred_gap, iterations, region, smoothness=1.0, progress_callback=None):
    """Redistribute into voids, repel crowded sections, and smooth curvature.

    The probe lattice senses space only; it does not define the drawn path.
    Longer deformed segments are resampled so added arc length remains smooth.
    Each accepted step passes exact segment collision/clearance checks.
    progress_callback(phase, current, total), when given, is called instead
    of printing so a GUI can drive its own progress bar. probes are drawn
    from `region` so voids outside the target fill shape never pull the path
    toward them (matters for circle/triangle, where region is smaller than
    its own bounding box).
    """
    probes = probe_grid(region, step=6.0, inset=3.0)
    before = empty_space_stats(p, probes, stroke)
    accepted = 0
    target = preferred_gap + stroke
    for iteration in range(iterations):
        tree = cKDTree(p)
        d, nearest = tree.query(probes)
        # Cells farther than half the preferred centerline spacing pull the
        # closest section toward them. Bigger voids receive greater weight.
        w = np.clip((d-target/2)/target, 0, 3)**2
        force = np.zeros_like(p)
        mass = np.bincount(nearest, weights=w, minlength=len(p))
        for k in range(2):
            force[:, k] = np.bincount(nearest, weights=w*(probes[:, k]-p[nearest, k]), minlength=len(p))
        force = gaussian_filter1d(force, 3.0, axis=0, mode='nearest')
        mass = gaussian_filter1d(mass, 3.0, mode='nearest')
        force /= np.maximum(mass[:, None], 0.1)
        # Repulsion uses actual along-path distance to exclude local neighbors.
        arc = np.r_[0, np.cumsum(np.linalg.norm(np.diff(p, axis=0), axis=1))]
        pairs = tree.query_pairs(target, output_type='ndarray')
        repel = np.zeros_like(p)
        if len(pairs):
            i, j = pairs.T
            keep = arc[j]-arc[i] > 3*(gap+stroke)
            i, j = i[keep], j[keep]
            delta = p[i]-p[j]
            length = np.linalg.norm(delta, axis=1)
            push = delta / np.maximum(length[:,None], 1e-9) * ((target-length)/target)[:,None]
            np.add.at(repel, i, push)
            np.add.at(repel, j, -push)
        smooth = gaussian_filter1d(p, 3.5*smoothness, axis=0, mode='nearest')-p
        motion = 0.12*force + 0.65*repel + 0.7*smooth
        motion = gaussian_filter1d(motion, 1.5, axis=0, mode='nearest')
        motion /= np.maximum(1, np.linalg.norm(motion, axis=1)/0.75)[:,None]
        # Preserve the two endpoints and their immediate tangent neighborhoods.
        taper = np.minimum(np.arange(len(p)), np.arange(len(p))[::-1])/12
        motion *= np.clip(taper, 0, 1)[:,None]
        for scale in (1, 0.5, 0.25, 0.125, 0.0625):
            trial = np.round(resample(p+motion*scale), 4)
            if validate(trial, size, gap, stroke, edge, region, geometry_only=True):
                p = trial
                accepted += 1
                break
        if iteration % 10 == 9:
            if progress_callback:
                progress_callback('relax', iteration+1, iterations)
            else:
                print(f'Relaxation {iteration+1}/{iterations}: {accepted} safe steps', flush=True)
    # Remove small force-field ripples with a broad Gaussian finishing pass.
    # Try the smoothest version first and back off if it crowds another bend.
    polish_sigma = 0
    # Preserve the old defaults at 1.0; allow gentler settings and stronger
    # attempts while keeping the original safe fallbacks for values above 1.
    strengths = [s*smoothness for s in (7.0, 6.0, 5.0, 4.0, 3.0)]
    if smoothness > 1:
        strengths += [7.0, 6.0, 5.0, 4.0, 3.0]
    for sigma in sorted(set(strengths), reverse=True):
        polished = gaussian_filter1d(p, sigma, axis=0, mode='nearest')
        # Keep the endpoints exactly, but blend their neighborhoods smoothly.
        taper = np.clip(np.minimum(np.arange(len(p)), np.arange(len(p))[::-1])/24, 0, 1)
        polished = p+(polished-p)*taper[:,None]
        trial = np.round(resample(polished, step=0.75), 4)
        if validate(trial, size, gap, stroke, edge, region, geometry_only=True):
            p = trial
            polish_sigma = sigma
            break
    return p, dict(before=before, after=empty_space_stats(p, probes, stroke),
                   requested_smoothness=smoothness,
                   finishing_smoothing_sigma_samples=polish_sigma,
                   accepted_steps=accepted, requested_steps=iterations,
                   preferred_ink_gap_px=preferred_gap, probe_grid_step_px=6)


def generate(size=1200, gap=12.0, preferred_gap=18.0, iterations=50, smoothness=1.0,
             edge_wave=20.0, stroke=3.0, edge=35.0, seed=17, attempts=60,
             fill_shape='square', progress_callback=None, custom_region=None):
    """Run the full pipeline and return (path, report) without any file I/O
    or argparse/CLI involvement, so it can be called directly (e.g. from a GUI)
    many times in the same process instead of spawning a subprocess per call.

    size is a single number for a square canvas (original behavior,
    unchanged) or a (width, height) pair to fill a true rectangle -- e.g. a
    monitor resolution -- edge-to-edge with no cropping needed.

    fill_shape selects the region the curve fills, inscribed in the usual
    edge-inset area: 'square' (default; the whole inset canvas -- a
    rectangle when width != height), a preset shape ('circle', 'triangle',
    'diamond', 'pentagon', 'hexagon', 'octagon', 'star'), or 'custom' to fill
    a caller-supplied boundary instead (pass it as custom_region, a Shapely
    Polygon in canvas pixel coordinates -- e.g. one the user hand-drew in the
    GUI). gap/preferred_gap/stroke/edge/smoothness all mean the same thing
    regardless of shape. edge_wave boundary bending is currently only
    implemented for 'square' (see soften_boundary); it's silently skipped
    for the other shapes.

    Raises GenerationError (never a raw ValueError/traceback) if no valid
    curve could be produced for the given parameters.
    """
    width, height = _wh(size)
    if not np.isfinite(smoothness) or not 0.25 <= smoothness <= 3:
        raise GenerationError('smoothness must be a finite number between 0.25 and 3.')
    if min(width, height, gap, stroke, edge, attempts) <= 0:
        raise GenerationError('size (width/height), gap, stroke, edge, and attempts must be positive.')
    if preferred_gap < gap or iterations < 0 or edge_wave < 0:
        raise GenerationError('preferred_gap must be >= gap; iterations and edge_wave must be >= 0.')
    if fill_shape not in FILL_SHAPES:
        raise GenerationError(f'fill_shape must be one of {FILL_SHAPES}, got {fill_shape!r}.')
    if fill_shape == 'custom' and custom_region is None:
        raise GenerationError("fill_shape='custom' requires custom_region (a Shapely Polygon in canvas pixel coordinates).")
    if fill_shape != 'custom' and custom_region is not None:
        raise GenerationError("custom_region was given but fill_shape isn't 'custom'; set fill_shape='custom' to use it.")

    region = fill_polygon(fill_shape, size, edge, custom_region=custom_region)
    prepare(region)

    report = None
    for attempt in range(attempts):
        result = candidate(size, gap, stroke, edge, seed+attempt, fill_shape, region)
        if result is None:
            if progress_callback:
                progress_callback('search', attempt+1, attempts)
            continue
        p, sites = result
        # Quantize before testing: SVG and PNG use exactly these coordinates.
        p = np.round(p, 4)
        candidate_report = validate(p, size, gap, stroke, edge, region)
        if candidate_report:
            candidate_report.update(seed=seed+attempt, requested_seed=seed, sites=sites, fill_shape=fill_shape)
            report = candidate_report
            break
        if progress_callback:
            progress_callback('search', attempt+1, attempts)
        else:
            print(f'Rejected attempt {attempt+1}', flush=True)
    else:
        raise GenerationError(
            f'No candidate passed after {attempts} attempts with fill_shape={fill_shape!r}. '
            f'Try more --attempts, a larger canvas, a smaller --gap, or a less constrained shape.'
        )

    p, optimization = relax(p, size, gap, stroke, edge, preferred_gap, iterations,
                             region, smoothness, progress_callback=progress_callback)
    p, boundary = soften_boundary(p, size, gap, stroke, edge, report['seed'], edge_wave,
                                   region=region, fill_shape=fill_shape)
    final_report = validate(p, size, gap, stroke, edge, region)
    if not final_report:
        raise GenerationError('Final validation failed; try different parameters.')
    report.update(final_report)
    report['fill_shape'] = fill_shape
    report['canvas_width'] = width
    report['canvas_height'] = height
    report['optimization'] = optimization
    report['boundary_bending'] = boundary
    report['final_empty_space'] = empty_space_stats(p, probe_grid(region, step=6.0, inset=3.0), stroke)
    return p, report


def render_png(p, size, stroke, scale=4, line_color='#ffffff', bg_color='#000000'):
    """Return a PIL Image for the path. Pure function: no file I/O.

    line_color/bg_color and stroke are pure presentation -- changing them
    does not affect the validated geometry, so a caller can re-render as
    many times as it likes without recomputing the path. See
    max_safe_render_stroke() for the ceiling on how thick stroke can safely
    go before the line would visually touch itself.
    """
    width, height = _wh(size)
    w, h = round(width), round(height)
    img = Image.new('RGB', (w*scale, h*scale), bg_color)
    draw = ImageDraw.Draw(img)
    draw.line([tuple(q*scale) for q in p], fill=line_color, width=round(stroke*scale), joint='curve')
    for x, y in p[[0, -1]] * scale:
        r = stroke * scale / 2
        draw.ellipse((x-r, y-r, x+r, y+r), fill=line_color)
    return img.resize((w, h), Image.Resampling.LANCZOS)


def crawl_bands(p, crawler_len, gap_len, phase=0.0, n_colors=3):
    """Split an already-generated path into colored "crawler" runs for a
    chasing-lights animation: n_colors repeating bands of crawler_len px
    (measured along the path's own arc length, not straight-line distance)
    each, separated by gap_len px of nothing (the background shows through),
    cycling through color index 0..n_colors-1 and shifted along the path by
    `phase` px -- animate by calling this repeatedly with an increasing
    phase (wrapping at n_colors*(crawler_len+gap_len), the pattern's period,
    is safe but not required; the % below handles any phase).

    Returns a list of (color_index, points) -- points is a slice of `p` for
    one continuous colored run, ready to hand to any line-drawing backend.
    Gaps aren't included (nothing is drawn there).

    Pure function of the path and pattern parameters only -- no rendering,
    no Tkinter, no file I/O -- so a live GUI preview, a static frame export
    (render_crawl_frame below), and a future live-wallpaper daemon can all
    drive the exact same animation logic and always match each other frame
    for frame.
    """
    if crawler_len <= 0 or gap_len < 0:
        raise GenerationError('crawler_len must be > 0 and gap_len must be >= 0.')
    if n_colors < 1:
        raise GenerationError('n_colors must be >= 1.')
    arc = np.r_[0, np.cumsum(np.linalg.norm(np.diff(p, axis=0), axis=1))]
    cycle = crawler_len + gap_len
    period = n_colors * cycle
    pos = (arc + phase) % period
    cycle_pos = pos % cycle
    # -1 marks a gap point (nothing drawn there); otherwise the color index.
    band = np.where(cycle_pos < crawler_len, (pos // cycle).astype(int) % n_colors, -1)
    boundaries = np.flatnonzero(np.diff(band) != 0) + 1
    starts = np.r_[0, boundaries]
    ends = np.r_[boundaries, len(band)]
    return [(int(band[s]), p[s:e]) for s, e in zip(starts, ends) if band[s] != -1 and e - s >= 2]


def render_crawl_frame(p, size, stroke, colors, crawler_len, gap_len, phase=0.0,
                        bg_color='#000000', scale=4):
    """Render one frame of the crawl animation as a PIL Image. Pure
    function: no file I/O, no Tkinter. `colors` is a list of hex strings
    (any length -- the GUI's crawl control uses 3); each drawn run gets
    rounded end caps, matching render_png's look for a single continuous
    line. Calling this repeatedly with an advancing `phase` produces the
    animation's frames -- meant to be reusable well beyond the GUI's own
    live preview: a GIF exporter or a future live-wallpaper daemon can call
    it exactly the same way, on a timer, with no Tkinter dependency at all.
    """
    width, height = _wh(size)
    w, h = round(width), round(height)
    img = Image.new('RGB', (w*scale, h*scale), bg_color)
    draw = ImageDraw.Draw(img)
    for color_idx, pts in crawl_bands(p, crawler_len, gap_len, phase, n_colors=len(colors)):
        color = colors[color_idx]
        draw.line([tuple(q*scale) for q in pts], fill=color, width=round(stroke*scale), joint='curve')
        r = stroke * scale / 2
        for x, y in (pts[0]*scale, pts[-1]*scale):
            draw.ellipse((x-r, y-r, x+r, y+r), fill=color)
    return img.resize((w, h), Image.Resampling.LANCZOS)


def _fmt_dim(n):
    """Format a canvas dimension for SVG markup: no trailing '.0' for a
    whole-pixel value (matches the original viewBox="0 0 {size} {size}"
    formatting exactly when size was an int), a few decimals otherwise."""
    return str(int(n)) if float(n).is_integer() else f'{n:.4f}'


def render_svg(p, size, stroke, line_color='#111111', bg_color='#faf9f5'):
    """Return SVG markup for the path as a string. Pure function: no file I/O."""
    width, height = _wh(size)
    w, h = _fmt_dim(width), _fmt_dim(height)
    d = 'M ' + ' L '.join(f'{x:.4f},{y:.4f}' for x, y in p)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}">'
            f'<rect width="100%" height="100%" fill="{bg_color}"/>'
            f'<path d="{d}" fill="none" stroke="{line_color}" stroke-width="{stroke}" '
            f'stroke-linecap="round" stroke-linejoin="round"/></svg>')


def max_safe_render_stroke(report, generation_stroke, margin=0.5):
    """The largest stroke width render_png/render_svg can use for this
    already-generated path without the line visually touching itself.

    The path was validated at generation time with a specific stroke, giving
    a guaranteed minimum edge-to-edge ink gap (report['minimum_nonlocal_ink_gap_px']).
    That gap shrinks by exactly (new_stroke - generation_stroke) as stroke is
    thickened after the fact, since the centerline spacing itself never
    changes. margin keeps a small safety buffer above the hard zero-gap point.
    """
    return max(generation_stroke, generation_stroke + report['minimum_nonlocal_ink_gap_px'] - margin)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--size', type=int, default=1200,
                    help='Square canvas size in px. Ignored if --width/--height are both given.')
    ap.add_argument('--width', type=int, default=None,
                    help='Canvas width in px, for a non-square/rectangular canvas. Requires --height too.')
    ap.add_argument('--height', type=int, default=None,
                    help='Canvas height in px, for a non-square/rectangular canvas. Requires --width too.')
    ap.add_argument('--gap', type=float, default=12)
    ap.add_argument('--preferred-gap', type=float, default=18,
                    help='Soft ink spacing target, not a hard maximum')
    ap.add_argument('--iterations', type=int, default=50)
    ap.add_argument('--smoothness', type=float, default=1.0,
                    help='Curve smoothing multiplier: 0.25 to 3; default 1, try 1.5 or 2 for rounder bends')
    ap.add_argument('--edge-wave', type=float, default=20,
                    help='Smooth inward boundary bending in pixels; 0 disables it')
    ap.add_argument('--stroke', type=float, default=3)
    ap.add_argument('--edge', type=float, default=35)
    ap.add_argument('--seed', type=int, default=17)
    ap.add_argument('--attempts', type=int, default=60)
    ap.add_argument('--fill-shape', choices=FILL_SHAPES, default='square',
                    help="Region the curve fills, inscribed in the usual edge-inset area. 'square' "
                         "fills the whole canvas (a rectangle if --width/--height differ). 'custom' "
                         "isn't usable from the CLI (no way to supply a boundary) -- it's for library/GUI "
                         "callers that pass custom_region directly. edge-wave boundary bending is "
                         "currently 'square'-only.")
    ap.add_argument('--output', default='organic_curve.png')
    a = ap.parse_args()

    if (a.width is None) != (a.height is None):
        ap.error('--width and --height must be given together.')
    size_arg = (a.width, a.height) if a.width is not None else a.size

    try:
        p, report = generate(size=size_arg, gap=a.gap, preferred_gap=a.preferred_gap,
                              iterations=a.iterations, smoothness=a.smoothness,
                              edge_wave=a.edge_wave, stroke=a.stroke, edge=a.edge,
                              seed=a.seed, attempts=a.attempts, fill_shape=a.fill_shape)
    except GenerationError as e:
        ap.error(str(e))

    out = Path(a.output)
    render_png(p, size_arg, a.stroke).save(out)
    out.with_suffix('.svg').write_text(render_svg(p, size_arg, a.stroke))
    out.with_suffix('.validation.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
