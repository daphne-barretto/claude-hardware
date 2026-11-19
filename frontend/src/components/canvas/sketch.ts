import { sessionPath } from "./api";
import type { Picked } from "./selection";
import { fromMm, MM_PER_INCH, type LengthUnit } from "./units";
// `fetch` here says a dropped connection plainly, and `failure` a failed response (QA fix brief 8).
import { failure, reach as fetch } from "@/lib/errors";

// Sketch on a face (`FacePlane`, `SketchCreate` and `Sketch` in
// contracts/session-api.openapi.json). Pick a flat face, place a circle, slot, rectangle or vent
// on it with typed size and position, and cut or extrude it. The sketch becomes a named, locked
// feature in the code that Claude keeps; the API rebuilds and rechecks, and the rebuild arrives
// as a `build_ready` event without a message ID.

export const SKETCH_SHAPES = ["circle", "slot", "rectangle", "vent"] as const;
export type SketchShape = (typeof SKETCH_SHAPES)[number];
export const SKETCH_OPERATIONS = ["cut", "extrude"] as const;
export type SketchOperation = (typeof SKETCH_OPERATIONS)[number];

export const SHAPE_LABELS: Record<SketchShape, string> = {
  circle: "Circle",
  slot: "Slot",
  rectangle: "Rectangle",
  vent: "Vent",
};
export const OPERATION_LABELS: Record<SketchOperation, string> = { cut: "Cut", extrude: "Extrude" };

type Point3 = [number, number, number];

export type FacePlane = {
  build_id: string;
  face_id: string;
  part: string;
  feature: string;
  planar: boolean;
  origin?: Point3 | null;
  normal?: Point3 | null;
  x_dir?: Point3 | null;
  /** The face's extent along the sketch's x and y axes, mm. */
  size?: [number, number] | null;
};

export type SketchCreate = {
  build_id: string;
  face_id: string;
  shape: SketchShape;
  operation: SketchOperation;
  width: number;
  height?: number | null;
  count?: number | null;
  x: number;
  y: number;
  depth: number;
};

export type Sketch = SketchCreate & { tag: string; part: string; plane: FacePlane; locked: boolean };

/** The fields the form shows for a shape, in order. Lengths are typed in the canvas unit. */
export type SketchField = "width" | "height" | "count" | "x" | "y" | "depth";
export type SketchDraft = Record<SketchField, string>;

export function sketchFields(shape: SketchShape): SketchField[] {
  if (shape === "circle") return ["width", "x", "y", "depth"];
  if (shape === "vent") return ["width", "height", "count", "x", "y", "depth"];
  return ["width", "height", "x", "y", "depth"];
}

/** The label of a field for a shape: a circle's width is its diameter, a slot's its length. */
export function fieldLabel(field: SketchField, shape: SketchShape, operation: SketchOperation): string {
  switch (field) {
    case "width":
      return shape === "circle" ? "Diameter" : shape === "slot" ? "Length" : "Width";
    case "height":
      return shape === "slot" ? "Slot width" : "Height";
    case "count":
      return "Slots";
    case "x":
      return "Position x";
    case "y":
      return "Position y";
    case "depth":
      return operation === "cut" ? "Depth" : "Height out";
  }
}

const LENGTHS: SketchField[] = ["width", "height", "x", "y", "depth"];
export const isLength = (field: SketchField) => LENGTHS.includes(field);

/** A starting draft for a face, in the canvas unit: a shape a quarter of the face's smaller side, centred. */
export function defaultDraft(plane: FacePlane | null, unit: LengthUnit): SketchDraft {
  const side = plane?.size ? Math.min(...plane.size) : 8;
  const mm = (v: number) => String(Number(fromMm(v, unit).toFixed(unit === "in" ? 3 : 2)));
  return { width: mm(side / 4), height: mm(side / 8), count: "3", x: "0", y: "0", depth: mm(1) };
}

/**
 * The typed form as a `SketchCreate` (lengths typed in inches become millimetres), or the
 * error for each field that is wrong. Only the fields the shape uses are read.
 */
export function parseSketch(
  draft: SketchDraft,
  shape: SketchShape,
  operation: SketchOperation,
  face: { build_id: string; face_id: string },
  unit: LengthUnit,
): { body: SketchCreate } | { errors: Partial<Record<SketchField, string>> } {
  const errors: Partial<Record<SketchField, string>> = {};
  const values: Partial<Record<SketchField, number>> = {};
  for (const field of sketchFields(shape)) {
    const text = draft[field].trim();
    const n = Number(text);
    if (!text) errors[field] = "Type a value.";
    else if (!Number.isFinite(n)) errors[field] = "Type a number.";
    else if (field === "count" && (!Number.isInteger(n) || n < 2 || n > 50)) errors[field] = "2 to 50 slots.";
    else if (field !== "x" && field !== "y" && field !== "count" && n <= 0) errors[field] = "More than 0.";
    else values[field] = isLength(field) && unit === "in" ? n * MM_PER_INCH : n;
  }
  if (shape === "slot" && values.width != null && values.height != null && values.height >= values.width)
    errors.height = "Less than the length.";
  if (Object.keys(errors).length) return { errors };
  return {
    body: {
      ...face,
      shape,
      operation,
      width: values.width!,
      height: shape === "circle" ? null : values.height!,
      count: shape === "vent" ? values.count! : null,
      x: values.x!,
      y: values.y!,
      depth: values.depth!,
    },
  };
}

/** The one picked face to sketch on, or why there isn't one. */
export function sketchFace(picks: Picked[]): { face: Picked } | { reason: string } {
  const faces = picks.filter((p) => p.kind === "face");
  if (faces.length === 1 && picks.length === 1) return { face: faces[0] };
  return { reason: picks.length > 1 ? "Pick only one face" : "Pick a flat face on the model" };
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) throw await failure(res);
  return (await res.json()) as T;
}

export function getFace(sessionId: string, buildId: string, faceId: string): Promise<FacePlane> {
  return fetch(
    `${sessionPath(sessionId)}/builds/${encodeURIComponent(buildId)}/faces/${encodeURIComponent(faceId)}`,
  ).then((r) => json<FacePlane>(r));
}

export function createSketch(sessionId: string, body: SketchCreate): Promise<Sketch> {
  return fetch(`${sessionPath(sessionId)}/sketches`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then((r) => json<Sketch>(r));
}
