import type { FindMatch, FindResult } from "./api";
import type { Picked } from "./selection";

/**
 * Find in model, kept free of React and three.js: a find result's matches as canvas picks, and
 * grouped by feature for the result list.
 */

/** The matched faces as picks, to highlight and to send to chat as the selection. */
export function findPicks(result: FindResult): Picked[] {
  return result.matches.map((m) => ({ kind: "face", id: m.face, part: m.part }));
}

export type FindGroup = { feature: string; part: string; faces: string[]; value: string | null };

/** The matches by feature, in the order they came: each feature's faces and its value range. */
export function groupMatches(matches: FindMatch[]): FindGroup[] {
  const groups = new Map<string, { part: string; faces: string[]; values: number[]; unit: FindMatch["unit"] }>();
  for (const m of matches) {
    const g = groups.get(m.feature) ?? { part: m.part, faces: [], values: [], unit: m.unit };
    g.faces.push(m.face);
    if (m.value != null) g.values.push(m.value);
    groups.set(m.feature, g);
  }
  return [...groups].map(([feature, g]) => ({
    feature,
    part: g.part,
    faces: g.faces,
    value: g.values.length ? formatRange(g.values, g.unit ?? null) : null,
  }));
}

function formatRange(values: number[], unit: "mm" | "deg" | null): string {
  const fmt = (v: number) => (unit === "deg" ? `${round(v)}°` : `${round(v)}${unit ? ` ${unit}` : ""}`);
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  return lo === hi ? fmt(lo) : `${fmt(lo)} to ${fmt(hi)}`;
}

const round = (v: number) => String(Math.round(v * 100) / 100);

/** The line above the matches: how many faces, or that none matched. */
export function findCount(result: FindResult): string {
  const n = result.matches.length;
  if (result.status === "unclear") return "";
  if (!n) return "No faces match.";
  const features = new Set(result.matches.map((m) => m.feature)).size;
  return `${n} face${n > 1 ? "s" : ""} in ${features} feature${features > 1 ? "s" : ""}`;
}
