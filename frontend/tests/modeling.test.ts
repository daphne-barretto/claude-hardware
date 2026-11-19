import { describe, expect, it } from "vitest";
import {
  EDIT_KINDS,
  editFields,
  editSummary,
  editTarget,
  fixText,
  parseEdit,
  pickedEdits,
  TOOLS,
  type Edit,
} from "../src/components/canvas/modeling";
import { indexFromGltf } from "../src/components/canvas/selection";
import { MM_PER_INCH } from "../src/components/canvas/units";
import { readRepoFile, sessionOpenApi } from "./fixtures";

// Modeling tools: the tools match the contract, the forms parse typed values, a tool's
// target comes from the picks.

const gltf = JSON.parse(readRepoFile("fixtures/enclosure.gltf"));
const index = indexFromGltf(gltf);
const part = index[0];
const face = { kind: "face" as const, id: part.faces[0].id, part: part.part };
const edge = { kind: "edge" as const, id: part.edges[0].id, part: part.part };

describe("modeling tools", () => {
  it("has a tool for every kind of edit the API takes", () => {
    const kinds = sessionOpenApi.components.schemas.EditCreate.properties.kind.enum;
    expect([...EDIT_KINDS].sort()).toEqual([...kinds].sort());
    expect(TOOLS.map((t) => t.kind)).toEqual([...EDIT_KINDS]);
  });

  it("shows the fields each tool reads, and a hole for the insert takes its size from the standard", () => {
    expect(editFields("hole")).toEqual(["diameter", "depth", "x", "y"]);
    expect(editFields("hole", { insert: true })).toEqual(["x", "y"]);
    expect(editFields("pattern", { pattern: "circular" })).toEqual(["count", "angle"]);
    expect(editFields("pattern", { pattern: "linear" })).toEqual(["count", "spacing"]);
  });

  it("parses typed values, converting inches to millimetres", () => {
    const parsed = parseEdit("hole", { diameter: "0.1", depth: "0.05", x: "-0.2", y: "0" }, { insert: false }, "in");
    expect(parsed).toEqual({
      values: { diameter: 0.1 * MM_PER_INCH, depth: 0.05 * MM_PER_INCH, x: -0.2 * MM_PER_INCH, y: 0, insert: false },
    });
    expect(parseEdit("push_pull", { distance: "-1" }, {}, "mm")).toEqual({ values: { distance: -1 } });
    expect(parseEdit("pattern", { count: "4", angle: "360" }, { pattern: "circular" }, "mm")).toEqual({
      values: { count: 4, angle: 360, pattern: "circular" },
    });
    expect(parseEdit("mirror", {}, { axis: "y" }, "mm")).toEqual({ values: { axis: "y" } });
  });

  it("explains a value that is wrong", () => {
    expect(parseEdit("hole", { diameter: "0", depth: "x", x: "", y: "0" }, {}, "mm")).toEqual({
      errors: { diameter: "More than 0.", depth: "Type a number.", x: "Type a value." },
    });
    expect(parseEdit("push_pull", { distance: "0" }, {}, "mm")).toEqual({ errors: { distance: "Not 0." } });
    expect(parseEdit("pattern", { count: "1", spacing: "3" }, {}, "mm")).toEqual({ errors: { count: "2 to 50 copies." } });
  });

  it("takes what a tool is used on from the picks", () => {
    expect(editTarget("hole", [face], index)).toEqual({ target: { face_id: face.id }, label: `${face.id} · ${part.part}` });
    expect(editTarget("hole", [edge], index)).toEqual({ reason: "Pick a flat face (face filter)" });
    expect(editTarget("fillet", [edge], index)).toMatchObject({ target: { edge_id: edge.id } });
    expect(editTarget("move", [face], index)).toEqual({ target: { part: part.part }, label: part.part });
    expect(editTarget("mirror", [face], index)).toEqual({ target: { source: part.faces[0].feature }, label: part.faces[0].feature });
    expect(editTarget("hole", [], index)).toEqual({ reason: "Pick a flat face on the model" });
    expect(editTarget("hole", [face, edge], index)).toEqual({ reason: "Pick only one" });
  });

  it("finds the hand edits a pick is on, the insert pocket included", () => {
    const feature = part.faces[0].feature;
    const boss = { tag: "boss_hand_1", insert_tag: feature, kind: "boss" } as Edit;
    const rib = { tag: "rib_hand_1", kind: "rib" } as Edit;
    expect(pickedEdits([boss, rib], [face], index)).toEqual([boss]);
  });

  it("describes an edit and asks Claude for a fix that keeps it", () => {
    const hole = { kind: "hole", diameter: 4, depth: 4, insert: true } as Edit;
    expect(editSummary(hole, "mm")).toBe("⌀4 mm, 4 mm deep, for the insert");
    const wall = { check: "wall_thickness", title: "Minimum wall thickness" };
    const text = fixText("hole_hand_1", [wall, wall, { check: "draft_angle", title: "Minimum draft angle" }]);
    // The checks by their titles, never their IDs (QA fix brief 2).
    expect(text).toContain("(hole_hand_1) breaks checks: Minimum wall thickness, Minimum draft angle.");
    expect(text).toContain("Keep my edit");
  });
});
