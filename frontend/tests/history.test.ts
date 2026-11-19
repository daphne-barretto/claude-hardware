import { describe, expect, it } from "vitest";
import type { Build, BuildDiff } from "@/components/canvas/api";
import {
  changesHeading,
  checksLine,
  compareTarget,
  featureChangeDetail,
  featureChangeText,
  historyEntries,
  parameterChangeText,
  revisionLabel,
  summaryLine,
} from "@/components/canvas/history";
import { MM_PER_INCH } from "@/components/canvas/units";
import { readRepoFile } from "./fixtures";

// Revisions, compare and history, against the fixtures (fixtures/README.md).
const fixture = <T>(name: string) => JSON.parse(readRepoFile(`fixtures/${name}`)) as T;
const latest = fixture<Build>("build.json");
const previous = fixture<Build>("previous-build.json");
const diff = fixture<BuildDiff>("diff.json");

describe("compare", () => {
  it("compares with the latest build on another revision", () => {
    const edit = { ...latest, id: `${latest.id}-edit-1` };
    expect(previous.board!.revision).not.toBe(latest.board!.revision);
    expect(compareTarget([previous, latest, edit], edit)?.id).toBe(previous.id);
    expect(compareTarget([previous, latest, edit], previous)?.id).toBe(edit.id);
    expect(compareTarget([latest, edit], edit)).toBeUndefined();
    expect(compareTarget([{ ...previous, status: "failed" }, latest], latest)).toBeUndefined();
    expect(compareTarget([], undefined)).toBeUndefined();
  });

  it("names the changes and counts the features", () => {
    expect(changesHeading(diff, previous)).toBe(`${diff.features.length} changes from v${previous.board!.revision}`);
    expect(changesHeading({ ...diff, features: diff.features.slice(0, 1) }, previous)).toMatch(/^1 change from/);
    for (const c of diff.features) expect(featureChangeText(c)).toBe(`${c.tag} ${c.change}`);
  });

  it("gives how far a feature moved and turned, in the display unit", () => {
    const moved = diff.features.find((c) => c.change.includes("moved") && c.distance)!;
    expect(featureChangeDetail(moved, "mm")).toContain(`${moved.distance!.toFixed(1)} mm`);
    expect(featureChangeDetail(moved, "in")).toContain(`${(moved.distance! / MM_PER_INCH).toFixed(3)} in`);
    const turned = diff.features.find((c) => c.change.includes("rotated"));
    if (turned) expect(featureChangeDetail(turned, "mm")).toContain(`turned ${Math.round(turned.rotation!)}°`);
    for (const c of diff.features.filter((f) => f.change === "added" || f.change === "removed"))
      expect(featureChangeDetail(c, "mm")).toBe("");
  });

  it("writes each parameter change with its values", () => {
    expect(parameterChangeText({ name: "wall", change: "changed", old: 2, new: 2.5 })).toBe("wall: 2 → 2.5");
    expect(parameterChangeText({ name: "a", change: "added", old: null, new: 1.23456 })).toBe("a: added 1.235");
    expect(parameterChangeText({ name: "b", change: "removed", old: "x", new: null })).toBe("b: removed (was x)");
    for (const p of diff.parameters) expect(parameterChangeText(p)).toContain(p.name);
  });
});

describe("history", () => {
  it("lists the builds newest first with their checks", () => {
    expect(historyEntries([previous, latest]).map((b) => b.id)).toEqual([latest.id, previous.id]);
    const { pass, fail, untested, not_applicable } = latest.checks;
    // Standards that don't apply to the design (a board's, an object's) are counted apart.
    expect(not_applicable).toBeGreaterThan(0);
    expect(checksLine(latest)).toBe(`${pass} pass · ${fail} fail · ${untested} untested · ${not_applicable} not applicable`);
    expect(checksLine({ checks: { pass, fail, untested, not_applicable: 0 } })).toBe(`${pass} pass · ${fail} fail · ${untested} untested`);
    expect(revisionLabel(previous)).toBe(`v${previous.board!.revision}`);
  });
});

describe("a design without a board", () => {
  const boardless = (b: Build, id: string): Build => ({ ...b, id, board: null });

  it("labels it without a revision and compares only with a board revision", () => {
    const first = boardless(latest, "boardless-1");
    const second = boardless(latest, "boardless-2");
    expect(revisionLabel(first)).toBe("Design");
    expect(compareTarget([first, second], second)).toBeUndefined();
    expect(compareTarget([previous, second], second)?.id).toBe(previous.id);
  });

  it("counts the checks that don't apply apart from the failures", () => {
    const summary = { pass: 3, fail: 1, untested: 2, not_applicable: 4 };
    expect(summaryLine(summary)).toBe("3 pass · 1 fail · 2 untested · 4 not applicable");
    expect(summaryLine({ ...summary, not_applicable: 0 })).toBe("3 pass · 1 fail · 2 untested");
    expect(checksLine({ checks: summary })).toBe(summaryLine(summary));
  });
});
