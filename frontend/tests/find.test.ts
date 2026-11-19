import { describe, expect, it } from "vitest";
import type { FindResult } from "@/components/canvas/api";
import { findCount, findPicks, groupMatches } from "@/components/canvas/find";
import { indexFromGltf, toCanvasSelection } from "@/components/canvas/selection";
import { readRepoFile, sessionOpenApi } from "./fixtures";

// Find results built from the fixture mesh's faces; no scenario values are written here.
const index = indexFromGltf(JSON.parse(readRepoFile("fixtures/enclosure.gltf")));
const faces = index.flatMap((p) => p.faces.map((f) => ({ ...f, part: p.part })));
const features = [...new Set(faces.map((f) => f.feature))];
const twoFeatures = features.slice(0, 2);
const matches = faces
  .filter((f) => twoFeatures.includes(f.feature))
  .map((f, i) => ({ face: f.id, part: f.part, feature: f.feature, value: i + 0.5, unit: "mm" as const }));
const found: FindResult = { query: "q", status: "found", understood: "Faces", matches };

describe("find in model", () => {
  it("is a session API operation that returns a FindResult", () => {
    const op = sessionOpenApi.paths["/api/sessions/{session_id}/builds/{build_id}/find"]?.post;
    expect(op).toBeDefined();
    const schema = sessionOpenApi.components.schemas.FindResult;
    expect(Object.keys(schema.properties).sort()).toEqual(["matches", "query", "question", "status", "understood"]);
  });

  it("turns the matches into face picks that become the chat selection", () => {
    const picks = findPicks(found);
    expect(picks).toEqual(matches.map((m) => ({ kind: "face", id: m.face, part: m.part })));
    const sel = toCanvasSelection(index, picks)!;
    expect(sel.selection.faces).toEqual(matches.map((m) => m.face));
    expect(sel.selection.edges).toEqual([]);
  });

  it("groups the matches by feature with each feature's value range", () => {
    const groups = groupMatches(matches);
    expect(groups.map((g) => g.feature)).toEqual(twoFeatures);
    for (const g of groups) {
      const mine = matches.filter((m) => m.feature === g.feature);
      expect(g.faces).toEqual(mine.map((m) => m.face));
      const lo = Math.min(...mine.map((m) => m.value));
      const hi = Math.max(...mine.map((m) => m.value));
      expect(g.value).toBe(lo === hi ? `${lo} mm` : `${lo} mm to ${hi} mm`);
    }
    expect(groupMatches([{ ...matches[0], value: 0, unit: "deg" }])[0].value).toBe("0°");
    expect(groupMatches([{ ...matches[0], value: null, unit: null }])[0].value).toBeNull();
  });

  it("counts faces and features, says when none match, and says nothing when unclear", () => {
    expect(findCount(found)).toBe(`${matches.length} faces in 2 features`);
    expect(findCount({ ...found, status: "none", matches: [] })).toBe("No faces match.");
    expect(findCount({ ...found, status: "unclear", matches: [], question: "?" })).toBe("");
  });
});
