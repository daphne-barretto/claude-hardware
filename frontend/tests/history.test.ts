import { describe, expect, it } from "vitest";
import type { Build } from "@/components/canvas/api";
import { revisionLabel, summaryLine } from "@/components/canvas/history";
import { readRepoFile } from "./fixtures";

// How builds are labelled, against the fixtures (fixtures/README.md).
const fixture = <T>(name: string) => JSON.parse(readRepoFile(`fixtures/${name}`)) as T;
const latest = fixture<Build>("build.json");
const previous = fixture<Build>("previous-build.json");

describe("a build's labels", () => {
  it("labels a build by its board revision, and its checks in a line", () => {
    const { pass, fail, untested, not_applicable } = latest.checks;
    // Standards that don't apply to the design (a board's, an object's) are counted apart.
    expect(not_applicable).toBeGreaterThan(0);
    expect(summaryLine(latest.checks)).toBe(`${pass} pass · ${fail} fail · ${untested} untested · ${not_applicable} not applicable`);
    expect(revisionLabel(previous)).toBe(`v${previous.board!.revision}`);
  });
});

describe("a design without a board", () => {
  it("labels it without a revision", () => {
    expect(revisionLabel({ board: null })).toBe("Design");
  });

  it("counts the checks that don't apply apart from the failures", () => {
    const summary = { pass: 3, fail: 1, untested: 2, not_applicable: 4 };
    expect(summaryLine(summary)).toBe("3 pass · 1 fail · 2 untested · 4 not applicable");
    expect(summaryLine({ ...summary, not_applicable: 0 })).toBe("3 pass · 1 fail · 2 untested");
  });
});
