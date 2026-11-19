import { describe, expect, it } from "vitest";
import { featureTree } from "../src/components/canvas/featureTree";
import { indexFromGltf } from "../src/components/canvas/selection";
import {
  defaultDraft,
  fieldLabel,
  parseSketch,
  SKETCH_SHAPES,
  sketchFace,
  sketchFields,
  type FacePlane,
  type SketchDraft,
} from "../src/components/canvas/sketch";
import { MM_PER_INCH } from "../src/components/canvas/units";
import type { BuildSource } from "../src/components/canvas/api";
import { loadFixtureFaces, sketchTargets } from "../e2e/sketchFaces";
import { readRepoFile } from "./fixtures";

// Sketch on a face: the form's fields per shape, parsing typed sizes, the face to sketch on.

const gltf = JSON.parse(readRepoFile("fixtures/enclosure.gltf"));
const buildSource = JSON.parse(readRepoFile("fixtures/build-source.json")) as BuildSource;
const face = { build_id: "b", face_id: "f" };
const draft = (over: Partial<SketchDraft> = {}): SketchDraft => ({
  width: "8",
  height: "3",
  count: "4",
  x: "-1.5",
  y: "2",
  depth: "1",
  ...over,
});

describe("sketch form", () => {
  it("shows the fields each shape uses", () => {
    expect(sketchFields("circle")).toEqual(["width", "x", "y", "depth"]);
    expect(sketchFields("slot")).toEqual(["width", "height", "x", "y", "depth"]);
    expect(sketchFields("rectangle")).toEqual(["width", "height", "x", "y", "depth"]);
    expect(sketchFields("vent")).toEqual(["width", "height", "count", "x", "y", "depth"]);
    expect(fieldLabel("width", "circle", "cut")).toBe("Diameter");
    expect(fieldLabel("width", "slot", "cut")).toBe("Length");
    expect(fieldLabel("height", "slot", "cut")).toBe("Slot width");
    expect(fieldLabel("depth", "rectangle", "cut")).toBe("Depth");
    expect(fieldLabel("depth", "rectangle", "extrude")).toBe("Height out");
  });

  it("turns the typed values into a sketch for every shape and operation", () => {
    for (const shape of SKETCH_SHAPES)
      for (const operation of ["cut", "extrude"] as const) {
        const parsed = parseSketch(draft(), shape, operation, face, "mm");
        expect(parsed).toEqual({
          body: {
            ...face,
            shape,
            operation,
            width: 8,
            height: shape === "circle" ? null : 3,
            count: shape === "vent" ? 4 : null,
            x: -1.5,
            y: 2,
            depth: 1,
          },
        });
      }
  });

  it("takes lengths typed in inches as millimetres, but not the slot count", () => {
    const parsed = parseSketch(draft({ width: "1", height: "0.5", x: "0.1", depth: "0.04" }), "vent", "cut", face, "in");
    if (!("body" in parsed)) throw new Error("expected a sketch");
    expect(parsed.body.width).toBeCloseTo(MM_PER_INCH);
    expect(parsed.body.height).toBeCloseTo(0.5 * MM_PER_INCH);
    expect(parsed.body.x).toBeCloseTo(0.1 * MM_PER_INCH);
    expect(parsed.body.depth).toBeCloseTo(0.04 * MM_PER_INCH);
    expect(parsed.body.count).toBe(4);
  });

  it("refuses wrong values field by field", () => {
    expect(parseSketch(draft({ width: "" }), "circle", "cut", face, "mm")).toEqual({ errors: { width: "Type a value." } });
    expect(parseSketch(draft({ depth: "x" }), "circle", "cut", face, "mm")).toEqual({ errors: { depth: "Type a number." } });
    expect(parseSketch(draft({ width: "0" }), "rectangle", "cut", face, "mm")).toEqual({ errors: { width: "More than 0." } });
    expect(parseSketch(draft({ count: "1" }), "vent", "cut", face, "mm")).toEqual({ errors: { count: "2 to 50 slots." } });
    expect(parseSketch(draft({ height: "9" }), "slot", "cut", face, "mm")).toEqual({
      errors: { height: "Less than the length." },
    });
    // A negative position is fine; a field the shape doesn't use is not read.
    expect("body" in parseSketch(draft({ x: "-3", height: "" }), "circle", "cut", face, "mm")).toBe(true);
  });

  it("starts from a shape sized to the face, centred", () => {
    const plane = { size: [40, 12] } as FacePlane;
    expect(defaultDraft(plane, "mm")).toMatchObject({ width: "3", height: "1.5", x: "0", y: "0", depth: "1" });
    expect(Number(defaultDraft(plane, "in").width)).toBeCloseTo(3 / MM_PER_INCH, 3);
  });

  it("sketches on exactly one picked face", () => {
    const f = { kind: "face" as const, id: "a", part: "p" };
    expect(sketchFace([f])).toEqual({ face: f });
    expect(sketchFace([])).toEqual({ reason: "Pick a flat face on the model" });
    expect(sketchFace([{ kind: "edge", id: "e", part: "p" }])).toEqual({ reason: "Pick a flat face on the model" });
    expect(sketchFace([f, { ...f, id: "b" }])).toEqual({ reason: "Pick only one face" });
  });
});

describe("sketched features in the tree", () => {
  it("are listed under their part, locked, at their line", () => {
    const index = indexFromGltf(gltf);
    const part = index[0].part;
    const last = buildSource.code.split("\n").length;
    const source = [...buildSource.features, { tag: "sketch_1", part, lines: [last, last] as [number, number], locked: true }];
    const tree = featureTree(index, source, []);
    const features = tree.find((p) => p.part === part)!.features;
    const sketched = features.find((f) => f.tag === "sketch_1")!;
    expect(sketched).toMatchObject({ locked: true, lines: [last, last], faces: [] });
    expect(features.filter((f) => f.locked)).toEqual([sketched]);
    // Without sketches, nothing is locked.
    const plain = featureTree(index, buildSource.features, []);
    expect(plain.flatMap((p) => p.features).some((f) => f.locked)).toBe(false);
  });
});

describe("fixture faces for the tests", () => {
  it("finds a full flat face and a curved one in the fixture mesh", () => {
    const faces = loadFixtureFaces();
    expect(faces.length).toBe(indexFromGltf(gltf).flatMap((p) => p.faces).length);
    const { flat, curved } = sketchTargets(faces);
    expect(flat.flat && !curved.flat).toBe(true);
  });
});
