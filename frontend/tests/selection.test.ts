import { describe, expect, it } from "vitest";
import {
  addPicks,
  boxFrom,
  componentFaces,
  componentOf,
  highlightPicks,
  pickAs,
  PICK_KINDS,
  hideParts,
  indexFromGltf,
  isIsolated,
  isolate,
  isolateParts,
  labelFor,
  partView,
  pickedParts,
  resolveId,
  showAll,
  toCanvasSelection,
  togglePick,
  type Picked,
} from "@/components/canvas/selection";
import { readRepoFile } from "./fixtures";
import { selectionItems, selectionLabel } from "@/components/sites/claude-shell/Composer";

// The part index of the mesh fixture (generated from demos/b-parasite/); no scenario values are written here.
const index = indexFromGltf(JSON.parse(readRepoFile("fixtures/enclosure.gltf")));
const parts = index.map((p) => p.part);
const allFaces = index.flatMap((p) => p.faces.map((f) => ({ ...f, part: p.part })));
const allEdges = index.flatMap((p) => p.edges.map((e) => ({ ...e, part: p.part })));
// A feature with more than one face that is not the part itself, e.g. an opening for a component.
const feature = allFaces.find((f) => f.feature !== f.part && allFaces.filter((g) => g.feature === f.feature).length > 1)!;

describe("selection", () => {
  it("reads the parts, faces and edges from the fixture mesh", () => {
    expect(parts.length).toBeGreaterThan(1);
    expect(allFaces.length).toBeGreaterThan(0);
    expect(allEdges.length).toBeGreaterThan(0);
    expect(feature).toBeDefined();
  });

  it("labels a selected face with its feature name and sends its ID", () => {
    for (const face of allFaces) {
      const s = toCanvasSelection(index, [{ kind: "face", id: face.id, part: face.part }], [1, 2, 3])!;
      expect(s.label).toBe(face.feature);
      expect(s.selection).toEqual({ part: face.part, parts: [], faces: [face.id], edges: [], components: [], point: [1, 2, 3] });
    }
  });

  it("labels an edge with its feature and a part with its name", () => {
    const edge = allEdges[0];
    const e = toCanvasSelection(index, [{ kind: "edge", id: edge.id, part: edge.part }])!;
    expect(e.label).toBe(edge.feature);
    expect(e.selection.edges).toEqual([edge.id]);
    const p = toCanvasSelection(index, [{ kind: "part", id: parts[0], part: parts[0] }])!;
    expect(p.label).toBe(parts[0]);
    expect(p.selection).toEqual({ part: parts[0], parts: [parts[0]], faces: [], edges: [], components: [], point: null });
    expect(toCanvasSelection(index, [])).toBeNull();
  });

  it("selects by face ID, edge ID, part, feature and component reference", () => {
    const face = allFaces[0];
    expect(resolveId(index, face.id)).toEqual([{ kind: "face", id: face.id, part: face.part }]);
    expect(resolveId(index, ` ${allEdges[0].id.toUpperCase()} `)).toEqual([
      { kind: "edge", id: allEdges[0].id, part: allEdges[0].part },
    ]);
    expect(resolveId(index, parts[1])).toEqual([{ kind: "part", id: parts[1], part: parts[1] }]);
    const featureFaces = allFaces.filter((f) => f.feature === feature.feature).map((f) => f.id);
    expect(resolveId(index, feature.feature).map((p) => p.id)).toEqual(featureFaces);
    // `opening_Q4` is found by `Q4`: the component reference is the part after the last underscore.
    const ref = feature.feature.slice(feature.feature.lastIndexOf("_") + 1);
    expect(resolveId(index, ref).map((p) => p.id)).toEqual(featureFaces);
    expect(labelFor(index, resolveId(index, ref))).toBe(`${feature.feature} · ${featureFaces.length} faces`);
    expect(resolveId(index, "no-such-id")).toEqual([]);
    expect(resolveId(index, "  ")).toEqual([]);
  });

  it("shift-click adds and removes picks", () => {
    const [a, b] = allFaces;
    const pa: Picked = { kind: "face", id: a.id, part: a.part };
    const pb: Picked = { kind: "face", id: b.id, part: b.part };
    expect(togglePick(togglePick([pa], pb), pa)).toEqual([pb]);
  });
});

// Board components with an opening in the enclosure, read from the board data (demos/b-parasite/).
const { board: boardRef } = JSON.parse(readRepoFile("fixtures/build.json"));
const board = JSON.parse(readRepoFile(`demos/b-parasite/${boardRef.name}-${boardRef.revision}.json`));
const refs: string[] = [...new Set<string>(board.openings.map((o: { component: string }) => o.component))];
const meshRefs = [...new Set(allFaces.flatMap((f) => componentOf(f.feature) ?? []))];

describe("selection filters and multi-select", () => {
  it("has a filter for faces, edges, parts and board components", () => {
    expect(PICK_KINDS).toEqual(["face", "edge", "part", "component"]);
  });

  it("finds the board's components in the mesh by their opening features", () => {
    expect(meshRefs.length).toBeGreaterThan(0);
    for (const ref of meshRefs) expect(refs).toContain(ref);
    // Part bodies and bosses belong to no component.
    for (const p of parts) expect(componentOf(p)).toBeUndefined();
  });

  it("limits a click to the filter's kind", () => {
    const opening = allFaces.find((f) => componentOf(f.feature))!;
    const plain = allFaces.find((f) => !componentOf(f.feature))!;
    const face: Picked = { kind: "face", id: opening.id, part: opening.part };
    expect(pickAs(index, face, "face")).toEqual(face);
    expect(pickAs(index, face, "part")).toEqual({ kind: "part", id: opening.part, part: opening.part });
    expect(pickAs(index, face, "component")).toEqual({ kind: "component", id: componentOf(opening.feature), part: opening.part });
    // In the component filter, a face that is no component's opening picks nothing.
    expect(pickAs(index, { kind: "face", id: plain.id, part: plain.part }, "component")).toBeNull();
  });

  it("highlights a component's opening faces and selects it by reference", () => {
    for (const ref of meshRefs) {
      const faces = allFaces.filter((f) => componentOf(f.feature) === ref).map((f) => f.id);
      expect(componentFaces(index, ref).map((p) => p.id)).toEqual(faces);
      const pick: Picked = { kind: "component", id: ref, part: allFaces.find((f) => f.id === faces[0])!.part };
      expect(highlightPicks(index, [pick]).map((p) => p.id)).toEqual(faces);
      expect(resolveId(index, ref).map((p) => p.id)).toEqual(faces);
      const s = toCanvasSelection(index, [pick])!;
      expect(s.label).toBe(ref);
      expect(s.selection.components).toEqual([ref]);
    }
  });

  it("Shift/Cmd toggling and box adding keep one of each item", () => {
    const [a, b, c] = allFaces.map((f): Picked => ({ kind: "face", id: f.id, part: f.part }));
    expect(togglePick([a], b)).toEqual([a, b]);
    expect(togglePick([a, b], a)).toEqual([b]);
    expect(addPicks([a, b], [b, c])).toEqual([a, b, c]);
    expect(boxFrom({ x: 30, y: 5 }, { x: 10, y: 40 })).toEqual([10, 5, 30, 40]);
  });

  it("sends every selected item's ID, and the chip lists every one of them", () => {
    const ref = meshRefs[0];
    const picks: Picked[] = [
      { kind: "part", id: parts[0], part: parts[0] },
      { kind: "component", id: ref, part: componentFaces(index, ref)[0].part },
      { kind: "face", id: allFaces[0].id, part: allFaces[0].part },
      { kind: "edge", id: allEdges.at(-1)!.id, part: allEdges.at(-1)!.part },
    ];
    const s = toCanvasSelection(index, picks)!;
    expect(s.selection.parts).toEqual([parts[0]]);
    expect(s.selection.components).toEqual([ref]);
    expect(s.selection.faces).toEqual([allFaces[0].id]);
    expect(s.selection.edges).toEqual([allEdges.at(-1)!.id]);
    expect(s.label).toBe("1 part, 1 component, 1 face, 1 edge");
    expect(selectionItems(s.selection).sort()).toEqual(picks.map((p) => p.id).sort());
    expect(selectionLabel(s.selection)).toContain("1 component");
  });
});

describe("part views", () => {
  it("isolates one part, and isolating it again shows everything", () => {
    const [first] = parts;
    const alone = isolate({}, parts, first);
    expect(parts.filter((p) => !partView(alone, p).hidden)).toEqual([first]);
    expect(isIsolated(alone, parts, first)).toBe(true);
    const back = isolate(alone, parts, first);
    expect(parts.every((p) => !partView(back, p).hidden)).toBe(true);
    expect(isIsolated(back, parts, first)).toBe(false);
  });

  it("keeps transparency when isolating", () => {
    const [first, second] = parts;
    const views = isolate({ [second]: { hidden: false, transparent: true } }, parts, first);
    expect(partView(views, second)).toEqual({ hidden: true, transparent: true });
  });

  it("finds the parts a selection touches, from part, face and edge picks", () => {
    const face = allFaces[0];
    const edge = allEdges.find((e) => e.part !== face.part) ?? allEdges[0];
    const picks: Picked[] = [
      { kind: "face", id: face.id, part: face.part },
      { kind: "edge", id: edge.id, part: edge.part },
      { kind: "part", id: face.part, part: face.part },
    ];
    expect(pickedParts(picks)).toEqual([...new Set([face.part, edge.part])]);
    expect(pickedParts([])).toEqual([]);
  });

  it("hides the given parts and leaves the others as they are", () => {
    const [first, second] = parts;
    const views = hideParts({ [second]: { hidden: false, transparent: true } }, [first]);
    expect(parts.filter((p) => partView(views, p).hidden)).toEqual([first]);
    expect(partView(views, second)).toEqual({ hidden: false, transparent: true });
  });

  it("isolates several parts, toggles back, and ignores an empty selection", () => {
    const keep = parts.slice(0, 2);
    const views = isolateParts({}, parts, keep);
    expect(parts.filter((p) => !partView(views, p).hidden)).toEqual(keep);
    expect(parts.every((p) => !partView(isolateParts(views, parts, keep), p).hidden)).toBe(true);
    expect(isolateParts(views, parts, [])).toBe(views);
  });

  it("shows all parts and keeps transparency", () => {
    const [first, second] = parts;
    const views = showAll(hideParts({ [first]: { hidden: false, transparent: true } }, [first, second]), parts);
    expect(parts.every((p) => !partView(views, p).hidden)).toBe(true);
    expect(partView(views, first).transparent).toBe(true);
  });
});
