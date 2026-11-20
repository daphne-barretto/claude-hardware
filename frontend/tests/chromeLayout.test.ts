import { describe, expect, it } from "vitest";
import { coverRects } from "@/components/canvas/CheckOverlay";
import { chromeVars, outsideBand, PANEL, PLACE } from "@/components/canvas/chromeLayout";
import { covered } from "@/components/canvas/checks";

// The canvas layout model (chromeLayout.ts) places every panel over the 3D view, and what the
// check pins and the dimension labels keep out of (fix briefs 10 to 13, 17).

const box = (el: Element, [left, top, width, height]: number[]) => {
  el.getBoundingClientRect = () => ({ left, top, width, height, right: left + width, bottom: top + height, x: left, y: top, toJSON: () => ({}) }) as DOMRect;
};

describe("the panel columns", () => {
  it("place tool panels between the side columns, over the view cube, above the inspection panels", () => {
    for (const cls of ["left-[var(--chrome-left)]", "right-[var(--chrome-right)]", "top-[var(--chrome-top)]", "bottom-[var(--chrome-bottom)]", "z-20"])
      expect(PLACE.tools.split(" ")).toContain(cls);
    expect(PLACE.inspection.split(" ")).toContain("top-[var(--chrome-below-cube)]");
    expect(PLACE.inspection.split(" ")).toContain("z-10");
  });

  it("start at the left margin on a canvas too narrow for a panel beside the parts panel", () => {
    for (const column of [PLACE.tools, PLACE.inspection]) expect(column).toContain("@max-[30rem]:left-[var(--chrome-margin)]");
    expect(chromeVars({ parts: true })).toMatchObject({ "--chrome-margin": "0.75rem" });
  });

  it("let clicks through to the model around a panel, and a panel scrolls inside rather than overflow", () => {
    expect(PLACE.tools).toContain("pointer-events-none");
    expect(PANEL.split(" ")).toEqual(expect.arrayContaining(["pointer-events-auto", "absolute", "max-h-full", "max-w-full", "min-h-0"]));
  });

  it("keep the dimensions to the band between the top and bottom bars", () => {
    expect(PLACE.dimensions.split(" ")).toEqual(expect.arrayContaining(["top-[var(--chrome-top)]", "bottom-[var(--chrome-bottom)]", "overflow-hidden"]));
  });
});

describe("outsideBand", () => {
  it("is the canvas above and below the dimensions' band, relative to the 3D view", () => {
    const root = document.createElement("div");
    const band = document.createElement("div");
    band.setAttribute("data-chrome-band", "");
    root.append(band);
    box(band, [500, 156, 400, 590]);
    const origin = { left: 500, top: 100, width: 400, height: 750 } as DOMRect;
    expect(outsideBand(root, origin)).toEqual([
      { x: 0, y: 0, width: 400, height: 56 },
      { x: 0, y: 646, width: 400, height: 104 },
    ]);
  });

  it("is nothing without the band", () => {
    expect(outsideBand(document.createElement("div"), { left: 0, top: 0, width: 400, height: 700 } as DOMRect)).toEqual([]);
  });
});

describe("what check pins stay under", () => {
  const canvas = () => {
    const root = document.createElement("div");
    const layer = document.createElement("div");
    root.append(layer);
    box(layer, [100, 50, 600, 700]);
    const add = (attrs: Record<string, string>, rect: number[]) => {
      const el = document.createElement("div");
      for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
      box(el, rect);
      root.append(el);
      return el;
    };
    return { root, layer, add };
  };

  it("are the panels and open dialogs, in the pin layer's coordinates, not the check's own card", () => {
    const { layer, add } = canvas();
    add({ "data-pin-cover": "" }, [112, 106, 224, 450]);
    add({ role: "dialog", "aria-label": "Keyboard shortcuts" }, [300, 106, 288, 300]);
    add({ role: "dialog", "data-testid": "check-card" }, [400, 400, 300, 120]);
    add({ role: "dialog" }, [0, 0, 0, 0]);
    expect(coverRects(layer)).toEqual([
      { x: 12, y: 56, width: 224, height: 450 },
      { x: 200, y: 56, width: 288, height: 300 },
    ]);
  });

  it("hide a pin any part of which is under one", () => {
    const sheet = { x: 200, y: 56, width: 288, height: 300 };
    expect(covered({ x: 300, y: 200 }, [sheet], 10)).toBe(true);
    // Its centre outside, its edge over the sheet.
    expect(covered({ x: 195, y: 200 }, [sheet], 10)).toBe(true);
    expect(covered({ x: 185, y: 200 }, [sheet], 10)).toBe(false);
  });
});
