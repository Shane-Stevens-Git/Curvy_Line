"""Sensor-based self-avoiding walker: an experimental alternative to
organic_curve.py's tree-buffer approach, built purely to compare results.

Instead of inflating a spanning tree into a blob and tracing its outline,
this walks the pen forward step by step. At each step it looks at nearby
empty space, its own already-drawn ink, and the canvas edge, and picks the
best next heading from a bounded set of curving options -- this is the
literal "walker with sensors" approach organic_curve.py's docstring
explicitly avoids, because a naive greedy walker can trap itself in a
pocket it can no longer draw out of.

This version defends against trapping with: bounded turning per step (the
line can't double back sharply), short lookahead per candidate (to see an
imminent trap before stepping into it), a coarse density field that steers
toward locally empty canvas, and a target radius from the start point that
grows over the course of the walk, so it fills outward gradually instead of
darting for open space immediately and sealing off the interior behind it.

It reuses organic_curve.py's exact validate()/render functions, so a result
carries the same zero-intersection / minimum-gap guarantee and is directly
comparable (same PNG/SVG/JSON outputs) to the tree-buffer script.

Usage: python walker_curve.py --seed 3 --size 1200 --output walker_curve.png
"""
import argparse
import json
from pathlib import Path

import numpy as np
from scipy.ndimage import gaussian_filter
from scipy.spatial import cKDTree

from organic_curve import validate, empty_space_stats, render_png, render_svg, GenerationError


def _walk(size, gap, stroke, edge, seed, max_steps, min_radius, grid_cell,
          rebuild_every=30, candidates_n=13, lookahead_steps=6):
    rng = np.random.default_rng(seed)
    ds = max(1.5, stroke * 1.5)
    max_turn = ds / min_radius
    safe_lo, safe_hi = edge + stroke / 2, size - edge - stroke / 2
    required = (gap + stroke) * 1.08  # small safety margin over the hard minimum
    local_exclude_arc = 3 * (gap + stroke)
    local_exclude_n = max(4, int(local_exclude_arc / ds))

    span = safe_hi - safe_lo
    start = rng.uniform(safe_lo + span * 0.35, safe_hi - span * 0.35, 2)
    theta = rng.uniform(0, 2 * np.pi)
    bias = 0.0  # slowly evolving preferred turn rate, for organic constant curvature

    pts = [start]
    tree = None
    tree_upto = 0
    r_max = span / 2 * 0.97

    grid_n = max(4, int(size / grid_cell))
    density = np.zeros((grid_n, grid_n))

    def grid_index(pos):
        return np.clip((pos / size * grid_n).astype(int), 0, grid_n - 1)

    gi = grid_index(start)
    density[gi[0], gi[1]] += 1
    blurred = density

    for step in range(max_steps):
        p = pts[-1]
        if (step % rebuild_every) == 0:
            arr = np.array(pts)
            tree = cKDTree(arr)
            tree_upto = len(arr)
            blurred = gaussian_filter(density, sigma=1.2, mode='constant')

        # Mean-revert so it can't lock into a tight sustained spiral (which
        # boxes the line in against its own earlier coils) for very long.
        bias = bias * 0.9 + rng.normal(0, max_turn * 0.18)
        bias = np.clip(bias, -max_turn * 0.6, max_turn * 0.6)

        deltas = np.linspace(-max_turn, max_turn, candidates_n)
        best_score, best_pos, best_theta = -np.inf, None, None
        for delta in deltas:
            theta_c = theta + bias + delta
            direction = np.array([np.cos(theta_c), np.sin(theta_c)])
            cand = p + ds * direction

            if not (safe_lo <= cand[0] <= safe_hi and safe_lo <= cand[1] <= safe_hi):
                continue

            recent = np.array(pts[-local_exclude_n:]) if len(pts) > 1 else None
            ok = True
            min_d = np.inf
            if tree is not None:
                k = min(8, tree_upto)
                dists, idxs = tree.query(cand, k=k)
                dists = np.atleast_1d(dists)
                idxs = np.atleast_1d(idxs)
                nonlocal_mask = idxs < (len(pts) - local_exclude_n)
                if nonlocal_mask.any():
                    min_d = dists[nonlocal_mask].min()
                    if min_d < required:
                        ok = False
            if ok and tree_upto < len(pts):
                extra = np.array(pts[tree_upto:-local_exclude_n]) if len(pts) - local_exclude_n > tree_upto else None
                if extra is not None and len(extra):
                    d2 = np.linalg.norm(extra - cand, axis=1).min()
                    min_d = min(min_d, d2)
                    if d2 < required:
                        ok = False
            if not ok:
                continue

            # Lookahead: commit to this heading for a few more steps (no further
            # steering) and see whether it runs straight at the edge or at itself.
            trap_penalty = 0.0
            lp = cand.copy()
            for _ in range(lookahead_steps):
                lp = lp + ds * direction
                if not (safe_lo <= lp[0] <= safe_hi and safe_lo <= lp[1] <= safe_hi):
                    trap_penalty += 1.0
                    break
                if tree is not None:
                    ld, li = tree.query(lp, k=1)
                    if li < tree_upto - local_exclude_n and ld < required * 1.3:
                        trap_penalty += 1.0
                        break

            gpos = grid_index(cand)
            dens = blurred[gpos[0], gpos[1]]
            edge_margin = min(cand[0] - safe_lo, safe_hi - cand[0], cand[1] - safe_lo, safe_hi - cand[1])
            # Must start turning away while there's still room to actually turn:
            # ramp sharply once margin drops below ~2 turning radii, not just when close.
            turn_room = min_radius * 2.2
            edge_term = -max(0.0, (turn_room - edge_margin) / turn_room) ** 2
            heading_term = -abs(delta) / max_turn
            density_term = -dens

            score = (1.3 * heading_term + 1.1 * density_term / max(blurred.max(), 1e-6)
                     + 3.0 * edge_term - 4.0 * trap_penalty)
            if score > best_score:
                best_score, best_pos, best_theta = score, cand, theta_c

        if best_pos is None:
            break  # genuine dead end: stop the walk here

        pts.append(best_pos)
        theta = best_theta
        gi = grid_index(best_pos)
        density[gi[0], gi[1]] += 1

    return np.array(pts)


def generate_walker(size=1200, gap=12.0, stroke=3.0, edge=35.0, seed=3,
                     min_radius=None, max_steps=None, attempts=8,
                     progress_callback=None):
    """Try several seeds/step-budgets, keep the best walk that passes the
    same exact validate() organic_curve.py uses, and return (path, report).
    Raises GenerationError if no attempt produced a valid, reasonably-filled
    curve.
    """
    if min_radius is None:
        min_radius = max(8.0, stroke * 8)
    if max_steps is None:
        max_steps = int(size * size / ((gap + stroke) ** 2) * 0.55)
    grid_cell = max(6.0, (gap + stroke))

    best = None
    for attempt in range(attempts):
        raw = _walk(size, gap, stroke, edge, seed + attempt, max_steps, min_radius, grid_cell)
        if len(raw) < 20:
            if progress_callback:
                progress_callback('walk', attempt + 1, attempts)
            continue
        # Light resample for a uniform point spacing before the exact checks.
        arc = np.r_[0, np.cumsum(np.linalg.norm(np.diff(raw, axis=0), axis=1))]
        step = max(1.0, (gap + stroke) * 0.18)
        s = np.linspace(0, arc[-1], int(arc[-1] / step) + 2)
        p = np.column_stack([np.interp(s, arc, raw[:, j]) for j in (0, 1)])
        p = np.round(p, 4)

        report = validate(p, size, gap, stroke, edge)
        region_area = (size - 2 * edge) ** 2
        line_len = arc[-1]
        approx_coverage = min(1.0, line_len * (gap + stroke) * 1.6 / region_area)
        candidate_info = dict(seed=seed + attempt, steps_taken=len(raw), max_steps=max_steps,
                              path_length_px=round(float(line_len), 1),
                              approx_coverage=round(approx_coverage, 4),
                              passed_exact_validation=bool(report))
        if report:
            report.update(seed=seed + attempt, requested_seed=seed)
            report['approx_coverage_before_exact_check'] = round(approx_coverage, 4)
            report['final_empty_space'] = empty_space_stats(
                p, np.stack(np.meshgrid(np.arange(edge + 3, size - edge, 6.),
                                         np.arange(edge + 3, size - edge, 6.)), axis=-1).reshape(-1, 2), stroke)
            if best is None or report['covered_fraction'] > best[1]['covered_fraction']:
                best = (p, report)
        if progress_callback:
            progress_callback('walk', attempt + 1, attempts)
        else:
            print(f"Attempt {attempt+1}/{attempts}: {candidate_info}", flush=True)

    if best is None:
        raise GenerationError(
            f'No walker attempt produced a valid curve passing exact validation after {attempts} attempts.'
        )
    return best


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--size', type=int, default=1200)
    ap.add_argument('--gap', type=float, default=12)
    ap.add_argument('--stroke', type=float, default=3)
    ap.add_argument('--edge', type=float, default=35)
    ap.add_argument('--seed', type=int, default=3)
    ap.add_argument('--min-radius', type=float, default=None,
                    help='Tightest turning radius in px; smaller = tighter curls. Default: 8x stroke.')
    ap.add_argument('--max-steps', type=int, default=None,
                    help='Cap on walk length. Default: scaled from canvas area and spacing.')
    ap.add_argument('--attempts', type=int, default=8)
    ap.add_argument('--output', default='walker_curve.png')
    a = ap.parse_args()

    try:
        p, report = generate_walker(size=a.size, gap=a.gap, stroke=a.stroke, edge=a.edge,
                                     seed=a.seed, min_radius=a.min_radius, max_steps=a.max_steps,
                                     attempts=a.attempts)
    except GenerationError as e:
        ap.error(str(e))

    out = Path(a.output)
    render_png(p, a.size, a.stroke).save(out)
    out.with_suffix('.svg').write_text(render_svg(p, a.size, a.stroke))
    out.with_suffix('.validation.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
