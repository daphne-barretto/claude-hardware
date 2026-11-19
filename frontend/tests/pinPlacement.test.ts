import { describe, expect, it } from "vitest";
import { clearOf, covered, PIN_CLEARANCE, PIN_GAP, spreadPins } from "@/components/canvas/checks";

// Pins under a panel move to its edge, inside the view, clear of other pins.
const panel = { x: 10, y: 50, width: 200, height: 400 };
const cube = { x: 500, y: 50, width: 120, height: 120 };
const view = { width: 640, height: 600 };

describe("pin placement", () => {
  it("leaves a pin that no panel covers where it is", () => {
    expect(clearOf({ x: 300, y: 300 }, [panel, cube], view)).toEqual({ x: 300, y: 300 });
  });

  it("moves a pin under a panel to the panel's nearest edge", () => {
    const p = clearOf({ x: 190, y: 300 }, [panel], view);
    expect(p).toEqual({ x: panel.x + panel.width + PIN_CLEARANCE, y: 300 });
    expect(covered(p, [panel])).toBe(false);
  });

  it("prefers an edge inside the view over a nearer one cut off by the view's edge", () => {
    const p = clearOf({ x: 615, y: 110 }, [cube], view);
    expect(covered(p, [cube])).toBe(false);
    expect(p.x).toBeLessThanOrEqual(view.width - PIN_CLEARANCE);
  });

  it("spreads pins moved to the same edge so none covers another or a panel", () => {
    const pins = [0, 1, 2].map((i) => ({ id: `p${i}`, at: { x: 100, y: 200 } }));
    const out = [...spreadPins(pins, [panel], view).values()];
    for (const p of out) expect(covered(p, [panel])).toBe(false);
    for (let i = 0; i < out.length; i++)
      for (let j = i + 1; j < out.length; j++)
        expect(Math.abs(out[i].x - out[j].x) >= PIN_GAP || Math.abs(out[i].y - out[j].y) >= PIN_GAP).toBe(true);
  });
});
