import { sessionPath, type Build, type CheckReport, type CheckResult } from "./api";
import { fromMm, MM_PER_INCH, type LengthUnit } from "./units";
// `fetch` here says a dropped connection plainly, and `failure` a failed response (QA fix brief 8).
import { failure, reach as fetch } from "@/lib/errors";

// Design parameters (contracts/session-api.openapi.json, `Parameter`). Setting a value is a hand
// edit: the API locks the parameter and rebuilds, and the rebuild arrives as a `build_ready`
// event without a message ID. Unlocking hands the parameter back to Claude.

export type Point3 = [number, number, number];

/** One end of a dimension: a point (board coordinates, mm) and the face it lies on. */
export type DimensionEnd = { point: Point3; part?: string | null; face?: string | null };

/**
 * Where a parameter is measured on the model. `distance`: the value is the length from `start`
 * to `end`. `radius`: `start` is the centre, `end` is on the curved face.
 */
export type Dimension = { kind: "distance" | "radius"; start: DimensionEnd; end: DimensionEnd };

/** Faces that change a parameter when dragged: moving them d mm along `axis` adds d mm. */
export type Drag = { faces: string[]; axis: Point3 };

export type Parameter = {
  name: string;
  value: number | string;
  unit?: "mm" | "deg" | null;
  /** Set by a hand edit; Claude designs around it. */
  locked: boolean;
  min?: number | null;
  max?: number | null;
  description?: string;
  /** The feature the parameter drives. */
  feature?: string | null;
  /** Where the value is measured on the model; null when it has no single place. */
  dimension?: Dimension | null;
  /** The faces that change the value when dragged; null when none do. */
  drag?: Drag | null;
};

export type ParameterUpdate = { value?: number | string; locked?: boolean };

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) throw await failure(res);
  return (await res.json()) as T;
}

export function listParameters(sessionId: string): Promise<Parameter[]> {
  return fetch(`${sessionPath(sessionId)}/parameters`).then((r) => json<Parameter[]>(r));
}

export function updateParameter(sessionId: string, name: string, update: ParameterUpdate): Promise<Parameter> {
  return fetch(`${sessionPath(sessionId)}/parameters/${encodeURIComponent(name)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(update),
  }).then((r) => json<Parameter>(r));
}

/** Replaces a parameter in a list by name. */
export function withParameter(params: Parameter[], param: Parameter): Parameter[] {
  return params.map((p) => (p.name === param.name ? param : p));
}

/** The unit a parameter is shown and typed in: lengths follow the mm/in toggle. */
export function displayUnit(param: Pick<Parameter, "unit">, lengthUnit: LengthUnit): string {
  return param.unit === "mm" ? lengthUnit : (param.unit ?? "");
}

/** A parameter's value as the text in its field. */
export function formatParameter(param: Pick<Parameter, "value" | "unit">, lengthUnit: LengthUnit): string {
  if (typeof param.value === "string") return param.value;
  const v = param.unit === "mm" ? fromMm(param.value, lengthUnit) : param.value;
  const digits = param.unit === "mm" && lengthUnit === "in" ? 4 : 3;
  return String(Number(v.toFixed(digits)));
}

/**
 * The typed text as a parameter value, in the parameter's own unit (lengths typed in inches
 * become millimetres), or an error message.
 */
export function parseParameter(
  text: string,
  param: Pick<Parameter, "value" | "unit" | "min" | "max">,
  lengthUnit: LengthUnit,
): { value: number | string } | { error: string } {
  const trimmed = text.trim();
  if (!trimmed) return { error: "Type a value." };
  if (typeof param.value === "string") return { value: trimmed };
  const typed = Number(trimmed);
  if (!Number.isFinite(typed)) return { error: "Type a number." };
  const value = param.unit === "mm" && lengthUnit === "in" ? typed * MM_PER_INCH : typed;
  const unit = displayUnit(param, lengthUnit);
  const show = (mm: number) => `${formatParameter({ value: mm, unit: param.unit }, lengthUnit)}${unit ? ` ${unit}` : ""}`;
  if (param.min != null && value < param.min) return { error: `At least ${show(param.min)}.` };
  if (param.max != null && value > param.max) return { error: `At most ${show(param.max)}.` };
  return { value };
}

/** The first build in `builds` that was not there before the edit: the rebuild. */
export function rebuiltBuild(builds: Build[], before: string[]): Build | null {
  return builds.find((b) => !before.includes(b.id)) ?? null;
}

export function failingChecks(report: CheckReport): CheckResult[] {
  return report.results.filter((r) => r.status === "fail");
}

/** The parameter a face drags, if any: faces of other features don't drag. */
export function draggedBy(params: Parameter[], faceId: string): Parameter | null {
  return params.find((p) => p.drag?.faces.includes(faceId)) ?? null;
}

/**
 * How far a pointer move of (dx, dy) CSS pixels moves a face along its axis, in mm, given where
 * one millimetre along the axis lands on screen (`perMm`, CSS pixels). Only the part of the move
 * along the axis counts. Null when the axis points at the viewer (it can't be dragged there).
 */
export function dragDistance(perMm: [number, number], dx: number, dy: number): number | null {
  const len2 = perMm[0] * perMm[0] + perMm[1] * perMm[1];
  if (len2 < 1e-6) return null;
  return (dx * perMm[0] + dy * perMm[1]) / len2;
}

/** Drags snap to this step, in mm. */
export const DRAG_STEP_MM = 0.1;

/**
 * The value a drag of `distance` mm sets: the current value plus the distance, snapped to
 * DRAG_STEP_MM, or an error if it isn't a valid value (not above zero, or outside min and max).
 * Null when the drag is too short to change anything.
 */
export function draggedValue(
  param: Pick<Parameter, "value" | "unit" | "min" | "max">,
  distance: number,
  lengthUnit: LengthUnit,
): { value: number } | { error: string } | null {
  if (typeof param.value !== "number") return null;
  const value = Number((Math.round((param.value + distance) / DRAG_STEP_MM) * DRAG_STEP_MM).toFixed(3));
  if (Math.abs(value - param.value) < DRAG_STEP_MM / 2) return null;
  if (value <= 0) return { error: "Too small." };
  const checked = parseParameter(String(value), param, "mm");
  if ("error" in checked) {
    // Say the limit in the unit the user is looking at.
    const shown = parseParameter(formatParameter({ value, unit: param.unit }, lengthUnit), param, lengthUnit);
    return "error" in shown ? shown : checked;
  }
  return { value };
}

/** A dimension's text on the model: the value and its unit, with R in front of a radius. */
export function dimensionText(param: Parameter, lengthUnit: LengthUnit, value: number | string = param.value): string {
  const text = `${formatParameter({ value, unit: param.unit }, lengthUnit)} ${displayUnit(param, lengthUnit)}`;
  return param.dimension?.kind === "radius" ? `R${text}` : text;
}

type Px = [number, number];
/** A rectangle on the canvas, CSS pixels. */
export type Rect = { x: number; y: number; width: number; height: number };
/** A label's place: its centre, and the point on its line it labels (the same unless it moved off the line). */
export type LabelSpot = { at: Px; anchor: Px };

/**
 * Where to put each dimension's label (canvas pixels), so labels don't cover each other or the
 * panels over the canvas (`obstacles`), and stay inside the canvas (`bounds`, its width and
 * height): the middle of the line if it is free, otherwise the first free spot moving out from
 * the middle toward either end. If the whole line is under a panel, the label goes just beside
 * the panel, level with its line's middle (or stepped up or down to a free spot), and the
 * anchor is where it belongs on the line. Failing that, it takes the nearest free spot around the
 * line's middle. `size` is a label's width and height.
 */
export function placeLabels(lines: [Px, Px][], size: Px, obstacles: Rect[] = [], bounds?: Px): LabelSpot[] {
  const [w, h] = size;
  const placed: Px[] = [];
  const hits = (p: Px, r: Rect) =>
    p[0] + w / 2 > r.x && p[0] - w / 2 < r.x + r.width && p[1] + h / 2 > r.y && p[1] - h / 2 < r.y + r.height;
  const inside = (p: Px) =>
    !bounds || (p[0] - w / 2 >= 0 && p[1] - h / 2 >= 0 && p[0] + w / 2 <= bounds[0] && p[1] + h / 2 <= bounds[1]);
  const free = (p: Px) =>
    inside(p) &&
    !obstacles.some((r) => hits(p, r)) &&
    placed.every((q) => Math.abs(p[0] - q[0]) >= w || Math.abs(p[1] - q[1]) >= h);
  const at = ([a, b]: [Px, Px], t: number): Px => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
  // The nearest free spot around `p`, in rings of label-sized steps, for a line whose own spots
  // and panel sides are all taken (small obstacles such as check pins can leave it so).
  const nearby = (p: Px): Px | undefined => {
    for (let k = 1; k <= 6; k++)
      for (let i = -k; i <= k; i++)
        for (const [dx, dy] of [[i, -k], [i, k], [-k, i], [k, i]] as Px[]) {
          const q: Px = [p[0] + dx * w, p[1] + dy * h];
          if (free(q)) return q;
        }
    return undefined;
  };
  const spots = [0.5, 0.35, 0.65, 0.2, 0.8, 0.1, 0.9];
  const steps = [0, 1, -1, 2, -2, 3, -3, 4, -4, 5, -5, 6, -6];
  return lines.map((line) => {
    const t = spots.find((s) => free(at(line, s)));
    const anchor = at(line, t ?? 0.5);
    let spot = anchor;
    if (t === undefined) {
      const gap = 4;
      const beside = obstacles
        .filter((r) => hits(anchor, r))
        .flatMap((r): Px[] => [
          [r.x + r.width + w / 2 + gap, anchor[1]],
          [r.x - w / 2 - gap, anchor[1]],
        ]);
      const candidates = steps.flatMap((k) => beside.map(([x, y]): Px => [x, y + k * h]));
      spot = candidates.find(free) ?? nearby(anchor) ?? anchor;
    }
    placed.push(spot);
    return { at: spot, anchor };
  });
}
