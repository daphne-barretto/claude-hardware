/**
 * The exploded view: parts pulled apart along the assembly direction (board +Z, the way the
 * lid closes onto the base), kept free of three.js so it can be tested on its own.
 *
 * Parts are put into levels by the height of their centres: parts side by side at the same
 * height (the heat-set inserts) share a level. Fully exploded, each level sits a gap above the
 * top of the level below it, so no two levels overlap along Z. The lowest level never moves.
 * In between, every offset is the full offset times the explode amount, so an amount of 0 is
 * exactly the assembly.
 */

/** A part's extent along the assembly direction, in mm (board Z). */
export type PartSpan = { part: string; min: number; max: number };

/** The fraction of the assembly's height between two levels when fully exploded. */
export const EXPLODE_GAP = 0.35;
/** The smallest gap between levels, in mm. */
export const MIN_GAP_MM = 2;

/** Each part's offset along board +Z (mm) when fully exploded. */
export function explodeOffsets(spans: PartSpan[]): Record<string, number> {
  const out: Record<string, number> = {};
  if (!spans.length) return out;
  const bottom = Math.min(...spans.map((s) => s.min));
  const top = Math.max(...spans.map((s) => s.max));
  const height = top - bottom;
  const gap = Math.max(height * EXPLODE_GAP, MIN_GAP_MM);
  // Centres closer than this are side by side, not stacked.
  const same = Math.max(height * 0.02, 0.05);
  const sorted = [...spans].sort((a, b) => centre(a) - centre(b) || a.part.localeCompare(b.part));
  const levels: PartSpan[][] = [];
  for (const s of sorted) {
    const level = levels[levels.length - 1];
    if (level && centre(s) - centre(level[0]) <= same) level.push(s);
    else levels.push([s]);
  }
  let below: number | null = null; // The top of the level below, exploded.
  let offset = 0;
  for (const level of levels) {
    const min = Math.min(...level.map((s) => s.min));
    const max = Math.max(...level.map((s) => s.max));
    if (below !== null) offset = Math.max(offset, below + gap - min);
    for (const s of level) out[s.part] = offset;
    below = max + offset;
  }
  return out;
}

/** The offset of a part at an explode amount from 0 (assembled) to 1 (fully exploded). */
export function offsetAt(full: number | undefined, amount: number): number {
  const t = Math.min(1, Math.max(0, amount));
  return t === 0 || !full ? 0 : full * t;
}

/**
 * The part a check location is on: its `part`, or else the part that owns its first face.
 * Faces are looked up in each part's face list (mesh contract), so this works for any names.
 */
export function locationPart(
  location: { part?: string | null; faces?: string[] | null },
  index: { part: string; faces: { id: string }[] }[],
): string | null {
  if (location.part) return location.part;
  for (const face of location.faces ?? []) {
    const owner = index.find((p) => p.faces.some((f) => f.id === face));
    if (owner) return owner.part;
  }
  return null;
}

function centre(s: PartSpan) {
  return (s.min + s.max) / 2;
}
