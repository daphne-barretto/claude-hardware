import type { Build, DesignHistory, HistoryMove } from "./api";

// The design timeline (`GET .../history`, `POST .../history/undo|redo`): undo and redo move over
// one history of design changes, whoever made them (a hand edit, a Claude run, a review, a
// restore, an import). And the build the canvas shows: never a failed one, which the canvas
// reports instead.

export type Direction = "undo" | "redo";

/**
 * The build the canvas shows and acts on (comments, edits, measure, export): the picked one, or
 * else the latest build that has a model. A failed build has none, so it is never shown.
 */
export function shownBuild(builds: Build[], pickedId: string | null | undefined): Build | undefined {
  const picked = pickedId ? builds.find((b) => b.id === pickedId) : undefined;
  if (picked && picked.status === "ready") return picked;
  return [...builds].reverse().find((b) => b.status === "ready") ?? builds[builds.length - 1];
}

/** The newest build, when it failed and the canvas shows an older one instead. */
export function failedLatest(builds: Build[], shown: Build | undefined): Build | undefined {
  const latest = builds[builds.length - 1];
  return latest && latest.status === "failed" && latest.id !== shown?.id ? latest : undefined;
}

/** The plain reason a build failed: its own (`Build.failure`), or a general one. */
export function failureReason(build: Pick<Build, "failure">): string {
  return build.failure?.trim() || "the modeler couldn't make the shape";
}

/** The failed-build banner's text: "This change couldn't be built: <plain reason>". */
export function failureText(build: Pick<Build, "failure">): string {
  return `This change couldn't be built: ${failureReason(build)}.`;
}

/** What to send Claude to fix a failed change: the change, if the timeline names it, and why it failed. */
export function fixFailureText(build: Pick<Build, "failure">, history: DesignHistory | null): string {
  const step = history?.undo;
  const what = step ? `The last change (${step.text}) couldn't be built` : "The last change couldn't be built";
  return `${what}: ${failureReason(build)}. Please fix the design so it builds, keeping what the change was for.`;
}

/** Why Undo or Redo can't be used now; undefined when it can. */
export function stepBlocked(
  history: DesignHistory | null,
  direction: Direction,
  { busy, blocked, stepping }: { busy: boolean; blocked?: string | null; stepping: boolean },
): string | undefined {
  if (blocked) return blocked;
  if (busy) return "Claude is working";
  if (stepping) return direction === "undo" ? "Undoing" : "Redoing";
  if (!history?.[direction]) return `Nothing to ${direction}`;
  return undefined;
}

/** The Undo or Redo button's title: what it would undo or redo, and its shortcut. */
export function stepTitle(history: DesignHistory | null, direction: Direction, blocked: string | undefined): string {
  if (blocked) return blocked;
  const step = history?.[direction];
  const key = direction === "undo" ? "⌘Z" : "⇧⌘Z";
  return `${direction === "undo" ? "Undo" : "Redo"}: ${step?.text ?? ""} (${key})`;
}

/** The toast after an undo or redo: "Undid: You: push/pull 1 mm". */
export function moveText(move: Pick<HistoryMove, "direction" | "step">): string {
  return `${move.direction === "undo" ? "Undid" : "Redid"}: ${move.step.text}`;
}

const RAW = /\bbuild-[\w-]+|Traceback|\w*(Exception|Error)\b|\{"|\bat line \d+/;

/**
 * An error message as the user sees it: the API's own words when they are plain, else a plain
 * fallback. Never raw build IDs or internal error text.
 */
export function plainError(message: string | null | undefined, fallback: string): string {
  const text = (message ?? "").trim();
  if (!text || RAW.test(text) || /^\d{3}\b/.test(text)) return fallback;
  return text;
}
