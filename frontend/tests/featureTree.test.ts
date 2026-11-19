import { describe, expect, it } from "vitest";
import type { BuildSource, Parameter } from "@/components/canvas/api";
import { formatParameter } from "@/components/canvas/HardwareCanvas";
import {
  featureKey,
  featurePicks,
  featureTree,
  linesLabel,
  pickedFeatures,
} from "@/components/canvas/featureTree";
import { indexFromGltf, type Picked } from "@/components/canvas/selection";
import { readRepoFile } from "./fixtures";

// The feature tree built from the fixtures (generated from demos/b-parasite/ and standards.yaml);
// no scenario values are written here.
const index = indexFromGltf(JSON.parse(readRepoFile("fixtures/enclosure.gltf")));
const source = JSON.parse(readRepoFile("fixtures/build-source.json")) as BuildSource;
const params = JSON.parse(readRepoFile("fixtures/parameters.json")) as Parameter[];
const tree = featureTree(index, source.features, params);
const nodes = tree.flatMap((p) => p.features);
const node = (part: string, tag: string) => nodes.find((n) => n.part === part && n.tag === tag);

describe("feature tree", () => {
  it("lists every part of the model, in mesh order", () => {
    expect(tree.slice(0, index.length).map((p) => p.part)).toEqual(index.map((p) => p.part));
  });

  it("lists every tagged feature on the model under its part, with all its faces", () => {
    for (const p of index) {
      for (const face of p.faces) expect(node(p.part, face.feature)?.faces).toContain(face.id);
    }
    const faces = nodes.flatMap((n) => n.faces).sort();
    expect(faces).toEqual(index.flatMap((p) => p.faces.map((f) => f.id)).sort());
  });

  it("lists every feature the code creates under its part, with its lines", () => {
    for (const f of source.features) {
      const n = node(f.part, f.tag);
      expect(n, `${f.part}/${f.tag}`).toBeDefined();
      expect(n!.lines).toEqual(f.lines);
      const lines = source.code.split("\n");
      expect(f.lines[0]).toBeGreaterThanOrEqual(1);
      expect(f.lines[1]).toBeLessThanOrEqual(lines.length);
    }
  });

  it("orders features as the code applied them", () => {
    for (const part of tree) {
      const inCode = source.features.filter((f) => f.part === part.part).map((f) => f.tag);
      expect(part.features.map((f) => f.tag).slice(0, inCode.length)).toEqual(inCode);
    }
  });

  it("shows each feature's parameters", () => {
    const driving = params.filter((p) => p.feature);
    expect(driving.length).toBeGreaterThan(0);
    for (const p of driving) {
      const owners = nodes.filter((n) => n.tag === p.feature);
      expect(owners.length, p.name).toBeGreaterThan(0);
      for (const n of owners) expect(n.params.map((x) => x.name)).toContain(p.name);
    }
    // Design-wide parameters belong to no feature.
    for (const n of nodes) expect(n.params.every((p) => p.feature === n.tag)).toBe(true);
  });

  it("gives each part the material the mesh names, or the design's material", () => {
    const design = params.find((p) => p.name === "material" && !p.feature);
    for (const p of tree) expect(p.material).toBe(design ? String(design.value) : null);
    const [first, ...rest] = index.map((p) => p.part);
    const named = featureTree(index, source.features, params, { [first]: "test-material" });
    expect(named.find((p) => p.part === first)!.material).toBe("test-material");
    for (const part of rest) expect(named.find((p) => p.part === part)!.material).toBe(tree.find((p) => p.part === part)!.material);
  });

  it("works without the code or the parameters", () => {
    const bare = featureTree(index, null, []);
    expect(bare.map((p) => p.part)).toEqual(index.map((p) => p.part));
    for (const p of bare) {
      expect(p.material).toBeNull();
      for (const f of p.features) {
        expect(f.lines).toBeNull();
        expect(f.faces.length).toBeGreaterThan(0);
      }
    }
  });

  it("selects a feature's faces, and a picked face or edge highlights its feature", () => {
    for (const n of nodes.filter((x) => x.faces.length)) {
      const picks = featurePicks(n);
      expect(picks.map((p) => p.id)).toEqual(n.faces);
      expect(pickedFeatures(index, picks)).toEqual(new Set([featureKey(n.part, n.tag)]));
    }
    for (const p of index) {
      for (const face of p.faces) {
        const pick: Picked = { kind: "face", id: face.id, part: p.part };
        expect(pickedFeatures(index, [pick])).toEqual(new Set([featureKey(p.part, face.feature)]));
      }
      for (const edge of p.edges) {
        const pick: Picked = { kind: "edge", id: edge.id, part: p.part };
        expect(pickedFeatures(index, [pick])).toEqual(new Set([featureKey(p.part, edge.feature)]));
      }
    }
    // A whole part highlights the part's row, not a feature.
    expect(pickedFeatures(index, [{ kind: "part", id: index[0].part, part: index[0].part }]).size).toBe(0);
  });

  it("follows a rebuild: new and removed features, moved lines", () => {
    const [part] = index;
    const renamed = index.map((p) =>
      p === part ? { ...p, faces: p.faces.map((f, i) => (i === 0 ? { ...f, feature: "rebuilt_feature" } : f)) } : p,
    );
    const moved = source.features.map((f) => ({ ...f, lines: [f.lines[0] + 1, f.lines[1] + 1] as [number, number] }));
    const next = featureTree(renamed, moved, params).flatMap((p) => p.features);
    expect(next.find((n) => n.tag === "rebuilt_feature")?.faces).toEqual([part.faces[0].id]);
    for (const f of moved) expect(next.find((n) => n.part === f.part && n.tag === f.tag)!.lines).toEqual(f.lines);
  });

  it("labels lines and formats parameters", () => {
    expect(linesLabel([7, 7])).toBe("L7");
    expect(linesLabel([7, 9])).toBe("L7–9");
    expect(formatParameter({ value: 25.4, unit: "mm" }, "in")).toBe("1.000 in");
    expect(formatParameter({ value: 2, unit: "mm" }, "mm")).toBe("2.0 mm");
    expect(formatParameter({ value: 1.5, unit: "deg" }, "mm")).toBe("1.5°");
    expect(formatParameter({ value: "text", unit: null }, "mm")).toBe("text");
  });
});
