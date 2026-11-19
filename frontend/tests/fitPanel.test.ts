import { describe, expect, it } from "vitest";
import { fitInside } from "../src/components/canvas/fitPanel";

// A popover anchored to a toolbar button stays inside the canvas: the change panel, 20 rem from
// the Changes button, ran past a 390 px phone screen (QA brief 5).

const phone = { left: 0, right: 390 };

describe("fitInside", () => {
  it("leaves a panel that fits where it is", () => {
    expect(fitInside({ left: 100, width: 320 }, { left: 0, right: 1280 })).toEqual({ dx: 0, maxWidth: 1264 });
  });

  it("moves a panel that runs past the right edge back inside, by the overflow and the margin", () => {
    // The change panel on a phone: 320 px from the Changes button at x = 150.
    const fit = fitInside({ left: 150, width: 320 }, phone);
    expect(fit.dx).toBe(390 - 8 - (150 + 320));
    expect(150 + fit.dx + 320).toBe(382);
  });

  it("narrows a panel wider than the bounds and puts it at the left margin", () => {
    const fit = fitInside({ left: 40, width: 400 }, { left: 0, right: 300 });
    expect(fit.maxWidth).toBe(284);
    expect(40 + fit.dx).toBe(8);
  });

  it("never moves a panel past the left edge", () => {
    const fit = fitInside({ left: 20, width: 380 }, phone);
    expect(20 + fit.dx).toBeGreaterThanOrEqual(8);
    expect(20 + fit.dx + Math.min(380, fit.maxWidth)).toBeLessThanOrEqual(382);
  });
});
