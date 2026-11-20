import type { Build, CheckSummary } from "./api";

// How builds are named: by board revision, by their place in the session, and their checks.

/** "v2.0" for a build on a board revision; "Design" for a design without a board. */
export const revisionLabel = ({ board }: Pick<Build, "board">) => (board ? `v${board.revision}` : "Design");

/** A build's place in the session's builds, from 1 (oldest first); null when it isn't listed. */
export function buildNumber(build: Pick<Build, "id">, builds: readonly Pick<Build, "id">[]): number | null {
  const i = builds.findIndex((b) => b.id === build.id);
  return i < 0 ? null : i + 1;
}

/**
 * How the chat names a build: "Build 3", its place in the session's builds. Never
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

/** The board a build was checked against, "name revision"; "No board" without one. */
export const boardLabel = ({ board }: Pick<Build, "board">) => (board ? `${board.name} ${board.revision}` : "No board");

/**
 * A check summary in a line: "8 pass · 2 fail · 3 untested", plus "· 2 not applicable" when
 * some standards don't apply to the design (those are not failures).
 */
export function summaryLine({ pass, fail, untested, not_applicable }: CheckSummary): string {
  const line = `${pass} pass · ${fail} fail · ${untested} untested`;
  return not_applicable ? `${line} · ${not_applicable} not applicable` : line;
}
