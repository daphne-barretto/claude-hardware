import { readFileSync } from "node:fs";
import path from "node:path";

// Faces of the fixture mesh (fixtures/enclosure.gltf) to sketch on in tests, found from
// the mesh itself so no face is named in a test: a flat face has one normal on every vertex. The
// API decides for real (GET .../faces/{id}); this only picks test targets.

type Accessor = { bufferView: number; byteOffset?: number; componentType: number; count: number; type: string };
type Gltf = {
  nodes: { mesh?: number; extras?: { part?: string; faces?: { id: string; feature: string }[] } }[];
  meshes: { primitives: { mode?: number; attributes: Record<string, number>; indices?: number }[] }[];
  accessors: Accessor[];
  bufferViews: { buffer: number; byteOffset?: number; byteStride?: number }[];
  buffers: { uri: string }[];
};

const WIDTH: Record<string, number> = { SCALAR: 1, VEC2: 2, VEC3: 3 };
const READ: Record<number, [number, (v: DataView, at: number) => number]> = {
  5121: [1, (v, at) => v.getUint8(at)],
  5123: [2, (v, at) => v.getUint16(at, true)],
  5125: [4, (v, at) => v.getUint32(at, true)],
  5126: [4, (v, at) => v.getFloat32(at, true)],
};

function reader(doc: Gltf) {
  const buffers = doc.buffers.map((b) => Buffer.from(b.uri.split(",", 2)[1], "base64"));
  return (index: number): number[][] => {
    const acc = doc.accessors[index];
    const view = doc.bufferViews[acc.bufferView];
    const data = buffers[view.buffer];
    const dv = new DataView(data.buffer, data.byteOffset, data.byteLength);
    const [size, read] = READ[acc.componentType];
    const width = WIDTH[acc.type];
    const stride = view.byteStride || size * width;
    const start = (view.byteOffset ?? 0) + (acc.byteOffset ?? 0);
    return Array.from({ length: acc.count }, (_, i) =>
      Array.from({ length: width }, (_, j) => read(dv, start + i * stride + j * size)),
    );
  };
}

export type FixtureFace = {
  id: string;
  part: string;
  feature: string;
  flat: boolean;
  normal: number[];
  /** Area, mm². */
  area: number;
  /** Area over the area of its bounding box in its plane: 1 for a full rectangle. */
  fill: number;
};

/** Every face of the mesh: whether it is flat, its normal, and how much of its bounding box it fills. */
export function fixtureFaces(gltf: Gltf): FixtureFace[] {
  const read = reader(gltf);
  const out: FixtureFace[] = [];
  for (const node of gltf.nodes) {
    const faces = node.extras?.faces;
    if (!faces || node.mesh == null) continue;
    for (const prim of gltf.meshes[node.mesh].primitives) {
      if ((prim.mode ?? 4) !== 4 || prim.attributes._FACE_ID == null) continue;
      const pos = read(prim.attributes.POSITION);
      const nor = read(prim.attributes.NORMAL);
      const ids = read(prim.attributes._FACE_ID).map((v) => v[0]);
      const idx = prim.indices != null ? read(prim.indices).map((v) => v[0]) : pos.map((_, i) => i);
      faces.forEach((f, n) => {
        const verts = ids.flatMap((id, i) => (id === n ? [i] : []));
        const normal = nor[verts[0]];
        const flat = verts.every((i) => nor[i].every((c, k) => Math.abs(c - normal[k]) < 1e-4));
        // Area of the face over the area of its bounding box in the plane (x level, y up on a wall).
        let area = 0;
        for (let t = 0; t < idx.length; t += 3) {
          if (ids[idx[t]] !== n) continue;
          const [a, b, c] = [pos[idx[t]], pos[idx[t + 1]], pos[idx[t + 2]]];
          const u = b.map((v, k) => v - a[k]);
          const w = c.map((v, k) => v - a[k]);
          area += Math.hypot(u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2], u[0] * w[1] - u[1] * w[0]) / 2;
        }
        const x = Math.abs(normal[2]) > 0.9 ? [1, 0, 0] : [-normal[1], normal[0], 0].map((c) => c / Math.hypot(normal[0], normal[1]));
        const y = [normal[1] * x[2] - normal[2] * x[1], normal[2] * x[0] - normal[0] * x[2], normal[0] * x[1] - normal[1] * x[0]];
        const span = (axis: number[]) => {
          const d = verts.map((i) => pos[i].reduce((s, c, k) => s + c * axis[k], 0));
          return Math.max(...d) - Math.min(...d);
        };
        const box = span(x) * span(y);
        out.push({ id: f.id, part: node.extras!.part!, feature: f.feature, flat, normal, area, fill: box ? area / box : 0 });
      });
    }
  }
  return out;
}

export function loadFixtureFaces(): FixtureFace[] {
  const file = path.join(__dirname, "../../fixtures/enclosure.gltf");
  return fixtureFaces(JSON.parse(readFileSync(file, "utf8")) as Gltf);
}

/** The largest flat face that fills its bounding box (so its centre is on it), and a curved face. */
export function sketchTargets(faces: FixtureFace[] = loadFixtureFaces()) {
  const flat = faces.filter((f) => f.flat && f.fill > 0.95).sort((a, b) => b.area - a.area)[0];
  const curved = faces.find((f) => !f.flat);
  if (!flat || !curved) throw new Error("the fixture mesh needs a full flat face and a curved face");
  return { flat, curved };
}
