import { describe, expect, it } from "vitest";
import { BOX_ON_HINT, BOX_TIP, PICK_TIPS, selectHint } from "@/components/canvas/selectHelp";

// The parts panel's Select filters and box select explain themselves: each says what a
// click picks, a hint says how to select until the first selection, and box select says how to
// use it and how to stop.
describe("selecting on the model", () => {
  it("says what a click picks in each mode", () => {
    expect(PICK_TIPS).toEqual({
      face: "Click a face on the model",
      edge: "Click an edge",
      part: "Click a whole part",
      component: "Click a board component",
    });
    expect(BOX_TIP).toBe("Drag a box on the model to select several");
  });

  it("hints how to select, in the mode's words, until something is selected", () => {
    expect(selectHint("face", false, false)).toBe("Click the model to select a face");
    expect(selectHint("edge", false, false)).toBe("Click the model to select an edge");
    expect(selectHint("part", false, false)).toBe("Click the model to select a part");
    expect(selectHint("component", false, false)).toBe("Click the model to select a component");
    expect(selectHint("face", false, true)).toBeNull();
  });

  it("says how to use box select and how to stop, whatever was selected before", () => {
    expect(selectHint("face", true, false)).toBe(BOX_ON_HINT);
    expect(selectHint("part", true, true)).toBe("Drag on the model · Esc to stop");
  });
});
