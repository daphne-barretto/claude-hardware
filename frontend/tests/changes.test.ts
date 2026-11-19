import { describe, expect, it } from "vitest";
import type { CheckReport } from "@/components/canvas/api";
import {
  changeAnchor,
  changeGhost,
  changesSummary,
  checkTitle,
  proofText,
  reviewFor,
  type Change,
  type ChangeSet,
} from "@/components/canvas/changes";
import { MM_PER_INCH } from "@/components/canvas/units";
import { readRepoFile } from "./fixtures";

// The fixture change set (one change per demos/b-parasite/CHANGES.md row) and the check report it is shown with.
const set = JSON.parse(readRepoFile("fixtures/change-set.json")) as ChangeSet;
const report = JSON.parse(readRepoFile("fixtures/checks.json")) as CheckReport;

describe("change anchors", () => {
  it("places every change in the fixture on the model, at its check's location", () => {
    expect(set.changes.length).toBeGreaterThan(0);
    for (const change of set.changes) {
      const anchor = changeAnchor(change, report);
      const located = report.results.find((r) => change.checks.some((c) => c.id === r.id) && r.location);
      expect(anchor, change.id).toEqual(located!.location!.point);
    }
  });

  it("has no anchor when the report places none of the change's checks", () => {
    expect(changeAnchor(set.changes[0], { ...report, results: [] })).toBeNull();
  });
});

describe("change ghosts", () => {
  const anchor: [number, number, number] = [10, 20, 5];
  const base = set.changes[0];
  const withDelta = (delta: Partial<Change["delta"]>): Change => ({ ...base, delta: { ...base.delta, ...delta } });

  it("draws a moved element where it was, with the distance and turn as the callout", () => {
    const g = changeGhost(withDelta({ kind: "modified", move: [3, -4], rotate: 90 }), anchor, "mm");
    expect(g.kind).toBe("moved");
    expect(g.at).toEqual(anchor);
    expect(g.from).toEqual([7, 24, 5]);
    expect(g.callout).toBe("5.0 mm · 90°");
    expect(changeGhost(withDelta({ kind: "modified", move: [0, MM_PER_INCH], rotate: null }), anchor, "in").callout).toBe(
      "1.000 in",
    );
  });

  it("marks added and removed elements at their place", () => {
    for (const kind of ["added", "removed"] as const) {
      const g = changeGhost(withDelta({ kind, move: null, rotate: null }), anchor, "mm");
      expect(g).toEqual({ kind, at: anchor, from: null, callout: null });
    }
  });

  it("gives every fixture change a ghost of its kind", () => {
    for (const change of set.changes) {
      const g = changeGhost(change, changeAnchor(change, report)!, "mm");
      if (change.delta.kind === "modified" && change.delta.move) {
        expect(g.kind).toBe("moved");
        expect(g.callout).toContain(Math.hypot(...change.delta.move).toFixed(1));
      } else expect(g.kind).toBe(change.delta.kind);
    }
  });
});

describe("reviews", () => {
  it("accepts or rejects one change by ID", () => {
    expect(reviewFor(set.changes[0], "accept")).toEqual({ accept: [set.changes[0].id], reject: [] });
    expect(reviewFor(set.changes[0], "reject")).toEqual({ accept: [], reject: [set.changes[0].id] });
  });

  it("summarises the statuses", () => {
    const n = set.changes.length;
    expect(changesSummary(set)).toBe(`${n} changes`);
    const decided = { ...set, changes: set.changes.map((c, i) => ({ ...c, status: i === 0 ? "accepted" : i === 1 ? "rejected" : c.status }) as Change) };
    expect(changesSummary(decided)).toBe(`${n} changes · 1 accepted · 1 rejected`);
  });

  it("names the checks a change fixes by their titles, once", () => {
    for (const change of set.changes) {
      const fixes = change.checks.filter((c) => c.effect === "fixes");
      // The reason names them (QA fix brief 2), so nothing under it says it again.
      for (const c of fixes) expect(change.reason).toContain(checkTitle(c));
      for (const c of fixes) expect(change.reason).not.toContain(c.id);
      expect(proofText(change)).toBeNull();
      expect(proofText({ ...change, reason: "Changed by Claude." })).toBe(`Fixes ${fixes.map(checkTitle).join(", ")}`);
    }
  });
});
