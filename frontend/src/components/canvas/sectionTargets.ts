import { AXES, bodyCenter, componentBody, type Axis, type Board, type Triangles, type Vec3 } from "./inspect";

/**
 * What the section cut can go through: each part of the design, and, when the build has a
 * board, each of its components. A design without a board (a plain request) is cut through its
 * own parts; nothing about the section needs a board.
 */
export type SectionTarget = { kind: "part" | "component"; id: string; label: string; center: Vec3 };

/** The centre of a triangle set's bounding box, or null for none. */
export function boundsCenter(t: Triangles): Vec3 | null {
  if (!t.length) return null;
  const min: Vec3 = [Infinity, Infinity, Infinity];
  const max: Vec3 = [-Infinity, -Infinity, -Infinity];
  for (let i = 0; i < t.length; i += 3)
    for (let k = 0; k < 3; k++) {
      min[k] = Math.min(min[k], t[i + k]);
      max[k] = Math.max(max[k], t[i + k]);
    }
  return [0, 1, 2].map((k) => (min[k] + max[k]) / 2) as Vec3;
}

const componentLabel = (c: { ref: string; role: string | null }) => (c.role ? `${c.ref} · ${c.role}` : c.ref);

/** The board's components when there is a board (what a section is usually through), then the design's parts. */
export function sectionTargets(parts: { part: string; triangles: Triangles }[], board: Board | null): SectionTarget[] {
  const own = parts.flatMap(({ part, triangles }): SectionTarget[] => {
    const center = boundsCenter(triangles);
    return center ? [{ kind: "part", id: part, label: part, center }] : [];
  });
  const taken = new Set(own.map((t) => t.id));
  const components = (board?.components ?? [])
    .filter((c) => !taken.has(c.ref))
    .map((c): SectionTarget => ({ kind: "component", id: c.ref, label: componentLabel(c), center: bodyCenter(componentBody(board!, c)) }));
  return [...components, ...own];
}

/** The section through a target's centre, normal to `axis`. */
export function sectionThrough(target: SectionTarget, axis: Axis): { axis: Axis; at: number; ref: string } {
  return { axis, at: target.center[AXES.indexOf(axis)], ref: target.id };
}
