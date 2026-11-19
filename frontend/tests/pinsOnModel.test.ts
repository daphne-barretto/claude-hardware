import { describe, expect, it } from "vitest";
import { MAX_PIN_SHIFT, PIN_GAP, pinAnchor, placePins, type Point3 } from "@/components/canvas/checks";
import { pointDistance } from "@/components/canvas/inspect";
import { hasPin } from "@/components/canvas/pinVisibility";
import { readRepoFile } from "./fixtures";

// Check pins sit on the model where the check applies: the pin's 3D point is on the faces
// the result names (or its part), and on screen a pin moves at most a pin's width off its spot,
// however many pins share it. Read from the fixtures and a sample, never written here.
type Gltf = {
  nodes: { mesh?: number; extras?: { part?: string; faces?: { id: string }[] } }[];
  meshes: { primitives: { mode?: number; attributes: Record<string, number>; indices?: number }[] }[];
  accessors: { bufferView: number; byteOffset?: number; componentType: number; count: number; type: string }[];
  bufferViews: { byteOffset?: number }[];
  buffers: { uri: string }[];
};
type Result = { id: string; status: string; location?: { point: Point3; part?: string | null; faces?: string[] } | null };

function meshFaces(gltf: Gltf) {
  const bytes = Buffer.from(gltf.buffers[0].uri.split(",")[1], "base64");
  const read = (index: number) => {
    const a = gltf.accessors[index];
    const offset = (gltf.bufferViews[a.bufferView].byteOffset ?? 0) + (a.byteOffset ?? 0);
    const n = a.count * ({ SCALAR: 1, VEC2: 2, VEC3: 3, VEC4: 4 } as Record<string, number>)[a.type];
    const data = new DataView(bytes.buffer, bytes.byteOffset + offset);
    return Array.from({ length: n }, (_, i) =>
      a.componentType === 5126 ? data.getFloat32(i * 4, true) : a.componentType === 5123 ? data.getUint16(i * 2, true) : data.getUint32(i * 4, true),
    );
  };
  const faces: Record<string, number[]> = {};
  const parts: Record<string, number[]> = {};
  for (const node of gltf.nodes) {
    if (!node.extras?.part || node.mesh === undefined) continue;
    const prim = gltf.meshes[node.mesh].primitives.find((p) => (p.mode ?? 4) === 4)!;
    const pos = read(prim.attributes.POSITION);
    const ids = read(prim.attributes._FACE_ID);
    const idx = read(prim.indices!);
    for (let i = 0; i < idx.length; i += 3) {
      const corners = [idx[i], idx[i + 1], idx[i + 2]].flatMap((v) => pos.slice(v * 3, v * 3 + 3));
      (parts[node.extras.part] ??= []).push(...corners);
      (faces[node.extras.faces![ids[idx[i]]].id] ??= []).push(...corners);
    }
  }
  return { faces, parts };
}

const index = JSON.parse(readRepoFile("demos/sessions/index.json")) as { samples: string[] };
const BUILDS = [
  { name: "fixture build", mesh: "fixtures/enclosure.gltf", checks: "fixtures/checks.json" },
  ...index.samples.map((sid) => {
    const latest = JSON.parse(readRepoFile(`demos/sessions/${sid}/session.json`)).session.latest_build_id as string;
    const dir = `demos/sessions/${sid}/builds/${latest}`;
    return { name: sid, mesh: `${dir}/mesh.gltf`, checks: `${dir}/checks.json` };
  }),
];

describe("check pins on the model", () => {
  for (const b of BUILDS) {
    it(`puts every pin of ${b.name} on the faces its check names`, () => {
      const { faces, parts } = meshFaces(JSON.parse(readRepoFile(b.mesh)) as Gltf);
      const results = (JSON.parse(readRepoFile(b.checks)) as { results: Result[] }).results.filter(
        (r) => hasPin(r) && r.location,
      );
      expect(results.length).toBeGreaterThan(0);
      for (const r of results) {
        const loc = r.location!;
        const named = (loc.faces ?? []).flatMap((f) => faces[f] ?? []);
        const surface = Float64Array.from(named.length ? named : (parts[loc.part ?? ""] ?? Object.values(parts).flat()));
        const anchor = pinAnchor(loc.point, surface);
        expect(pointDistance(anchor, surface).distance, r.id).toBeLessThan(1e-3);
      }
    });
  }

  it("keeps a crowd of pins on one spot within a pin's width of it, inside the view", () => {
    const view = { width: 400, height: 300 };
    const pins = Array.from({ length: 40 }, (_, i) => ({ id: `p${i}`, at: { x: 390, y: 150 } }));
    const out = placePins(pins, view);
    expect(out.size).toBe(40);
    for (const p of out.values()) {
      expect(Math.hypot(p.x - 390, p.y - 150)).toBeLessThanOrEqual(MAX_PIN_SHIFT + 1);
      expect(p.x).toBeLessThanOrEqual(view.width);
      expect(p.x).toBeGreaterThanOrEqual(0);
    }
    expect(MAX_PIN_SHIFT).toBeLessThanOrEqual(PIN_GAP);
  });

  it("leaves out pins whose spot is outside the view", () => {
    const out = placePins([{ id: "off", at: { x: -20, y: 10 } }, { id: "on", at: { x: 10, y: 10 } }], { width: 100, height: 100 });
    expect([...out.keys()]).toEqual(["on"]);
  });
});
