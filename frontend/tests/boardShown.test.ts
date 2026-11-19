import { describe, expect, it } from "vitest";
import { boardShown } from "@/components/canvas/inspect";

const base = { xray: false, section: false, enclosureHidden: false };

describe("boardShown", () => {
  // Regression: a board longer than its case (b-parasite's soil probe) vanished in the
  // assembled view, which showed a closed box with nothing coming out of the slot.
  it("draws the board in the assembled view", () => {
    expect(boardShown({ ...base, layout: "assembled" })).toBe(true);
  });

  it("draws it exploded and while inspecting", () => {
    expect(boardShown({ ...base, layout: "exploded" })).toBe(true);
    expect(boardShown({ ...base, layout: "assembled", xray: true })).toBe(true);
    expect(boardShown({ ...base, layout: "assembled", section: true })).toBe(true);
  });

  it("leaves it off the print bed, which holds only the parts to print", () => {
    expect(boardShown({ ...base, layout: "print" })).toBe(false);
    expect(boardShown({ ...base, layout: "print", xray: true, enclosureHidden: true })).toBe(false);
  });
});
