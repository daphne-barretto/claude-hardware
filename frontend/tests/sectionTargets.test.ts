import { describe, expect, it } from "vitest";
import { componentBody, bodyCenter, type Board } from "@/components/canvas/inspect";
import { boundsCenter, sectionTargets, sectionThrough } from "@/components/canvas/sectionTargets";
import { readRepoFile } from "./fixtures";

// The section cut must not depend on a board (a plain request has none): it goes through the
// design's own parts, and through the board's components only when there is a board.
const build = JSON.parse(readRepoFile("fixtures/build.json")) as { board: { name: string; revision: string } };
const board = JSON.parse(readRepoFile(`demos/b-parasite/${build.board.name}-${build.board.revision}.json`)) as Board;
// A unit cube from (0,0,0) to (2,4,6), as two triangles is enough for its bounds.
const box = Float64Array.from([0, 0, 0, 2, 4, 0, 0, 4, 6, 2, 0, 6, 0, 0, 0, 2, 4, 6]);

describe("section targets", () => {
  it("cuts a design without a board through its own parts", () => {
    const targets = sectionTargets([{ part: "sphere", triangles: box }], null);
    expect(targets).toEqual([{ kind: "part", id: "sphere", label: "sphere", center: [1, 2, 3] }]);
    expect(sectionThrough(targets[0], "y")).toEqual({ axis: "y", at: 2, ref: "sphere" });
  });

  it("lists the board's components, then the parts, when there is a board", () => {
    const targets = sectionTargets([{ part: "lid", triangles: box }], board);
    expect(targets.map((t) => t.id)).toEqual([...board.components.map((c) => c.ref), "lid"]);
    const c = board.components[0];
    const t = targets.find((x) => x.id === c.ref)!;
    expect(t.kind).toBe("component");
    expect(t.center).toEqual(bodyCenter(componentBody(board, c)));
    expect(t.label).toBe(c.role ? `${c.ref} · ${c.role}` : c.ref);
  });

  it("skips parts with no triangles", () => {
    expect(boundsCenter(new Float64Array())).toBeNull();
    expect(sectionTargets([{ part: "empty", triangles: new Float64Array() }], null)).toEqual([]);
  });
});
