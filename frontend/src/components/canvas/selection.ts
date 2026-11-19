import type { Selection } from "@/lib/session-api";

/**
 * Selection on the 3D canvas, kept free of three.js so it can be tested against the mesh
 * fixture. The mesh contract (contracts/README.md, "Mesh") gives every part node
 * `extras = {part, faces: [{id, feature}], edges: [{id, feature, faces}]}`.
 */

export type MeshFace = { id: string; feature: string };
export type MeshEdge = { id: string; feature: string; faces: string[] };
export type PartIndex = { part: string; faces: MeshFace[]; edges: MeshEdge[] };

/** What a click picks: the selection filter. */
export type PickKind = "face" | "edge" | "part" | "component";
export const PICK_KINDS: PickKind[] = ["face", "edge", "part", "component"];

/**
 * One picked item: a face or edge by its ID, a whole part by its name, or a board component
 * by its reference designator (`part` is the part its opening was picked on).
 */
export type Picked = { kind: PickKind; id: string; part: string };

/** The canvas selection: what the chat sends (`selection`), and the chip text (`label`). */
export type CanvasSelection = { selection: Selection; label: string };

type Point = [number, number, number];

/** The part index of a glTF JSON document that follows the mesh contract. */
export function indexFromGltf(gltf: { nodes?: { extras?: Partial<PartIndex> }[] }): PartIndex[] {
  return (gltf.nodes ?? []).flatMap((n) => {
    const e = n.extras;
    return e?.part ? [{ part: e.part, faces: e.faces ?? [], edges: e.edges ?? [] }] : [];
  });
}

/**
 * The board component a feature was made for, from the feature tag convention
 * (`geometry.params.opening_tags`): `opening_<ref>`, or `opening_<ref>_<kind>` when a component
 * has more than one opening. Other features (the part bodies, bosses) belong to no component.
 */
export function componentOf(feature: string): string | undefined {
  const m = /^opening_([^_]+)/.exec(feature);
  return m?.[1];
}

/** The faces of the openings made for a component, on every part. */
export function componentFaces(index: PartIndex[], ref: string): Picked[] {
  const r = ref.toLowerCase();
  return index.flatMap((p) =>
    p.faces.filter((f) => componentOf(f.feature)?.toLowerCase() === r).map((f): Picked => ({ kind: "face", id: f.id, part: p.part })),
  );
}

/** A face pick turned into what the filter selects: the face, its part, or its component. */
export function pickAs(index: PartIndex[], face: Picked, kind: PickKind): Picked | null {
  if (kind === "face" || kind === "edge") return face;
  if (kind === "part") return { kind: "part", id: face.part, part: face.part };
  const feature = featureOf(index, face);
  const ref = feature ? componentOf(feature) : undefined;
  return ref ? { kind: "component", id: ref, part: face.part } : null;
}

/** The faces and edges to highlight: component picks become the faces of their openings. */
export function highlightPicks(index: PartIndex[], picks: Picked[]): Picked[] {
  return picks.flatMap((p) => (p.kind === "component" ? componentFaces(index, p.id) : [p]));
}

/** The feature that created a picked face or edge; a part's own name for a part; a component's reference. */
export function featureOf(index: PartIndex[], pick: Picked): string | undefined {
  if (pick.kind === "component") return pick.id;
  const p = index.find((x) => x.part === pick.part);
  if (!p) return undefined;
  if (pick.kind === "part") return p.part;
  const items: { id: string; feature: string }[] = pick.kind === "face" ? p.faces : p.edges;
  return items.find((x) => x.id === pick.id)?.feature;
}

const same = (a: Picked, b: Picked) => a.kind === b.kind && a.id === b.id;

/** Adds a pick, or removes it if it is already picked (shift- or Cmd-click). */
export function togglePick(picks: Picked[], pick: Picked): Picked[] {
  const has = picks.some((p) => same(p, pick));
  return has ? picks.filter((p) => !same(p, pick)) : [...picks, pick];
}

/** Adds picks that are not picked yet (a box drawn with Shift or Cmd held). */
export function addPicks(picks: Picked[], more: Picked[]): Picked[] {
  const out = [...picks];
  for (const p of more) if (!out.some((q) => same(q, p))) out.push(p);
  return out;
}

/** Normalised screen rectangle from two corners: [left, top, right, bottom]. */
export function boxFrom(a: { x: number; y: number }, b: { x: number; y: number }): [number, number, number, number] {
  return [Math.min(a.x, b.x), Math.min(a.y, b.y), Math.max(a.x, b.x), Math.max(a.y, b.y)];
}

/**
 * Resolves an ID typed by the user to picks: a face ID, an edge ID, a part name, a feature
 * name (all its faces), or a board component's reference designator (the faces of the
 * features named after it, such as `opening_<ref>`). Matching ignores case.
 */
export function resolveId(index: PartIndex[], query: string): Picked[] {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  const eq = (s: string) => s.toLowerCase() === q;
  for (const p of index) {
    const face = p.faces.find((f) => eq(f.id));
    if (face) return [{ kind: "face", id: face.id, part: p.part }];
    const edge = p.edges.find((e) => eq(e.id));
    if (edge) return [{ kind: "edge", id: edge.id, part: p.part }];
  }
  const part = index.find((p) => eq(p.part));
  if (part) return [{ kind: "part", id: part.part, part: part.part }];
  const byFeature = (match: (feature: string) => boolean) =>
    index.flatMap((p) => p.faces.filter((f) => match(f.feature)).map((f): Picked => ({ kind: "face", id: f.id, part: p.part })));
  const feature = byFeature(eq);
  if (feature.length) return feature;
  // A component reference: features are named `<kind>_<ref>` after the board data.
  return byFeature((f) => f.toLowerCase().endsWith(`_${q}`) || componentOf(f)?.toLowerCase() === q);
}

/** The chip text: the feature name when every pick shares one, otherwise counts. */
export function labelFor(index: PartIndex[], picks: Picked[]): string {
  if (!picks.length) return "";
  const parts = [...new Set(picks.map((p) => p.part))];
  if (picks.every((p) => p.kind === "part")) return parts.join(", ");
  if (picks.length === 1) return featureOf(index, picks[0]) ?? picks[0].id;
  const count = (k: PickKind) => picks.filter((p) => p.kind === k).length;
  const counts = [
    plural(count("part"), "part"),
    plural(count("component"), "component"),
    plural(count("face"), "face"),
    plural(count("edge"), "edge"),
  ]
    .filter(Boolean)
    .join(", ");
  const geometric = picks.every((p) => p.kind === "face" || p.kind === "edge");
  const features = [...new Set(picks.map((p) => featureOf(index, p) ?? p.id))];
  if (geometric && features.length === 1) return `${features[0]} · ${counts}`;
  return geometric && parts.length === 1 ? `${counts} on ${parts[0]}` : counts;
}

/** What the chat sends for a set of picks; `point` is where the last click hit, in board mm. */
export function toCanvasSelection(index: PartIndex[], picks: Picked[], point: Point | null = null): CanvasSelection | null {
  if (!picks.length) return null;
  const parts = [...new Set(picks.map((p) => p.part))];
  const ids = (k: PickKind) => picks.filter((p) => p.kind === k).map((p) => p.id);
  return {
    selection: {
      part: parts.length === 1 ? parts[0] : null,
      parts: ids("part"),
      faces: ids("face"),
      edges: ids("edge"),
      components: ids("component"),
      point,
    },
    label: labelFor(index, picks),
  };
}

function plural(n: number, noun: string) {
  return n ? `${n} ${noun}${n > 1 ? "s" : ""}` : "";
}

/** How a part is drawn: hidden, or see-through. Isolating a part hides all the others. */
export type PartView = { hidden: boolean; transparent: boolean };
export type PartViews = Record<string, PartView>;

export const DEFAULT_PART_VIEW: PartView = { hidden: false, transparent: false };

export const partView = (views: PartViews, part: string): PartView => views[part] ?? DEFAULT_PART_VIEW;

/** The parts a selection touches: picked parts, and the parts of picked faces and edges. */
export function pickedParts(picks: Picked[]): string[] {
  return [...new Set(picks.map((p) => p.part))];
}

/** Hides `hide` and leaves every other part as it is. */
export function hideParts(views: PartViews, hide: string[]): PartViews {
  return { ...views, ...Object.fromEntries(hide.map((p) => [p, { ...partView(views, p), hidden: true }])) };
}

/** Shows every part. */
export function showAll(views: PartViews, parts: string[]): PartViews {
  return Object.fromEntries(parts.map((p) => [p, { ...partView(views, p), hidden: false }]));
}

/** Shows only `keep`, or, if exactly those parts are already the ones shown, shows every part again. */
export function isolateParts(views: PartViews, parts: string[], keep: string[]): PartViews {
  if (!keep.length) return views;
  const alone = parts.every((p) => partView(views, p).hidden === !keep.includes(p));
  return alone ? showAll(views, parts) : Object.fromEntries(
    parts.map((p) => [p, { ...partView(views, p), hidden: !keep.includes(p) }]),
  );
}

/** Shows only `part`, or, if it is already the only part shown, shows every part again. */
export function isolate(views: PartViews, parts: string[], part: string): PartViews {
  return isolateParts(views, parts, [part]);
}

export function isIsolated(views: PartViews, parts: string[], part: string): boolean {
  return parts.length > 1 && parts.every((p) => partView(views, p).hidden === (p !== part));
}
