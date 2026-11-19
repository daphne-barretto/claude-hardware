import { sessionPath } from "./api";
import { featureOf, type PartIndex, type Picked } from "./selection";
import { fromMm, MM_PER_INCH, type LengthUnit } from "./units";
// `fetch` here says a dropped connection plainly, and `failure` a failed response (QA fix brief 8).
import { failure, reach as fetch } from "@/lib/errors";

// Modeling tools on the canvas (`EditCreate`, `Edit`, `EditUpdate` and `EditRemoval` in
// contracts/session-api.openapi.json). Each tool's edit is a hand edit: it becomes a named,
// locked feature in the design's code that Claude keeps, the API rebuilds and rechecks, and the
// rebuild arrives as a `build_ready` event without a message ID. Editing, suppressing and
// deleting an edit rebuild the same way. Undo and redo move along the design timeline
// (`./designHistory`), where each edit is a step.

export const EDIT_KINDS = [
  "move",
  "push_pull",
  "hole",
  "boss",
  "rib",
  "fillet",
  "chamfer",
  "shell",
  "mirror",
  "pattern",
] as const;
export type EditKind = (typeof EDIT_KINDS)[number];

/** What a tool is used on: a picked flat face, edge or part, or a feature of the design. */
export type EditTarget = "face" | "edge" | "part" | "feature";

export type EditField =
  | "x"
  | "y"
  | "diameter"
  | "depth"
  | "height"
  | "length"
  | "thickness"
  | "radius"
  | "distance"
  | "dx"
  | "dy"
  | "dz"
  | "rotation"
  | "count"
  | "spacing"
  | "angle";

export type EditValues = Partial<Record<EditField, number | null>> & {
  insert?: boolean | null;
  axis?: Axis | null;
  pattern?: PatternKind | null;
};

export const AXES = ["x", "y", "z"] as const;
export type Axis = (typeof AXES)[number];
export const PATTERN_KINDS = ["linear", "circular"] as const;
export type PatternKind = (typeof PATTERN_KINDS)[number];

export type EditCreate = EditValues & {
  build_id: string;
  kind: EditKind;
  part?: string | null;
  face_id?: string | null;
  edge_id?: string | null;
  source?: string | null;
};

export type Edit = EditCreate & {
  tag: string;
  part: string;
  insert_tag?: string | null;
  pocket_diameter?: number | null;
  pocket_depth?: number | null;
  suppressed: boolean;
  locked: boolean;
};

export type EditUpdate = EditValues & { suppressed?: boolean | null };
export type EditRemoval = { tag: string; removed: string[] };

type Tool = {
  kind: EditKind;
  label: string;
  /** What the tool does, for its tooltip. */
  hint: string;
  target: EditTarget;
  fields: EditField[];
};

/** The tools, in toolbar order. */
export const TOOLS: Tool[] = [
  { kind: "move", label: "Move", hint: "Move and rotate a part", target: "part", fields: ["dx", "dy", "dz", "rotation"] },
  { kind: "push_pull", label: "Push/pull", hint: "Push or pull a flat face", target: "face", fields: ["distance"] },
  { kind: "hole", label: "Hole", hint: "Add a hole on a flat face", target: "face", fields: ["diameter", "depth", "x", "y"] },
  { kind: "boss", label: "Boss", hint: "Add a boss on a flat face", target: "face", fields: ["diameter", "height", "x", "y"] },
  {
    kind: "rib",
    label: "Rib",
    hint: "Add a rib on a flat face",
    target: "face",
    fields: ["length", "thickness", "height", "x", "y", "rotation"],
  },
  { kind: "fillet", label: "Fillet", hint: "Round an edge", target: "edge", fields: ["radius"] },
  { kind: "chamfer", label: "Chamfer", hint: "Bevel an edge", target: "edge", fields: ["distance"] },
  { kind: "shell", label: "Shell", hint: "Hollow a part to a wall thickness", target: "part", fields: ["thickness"] },
  { kind: "mirror", label: "Mirror", hint: "Mirror a feature", target: "feature", fields: [] },
  { kind: "pattern", label: "Pattern", hint: "Repeat a feature in a line or a circle", target: "feature", fields: ["count"] },
];

export const toolOf = (kind: EditKind): Tool => TOOLS.find((t) => t.kind === kind)!;

/** The fields a tool's form shows, given the pattern kind and the insert option. */
export function editFields(kind: EditKind, values: { pattern?: PatternKind | null; insert?: boolean | null } = {}): EditField[] {
  const fields = toolOf(kind).fields;
  if (kind === "pattern") return values.pattern === "circular" ? ["count", "angle"] : ["count", "spacing"];
  // A hole or boss for the insert takes its size from the standard.
  if ((kind === "hole" || kind === "boss") && values.insert)
    return fields.filter((f) => f !== "diameter" && f !== "depth" && f !== "height");
  return fields;
}

const ANGLES: EditField[] = ["rotation", "angle"];
const COUNTS: EditField[] = ["count"];
export const isLength = (field: EditField) => !ANGLES.includes(field) && !COUNTS.includes(field);
/** Fields that may be zero or negative. */
const SIGNED: EditField[] = ["x", "y", "dx", "dy", "dz", "rotation", "distance", "spacing", "angle"];

export function fieldLabel(field: EditField, kind: EditKind): string {
  switch (field) {
    case "x":
      return "Position x";
    case "y":
      return "Position y";
    case "dx":
      return "Move x";
    case "dy":
      return "Move y";
    case "dz":
      return "Move z";
    case "rotation":
      return kind === "move" ? "Rotate about z" : "Angle";
    case "distance":
      return kind === "push_pull" ? "Distance (− pushes in)" : "Size";
    case "thickness":
      return kind === "shell" ? "Wall thickness" : "Thickness";
    case "height":
      return "Height";
    case "angle":
      return "Spans";
    case "count":
      return "Copies in all";
    default:
      return field[0].toUpperCase() + field.slice(1);
  }
}

export type EditDraft = Partial<Record<EditField, string>>;

/** A starting draft for a tool, in the canvas unit, sized from the face it is on (if any). */
export function defaultEditDraft(kind: EditKind, unit: LengthUnit, faceSize?: [number, number] | null): EditDraft {
  const side = faceSize ? Math.min(...faceSize) : 8;
  const mm = (v: number) => String(Number(fromMm(v, unit).toFixed(unit === "in" ? 3 : 2)));
  return {
    x: "0",
    y: "0",
    dx: mm(1),
    dy: "0",
    dz: "0",
    rotation: "0",
    diameter: mm(Math.min(side / 4, 3)),
    depth: mm(1),
    height: mm(2),
    length: mm(side / 3),
    thickness: mm(1),
    radius: mm(0.5),
    distance: mm(kind === "push_pull" ? 1 : 0.5),
    count: "3",
    spacing: mm(5),
    angle: "360",
  };
}

/** An edit's values as a draft, in the canvas unit (to edit it). */
export function draftOf(edit: Edit, unit: LengthUnit): EditDraft {
  const out: EditDraft = {};
  for (const f of editFields(edit.kind, edit)) {
    const v = edit[f];
    if (v == null) continue;
    out[f] = String(Number((isLength(f) ? fromMm(v, unit) : v).toFixed(3)));
  }
  return out;
}

/**
 * The typed form as edit values (lengths typed in inches become millimetres), or the error for
 * each field that is wrong. Only the fields the tool shows are read.
 */
export function parseEdit(
  kind: EditKind,
  draft: EditDraft,
  options: { insert?: boolean; axis?: Axis; pattern?: PatternKind },
  unit: LengthUnit,
): { values: EditValues } | { errors: Partial<Record<EditField, string>> } {
  const errors: Partial<Record<EditField, string>> = {};
  const values: EditValues = {};
  for (const field of editFields(kind, options)) {
    const text = (draft[field] ?? "").trim();
    const n = Number(text);
    if (!text) errors[field] = "Type a value.";
    else if (!Number.isFinite(n)) errors[field] = "Type a number.";
    else if (field === "count" && (!Number.isInteger(n) || n < 2 || n > 50)) errors[field] = "2 to 50 copies.";
    else if (!SIGNED.includes(field) && n <= 0) errors[field] = "More than 0.";
    else if (field === "distance" && n === 0) errors[field] = "Not 0.";
    else values[field] = isLength(field) && unit === "in" ? n * MM_PER_INCH : n;
  }
  if (Object.keys(errors).length) return { errors };
  if (kind === "hole" || kind === "boss") values.insert = !!options.insert;
  if (kind === "mirror") values.axis = options.axis ?? "x";
  if (kind === "pattern") {
    values.pattern = options.pattern ?? "linear";
    if (values.pattern === "linear") values.axis = options.axis ?? "x";
  }
  return { values };
}

/** What a tool is used on, from the picks; or why it can't be used yet. */
export function editTarget(
  kind: EditKind,
  picks: Picked[],
  index: PartIndex[],
): { target: Pick<EditCreate, "part" | "face_id" | "edge_id" | "source">; label: string } | { reason: string } {
  const want = toolOf(kind).target;
  if (want === "feature" && picks.length) {
    // A feature picked whole (all its faces) or by one face or edge.
    const tags = [...new Set(picks.map((p) => (p.kind === "part" ? undefined : featureOf(index, p))))];
    if (tags.length === 1 && tags[0]) return { target: { source: tags[0] }, label: tags[0] };
    return { reason: tags.length > 1 ? "Pick one feature" : "Pick a face of the feature to copy" };
  }
  if (picks.length !== 1) {
    const what = { face: "a flat face", edge: "an edge", part: "a part", feature: "a feature" }[want];
    return { reason: picks.length > 1 ? "Pick only one" : `Pick ${what} on the model` };
  }
  const pick = picks[0];
  if (want === "part") return { target: { part: pick.part }, label: pick.part };
  if (pick.kind !== want) return { reason: want === "face" ? "Pick a flat face (face filter)" : "Pick an edge (edge filter)" };
  return want === "face"
    ? { target: { face_id: pick.id }, label: `${pick.id} · ${pick.part}` }
    : { target: { edge_id: pick.id }, label: `${pick.id} · ${pick.part}` };
}

/** The hand edits a set of picks is on (a face or edge of the feature), by tag. */
export function pickedEdits(edits: Edit[], picks: Picked[], index: PartIndex[]): Edit[] {
  const tags = new Set(picks.map((p) => featureOf(index, p)).filter(Boolean));
  return edits.filter((e) => tags.has(e.tag) || (e.insert_tag != null && tags.has(e.insert_tag)));
}

/** A one-line description of an edit's values in the canvas unit. */
export function editSummary(edit: Edit, unit: LengthUnit): string {
  const len = (v?: number | null) => (v == null ? "" : `${Number(fromMm(v, unit).toFixed(unit === "in" ? 3 : 2))} ${unit}`);
  switch (edit.kind) {
    case "move":
      return `by ${len(edit.dx)}, ${len(edit.dy)}, ${len(edit.dz)}, turned ${edit.rotation ?? 0}°`;
    case "push_pull":
      return `${(edit.distance ?? 0) > 0 ? "out" : "in"} ${len(Math.abs(edit.distance ?? 0))}`;
    case "hole":
      return `⌀${len(edit.diameter)}, ${len(edit.depth)} deep${edit.insert ? ", for the insert" : ""}`;
    case "boss":
      return `⌀${len(edit.diameter)}, ${len(edit.height)} tall${edit.insert ? ", insert pocket" : ""}`;
    case "rib":
      return `${len(edit.length)} × ${len(edit.thickness)}, ${len(edit.height)} tall`;
    case "fillet":
      return `radius ${len(edit.radius)}`;
    case "chamfer":
      return `${len(edit.distance)}`;
    case "shell":
      return `walls ${len(edit.thickness)}`;
    case "mirror":
      return `${edit.source} across ${edit.axis}`;
    case "pattern":
      return edit.pattern === "circular"
        ? `${edit.count} × ${edit.source} over ${edit.angle}°`
        : `${edit.count} × ${edit.source} every ${len(edit.spacing)} along ${edit.axis}`;
  }
}

/**
 * The fix request for a hand edit whose rebuild fails checks: Claude proposes a fix that keeps
 * the edit (it is locked), without undoing it.
 */
export function fixText(name: string, failing: { check: string; title?: string | null }[]): string {
  // The checks by their titles, as the checks strip shows them (QA fix brief 2).
  const checks = [...new Set(failing.map((r) => r.title || r.check))];
  return `My edit (${name}) breaks ${checks.length === 1 ? "a check" : "checks"}: ${checks.join(", ")}. Keep my edit as it is (it is locked) and propose a fix for the rest of the design.`;
}


async function json<T>(res: Response): Promise<T> {
  if (!res.ok) throw await failure(res);
  return (await res.json()) as T;
}

const send = (method: string, body?: unknown): RequestInit => ({
  method,
  headers: body === undefined ? undefined : { "Content-Type": "application/json" },
  body: body === undefined ? undefined : JSON.stringify(body),
});

export function listEdits(sessionId: string): Promise<Edit[]> {
  return fetch(`${sessionPath(sessionId)}/edits`).then((r) => json<Edit[]>(r));
}

export function createEdit(sessionId: string, body: EditCreate): Promise<Edit> {
  return fetch(`${sessionPath(sessionId)}/edits`, send("POST", body)).then((r) => json<Edit>(r));
}

export function updateEdit(sessionId: string, tag: string, body: EditUpdate): Promise<Edit> {
  return fetch(`${sessionPath(sessionId)}/edits/${encodeURIComponent(tag)}`, send("PATCH", body)).then((r) => json<Edit>(r));
}

export function deleteEdit(sessionId: string, tag: string): Promise<EditRemoval> {
  return fetch(`${sessionPath(sessionId)}/edits/${encodeURIComponent(tag)}`, send("DELETE")).then((r) => json<EditRemoval>(r));
}
