import { describe, expect, it } from "vitest";
import {
  cssColor,
  fixRequest,
  legendGradient,
  legendPosition,
  mapColor,
  NOT_APPLICABLE,
  SCALE,
  vertexColors,
  type AnalysisMap,
} from "@/components/canvas/colormap";
import { formatMapValue } from "@/components/canvas/HardwareCanvas";
import { MM_PER_INCH } from "@/components/canvas/units";
import { readRepoFile } from "./fixtures";

// The analysis maps and mesh come from the fixture files (generated from demos/b-parasite/ and standards.yaml).
type Gltf = {
  nodes: { mesh?: number; extras?: { part?: string; faces?: { id: string }[] } }[];
  meshes: { primitives: { mode?: number; attributes: Record<string, number> }[] }[];
  accessors: { count: number }[];
};
const mesh = JSON.parse(readRepoFile("fixtures/enclosure.gltf")) as Gltf;
const build = JSON.parse(readRepoFile("fixtures/build.json")) as { maps: string[] };
const maps = build.maps.map((t) => JSON.parse(readRepoFile(`fixtures/maps/${t}.json`)) as AnalysisMap);

/** Vertex count of each part's triangle primitive. */
const partVertices = Object.fromEntries(
  mesh.nodes.flatMap((n) => {
    if (!n.extras?.part || n.mesh === undefined) return [];
    const tri = mesh.meshes[n.mesh].primitives.find((p) => (p.mode ?? 4) === 4)!;
    return [[n.extras.part, mesh.accessors[tri.attributes.POSITION].count]];
  }),
);
const faceIds = new Set(mesh.nodes.flatMap((n) => n.extras?.faces?.map((f) => f.id) ?? []));

describe("color scale", () => {
  const legend = { min: 0, max: 10, unit: "mm" as const };

  it("puts the legend ends on the scale ends and clamps values outside", () => {
    expect(mapColor(legend.min, legend)).toEqual(SCALE[0].color);
    expect(mapColor(legend.max, legend)).toEqual(SCALE[SCALE.length - 1].color);
    expect(mapColor(legend.min - 5, legend)).toEqual(SCALE[0].color);
    expect(mapColor(legend.max + 5, legend)).toEqual(SCALE[SCALE.length - 1].color);
    expect(mapColor(null, legend)).toEqual(NOT_APPLICABLE);
  });

  it("hits every stop of the legend gradient at its position", () => {
    const gradient = legendGradient();
    for (const stop of SCALE) {
      const value = legend.min + stop.at * (legend.max - legend.min);
      mapColor(value, legend).forEach((c, i) => expect(c).toBeCloseTo(stop.color[i]));
      expect(legendPosition(value, legend)).toBeCloseTo(stop.at);
      expect(gradient).toContain(`${cssColor(stop.color)} ${stop.at * 100}%`);
    }
  });

  it("formats lengths in the display unit and angles in degrees", () => {
    expect(formatMapValue(MM_PER_INCH, "mm", "in")).toBe("1.000 in");
    expect(formatMapValue(2, "mm", "mm")).toBe("2.0 mm");
    expect(formatMapValue(1.5, "deg", "in")).toBe("1.5°");
  });
});

describe.each(maps.map((m) => [m.type, m] as const))("the %s fixture map", (_, map) => {
  it("has one value per vertex of each part and colors every vertex by its value", () => {
    for (const { part, values } of map.parts) {
      const colors = vertexColors(values, partVertices[part], map.legend);
      expect(colors, part).not.toBeNull();
      values.forEach((v, i) => expect(Array.from(colors!.slice(i * 3, i * 3 + 3))).toEqual(
        mapColor(v, map.legend).map((c) => Math.fround(c)),
      ));
    }
    expect(vertexColors(map.parts[0].values, map.parts[0].values.length + 1, map.legend)).toBeNull();
  });

  it("has a legend whose limit is inside its range", () => {
    const { min, max, limit } = map.legend;
    expect(max).toBeGreaterThan(min);
    if (limit != null) expect(legendPosition(limit, map.legend)).toBeCloseTo((limit - min) / (max - min));
  });

  it("turns each flagged spot into a fix request that selects its faces", () => {
    for (const spot of map.flagged ?? []) {
      const req = fixRequest(map, spot);
      expect(req.text).toContain(spot.message);
      // The standard by its title, never its ID (QA fix brief 2).
      expect(req.text).toContain(map.standard_title!);
      expect(req.text).not.toContain(map.standard);
      expect(req.selection.faces).toEqual(spot.location.faces);
      expect(req.selection.point).toEqual(spot.location.point);
      expect(req.selection.part).toBe(spot.location.part ?? null);
      for (const f of req.selection.faces) expect(faceIds.has(f), f).toBe(true);
    }
  });
});

it("the fixture build offers both map types with flagged spots", () => {
  expect(maps.map((m) => m.type).sort()).toEqual(["draft", "wall_thickness"]);
  expect(maps.some((m) => (m.flagged ?? []).length > 0)).toBe(true);
});
