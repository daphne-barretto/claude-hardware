import type { Constraint } from "@/lib/session-api";
import type { CheckReport, CheckResult, Required } from "./api";
import { formatValue } from "./checks";
import { partLabel } from "./comments/comments";
import type { LengthUnit } from "./units";

/**
 * The Constraints panel's rows: one per constraint of the design's spec, with the check results
 * that test it (`result.standard` is the constraint's id), plus one per other check that has
 * results but no constraint behind it (board checks such as opening alignment, a rule the spec
 * doesn't list), grouped by its standard or, without one, by its check.
 */

/** A constraint's state: `unchecked` is "not checked yet" (no build, or no result for it yet). */
export type GroupStatus = "fail" | "untested" | "unchecked" | "pass" | "not_applicable";

export type ConstraintGroup = {
  /** The constraint's id, the standard's, or the check's name. */
  key: string;
  /** The spec's constraint, which can be edited; null for a check with no constraint behind it. */
  constraint: Constraint | null;
  /** The constraint's name, e.g. "Fit tolerance". */
  title: string;
  /** The name with its required value, as the constraint shows it: "Fit tolerance ±0.2 mm". */
  label: string;
  status: GroupStatus;
  /** Every result of the constraint's check, in report order. */
  results: CheckResult[];
  /** How many places on the design the check tested (passed or failed). */
  places: number;
};

/** Failing first, then untested (and not checked yet), passing, and not applicable last. */
const RANK: Record<GroupStatus, number> = { fail: 0, untested: 1, unchecked: 1, pass: 2, not_applicable: 3 };

/** The words a status reads as, for the accessible names and the detail. */
export const STATUS_TEXT: Record<GroupStatus, string> = {
  fail: "failing",
  untested: "untested",
  unchecked: "not checked yet",
  pass: "passing",
  not_applicable: "not applicable",
};

const OPERATORS = { min: "≥ ", max: "≤ ", tolerance: "±" } as const;

/** Text compared loosely: case, spacing and a plural "s" aside. */
const loose = (text: string) =>
  text
    .toLowerCase()
    .replace(/\s+/g, " ")
    .trim()
    .replace(/s$/, "");

/**
 * The name with a required value, as the backend labels a constraint (`app.spec.label`):
 * "Fit tolerance ±0.2 mm". A spec whose value is its name ("Molded ABS") reads as the value; any
 * other keeps the name, "Board held: a stop within 0.201 mm", so a row never shows a value alone.
 * With no value (or an empty one), the name.
 */
export function requiredLabel(title: string, required: Required | null | undefined, unit: LengthUnit): string {
  if (!required) return title;
  const value = formatValue(required, unit).trim();
  if (!value) return title;
  if (required.kind === "spec") {
    if (loose(value) === loose(title)) return value.slice(0, 1).toUpperCase() + value.slice(1);
    return `${title}: ${value}`;
  }
  return `${title} ${OPERATORS[required.kind]}${value}`;
}

/** The required value alone, as the detail shows it after "Required:": "±0.2 mm", "≥ 2 mm". */
export function requiredValue(required: Required, unit: LengthUnit): string {
  const value = formatValue(required, unit).trim();
  return required.kind === "spec" ? value : `${OPERATORS[required.kind]}${value}`;
}

/** A group's status from its results: any failure fails it; with none, any untested one. */
export function groupStatus(results: CheckResult[]): GroupStatus {
  if (!results.length) return "unchecked";
  if (results.some((r) => r.status === "fail")) return "fail";
  if (results.some((r) => r.status === "untested")) return "untested";
  if (results.some((r) => r.status === "pass")) return "pass";
  return "not_applicable";
}

const tested = (r: CheckResult) => r.status === "pass" || r.status === "fail";

/**
 * The panel's rows, in order: failing, untested or not checked yet, passing, not applicable;
 * within a status, constraints in the spec's order, then the other checks in report order.
 * With no report (nothing built yet) every constraint is "not checked yet".
 */
export function constraintGroups(constraints: Constraint[], report: CheckReport | null, unit: LengthUnit): ConstraintGroup[] {
  const results = report?.results ?? [];
  const ids = new Set(constraints.map((c) => c.id));
  const groups: ConstraintGroup[] = constraints.map((c) => {
    const own = results.filter((r) => r.standard === c.id);
    return {
      key: c.id,
      constraint: c,
      title: c.title,
      // Lengths follow the display unit, as the check values do.
      label: c.kind === "spec" || !c.unit ? c.label : requiredLabel(c.title, { kind: c.kind, value: c.value, unit: c.unit } as Required, unit),
      status: groupStatus(own),
      results: own,
      places: own.filter(tested).length,
    };
  });
  const others = new Map<string, CheckResult[]>();
  for (const r of results) {
    if (r.standard && ids.has(r.standard)) continue;
    const key = r.standard ?? r.check;
    others.set(key, [...(others.get(key) ?? []), r]);
  }
  for (const [key, own] of others) {
    const first = own[0];
    // A result that wasn't run (untested, not applicable) has no value; another one may.
    const required = own.find((r) => r.required)?.required;
    groups.push({
      key,
      constraint: null,
      title: first.title,
      label: requiredLabel(first.title, required, unit),
      status: groupStatus(own),
      results: own,
      places: own.filter(tested).length,
    });
  }
  return groups
    .map((g, i) => ({ g, i }))
    .sort((a, b) => RANK[a.g.status] - RANK[b.g.status] || a.i - b.i)
    .map(({ g }) => g);
}

/** Where on the design a result is, for the detail's list of places. */
export function placeLabel(r: CheckResult): string {
  if (r.component) return r.component;
  if (r.parts) return r.parts.map(partLabel).join(" · ");
  if (r.opening) return partLabel(r.opening);
  if (r.location?.part) return partLabel(r.location.part);
  return r.title;
}

/** The row's accessible name: its label, its status and, when more than one, its places. */
export function groupName(g: ConstraintGroup): string {
  const places = g.places > 1 ? `, ${g.places} places` : "";
  return `${g.label}, ${STATUS_TEXT[g.status]}${places}`;
}
