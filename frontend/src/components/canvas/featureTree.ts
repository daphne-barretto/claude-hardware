import type { FeatureSource, Parameter } from "./api";
import { featureOf, type PartIndex, type Picked } from "./selection";

/**
 * The feature tree, kept free of React and three.js so it can be tested against the
 * fixtures: each part of the model with its material, and under it every feature tagged on
 * its faces (mesh contract `node.extras.faces[].feature`) or created for it in the CAD code
 * (`BuildSource.features`), with the parameters that drive it and the lines that create it.
 */

export type FeatureNode = {
  tag: string;
  part: string;
  /** The feature's faces on the model; empty for a feature that left no faces of its own. */
  faces: string[];
  /** First and last line in the build's code that create it, when the code says. */
  lines: [number, number] | null;
  /** The design parameters that drive it (`Parameter.feature`). */
  params: Parameter[];
  /** Made by hand (a sketch on a face): locked, Claude keeps it. */
  locked: boolean;
};

export type PartNode = { part: string; material: string | null; features: FeatureNode[] };

/** The parameter that names the design's material, for parts whose mesh doesn't name one. */
const MATERIAL_PARAMETER = "material";

/**
 * Builds the tree. Parts follow the mesh order; a part only the code mentions comes after.
 * Features follow the order the code applied them, then any the code doesn't list in the
 * order their first face appears.
 */
export function featureTree(
  index: PartIndex[],
  source: FeatureSource[] | null,
  params: Parameter[],
  materials: Record<string, string | undefined> = {},
): PartNode[] {
  const design = params.find((p) => p.name === MATERIAL_PARAMETER && !p.feature)?.value;
  const parts = new Map<string, PartNode>();
  const partNode = (part: string) => {
    let node = parts.get(part);
    if (!node) {
      node = { part, material: materials[part] ?? (design != null ? String(design) : null), features: [] };
      parts.set(part, node);
    }
    return node;
  };
  for (const p of index) partNode(p.part);

  const features = new Map<string, FeatureNode>();
  const feature = (part: string, tag: string) => {
    const key = `${part}\u0000${tag}`;
    let node = features.get(key);
    if (!node) {
      node = { tag, part, faces: [], lines: null, params: params.filter((p) => p.feature === tag), locked: false };
      features.set(key, node);
      partNode(part).features.push(node);
    }
    return node;
  };
  // The code's order first, so the tree reads like the code that built it.
  for (const f of source ?? []) {
    const node = feature(f.part, f.tag);
    node.lines ??= f.lines;
    node.locked ||= !!f.locked;
  }
  for (const p of index) for (const face of p.faces) feature(p.part, face.feature).faces.push(face.id);
  // Mesh parts were added first, in mesh order.
  return [...parts.values()];
}

/** The picks that select a feature: all of its faces. */
export function featurePicks(node: FeatureNode): Picked[] {
  return node.faces.map((id) => ({ kind: "face", id, part: node.part }));
}

/** The features the picked faces and edges belong to, as `featureKey`s. */
export function pickedFeatures(index: PartIndex[], picks: Picked[]): Set<string> {
  const keys = new Set<string>();
  for (const pick of picks) {
    if (pick.kind === "part") continue;
    const tag = featureOf(index, pick);
    if (tag) keys.add(featureKey(pick.part, tag));
  }
  return keys;
}

export const featureKey = (part: string, tag: string) => `${part}/${tag}`;

/** "L82" or "L82–85". */
export function linesLabel([first, last]: [number, number]): string {
  return first === last ? `L${first}` : `L${first}–${last}`;
}
