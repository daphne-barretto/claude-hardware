import * as THREE from "three";
import { describe, expect, it } from "vitest";
import type { CheckReport } from "@/components/canvas/api";
import {
  checkLabel,
  evidence,
  formatRequired,
  formatValue,
  locatedResults,
  PIN_GAP,
  spreadPins,
  untestedResults,
} from "@/components/canvas/checks";
import { MM_PER_INCH } from "@/components/canvas/units";
import { boardToWorld } from "@/components/canvas/viewer";
import { readRepoFile } from "./fixtures";

// The check results fixture (contracts/check-results.schema.json), generated from demos/b-parasite/ and standards.yaml.
const report = JSON.parse(readRepoFile("fixtures/checks.json")) as CheckReport;

describe("check results on the model", () => {
  it("pins every tested result that has a location, and lists every untested one", () => {
    const located = locatedResults(report);
    const untested = untestedResults(report);
    for (const r of report.results) {
      if (r.status === "untested") expect(untested).toContain(r);
      else if (r.location) expect(located).toContain(r);
    }
    expect(untested).toHaveLength(report.summary.untested);
    expect(located.filter((r) => r.status === "fail")).toHaveLength(report.summary.fail);
    expect(located.filter((r) => r.status === "pass")).toHaveLength(report.summary.pass);
  });

  it("labels results with their component when they have one", () => {
    const withComponent = report.results.find((r) => r.component)!;
    expect(checkLabel(withComponent)).toBe(`${withComponent.title} · ${withComponent.component}`);
    expect(checkLabel({ title: "T", component: null })).toBe("T");
  });

  it("shows every fixture failure's measured value and limit", () => {
    for (const r of report.results.filter((x) => x.status === "fail")) {
      const text = evidence(r, "mm")!;
      expect(text).toMatch(/^Measured .+ · (limit|min|max|requires) .+/);
    }
  });
});

describe("value formatting", () => {
  it("converts lengths, areas and volumes to inches, and leaves angles and text alone", () => {
    expect(formatValue({ value: 3.0874, unit: "mm" }, "mm")).toBe("3.087 mm");
    expect(formatValue({ value: MM_PER_INCH, unit: "mm" }, "in")).toBe("1 in");
    expect(formatValue({ value: MM_PER_INCH ** 2, unit: "mm2" }, "in")).toBe("1 in²");
    expect(formatValue({ value: MM_PER_INCH ** 3, unit: "mm3" }, "in")).toBe("1 in³");
    expect(formatValue({ value: 1.1, unit: "mm3" }, "mm")).toBe("1.1 mm³");
    expect(formatValue({ value: 1, unit: "deg" }, "in")).toBe("1°");
    expect(formatValue({ value: 0 }, "in")).toBe("0");
    expect(formatValue({ value: "molded ABS" }, "in")).toBe("molded ABS");
  });

  it("states each kind of limit", () => {
    expect(formatRequired({ kind: "tolerance", value: 0.2, unit: "mm" }, "mm")).toBe("limit ±0.2 mm");
    expect(formatRequired({ kind: "min", value: 2, unit: "mm" }, "mm")).toBe("min 2 mm");
    expect(formatRequired({ kind: "max", value: 0 }, "mm")).toBe("max 0");
    expect(formatRequired({ kind: "spec", value: "x" }, "mm")).toBe("requires x");
  });
});

describe("boardToWorld", () => {
  it("matches the mesh root's −90° rotation about X", () => {
    const root = new THREE.Object3D();
    root.quaternion.set(-Math.SQRT1_2, 0, 0, Math.SQRT1_2);
    root.updateMatrixWorld();
    const p: [number, number, number] = [1, 2, 3];
    const expected = new THREE.Vector3(...p).applyMatrix4(root.matrixWorld);
    const got = boardToWorld(p);
    expect(got.x).toBeCloseTo(expected.x);
    expect(got.y).toBeCloseTo(expected.y);
    expect(got.z).toBeCloseTo(expected.z);
  });
});

describe("pin layout", () => {
  const apart = (a: { x: number; y: number }, b: { x: number; y: number }) =>
    Math.abs(a.x - b.x) >= PIN_GAP || Math.abs(a.y - b.y) >= PIN_GAP;

  it("leaves pins that don't overlap where they are", () => {
    const pins = [
      { id: "a", at: { x: 10, y: 10 } },
      { id: "b", at: { x: 100, y: 10 } },
    ];
    const out = spreadPins(pins);
    for (const p of pins) expect(out.get(p.id)).toEqual(p.at);
  });

  it("spreads pins on the same or nearby points so none covers another, keeping the first in place", () => {
    const pins = [
      { id: "first", at: { x: 50, y: 50 } },
      { id: "same", at: { x: 50, y: 50 } },
      { id: "near", at: { x: 55, y: 48 } },
      { id: "also-same", at: { x: 50, y: 50 } },
    ];
    const out = spreadPins(pins);
    expect(out.get("first")).toEqual({ x: 50, y: 50 });
    const placed = pins.map((p) => out.get(p.id)!);
    for (let i = 0; i < placed.length; i++)
      for (let j = i + 1; j < placed.length; j++) expect(apart(placed[i], placed[j])).toBe(true);
    // Each moves only as far as it needs to.
    for (const [i, p] of pins.entries()) expect(Math.abs(placed[i].x - p.at.x)).toBeLessThanOrEqual(2 * PIN_GAP);
  });
});
