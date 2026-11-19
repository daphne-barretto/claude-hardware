/**
 * Geometry for the inspection tools (section cut, measure, clearance), free of three.js so it
 * can be tested on its own. Everything is in board coordinates (millimetres, Z up), the frame of
 * `backend/app/boards/SCHEMA.md` and of the mesh contract's parts in their assembled positions.
 *
 * Distances are exact for the triangles given: the enclosure's planar faces are exact in the
 * mesh, so a clearance measured here matches the check's exact solid distance up to float
 * precision (curved faces differ by the tessellation's chord error).
 */

export type Vec3 = [number, number, number];

/** The parts of a board revision the canvas draws (board JSON, `backend/app/boards/SCHEMA.md`). */
export type BoardComponent = {
  ref: string;
  value: string;
  role: string | null;
  side: "top" | "bottom";
  position: [number, number];
  rotation: number;
  size: [number, number];
  height: number;
};
export type Board = {
  name: string;
  revision: string;
  pcb: { thickness: number; outline: [number, number][] };
  components: BoardComponent[];
};

/** Triangles as a flat list: 9 numbers (three corners) per triangle. */
export type Triangles = Float64Array;

/**
 * A component's body: its footprint rectangle (counter-clockwise corners) extruded from the
 * board surface on its side by its height, as the component clearance check builds it.
 */
export type Body = { corners: [number, number][]; z0: number; z1: number };

/** Below this height a body is treated as this thin, as the checks do. */
const MIN_HEIGHT = 1e-3;

export function componentBody(board: Pick<Board, "pcb">, c: BoardComponent): Body {
  const [w, l] = c.size;
  const r = (c.rotation * Math.PI) / 180;
  const cos = Math.cos(r);
  const sin = Math.sin(r);
  const corners = (
    [
      [-w / 2, -l / 2],
      [w / 2, -l / 2],
      [w / 2, l / 2],
      [-w / 2, l / 2],
    ] as const
  ).map(([x, y]) => [c.position[0] + x * cos - y * sin, c.position[1] + x * sin + y * cos] as [number, number]);
  const h = Math.max(c.height, MIN_HEIGHT);
  const t = board.pcb.thickness;
  return c.side === "top" ? { corners, z0: t, z1: t + h } : { corners, z0: -h, z1: 0 };
}

/** The centre of a body. */
export function bodyCenter(b: Body): Vec3 {
  const x = b.corners.reduce((s, p) => s + p[0], 0) / b.corners.length;
  const y = b.corners.reduce((s, p) => s + p[1], 0) / b.corners.length;
  return [x, y, (b.z0 + b.z1) / 2];
}

/** The 12 triangles of a body's surface. */
export function bodyTriangles(b: Body): Triangles {
  const [a, bb, c, d] = b.corners;
  const lo = (p: [number, number]): Vec3 => [p[0], p[1], b.z0];
  const hi = (p: [number, number]): Vec3 => [p[0], p[1], b.z1];
  const quads: Vec3[][] = [
    [lo(a), lo(d), lo(c), lo(bb)],
    [hi(a), hi(bb), hi(c), hi(d)],
    [lo(a), lo(bb), hi(bb), hi(a)],
    [lo(bb), lo(c), hi(c), hi(bb)],
    [lo(c), lo(d), hi(d), hi(c)],
    [lo(d), lo(a), hi(a), hi(d)],
  ];
  const out: number[] = [];
  for (const [p, q, r, s] of quads) out.push(...p, ...q, ...r, ...p, ...r, ...s);
  return Float64Array.from(out);
}

/** The 8 corners of a body. */
export function bodyCorners(b: Body): Vec3[] {
  return [...b.corners.map((p): Vec3 => [p[0], p[1], b.z0]), ...b.corners.map((p): Vec3 => [p[0], p[1], b.z1])];
}

// ---------------------------------------------------------------------------------------
// Vector helpers

const sub = (a: Vec3, b: Vec3): Vec3 => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const add = (a: Vec3, b: Vec3): Vec3 => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
const scale = (a: Vec3, s: number): Vec3 => [a[0] * s, a[1] * s, a[2] * s];
const dot = (a: Vec3, b: Vec3) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const cross = (a: Vec3, b: Vec3): Vec3 => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const dist2 = (a: Vec3, b: Vec3) => {
  const d = sub(a, b);
  return dot(d, d);
};
const clamp01 = (t: number) => Math.min(1, Math.max(0, t));

const tri = (t: Triangles, i: number): [Vec3, Vec3, Vec3] => [
  [t[i * 9], t[i * 9 + 1], t[i * 9 + 2]],
  [t[i * 9 + 3], t[i * 9 + 4], t[i * 9 + 5]],
  [t[i * 9 + 6], t[i * 9 + 7], t[i * 9 + 8]],
];

/** The closest point on triangle abc to p (Ericson, Real-Time Collision Detection 5.1.5). */
export function closestOnTriangle(p: Vec3, a: Vec3, b: Vec3, c: Vec3): Vec3 {
  const ab = sub(b, a);
  const ac = sub(c, a);
  const ap = sub(p, a);
  const d1 = dot(ab, ap);
  const d2 = dot(ac, ap);
  if (d1 <= 0 && d2 <= 0) return a;
  const bp = sub(p, b);
  const d3 = dot(ab, bp);
  const d4 = dot(ac, bp);
  if (d3 >= 0 && d4 <= d3) return b;
  const vc = d1 * d4 - d3 * d2;
  if (vc <= 0 && d1 >= 0 && d3 <= 0) return add(a, scale(ab, d1 / (d1 - d3)));
  const cp = sub(p, c);
  const d5 = dot(ab, cp);
  const d6 = dot(ac, cp);
  if (d6 >= 0 && d5 <= d6) return c;
  const vb = d5 * d2 - d1 * d6;
  if (vb <= 0 && d2 >= 0 && d6 <= 0) return add(a, scale(ac, d2 / (d2 - d6)));
  const va = d3 * d6 - d5 * d4;
  if (va <= 0 && d4 - d3 >= 0 && d5 - d6 >= 0) return add(b, scale(sub(c, b), (d4 - d3) / (d4 - d3 + (d5 - d6))));
  const denom = va + vb + vc;
  if (denom === 0) return a;
  const v = vb / denom;
  const w = vc / denom;
  return add(a, add(scale(ab, v), scale(ac, w)));
}

/** The closest points between segments p1q1 and p2q2 (Ericson 5.1.9). */
export function closestSegmentSegment(p1: Vec3, q1: Vec3, p2: Vec3, q2: Vec3): [Vec3, Vec3] {
  const d1 = sub(q1, p1);
  const d2 = sub(q2, p2);
  const r = sub(p1, p2);
  const a = dot(d1, d1);
  const e = dot(d2, d2);
  const f = dot(d2, r);
  const EPS = 1e-12;
  let s: number;
  let t: number;
  if (a <= EPS && e <= EPS) return [p1, p2];
  if (a <= EPS) {
    s = 0;
    t = clamp01(f / e);
  } else {
    const c = dot(d1, r);
    if (e <= EPS) {
      t = 0;
      s = clamp01(-c / a);
    } else {
      const b = dot(d1, d2);
      const denom = a * e - b * b;
      s = denom > EPS ? clamp01((b * f - c * e) / denom) : 0;
      t = (b * s + f) / e;
      if (t < 0) {
        t = 0;
        s = clamp01(-c / a);
      } else if (t > 1) {
        t = 1;
        s = clamp01((b - c) / a);
      }
    }
  }
  return [add(p1, scale(d1, s)), add(p2, scale(d2, t))];
}

/** Where segment pq crosses triangle abc, or null. */
function segmentTriangle(p: Vec3, q: Vec3, a: Vec3, b: Vec3, c: Vec3): Vec3 | null {
  const dir = sub(q, p);
  const e1 = sub(b, a);
  const e2 = sub(c, a);
  const h = cross(dir, e2);
  const det = dot(e1, h);
  if (Math.abs(det) < 1e-12) return null;
  const inv = 1 / det;
  const s = sub(p, a);
  const u = dot(s, h) * inv;
  if (u < 0 || u > 1) return null;
  const qv = cross(s, e1);
  const v = dot(dir, qv) * inv;
  if (v < 0 || u + v > 1) return null;
  const t = dot(e2, qv) * inv;
  if (t < 0 || t > 1) return null;
  return add(p, scale(dir, t));
}

/** A closest pair of points and their distance. */
export type Closest = { distance: number; from: Vec3; to: Vec3 };

/** The exact distance between two triangles, with the closest points (`from` on the first). */
export function triangleDistance(t1: [Vec3, Vec3, Vec3], t2: [Vec3, Vec3, Vec3]): Closest {
  // Touching or crossing: an edge of one goes through the other.
  for (const [x, y] of [
    [t1, t2],
    [t2, t1],
  ] as const) {
    for (let i = 0; i < 3; i++) {
      const hit = segmentTriangle(x[i], x[(i + 1) % 3], y[0], y[1], y[2]);
      if (hit) return { distance: 0, from: hit, to: hit };
    }
  }
  // Apart: the closest pair is a vertex and a face, or two edges.
  let best: Closest = { distance: Infinity, from: t1[0], to: t2[0] };
  const consider = (from: Vec3, to: Vec3) => {
    const d = dist2(from, to);
    if (d < best.distance) best = { distance: d, from, to };
  };
  for (const v of t1) consider(v, closestOnTriangle(v, t2[0], t2[1], t2[2]));
  for (const v of t2) consider(closestOnTriangle(v, t1[0], t1[1], t1[2]), v);
  for (let i = 0; i < 3; i++)
    for (let j = 0; j < 3; j++) {
      const [a, b] = closestSegmentSegment(t1[i], t1[(i + 1) % 3], t2[j], t2[(j + 1) % 3]);
      consider(a, b);
    }
  return { ...best, distance: Math.sqrt(best.distance) };
}

/** Per-triangle bounding boxes, 6 numbers each (min xyz, max xyz). */
function boxes(t: Triangles): Float64Array {
  const n = t.length / 9;
  const out = new Float64Array(n * 6);
  for (let i = 0; i < n; i++) {
    for (let k = 0; k < 3; k++) {
      const a = t[i * 9 + k];
      const b = t[i * 9 + 3 + k];
      const c = t[i * 9 + 6 + k];
      out[i * 6 + k] = Math.min(a, b, c);
      out[i * 6 + 3 + k] = Math.max(a, b, c);
    }
  }
  return out;
}

function boxGap(a: Float64Array, i: number, b: Float64Array, j: number): number {
  let s = 0;
  for (let k = 0; k < 3; k++) {
    const g = Math.max(0, a[i * 6 + k] - b[j * 6 + 3 + k], b[j * 6 + k] - a[i * 6 + 3 + k]);
    s += g * g;
  }
  return Math.sqrt(s);
}

/** The exact distance between two triangle sets (surfaces), with the closest points. */
export function surfaceDistance(a: Triangles, b: Triangles): Closest {
  const ba = boxes(a);
  const bb = boxes(b);
  let best: Closest = { distance: Infinity, from: [0, 0, 0], to: [0, 0, 0] };
  for (let i = 0; i < a.length / 9; i++) {
    const ti = tri(a, i);
    for (let j = 0; j < b.length / 9; j++) {
      if (boxGap(ba, i, bb, j) >= best.distance) continue;
      const c = triangleDistance(ti, tri(b, j));
      if (c.distance < best.distance) best = c;
      if (best.distance === 0) return best;
    }
  }
  return best;
}

/** The distance from a point to a triangle set, with the closest point (`to`). */
export function pointDistance(p: Vec3, t: Triangles): Closest {
  let best: Closest = { distance: Infinity, from: p, to: p };
  for (let i = 0; i < t.length / 9; i++) {
    const [a, b, c] = tri(t, i);
    const q = closestOnTriangle(p, a, b, c);
    const d = Math.sqrt(dist2(p, q));
    if (d < best.distance) best = { distance: d, from: p, to: q };
  }
  return best;
}

/** True when p is inside the closed surface t (ray parity along +x, nudged off edges). */
export function insideSurface(p: Vec3, t: Triangles): boolean {
  const far: Vec3 = [p[0] + 1e6, p[1] + 1.234567e-3, p[2] + 2.345678e-3];
  let crossings = 0;
  for (let i = 0; i < t.length / 9; i++) {
    const [a, b, c] = tri(t, i);
    if (segmentTriangle(p, far, a, b, c)) crossings++;
  }
  return crossings % 2 === 1;
}

/** True when p is inside the body. */
function insideBody(p: Vec3, body: Body): boolean {
  if (p[2] < body.z0 || p[2] > body.z1) return false;
  const c = body.corners;
  // Counter-clockwise or clockwise, p is inside when it is on the same side of every edge.
  let sign = 0;
  for (let i = 0; i < c.length; i++) {
    const [x0, y0] = c[i];
    const [x1, y1] = c[(i + 1) % c.length];
    const s = (x1 - x0) * (p[1] - y0) - (y1 - y0) * (p[0] - x0);
    if (Math.abs(s) < 1e-12) continue;
    if (sign === 0) sign = Math.sign(s);
    else if (Math.sign(s) !== sign) return false;
  }
  return true;
}

/** A component's clearance: the shortest distance from its body to any enclosure part. */
export type Clearance = Closest & { ref: string; part: string };

/**
 * The exact distance from a component's body to every enclosure part (closed surfaces), as the
 * component clearance check measures it: zero when the body touches or overlaps a part.
 */
export function componentClearance(
  board: Pick<Board, "pcb">,
  component: BoardComponent,
  parts: { part: string; triangles: Triangles }[],
): Clearance | null {
  const body = componentBody(board, component);
  const bt = bodyTriangles(body);
  const center = bodyCenter(body);
  let best: Clearance | null = null;
  for (const { part, triangles } of parts) {
    let c: Closest = surfaceDistance(bt, triangles);
    if (c.distance > 0) {
      // Apart surfaces still overlap if one solid holds the other.
      if (insideSurface(center, triangles)) c = { distance: 0, from: center, to: center };
      else {
        for (let i = 0; i < triangles.length; i += 3) {
          const v: Vec3 = [triangles[i], triangles[i + 1], triangles[i + 2]];
          if (insideBody(v, body)) {
            c = { distance: 0, from: v, to: v };
            break;
          }
        }
      }
    }
    if (!best || c.distance < best.distance) best = { ...c, ref: component.ref, part };
  }
  return best;
}

// ---------------------------------------------------------------------------------------
// Section and layout

export type Axis = "x" | "y" | "z";
export const AXES: Axis[] = ["x", "y", "z"];

/**
 * The section plane through a point, normal to `axis`, as `normal · p + constant >= 0` for the
 * side that is kept. The kept side is the one away from the camera of the view that looks at
 * the cut: for x the right view looks at it (x below the cut is kept), for y the front view
 * (y above the cut), for z the top view (z below the cut).
 */
export function sectionPlane(axis: Axis, at: number): { normal: Vec3; constant: number } {
  if (axis === "x") return { normal: [-1, 0, 0], constant: at };
  if (axis === "y") return { normal: [0, 1, 0], constant: -at };
  return { normal: [0, 0, -1], constant: at };
}

/** The named view that looks squarely at a section on `axis`. */
export const SECTION_VIEW: Record<Axis, "right" | "front" | "top"> = { x: "right", y: "front", z: "top" };

export function isClipped(p: Vec3, plane: { normal: Vec3; constant: number }) {
  return dot(plane.normal, p) + plane.constant < -1e-6;
}

/** The assembled, exploded and print layouts of the parts. */
export type Layout = "assembled" | "exploded" | "print";

/**
 * Whether the board is drawn: in every layout but the print bed, which holds only the parts to
 * print. Assembled, the opaque enclosure hides what is inside, and whatever sticks out of the case
 * (a soil probe, a connector) shows as it would in the hand. The other fields are kept so callers
 * pass the whole viewing state and the rule can grow without changing them.
 */
export function boardShown(s: { layout: Layout; xray: boolean; section: boolean; enclosureHidden: boolean }): boolean {
  return s.layout !== "print";
}
export const LAYOUTS: Layout[] = ["assembled", "exploded", "print"];
export const LAYOUT_LABELS: Record<Layout, string> = {
  assembled: "Assembled",
  exploded: "Exploded",
  print: "Print layout",
};

type Bounds = { min: Vec3; max: Vec3 };
/** Where a part goes in a layout: moved by `offset`, and turned upside down (about x) if `flip`. */
export type Placement = { offset: Vec3; flip: boolean };

/** Gap between parts on the print bed, and the least an exploded part moves (mm). */
export const PRINT_GAP = 10;
export const MIN_EXPLODE = 10;

/**
 * Each part's placement in a layout, from the parts' assembled bounding boxes:
 * - assembled: as designed;
 * - exploded: the parts spread apart along Z, in their stacking order, around the board, so
 *   each part moves away from the middle by the stack's height (at least MIN_EXPLODE);
 * - print: every part on the bed (z = 0) with its outside face down, so a part above the middle
 *   of the stack is turned over (about x, through its centre), side by side along x, PRINT_GAP
 *   apart.
 */
export function layoutPlacements(layout: Layout, parts: { part: string; bounds: Bounds }[]): Record<string, Placement> {
  const out: Record<string, Placement> = {};
  if (layout === "assembled" || !parts.length) {
    for (const p of parts) out[p.part] = { offset: [0, 0, 0], flip: false };
    return out;
  }
  const zMin = Math.min(...parts.map((p) => p.bounds.min[2]));
  const zMax = Math.max(...parts.map((p) => p.bounds.max[2]));
  const middle = (zMin + zMax) / 2;
  const mid = (p: { bounds: Bounds }) => (p.bounds.min[2] + p.bounds.max[2]) / 2;
  if (layout === "exploded") {
    const step = Math.max(MIN_EXPLODE, zMax - zMin);
    const below = parts.filter((p) => mid(p) < middle).sort((a, b) => mid(b) - mid(a));
    const above = parts.filter((p) => mid(p) >= middle).sort((a, b) => mid(a) - mid(b));
    below.forEach((p, i) => (out[p.part] = { offset: [0, 0, -step * (i + 1)], flip: false }));
    above.forEach((p, i) => (out[p.part] = { offset: [0, 0, step * (i + 1)], flip: false }));
    return out;
  }
  // Print: in order along x, starting where the assembly starts.
  let x = Math.min(...parts.map((p) => p.bounds.min[0]));
  for (const p of parts) {
    const flip = mid(p) >= middle;
    const { min, max } = p.bounds;
    // Turning over about the x axis through the part's centre keeps its bounding box, so its
    // lowest point is still min z.
    out[p.part] = { offset: [x - min[0], 0, -min[2]], flip };
    x += max[0] - min[0] + PRINT_GAP;
  }
  return out;
}

// ---------------------------------------------------------------------------------------
// Measure

/** What the measure tool picked: an enclosure face, a component's body, a corner or a point. */
export type MeasureTarget =
  | { kind: "face"; part: string; id: string; label: string }
  | { kind: "component"; ref: string; label: string }
  | { kind: "vertex" | "point"; point: Vec3; label: string };

/** A measured thing as geometry: a point, or a surface's triangles. */
export type Measurable = { point: Vec3 } | { triangles: Triangles };

/** The exact shortest distance between two measured things, with the closest points. */
export function measureDistance(a: Measurable, b: Measurable): Closest {
  if ("point" in a && "point" in b) return { distance: Math.sqrt(dist2(a.point, b.point)), from: a.point, to: b.point };
  if ("point" in a) return pointDistance(a.point, (b as { triangles: Triangles }).triangles);
  if ("point" in b) {
    const c = pointDistance(b.point, a.triangles);
    return { distance: c.distance, from: c.to, to: c.from };
  }
  return surfaceDistance(a.triangles, b.triangles);
}

/** Two targets are the same thing (a second click on it starts nothing new). */
export function sameTarget(a: MeasureTarget, b: MeasureTarget): boolean {
  if (a.kind !== b.kind) return false;
  if (a.kind === "face" && b.kind === "face") return a.part === b.part && a.id === b.id;
  if (a.kind === "component" && b.kind === "component") return a.ref === b.ref;
  if ("point" in a && "point" in b) return dist2(a.point, b.point) < 1e-12;
  return false;
}
