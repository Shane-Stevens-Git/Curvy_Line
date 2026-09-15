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
from shapely.geometry import LineString, MultiLineString, box
from shapely import STRtree, linestrings, distance as geometric_distance
from PIL import Image, ImageDraw


class GenerationError(Exception):
    """Raised when no valid curve could be produced for the given parameters."""


def resample(p, step=2):
    arc = np.r_[0, np.cumsum(np.linalg.norm(np.diff(p, axis=0), axis=1))]
    keep = np.r_[True, np.diff(arc) > 1e-9]
    p, arc = p[keep], arc[keep]
    s = np.linspace(0, arc[-1], int(arc[-1] / step) + 2)
    return np.column_stack([np.interp(s, arc, p[:, j]) for j in (0, 1)])


def candidate(size, gap, stroke, edge, seed):
    rng = np.random.default_rng(seed)
    pitch = (gap + stroke) * 3.2
    radius = pitch * 0.245
    margin = edge + stroke / 2 + radius + 8
    lo, hi = margin, size - margin
    if hi - lo < 3 * pitch:
        raise GenerationError(
            f'Canvas is too small for this spacing: with gap={gap}, stroke={stroke}, '
            f'edge={edge}, size must be at least {int(3 * pitch + 2 * margin)} px '
            f'(got {size} px). Increase --size or reduce --gap/--edge.'
        )
    sites = [rng.uniform(lo, hi, 2)]
    # Best-candidate scattering fills empty areas without a lattice.
    for _ in range(int((hi-lo)**2 / pitch**2 * 1.5)):
        choices = rng.uniform(lo, hi, (180, 2))
        d = cdist(choices, sites).min(axis=1)
        if d.max() < pitch * 0.80:
            break
        sites.append(choices[d.argmax()])
    sites = np.array(sites)
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


def validate(p, size, gap, stroke, edge, geometry_only=False):
    line = LineString(p)
    if not line.is_simple:
        return None
    edge_actual = min(p.min(), size-p.max()) - stroke/2
    if edge_actual < edge:
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
    region = box(edge, edge, size-edge, size-edge)
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


def soften_boundary(p, size, gap, stroke, edge, seed, amplitude):
    """Replace ruler-like boundary runs with smooth, nonperiodic-looking bends.

    Nearby sections move together in a smooth spatial field, rather than
    adding independent noisy wiggles to each vertex. Displacement points
    inward and fades into the interior. Every candidate is checked exactly.
    """
    if amplitude == 0:
        return p, dict(requested_amplitude_px=0, accepted_amplitude_px=0)
    rng = np.random.default_rng(seed+941)
    phases = rng.uniform(0, 2*np.pi, (4, 2))
    wavelength = max(100., size*0.14)
    band = max(70., size*0.085)
    displacement = np.zeros_like(p)
    for side in range(4):
        axis = side//2
        sign = 1 if side%2 == 0 else -1
        depth = p[:, axis]-edge if sign == 1 else size-edge-p[:, axis]
        along = p[:, 1-axis]
        wave = (0.52 + 0.32*np.sin(2*np.pi*along/wavelength+phases[side,0])
                + 0.16*np.sin(2*np.pi*along/(wavelength*1.73)+phases[side,1]))
        influence = np.exp(-(np.maximum(0, depth)/band)**2)
        displacement[:, axis] += sign*influence*wave
    for fraction in (1., .8, .6, .4, .2):
        trial = np.round(resample(p+amplitude*fraction*displacement, step=.75), 4)
        if validate(trial, size, gap, stroke, edge, geometry_only=True):
            return trial, dict(requested_amplitude_px=amplitude,
                               accepted_amplitude_px=amplitude*fraction,
                               wavelength_px=wavelength, influence_band_px=band)
    raise GenerationError('Boundary rounding could not preserve clearance; reduce --edge-wave.')


def relax(p, size, gap, stroke, edge, preferred_gap, iterations, smoothness=1.0, progress_callback=None):
    """Redistribute into voids, repel crowded sections, and smooth curvature.

    The probe lattice senses space only; it does not define the drawn path.
    Longer deformed segments are resampled so added arc length remains smooth.
    Each accepted step passes exact segment collision/clearance checks.
    progress_callback(phase, current, total), when given, is called instead
    of printing so a GUI can drive its own progress bar.
    """
    axis = np.arange(edge+3, size-edge, 6.0)
    probes = np.stack(np.meshgrid(axis, axis), axis=-1).reshape(-1, 2)
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
            if validate(trial, size, gap, stroke, edge, geometry_only=True):
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
        if validate(trial, size, gap, stroke, edge, geometry_only=True):
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
             progress_callback=None):
    """Run the full pipeline and return (path, report) without any file I/O
    or argparse/CLI involvement, so it can be called directly (e.g. from a GUI)
    many times in the same process instead of spawning a subprocess per call.

    Raises GenerationError (never a raw ValueError/traceback) if no valid
    curve could be produced for the given parameters.
    """
    if not np.isfinite(smoothness) or not 0.25 <= smoothness <= 3:
        raise GenerationError('smoothness must be a finite number between 0.25 and 3.')
    if min(size, gap, stroke, edge, attempts) <= 0:
        raise GenerationError('size, gap, stroke, edge, and attempts must be positive.')
    if preferred_gap < gap or iterations < 0 or edge_wave < 0:
        raise GenerationError('preferred_gap must be >= gap; iterations and edge_wave must be >= 0.')

    report = None
    for attempt in range(attempts):
        result = candidate(size, gap, stroke, edge, seed+attempt)
        if result is None:
            if progress_callback:
                progress_callback('search', attempt+1, attempts)
            continue
        p, sites = result
        # Quantize before testing: SVG and PNG use exactly these coordinates.
        p = np.round(p, 4)
        candidate_report = validate(p, size, gap, stroke, edge)
        if candidate_report:
            candidate_report.update(seed=seed+attempt, requested_seed=seed, sites=sites)
            report = candidate_report
            break
        if progress_callback:
            progress_callback('search', attempt+1, attempts)
        else:
            print(f'Rejected attempt {attempt+1}', flush=True)
    else:
        raise GenerationError(
            f'No candidate passed after {attempts} attempts. Try more --attempts, '
            f'a larger --size, or a smaller --gap.'
        )

    p, optimization = relax(p, size, gap, stroke, edge, preferred_gap, iterations,
                             smoothness, progress_callback=progress_callback)
    p, boundary = soften_boundary(p, size, gap, stroke, edge, report['seed'], edge_wave)
    final_report = validate(p, size, gap, stroke, edge)
    if not final_report:
        raise GenerationError('Final validation failed; try different parameters.')
    report.update(final_report)
    report['optimization'] = optimization
    report['boundary_bending'] = boundary
    axis = np.arange(edge+3, size-edge, 6.)
    probes = np.stack(np.meshgrid(axis, axis), axis=-1).reshape(-1, 2)
    report['final_empty_space'] = empty_space_stats(p, probes, stroke)
    return p, report


def render_png(p, size, stroke, scale=4, line_color='#ffffff', bg_color='#000000'):
    """Return a PIL Image for the path. Pure function: no file I/O.

    line_color/bg_color and stroke are pure presentation -- changing them
    does not affect the validated geometry, so a caller can re-render as
    many times as it likes without recomputing the path. See
    max_safe_render_stroke() for the ceiling on how thick stroke can safely
    go before the line would visually touch itself.
    """
    img = Image.new('RGB', (size*scale, size*scale), bg_color)
    draw = ImageDraw.Draw(img)
    draw.line([tuple(q*scale) for q in p], fill=line_color, width=round(stroke*scale), joint='curve')
    for x, y in p[[0, -1]] * scale:
        r = stroke * scale / 2
        draw.ellipse((x-r, y-r, x+r, y+r), fill=line_color)
    return img.resize((size, size), Image.Resampling.LANCZOS)


def render_svg(p, size, stroke, line_color='#111111', bg_color='#faf9f5'):
    """Return SVG markup for the path as a string. Pure function: no file I/O."""
    d = 'M ' + ' L '.join(f'{x:.4f},{y:.4f}' for x, y in p)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}">'
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
    ap.add_argument('--size', type=int, default=1200)
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
    ap.add_argument('--output', default='organic_curve.png')
    a = ap.parse_args()

    try:
        p, report = generate(size=a.size, gap=a.gap, preferred_gap=a.preferred_gap,
                              iterations=a.iterations, smoothness=a.smoothness,
                              edge_wave=a.edge_wave, stroke=a.stroke, edge=a.edge,
                              seed=a.seed, attempts=a.attempts)
    except GenerationError as e:
        ap.error(str(e))

    out = Path(a.output)
    render_png(p, a.size, a.stroke).save(out)
    out.with_suffix('.svg').write_text(render_svg(p, a.size, a.stroke))
    out.with_suffix('.validation.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
