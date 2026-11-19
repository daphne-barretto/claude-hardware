import { describe, expect, it } from "vitest";
import {
  bodyCenter,
  bodyTriangles,
  componentBody,
  componentClearance,
  insideSurface,
  isClipped,
  layoutPlacements,
  sectionPlane,
  surfaceDistance,
  triangleDistance,
  type Board,
  type Triangles,
  type Vec3,
} from "@/components/canvas/inspect";
import { readRepoFile } from "./fixtures";

// The scenario comes from the fixtures and demos/b-parasite/ (the build's board revision), never from here.
type Gltf = {
  nodes: { mesh?: number; extras?: { part?: string; faces?: { id: string }[] } }[];
  meshes: { primitives: { mode?: number; attributes: Record<string, number>; indices?: number }[] }[];
  accessors: { bufferView: number; byteOffset?: number; componentType: number; count: number; type: string }[];
  bufferViews: { buffer: number; byteOffset?: number }[];
  buffers: { uri: string }[];
};
const gltf = JSON.parse(readRepoFile("fixtures/enclosure.gltf")) as Gltf;
const build = JSON.parse(readRepoFile("fixtures/build.json")) as { board: { name: string; revision: string } };
const board = JSON.parse(readRepoFile(`demos/b-parasite/${build.board.name}-${build.board.revision}.json`)) as Board;
type CheckResult = {
  check: string;
  measured?: { value: number | string; unit?: string } | null;
  location?: { point: Vec3; part?: string | null; faces?: string[] } | null;
};
const checks = JSON.parse(readRepoFile("fixtures/checks.json")) as { results: CheckResult[] };
const clearanceCheck = checks.results.find((r) => r.check === "component_clearance")!;

const bytes = Buffer.from(gltf.buffers[0].uri.split(",")[1], "base64");
function read(index: number): number[] {
  const a = gltf.accessors[index];
  const view = gltf.bufferViews[a.bufferView];
  const offset = (view.byteOffset ?? 0) + (a.byteOffset ?? 0);
  const width = { SCALAR: 1, VEC2: 2, VEC3: 3, VEC4: 4 }[a.type]!;
  const n = a.count * width;
  const data = new DataView(bytes.buffer, bytes.byteOffset + offset);
  const out: number[] = [];
  for (let i = 0; i < n; i++) {
    if (a.componentType === 5126) out.push(data.getFloat32(i * 4, true));
    else if (a.componentType === 5123) out.push(data.getUint16(i * 2, true));
    else out.push(data.getUint32(i * 4, true));
  }
  return out;
}

/** Each part's triangles in board coordinates, and its faces' triangles by face ID. */
const parts = gltf.nodes.flatMap((node) => {
  if (!node.extras?.part || node.mesh === undefined) return [];
  const prim = gltf.meshes[node.mesh].primitives.find((p) => (p.mode ?? 4) === 4)!;
  const pos = read(prim.attributes.POSITION);
  const ids = read(prim.attributes._FACE_ID);
  const idx = prim.indices !== undefined ? read(prim.indices) : pos.map((_, i) => i).slice(0, pos.length / 3);
  const all: number[] = [];
  const faces: Record<string, number[]> = {};
  for (let i = 0; i < idx.length; i += 3) {
    const corners = [idx[i], idx[i + 1], idx[i + 2]].flatMap((v) => pos.slice(v * 3, v * 3 + 3));
    all.push(...corners);
    const face = node.extras.faces![ids[idx[i]]].id;
    (faces[face] ??= []).push(...corners);
  }
  return [
    {
      part: node.extras.part,
      triangles: Float64Array.from(all),
      faces: Object.fromEntries(Object.entries(faces).map(([k, v]) => [k, Float64Array.from(v)])),
    },
  ];
});

describe("triangle distances", () => {
  it("is exact for parallel and crossing triangles", () => {
    const a: [Vec3, Vec3, Vec3] = [
      [0, 0, 0],
      [1, 0, 0],
      [0, 1, 0],
    ];
    const lifted = a.map(([x, y, z]) => [x + 0.2, y + 0.2, z + 0.75]) as [Vec3, Vec3, Vec3];
    expect(triangleDistance(a, lifted).distance).toBeCloseTo(0.75, 9);
    const crossing: [Vec3, Vec3, Vec3] = [
      [0.2, 0.2, -1],
      [0.2, 0.2, 1],
      [0.3, 0.1, 0],
    ];
    expect(triangleDistance(a, crossing).distance).toBe(0);
    // Edge to edge, skewed.
    const skew: [Vec3, Vec3, Vec3] = [
      [2, -1, 1],
      [2, 1, 1],
      [3, 0, 1],
    ];
    expect(triangleDistance(a, skew).distance).toBeCloseTo(Math.hypot(1, 1), 9);
  });

  it("knows inside from outside of a closed surface", () => {
    const body = componentBody(board, board.components[0]);
    const box = bodyTriangles(body);
    expect(insideSurface(bodyCenter(body), box)).toBe(true);
    expect(insideSurface([1e3, 1e3, 1e3], box)).toBe(false);
  });
});

describe("component clearance", () => {
  // The check reports the face and the point on it where the distance occurs; the component
  // it measures from is the one closest to that face.
  const face = clearanceCheck.location!.faces![0];
  const facePart = parts.find((p) => p.faces[face])!;
  const [component, toFace] = board.components
    .map((c) => [c, surfaceDistance(bodyTriangles(componentBody(board, c)), facePart.faces[face])] as const)
    .reduce((a, b) => (b[1].distance < a[1].distance ? b : a));

  it("measures the check's value from its component to its face within 0.05 mm", () => {
    expect(component).toBeDefined();
    expect(typeof clearanceCheck.measured?.value).toBe("number");
    expect(Math.abs(toFace.distance - Number(clearanceCheck.measured!.value))).toBeLessThan(0.05);
  });

  it("gives every component its distance to the closest part, never more than to any one face", () => {
    for (const c of board.components) {
      const clearance = componentClearance(board, c, parts)!;
      expect(Number.isFinite(clearance.distance)).toBe(true);
      expect(clearance.distance).toBeGreaterThanOrEqual(0);
      const body = bodyTriangles(componentBody(board, c));
      const nearest = Math.min(...parts.map((p) => surfaceDistance(body, p.triangles).distance));
      expect(clearance.distance).toBeCloseTo(nearest, 9);
    }
    const own = componentClearance(board, component, parts)!;
    expect(own.distance).toBeLessThanOrEqual(toFace.distance + 1e-9);
  });

  it("is zero for a component that overlaps the enclosure", () => {
    const c = board.components[0];
    const inside = parts[0].triangles;
    const sunk = { ...c, height: 1e3 };
    expect(componentClearance(board, sunk, [{ part: "p", triangles: inside }])!.distance).toBe(0);
  });
});

describe("section plane", () => {
  it("keeps the side away from the view that looks at the cut", () => {
    const keep: Record<string, Vec3> = { x: [-1, 0, 0], y: [0, 1, 0], z: [0, 0, -1] };
    for (const axis of ["x", "y", "z"] as const) {
      const plane = sectionPlane(axis, 5);
      const k = keep[axis].map((v) => 5 + v) as Vec3;
      const gone = keep[axis].map((v) => 5 - v) as Vec3;
      expect(isClipped(k.map((v, i) => (keep[axis][i] === 0 ? 0 : v)) as Vec3, plane)).toBe(false);
      expect(isClipped(gone.map((v, i) => (keep[axis][i] === 0 ? 0 : v)) as Vec3, plane)).toBe(true);
    }
  });
});

describe("layouts", () => {
  const bounds = (t: Triangles) => {
    const min: Vec3 = [Infinity, Infinity, Infinity];
    const max: Vec3 = [-Infinity, -Infinity, -Infinity];
    for (let i = 0; i < t.length; i += 3)
      for (let k = 0; k < 3; k++) {
        min[k] = Math.min(min[k], t[i + k]);
        max[k] = Math.max(max[k], t[i + k]);
      }
    return { min, max };
  };
  const placed = parts.map((p) => ({ part: p.part, bounds: bounds(p.triangles) }));

  it("leaves the assembly as designed", () => {
    for (const p of Object.values(layoutPlacements("assembled", placed))) expect(p).toEqual({ offset: [0, 0, 0], flip: false });
  });

  it("explodes the parts apart along Z, keeping their order", () => {
    const at = layoutPlacements("exploded", placed);
    const zs = placed.map((p) => ({ mid: (p.bounds.min[2] + p.bounds.max[2]) / 2, moved: at[p.part].offset[2] }));
    for (const z of zs) expect(z.moved).not.toBe(0);
    const order = [...zs].sort((a, b) => a.mid - b.mid);
    for (let i = 1; i < order.length; i++) expect(order[i].mid + order[i].moved).toBeGreaterThan(order[i - 1].mid + order[i - 1].moved);
  });

  it("lays the parts on the bed side by side without overlapping, the upper parts turned over", () => {
    const at = layoutPlacements("print", placed);
    const spans = placed.map((p) => {
      const { offset, flip } = at[p.part];
      expect(p.bounds.min[2] + offset[2]).toBeCloseTo(0, 9);
      return { x0: p.bounds.min[0] + offset[0], x1: p.bounds.max[0] + offset[0], flip };
    });
    for (let i = 1; i < spans.length; i++) expect(spans[i].x0).toBeGreaterThan(spans[i - 1].x1);
    expect(spans.some((s) => s.flip)).toBe(true);
    expect(spans.some((s) => !s.flip)).toBe(true);
  });
});
