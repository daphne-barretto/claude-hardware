import type { Build, BuildDiff, CheckSummary, FeatureChange, ParameterChange } from "./api";
import { formatLength, type LengthUnit } from "./units";

// Revisions, compare and history: which build the compare view puts next to the shown
// one, and the diff (`GET .../builds/{id}/diff`) as the change list shows it.

/** "v2.0" for a build on a board revision; "Design" for a design without a board. */
export const revisionLabel = ({ board }: Pick<Build, "board">) => (board ? `v${board.revision}` : "Design");

/** A build's place in the session's builds, from 1 (oldest first); null when it isn't listed. */
export function buildNumber(build: Pick<Build, "id">, builds: readonly Pick<Build, "id">[]): number | null {
  const i = builds.findIndex((b) => b.id === build.id);
  return i < 0 ? null : i + 1;
}

/**
 * How the chat and History name a build: "Build 3", its place in the session's builds. Never
 * its ID (QA fix brief 1); "Build" alone while the list hasn't caught up with it yet.
 */
export function buildName(build: Pick<Build, "id">, builds: readonly Pick<Build, "id">[]): string {
  const n = buildNumber(build, builds);
  return n === null ? "Build" : `Build ${n}`;
}

/** "Build 3 · v2.0" (with the board's name when there is one: "Build 3 · <board> 2.0"). */
export function buildTitle(build: Pick<Build, "id" | "board">, builds: readonly Pick<Build, "id">[]): string {
  const name = buildName(build, builds);
  return build.board ? `${name} · ${build.board.name} ${build.board.revision}` : name;
}

/** What History says of a restored build: "Restored from Build 1". */
export function restoredText(restoredFrom: string, builds: readonly Pick<Build, "id">[]): string {
  const n = buildNumber({ id: restoredFrom }, builds);
  return n === null ? "Restored from an earlier build" : `Restored from Build ${n}`;
}

/** The board a build was checked against, "name revision"; "No board" without one. */
export const boardLabel = ({ board }: Pick<Build, "board">) => (board ? `${board.name} ${board.revision}` : "No board");

/** The build to compare the shown one with: the latest ready build on another board revision. */
export function compareTarget(builds: Build[], shown: Build | undefined): Build | undefined {
  if (!shown) return undefined;
  const other = (b: Build) =>
    b.status === "ready" && (b.board?.name !== shown.board?.name || b.board?.revision !== shown.board?.revision);
  return [...builds].reverse().find(other);
}

/** "3 changes from v1.2": the features that changed, counted. */
export function changesHeading(diff: BuildDiff, from: Build): string {
  const n = diff.features.length;
  return `${n} ${n === 1 ? "change" : "changes"} from ${revisionLabel(from)}`;
}

/** A feature change as the list names it: the feature and what happened, e.g. "opening_Q4 moved and rotated". */
export function featureChangeText(change: FeatureChange): string {
  return `${change.tag} ${change.change}`;
}

/** How far a feature moved and turned, in the display unit: "3.1 mm, turned 90°". Empty when it only appeared or went. */
export function featureChangeDetail(change: FeatureChange, unit: LengthUnit): string {
  const parts: string[] = [];
  if (change.distance != null && change.change.includes("moved")) parts.push(formatLength(change.distance, unit));
  if (change.rotation != null && change.change.includes("rotated")) parts.push(`turned ${Math.round(change.rotation)}°`);
  return parts.join(", ");
}

function formatValue(value: unknown): string {
  if (value == null) return "none";
  if (typeof value === "number") return String(Number(value.toFixed(3)));
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

/** A parameter change: "wall: 2 → 2.5", "openings[sw1].x: added 12.3". */
export function parameterChangeText(change: ParameterChange): string {
  if (change.change === "added") return `${change.name}: added ${formatValue(change.new)}`;
  if (change.change === "removed") return `${change.name}: removed (was ${formatValue(change.old)})`;
  return `${change.name}: ${formatValue(change.old)} → ${formatValue(change.new)}`;
}

/** The history timeline: newest build first. */
export function historyEntries(builds: Build[]): Build[] {
  return [...builds].reverse();
}

/**
 * A check summary in a line: "8 pass · 2 fail · 3 untested", plus "· 2 not applicable" when
 * some standards don't apply to the design (those are not failures).
 */
export function summaryLine({ pass, fail, untested, not_applicable }: CheckSummary): string {
  const line = `${pass} pass · ${fail} fail · ${untested} untested`;
  return not_applicable ? `${line} · ${not_applicable} not applicable` : line;
}

/** A build's check summary in a line (`summaryLine`). */
export function checksLine({ checks }: Pick<Build, "checks">): string {
  return summaryLine(checks);
}
