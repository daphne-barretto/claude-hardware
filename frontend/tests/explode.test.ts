import { describe, expect, it } from "vitest";
import { locationPart } from "@/components/canvas/explode";
import { indexFromGltf } from "@/components/canvas/selection";
import type { CheckReport } from "@/components/canvas/api";
import { readRepoFile } from "./fixtures";

// Check locations on the parts. The assembly comes from the mesh fixture (generated from
// demos/b-parasite/ and standards.yaml), never from here.
type Gltf = {
  nodes: { mesh?: number; extras?: { part?: string } }[];
  meshes: { primitives: { attributes: { POSITION: number } }[] }[];
  accessors: { min?: number[]; max?: number[] }[];
};
const gltf = JSON.parse(readRepoFile("fixtures/enclosure.gltf")) as Gltf;
const report = JSON.parse(readRepoFile("fixtures/checks.json")) as CheckReport;

describe("check locations", () => {
  it("puts every located check result on a part of the mesh, so its pin moves with that part", () => {
    const index = indexFromGltf(gltf as never);
    const parts = index.map((p) => p.part);
    for (const r of report.results) {
      if (!r.location) continue;
      const part = locationPart(r.location, index);
      expect(parts, r.id).toContain(part);
      if (r.location.part) expect(part).toBe(r.location.part);
    }
    expect(locationPart({ part: null, faces: ["no-such-face"] }, index)).toBeNull();
  });
});
