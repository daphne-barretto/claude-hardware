import { describe, expect, it } from "vitest";
import { explodeOffsets, locationPart, MIN_GAP_MM, offsetAt, type PartSpan } from "@/components/canvas/explode";
import { indexFromGltf } from "@/components/canvas/selection";
import type { CheckReport } from "@/components/canvas/api";
import { readRepoFile } from "./fixtures";

// The exploded view. The assembly comes from the mesh fixture (generated from
// demos/b-parasite/ and standards.yaml), never from here.
type Gltf = {
  nodes: { mesh?: number; extras?: { part?: string } }[];
  meshes: { primitives: { attributes: { POSITION: number } }[] }[];
  accessors: { min?: number[]; max?: number[] }[];
};
const gltf = JSON.parse(readRepoFile("fixtures/enclosure.gltf")) as Gltf;
const report = JSON.parse(readRepoFile("fixtures/checks.json")) as CheckReport;

/** Each part's extent along board Z, from its vertex bounds (vertices are in board coordinates). */
const spans: PartSpan[] = gltf.nodes.flatMap((n) => {
  if (!n.extras?.part || n.mesh === undefined) return [];
  const prims = gltf.meshes[n.mesh].primitives.map((p) => gltf.accessors[p.attributes.POSITION]);
  return [{ part: n.extras.part, min: Math.min(...prims.map((a) => a.min![2])), max: Math.max(...prims.map((a) => a.max![2])) }];
});

/** True when the two spans, moved by their offsets, overlap along Z. */
const overlap = (a: PartSpan, da: number, b: PartSpan, db: number) => a.min + da < b.max + db && b.min + db < a.max + da;

describe("exploded view", () => {
  it("separates every part of the fixture assembly along the assembly direction", () => {
    expect(spans.length).toBeGreaterThan(1);
    const full = explodeOffsets(spans);
    expect(Object.keys(full).sort()).toEqual(spans.map((s) => s.part).sort());
    // The lowest part stays put; the others move up.
    const lowest = [...spans].sort((a, b) => a.min + a.max - (b.min + b.max))[0];
    expect(full[lowest.part]).toBe(0);
    for (const a of spans)
      for (const b of spans) {
        if (a === b) continue;
        expect(overlap(a, full[a.part], b, full[b.part]), `${a.part} and ${b.part} apart`).toBe(false);
        const gap = Math.max(b.min + full[b.part] - (a.max + full[a.part]), a.min + full[a.part] - (b.max + full[b.part]));
        expect(gap).toBeGreaterThanOrEqual(MIN_GAP_MM - 1e-9);
      }
  });

  it("keeps parts side by side at the same height on one level, and stacks the rest in order", () => {
    // A tray, four inserts at one height above it, and a cap that overlaps the inserts.
    const parts: PartSpan[] = [
      { part: "tray", min: 0, max: 10 },
      ...[1, 2, 3, 4].map((n) => ({ part: `insert_${n}`, min: 6, max: 12 })),
      { part: "cap", min: 5, max: 17 },
    ];
    const full = explodeOffsets(parts);
    expect(full.tray).toBe(0);
    expect(new Set([1, 2, 3, 4].map((n) => full[`insert_${n}`])).size).toBe(1);
    expect(full.insert_1).toBeGreaterThan(0);
    expect(full.cap).toBeGreaterThan(full.insert_1);
    for (const a of parts)
      for (const b of parts)
        if (a !== b && !(a.part.startsWith("insert") && b.part.startsWith("insert")))
          expect(overlap(a, full[a.part], b, full[b.part]), `${a.part} and ${b.part} apart`).toBe(false);
  });

  it("goes from assembled to fully exploded with the amount, and back to exactly zero", () => {
    const full = explodeOffsets(spans);
    for (const s of spans) {
      expect(offsetAt(full[s.part], 0)).toBe(0);
      expect(offsetAt(full[s.part], 1)).toBe(full[s.part]);
      expect(offsetAt(full[s.part], 0.5)).toBeCloseTo(full[s.part] / 2, 9);
      // Out of range amounts are clamped.
      expect(offsetAt(full[s.part], 2)).toBe(full[s.part]);
      expect(offsetAt(full[s.part], -1)).toBe(0);
    }
    expect(offsetAt(undefined, 1)).toBe(0);
    expect(explodeOffsets([])).toEqual({});
    expect(explodeOffsets([spans[0]])).toEqual({ [spans[0].part]: 0 });
  });

  it("puts every located check result on a part of the mesh, so its pin moves with that part", () => {
    const index = indexFromGltf(gltf as never);
    const parts = index.map((p) => p.part);
    for (const r of report.results) {
      if (!r.location) continue;
      const part = locationPart(r.location, index);
      expect(parts, r.id).toContain(part);
      if (r.location.part) expect(part).toBe(r.location.part);
    }
    expect(locationPart({ part: null, faces: ["no-such-face"] }, index)).toBeNull();
  });
});
