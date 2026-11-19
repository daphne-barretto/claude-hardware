import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import {
  dimensionText,
  dragDistance,
  draggedBy,
  draggedValue,
  DRAG_STEP_MM,
  placeLabels,
  type Parameter,
} from "@/components/canvas/parameters";
import { MM_PER_INCH } from "@/components/canvas/units";

// Dimensions on the model and face drag. Scenario values come from the fixtures.
const fixture = <T>(name: string) =>
  JSON.parse(readFileSync(path.join(__dirname, "../../fixtures", name), "utf8")) as T;
const PARAMS = fixture<Parameter[]>("parameters.json");
const draggable = PARAMS.find((p) => p.drag)!;
const radius = PARAMS.find((p) => p.dimension?.kind === "radius");
const distance = PARAMS.find((p) => p.dimension?.kind === "distance")!;

describe("which faces drag", () => {
  it("a face named by a parameter's drag drags that parameter", () => {
    for (const p of PARAMS.filter((q) => q.drag))
      for (const face of p.drag!.faces) expect(draggedBy(PARAMS, face)?.name).toBe(p.name);
  });

  it("faces of other features don't drag", () => {
    const dragged = new Set(PARAMS.flatMap((p) => p.drag?.faces ?? []));
    const others = PARAMS.flatMap((p) => (p.dimension ? [p.dimension.start.face, p.dimension.end.face] : [])).filter(
      (f): f is string => !!f && !dragged.has(f),
    );
    expect(others.length).toBeGreaterThan(0);
    for (const face of others) expect(draggedBy(PARAMS, face)).toBeNull();
    expect(draggedBy(PARAMS, "no.such_face")).toBeNull();
  });
});

describe("drag distance", () => {
  it("counts only the part of the pointer move along the axis as drawn on screen", () => {
    // 4 px per mm to the right.
    expect(dragDistance([4, 0], 20, 0)).toBeCloseTo(5);
    expect(dragDistance([4, 0], 20, 100)).toBeCloseTo(5);
    expect(dragDistance([4, 0], -8, 0)).toBeCloseTo(-2);
    // Diagonal axis: 3 px right and 4 px down per mm.
    expect(dragDistance([3, 4], 30, 40)).toBeCloseTo(10);
  });

  it("can't drag along an axis that points at the viewer", () => {
    expect(dragDistance([0, 0], 10, 10)).toBeNull();
  });
});

describe("dragged value", () => {
  const value = draggable.value as number;

  it("adds the distance and snaps to the drag step", () => {
    expect(draggedValue(draggable, 2.04, "mm")).toEqual({
      value: Number((Math.round((value + 2.04) / DRAG_STEP_MM) * DRAG_STEP_MM).toFixed(3)),
    });
    expect(draggedValue(draggable, -1, "mm")).toEqual({ value: Number((value - 1).toFixed(3)) });
  });

  it("ignores a drag too short to change the value", () => {
    expect(draggedValue({ ...draggable, value: 10 }, DRAG_STEP_MM / 4, "mm")).toBeNull();
  });

  it("refuses a value at or below zero, and outside min and max in the shown unit", () => {
    expect(draggedValue(draggable, -value - 1, "mm")).toEqual({ error: "Too small." });
    expect(draggedValue({ ...draggable, max: value + 1 }, 5, "mm")).toEqual({ error: `At most ${value + 1} mm.` });
    const inches = draggedValue({ ...draggable, max: MM_PER_INCH * 2 }, MM_PER_INCH * 3, "in");
    expect(inches).toEqual({ error: "At most 2 in." });
  });

  it("text values don't drag", () => {
    expect(draggedValue({ value: "ABS", unit: null }, 3, "mm")).toBeNull();
  });
});

describe("dimension text", () => {
  it("shows the value in the chosen unit, with R for a radius", () => {
    expect(dimensionText(distance, "mm")).toBe(`${Number((distance.value as number).toFixed(3))} mm`);
    expect(dimensionText(distance, "in")).toBe(`${Number(((distance.value as number) / MM_PER_INCH).toFixed(4))} in`);
    expect(dimensionText(distance, "mm", 12)).toBe("12 mm");
    if (radius) expect(dimensionText(radius, "mm")).toMatch(/^R[\d.]+ mm$/);
  });
});

describe("label placement", () => {
  const size: [number, number] = [40, 20];
  const overlaps = (p: [number, number], q: [number, number]) =>
    Math.abs(p[0] - q[0]) < size[0] && Math.abs(p[1] - q[1]) < size[1];

  it("puts a label in the middle of its line when it is free", () => {
    expect(
      placeLabels(
        [
          [
            [0, 0],
            [100, 0],
          ],
        ],
        size,
      ),
    ).toEqual([{ at: [50, 0], anchor: [50, 0] }]);
  });

  it("moves a label along its line so labels don't cover each other", () => {
    const [a, b] = placeLabels(
      [
        [
          [0, 0],
          [200, 0],
        ],
        [
          [100, -100],
          [100, 100],
        ],
      ],
      size,
    );
    expect(a.at).toEqual([100, 0]);
    expect(overlaps(a.at, b.at)).toBe(false);
    expect(b.at[0]).toBe(100);
    expect(b.anchor).toEqual(b.at);
  });

  it("moves a label off a short line that another label covers, with no panel to go beside", () => {
    const [a, b] = placeLabels(
      [
        [
          [0, 100],
          [200, 100],
        ],
        [
          [100, 98],
          [100, 122],
        ],
      ],
      size,
      [{ x: 300, y: 200, width: 20, height: 20 }],
      [400, 300],
    );
    expect(a.at).toEqual([100, 100]);
    expect(overlaps(a.at, b.at)).toBe(false);
    expect(b.anchor).toEqual([100, 110]);
  });

  it("keeps labels out from under the panels over the canvas, and inside it", () => {
    const panel = { x: 0, y: 0, width: 100, height: 300 };
    // Half the line is under the panel: the label slides to the free half.
    const [slid] = placeLabels(
      [
        [
          [50, 100],
          [250, 100],
        ],
      ],
      size,
      [panel],
      [400, 300],
    );
    expect(slid.at[0] - size[0] / 2).toBeGreaterThanOrEqual(100);
    expect(slid.anchor).toEqual(slid.at);
    // The whole line is under it: the label goes beside the panel, pointing back to the line.
    const [beside, next] = placeLabels(
      [
        [
          [20, 100],
          [80, 100],
        ],
        [
          [30, 100],
          [70, 100],
        ],
      ],
      size,
      [panel],
      [400, 300],
    );
    expect(beside.anchor).toEqual([50, 100]);
    expect(beside.at[0] - size[0] / 2).toBeGreaterThanOrEqual(100);
    expect(beside.at[1]).toBe(100);
    // A second one there steps up or down instead of covering the first.
    expect(next.at[0] - size[0] / 2).toBeGreaterThanOrEqual(100);
    expect(overlaps(beside.at, next.at)).toBe(false);
  });
});
