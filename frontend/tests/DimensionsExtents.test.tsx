import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { Dimensions } from "@/components/canvas/Dimensions";
import type { Parameter, Point3 } from "@/components/canvas/parameters";
import type { Parameters } from "@/components/canvas/useParameterEdits";
import type { LengthUnit } from "@/components/canvas/units";

afterEach(cleanup);

// Dimensions on: the enclosure's overall width, depth and height are drawn, read-only, even when
// no parameter's value is one of them (an imported case whose parameters are a wall, a draft, z
// levels...). A parameter whose dimension already measures an overall extent keeps that axis.
// Made-up values, no scenario data.
const BOUNDS = { min: [-10, -20, 0] as Point3, max: [30, 25, 12] as Point3 }; // 40 × 45 × 12 mm

// A simple oblique view: x right, y up and to the right, z up (CSS pixels, y down).
const project = ([x, y, z]: Point3): [number, number] => [400 + 5 * x + 2 * y, 300 - 5 * z - 2 * y];

const wall: Parameter = { name: "wall", value: 2, unit: "mm", locked: false };
const boss: Parameter = {
  name: "boss_radius",
  value: 3,
  unit: "mm",
  locked: false,
  dimension: { kind: "radius", start: { point: [0, 0, 5] }, end: { point: [3, 0, 5] } },
};
const height: Parameter = {
  name: "outside_height",
  value: 12,
  unit: "mm",
  locked: false,
  dimension: { kind: "distance", start: { point: [30, -20, 0] }, end: { point: [30, -20, 12] } },
};

function parameters(params: Parameter[]): Parameters {
  return {
    params,
    loadError: null,
    saving: null,
    errors: {},
    setError: vi.fn(),
    notice: null,
    rebuild: null,
    edit: vi.fn(async () => true),
    unlock: vi.fn(async () => {}),
  };
}

function show(params: Parameter[], unit: LengthUnit = "mm") {
  return render(
    <Dimensions
      parameters={parameters(params)}
      unit={unit}
      project={project}
      layout={() => ({ size: [1200, 800], panels: [] })}
      hidden={() => false}
      dragging={null}
      onEdited={() => {}}
      bounds={BOUNDS}
    />,
  );
}

const extents = (c: HTMLElement) =>
  [...c.querySelectorAll<HTMLElement>("div[data-extent]")].map((e) => [e.dataset.extent, Number(e.dataset.length), e.textContent]);

describe("overall dimensions", () => {
  it("draws width, depth and height when no parameter measures them", () => {
    const { container } = show([wall, boss]);
    expect(extents(container)).toEqual([
      ["width", 40, "40.0 mm"],
      ["depth", 45, "45.0 mm"],
      ["height", 12, "12.0 mm"],
    ]);
    // A line for each, beside the parameter's own radius.
    expect(container.querySelectorAll("g[data-extent]")).toHaveLength(3);
    expect(container.querySelector('g[data-dimension="boss_radius"]')).not.toBeNull();
    // Read-only: nothing to click for them, only the radius.
    expect(screen.getAllByRole("button").map((b) => b.getAttribute("aria-label"))).toEqual(["Edit boss_radius"]);
    expect(screen.getByRole("note", { name: "Overall width 40.0 mm" })).toBeTruthy();
  });

  it("doesn't draw an axis a parameter's dimension already measures", () => {
    const { container } = show([wall, height]);
    expect(extents(container).map(([name]) => name)).toEqual(["width", "depth"]);
    expect(container.querySelector('g[data-dimension="outside_height"]')).not.toBeNull();
  });

  it("labels them in the display unit", () => {
    const { container } = show([wall], "in");
    expect(extents(container).map(([, , text]) => text)).toEqual(["1.575 in", "1.772 in", "0.472 in"]);
  });

  it("runs each line along its axis, outside the box", () => {
    const { container } = show([wall]);
    const line = (name: string) => {
      const g = container.querySelector<SVGGElement>(`g[data-extent="${name}"]`)!;
      return [g.dataset.start!, g.dataset.end!].map((s) => s.split(",").map(Number));
    };
    // Width: 40 mm × 5 px along screen x.
    const [a, b] = line("width");
    expect(Math.hypot(b[0] - a[0], b[1] - a[1])).toBeCloseTo(200);
    const [c, d] = line("height");
    expect(d[0] - c[0]).toBeCloseTo(0);
    expect(Math.abs(d[1] - c[1])).toBeCloseTo(60);
  });
});
