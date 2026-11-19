import type { CheckReport, CheckSummary } from "./api";
import { formatLength, type LengthUnit } from "./units";

/**
 * Tracked changes (change-set contract, `contracts/change-set.schema.json`): the changes a run
 * proposed, each with a numbered pin on the model, a ghost of the geometry it adds, removes or
 * moves, and accept and reject in the change panel.
 */

export type Point3 = [number, number, number];
export type ChangeStatus = "proposed" | "accepted" | "rejected";
export type FieldDelta = { field: string; before?: unknown; after?: unknown };
export type GeometryDelta = {
  kind: "added" | "removed" | "modified";
  path: string;
  feature?: string | null;
  component?: string | null;
  fields: FieldDelta[];
  /** x, y offset in mm, for a moved element. */
  move?: [number, number] | null;
  /** Degrees, for a turned element. */
  rotate?: number | null;
};
export type CheckLink = {
  id: string;
  check: string;
  /** The result's title on the checks strip ("Minimum wall thickness"). */
  title?: string | null;
  standard?: string | null;
  effect: "fixes" | "breaks" | "related";
  before?: "pass" | "fail" | "untested" | null;
  alone?: "pass" | "fail" | "untested" | null;
  after?: "pass" | "fail" | "untested" | null;
};
export type Change = {
  id: string;
  title: string;
  reason: string;
  status: ChangeStatus;
  delta: GeometryDelta;
  checks: CheckLink[];
  /** The comment this change answers (a comment sent to Claude). */
  comment?: string | null;
};
export type ChangeSet = {
  contract: string;
  mode: "manual" | "auto";
  base_build: string;
  build: string;
  checks: CheckSummary;
  changes: Change[];
};
export type ChangeReview = { accept: string[]; reject: string[] };

/** Where a change is on the model: the location of the first of its checks the report places. */
export function changeAnchor(change: Change, report: CheckReport): Point3 | null {
  for (const link of change.checks) {
    const result = report.results.find((r) => r.id === link.id);
    if (result?.location) return result.location.point;
  }
  return null;
}

/**
 * The ghost of a change: where the element goes (`at`), and for a move where it was (`from`,
 * on the same plane), with the callout text (distance, turn).
 */
export type Ghost = { kind: GeometryDelta["kind"] | "moved"; at: Point3; from: Point3 | null; callout: string | null };

export function changeGhost(change: Change, anchor: Point3, unit: LengthUnit): Ghost {
  const { kind, move, rotate } = change.delta;
  const moved = kind === "modified" && !!move && (move[0] !== 0 || move[1] !== 0);
  const from: Point3 | null = moved ? [anchor[0] - move![0], anchor[1] - move![1], anchor[2]] : null;
  const parts = [
    moved && formatLength(Math.hypot(move![0], move![1]), unit),
    rotate ? `${Number(rotate.toFixed(1))}°` : null,
  ].filter(Boolean);
  return {
    kind: moved ? "moved" : kind,
    at: anchor,
    from,
    callout: parts.length ? parts.join(" · ") : null,
  };
}

/** The review that sets one change's status. */
export function reviewFor(change: Pick<Change, "id">, decision: "accept" | "reject"): ChangeReview {
  return decision === "accept" ? { accept: [change.id], reject: [] } : { accept: [], reject: [change.id] };
}

/** "3 changes · 1 accepted" for the panel heading. */
export function changesSummary(set: ChangeSet): string {
  const n = set.changes.length;
  const count = (s: ChangeStatus) => set.changes.filter((c) => c.status === s).length;
  const decided = [
    count("accepted") && `${count("accepted")} accepted`,
    count("rejected") && `${count("rejected")} rejected`,
  ].filter(Boolean);
  return [`${n} ${n === 1 ? "change" : "changes"}`, ...decided].join(" · ");
}

/** An ID in words: "Q4-sensor_window" → "Q4 sensor window". */
const words = (id: string) => id.replace(/[_-]+/g, " ").trim();

/**
 * A check as people read it: its title on the checks strip, never its ID (QA fix brief 2), with
 * the subject of a check that runs once per subject: "Opening alignment (Q4 sensor window)".
 * Without a title (a set from before titles were sent), the check's name in words.
 */
export function checkTitle(link: Pick<CheckLink, "id" | "check" | "title">): string {
  const name = link.title || words(link.check).replace(/^./, (c) => c.toUpperCase());
  const subject = link.id.startsWith(`${link.check}:`) ? link.id.slice(link.check.length + 1) : "";
  return subject ? `${name} (${words(subject)})` : name;
}

/**
 * The checks a change fixes and breaks, by title: "Fixes Minimum wall thickness". Null when
 * there are none, or when the change's reason already names them all (said once).
 */
export function proofText(change: Change): string | null {
  const titles = (effect: CheckLink["effect"]) => change.checks.filter((c) => c.effect === effect).map(checkTitle);
  const [fixes, breaks] = [titles("fixes"), titles("breaks")];
  if (![...fixes, ...breaks].some((t) => !change.reason.includes(t))) return null;
  const parts = [fixes.length && `Fixes ${fixes.join(", ")}`, breaks.length && `Breaks ${breaks.join(", ")}`].filter(Boolean);
  return parts.join(" · ");
}
