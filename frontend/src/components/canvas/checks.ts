import type { CheckReport, CheckResult, CheckValue, Required } from "./api";
import { lengthsInText, MM_PER_INCH, type LengthUnit } from "./units";
import { pointDistance, type Triangles } from "./inspect";

/**
 * Check results on the model (check-results contract): the located results get a pin, the
 * untested ones are listed, and values are shown in the canvas's display unit.
 */

export type Point3 = [number, number, number];
export type LocatedResult = CheckResult & { location: NonNullable<CheckResult["location"]> };

/** Results that have a place on the model, in report order. */
export function locatedResults(report: CheckReport): LocatedResult[] {
  return report.results.filter((r): r is LocatedResult => r.status !== "untested" && !!r.location);
}

/** Results the checks did not test (e.g. not simulated), in report order. */
export function untestedResults(report: CheckReport): CheckResult[] {
  return report.results.filter((r) => r.status === "untested");
}

/** The label a result has in the strip and on its card: the title, plus the component if any. */
export function checkLabel(r: Pick<CheckResult, "title" | "component">): string {
  return r.component ? `${r.title} · ${r.component}` : r.title;
}

/** A measured or required value as text; lengths, areas and volumes follow the display unit. */
export function formatValue(v: CheckValue, unit: LengthUnit): string {
  // Words ("a stop within 0.201 mm"): the lengths in them follow the display unit too.
  if (typeof v.value === "string") return lengthsInText(v.value, unit);
  const power = v.unit === "mm" ? 1 : v.unit === "mm2" ? 2 : v.unit === "mm3" ? 3 : 0;
  if (power === 0) {
    const n = trim(v.value, 2);
    return v.unit === "deg" ? `${n}°` : n;
  }
  if (unit === "in") {
    const sup = ["", "", "²", "³"][power];
    return `${trim(v.value / MM_PER_INCH ** power, 4)} in${sup}`;
  }
  return `${trim(v.value, 3)} ${["", "mm", "mm²", "mm³"][power]}`;
}

/** The limit a result is judged against, e.g. "limit ±0.2 mm", "min 2 mm", "requires molded ABS". */
export function formatRequired(r: Required, unit: LengthUnit): string {
  const value = formatValue(r, unit);
  switch (r.kind) {
    case "tolerance":
      return `limit ±${value}`;
    case "min":
      return `min ${value}`;
    case "max":
      return `max ${value}`;
    case "spec":
      return `requires ${value}`;
  }
}

/** "Measured … · limit …" for a result's card, or null when it has neither. */
export function evidence(r: CheckResult, unit: LengthUnit): string | null {
  const parts = [
    r.measured && `Measured ${formatValue(r.measured, unit)}`,
    r.required && formatRequired(r.required, unit),
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : null;
}

function trim(n: number, digits: number): string {
  return String(Number(n.toFixed(digits)));
}

export type ScreenPoint = { x: number; y: number };

/** Space between pin centres, in pixels: a failure pin is 20 px across. */
export const PIN_GAP = 22;

/** A screen rectangle, in the same pixels as `ScreenPoint`. */
export type ScreenRect = { x: number; y: number; width: number; height: number };

/** How far a pin centre stays from a panel's edge: half a failure pin plus its selection ring. */
export const PIN_CLEARANCE = 13;

const inside = (p: ScreenPoint, r: ScreenRect, m: number) =>
  p.x > r.x - m && p.x < r.x + r.width + m && p.y > r.y - m && p.y < r.y + r.height + m;

/** Whether a pin at `p` would draw over any of the rectangles. */
export function covered(p: ScreenPoint, covers: ScreenRect[], margin = PIN_CLEARANCE): boolean {
  return covers.some((r) => inside(p, r, margin));
}

/**
 * Moves a point that lies under a panel (one of `covers`) to the nearest spot just outside
 * it, so its pin draws next to the panel instead of over it and stays clickable. Points that
 * no panel covers are returned as they are.
 */
export function clearOf(
  at: ScreenPoint,
  covers: ScreenRect[],
  bounds?: { width: number; height: number },
  margin = PIN_CLEARANCE,
): ScreenPoint {
  // A spot next to the panel that stays in the view, so the pin isn't cut off at its edge.
  const inView = (o: ScreenPoint) =>
    !bounds || (o.x >= margin && o.x <= bounds.width - margin && o.y >= margin && o.y <= bounds.height - margin);
  let p = at;
  // Each step leaves one panel; a spot next to it can still be under a neighbour.
  for (let i = 0; i <= covers.length; i++) {
    const r = covers.find((c) => inside(p, c, margin));
    if (!r) return p;
    const options = [
      { x: r.x - margin, y: p.y },
      { x: r.x + r.width + margin, y: p.y },
      { x: p.x, y: r.y - margin },
      { x: p.x, y: r.y + r.height + margin },
    ].sort((a, b) => Math.hypot(a.x - p.x, a.y - p.y) - Math.hypot(b.x - p.x, b.y - p.y));
    p =
      options.find((o) => inView(o) && !covered(o, covers, margin)) ??
      options.find((o) => !covered(o, covers, margin)) ??
      options[0];
  }
  return p;
}

/**
 * Screen positions for the pins, so that no pin covers another or a panel and each can be
 * clicked (`bounds`: the view's size, which pins moved off a panel stay inside). Pins are placed in order (the caller puts the selected pin first, so it stays
 * exactly on its point); a pin under a panel moves to the panel's nearest edge, and a pin that
 * would overlap one already placed moves sideways, alternating right and left, to the
 * nearest free spot that no panel covers.
 */
export function spreadPins<T extends { id: string; at: ScreenPoint }>(
  pins: T[],
  covers: ScreenRect[] = [],
  bounds?: { width: number; height: number },
  gap = PIN_GAP,
): Map<string, ScreenPoint> {
  const placed: ScreenPoint[] = [];
  const out = new Map<string, ScreenPoint>();
  const free = (p: ScreenPoint) =>
    !covered(p, covers) && placed.every((q) => Math.abs(p.x - q.x) >= gap || Math.abs(p.y - q.y) >= gap);
  for (const { id, at } of pins) {
    const start = clearOf(at, covers, bounds);
    let p = start;
    for (let k = 1; !free(p) && k < 200; k++) p = { x: start.x + (k % 2 ? 1 : -1) * Math.ceil(k / 2) * gap, y: start.y };
    placed.push(p);
    out.set(id, p);
  }
  return out;
}

/**
 * Where a result's pin sits on the model: the point on the faces the result refers to (or on its
 * part) nearest the reported point, so the pin is on the enclosure even when the check reports a
 * point just off it (the middle of a slot, a pocket's centre). With no geometry, the point itself.
 */
export function pinAnchor(point: Point3, triangles: Triangles | null): Point3 {
  if (!triangles || triangles.length < 9) return point;
  return pointDistance(point, triangles).to as Point3;
}

/** How far a check pin may move from its spot to keep clear of another pin. */
export const MAX_PIN_SHIFT = PIN_GAP;

/**
 * Screen positions for check pins that keep every pin on its spot on the model: a pin that would
 * overlap one already placed moves at most `maxShift` pixels, to the nearest free spot around
 * its own, and otherwise stays on its spot under the earlier one (the caller puts the selected
 * pin, then failures, first). Pins outside `bounds` are left out.
 */
export function placePins<T extends { id: string; at: ScreenPoint }>(
  pins: T[],
  bounds?: { width: number; height: number },
  gap = PIN_GAP,
  maxShift = MAX_PIN_SHIFT,
): Map<string, ScreenPoint> {
  const placed: ScreenPoint[] = [];
  const out = new Map<string, ScreenPoint>();
  const inView = (p: ScreenPoint) => !bounds || (p.x >= 0 && p.y >= 0 && p.x <= bounds.width && p.y <= bounds.height);
  const free = (p: ScreenPoint) => placed.every((q) => Math.abs(p.x - q.x) >= gap || Math.abs(p.y - q.y) >= gap);
  const steps = [0.5, 1].map((f) => f * maxShift);
  const around = (at: ScreenPoint) => [
    at,
    ...steps.flatMap((d) =>
      [0, 1, 2, 3, 4, 5, 6, 7].map((k) => ({
        x: at.x + Math.round(Math.cos((k * Math.PI) / 4) * d),
        y: at.y + Math.round(Math.sin((k * Math.PI) / 4) * d),
      })),
    ),
  ];
  for (const { id, at } of pins) {
    if (!inView(at)) continue;
    const p = around(at).find((o) => inView(o) && free(o)) ?? at;
    placed.push(p);
    out.set(id, p);
  }
  return out;
}
