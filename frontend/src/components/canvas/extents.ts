import type { Parameter, Point3 } from "./parameters";
import { formatLength, type LengthUnit } from "./units";

// The enclosure's overall dimensions (width, depth, height), drawn like a drawing's overall
// dimensions along the outside of its bounding box. They are read-only: a parameter's own
// dimension that already measures an axis's overall extent is drawn instead (it can be edited).

export type Bounds = { min: Point3; max: Point3 };
type Px = [number, number];
export type Axis = 0 | 1 | 2;

export const EXTENT_NAMES = ["Width", "Depth", "Height"] as const;

/** One overall dimension: its line (offset outside the box) and the extension lines from the box to it. */
export type Extent = {
  axis: Axis;
  name: (typeof EXTENT_NAMES)[number];
  /** mm. */
  length: number;
  label: string;
  from: Point3;
  to: Point3;
  /** From the box edge's ends out to the line's ends. */
  extension: [[Point3, Point3], [Point3, Point3]];
};

/** How close a parameter's length must be to an extent to be it: 0.01 mm or 0.1 %, the larger (as `dimensions.py`). */
const tolerance = (v: number) => Math.max(0.01, 1e-3 * Math.abs(v));

/** The axis a parameter's distance dimension runs along, and its length; null for a radius or a slanted one. */
function measuredAxis(p: Parameter): { axis: Axis; length: number } | null {
  const d = p.dimension;
  if (!d || d.kind !== "distance") return null;
  const v = [0, 1, 2].map((k) => d.end.point[k] - d.start.point[k]);
  const length = Math.hypot(...v);
  const axis = v.findIndex((c) => Math.abs(Math.abs(c) - length) <= tolerance(length));
  return axis < 0 ? null : { axis: axis as Axis, length };
}

/** The axes whose overall extent one of `drawn` (the parameter dimensions on the model) already measures. */
export function coveredAxes(bounds: Bounds, drawn: Parameter[]): Set<Axis> {
  const covered = new Set<Axis>();
  for (const p of drawn) {
    const m = measuredAxis(p);
    if (!m) continue;
    const size = bounds.max[m.axis] - bounds.min[m.axis];
    if (Math.abs(m.length - size) <= tolerance(size)) covered.add(m.axis);
  }
  return covered;
}

/**
 * The overall dimensions to draw: one per axis of the box that no parameter dimension in `drawn`
 * covers and that isn't flat. Each goes along one of the box's four edges on that axis, offset
 * out from a face: the one whose line lies farthest from the box's middle on screen (`project`,
 * board mm → canvas px), so it runs along the outline rather than across the model, preferring
 * the bottom edges for width and depth. An axis pointing at the viewer (its line shorter than
 * `minPx` on screen) is left out, as a drawing's view leaves out the depth it can't show.
 */
export function overallExtents(
  bounds: Bounds,
  drawn: Parameter[],
  unit: LengthUnit,
  project: (p: Point3) => Px | null,
  minPx = 12,
): Extent[] {
  const { min, max } = bounds;
  const size = [0, 1, 2].map((k) => max[k] - min[k]);
  const gap = Math.max(2, 0.12 * Math.max(...size));
  const middle = project([0, 1, 2].map((k) => (min[k] + max[k]) / 2) as Point3);
  const covered = coveredAxes(bounds, drawn);
  const extents: Extent[] = [];
  for (const axis of [0, 1, 2] as Axis[]) {
    if (covered.has(axis) || size[axis] <= tolerance(0)) continue;
    const others = ([0, 1, 2] as Axis[]).filter((k) => k !== axis);
    const levels = (k: Axis) => [min[k], max[k]];
    let best: { extent: Extent; score: number } | null = null;
    for (const a of levels(others[0]))
      for (const b of levels(others[1]))
        for (const out of others) {
          const corner = (t: number): Point3 => {
            const p = [0, 0, 0] as Point3;
            p[axis] = t;
            p[others[0]] = a;
            p[others[1]] = b;
            return p;
          };
          const at = out === others[0] ? a : b;
          const sign = at === min[out] ? -1 : 1;
          const shift = (p: Point3): Point3 => {
            const q = [...p] as Point3;
            q[out] += sign * gap;
            return q;
          };
          const [e0, e1] = [corner(min[axis]), corner(max[axis])];
          const [from, to] = [shift(e0), shift(e1)];
          const [pa, pb] = [project(from), project(to)];
          if (!pa || !pb || !middle) continue;
          if (Math.hypot(pb[0] - pa[0], pb[1] - pa[1]) < minPx) continue;
          const mid: Px = [(pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2];
          // Farthest out on screen wins; a bottom edge gets a small head start for width and depth.
          const bottom = axis !== 2 && corner(0)[2] === min[2] ? 1.05 : 1;
          const score = Math.hypot(mid[0] - middle[0], mid[1] - middle[1]) * bottom;
          if (!best || score > best.score + 1e-6)
            best = {
              score,
              extent: {
                axis,
                name: EXTENT_NAMES[axis],
                length: size[axis],
                label: formatLength(size[axis], unit),
                from,
                to,
                extension: [
                  [e0, from],
                  [e1, to],
                ],
              },
            };
        }
    if (best) extents.push(best.extent);
  }
  return extents;
}
