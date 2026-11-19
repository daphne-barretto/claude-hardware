import { describe, expect, it } from "vitest";
import { placeNear } from "@/components/canvas/fitPanel";

// A card anchored to a point on the model (a comment thread, a check's card) stays inside the
// canvas between its top bar and its bottom bar, so its options can be reached: a thread pinned
// low on a short canvas ran under the checks strip and the Layout bar (~1075 x 660 window).
const band = { left: 240, top: 158, right: 1073, bottom: 556 }; // the canvas less its bars
const card = { width: 300, height: 420 }; // a thread with Claude's question and its options

const inside = (p: { left: number; top: number; maxHeight: number }, size: { width: number; height: number }) => {
  const height = Math.min(size.height, p.maxHeight);
  return p.left >= band.left && p.left + size.width <= band.right && p.top >= band.top && p.top + height <= band.bottom;
};

describe("a card anchored to a point on the model", () => {
  it("sits beside the point, on its right where it fits", () => {
    const p = placeNear({ x: 500, y: 250 }, { width: 300, height: 200 }, band);
    expect(p.left).toBe(522);
    expect(p.top).toBe(210);
    expect(inside(p, { width: 300, height: 200 })).toBe(true);
  });

  it("flips to the left of a point near the right edge", () => {
    const p = placeNear({ x: 1000, y: 250 }, { width: 300, height: 200 }, band);
    expect(p.left + 300).toBeLessThanOrEqual(1000);
    expect(inside(p, { width: 300, height: 200 })).toBe(true);
  });

  it("moves up, or above the point, so it ends above the bottom bar", () => {
    const p = placeNear({ x: 790, y: 330 }, { width: 300, height: 200 }, band);
    expect(p.top + 200).toBeLessThanOrEqual(band.bottom);
    expect(inside(p, { width: 300, height: 200 })).toBe(true);
    // A card below its point (a check's card) goes above the point when there is no room below.
    const below = placeNear({ x: 600, y: 520 }, { width: 300, height: 120 }, band, { gap: 20, lift: -16 });
    expect(below.top + 120).toBeLessThanOrEqual(520 - 20);
  });

  it("is never taller than the canvas between its bars: its body scrolls", () => {
    const p = placeNear({ x: 790, y: 330 }, card, band);
    expect(p.maxHeight).toBe(band.bottom - band.top - 16);
    expect(inside(p, card)).toBe(true);
  });

  it("narrows to a canvas narrower than itself", () => {
    const narrow = { left: 0, top: 50, right: 280, bottom: 600 };
    const p = placeNear({ x: 100, y: 200 }, { width: 300, height: 200 }, narrow);
    expect(p.maxWidth).toBe(264);
    expect(p.left).toBe(8);
  });
});
