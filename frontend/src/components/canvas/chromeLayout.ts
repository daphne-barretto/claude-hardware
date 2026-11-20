import type { CSSProperties } from "react";

/**
 * One layout model for the canvas chrome, so nothing over the 3D view overlaps at any canvas size:
 *
 *   ┌ top bar: revision · compare · changes · units · controls (icons scroll inside, zoom fixed) ┐
 *   │ parts panel      │ tool panels: parameters, from the   │ view cube                │
 *   │ (scrolls inside) │ top down, over the view cube;       │                          │
 *   │                  │ inspection panels: section,         │                          │
 *   │ view label,      │ bottom-up, below the cube           │                          │
 *   │ parts, size      │                                     │                          │
 *   └ bottom bar: comment tools (layout and section are in the top bar) ─────────────┘
 *
 * The regions are CSS variables on the canvas root; each piece of chrome is placed against them
 * instead of against a guessed size of its neighbours. Panels shrink and scroll inside, labels
 * truncate; the canvas itself never scrolls (`overflow: clip`). On a canvas too narrow for a
 * panel between the side columns (a phone), the panel columns start at the left margin, over the
 * parts panel. The dimensions on the model keep to the band between the top and bottom bars.
 *
 * Stacking, bottom to top: the 3D view, check pins (hidden where a panel or a dialog is over
 * them: `PIN_COVER`), the dimensions, the side columns and bars, the inspection panels (z-10),
 * the tool panels (z-20), dialogs and the check card (z-30), menus (z-40).
 */
export const CHROME = {
  /** Below the top bar (12 px padding + a 32 px row + a gap). */
  top: "3.5rem",
  /** Above the bottom bar (12 px margin + a 32 px row + a gap). */
  bottom: "3.5rem",
  /** Where the bottom bar sits: the canvas's bottom margin. */
  bar: "0.75rem",
  /** Below the view cube: where the right-hand column starts. */
  belowCube: "13rem",
  /** Right of the parts panel (0.75 rem margin + 14 rem panel + a gap). */
  left: "15.25rem",
  margin: "0.75rem",
} as const;

/** The CSS variables for the canvas root, given whether the parts panel is shown. */
export function chromeVars({ parts }: { parts: boolean }): CSSProperties {
  return {
    "--chrome-margin": CHROME.margin,
    "--chrome-top": CHROME.top,
    "--chrome-bottom": CHROME.bottom,
    "--chrome-bar": CHROME.bar,
    "--chrome-below-cube": CHROME.belowCube,
    "--chrome-left": parts ? CHROME.left : CHROME.margin,
    "--chrome-right": CHROME.margin,
  } as CSSProperties;
}

/**
 * Where each column of panels goes, as Tailwind classes against the variables above (literal,
 * so Tailwind sees them). Each column lets clicks through to the 3D view; the panel in it takes
 * them (`PANEL`). Below 30 rem of canvas (a phone) the columns start at the left margin.
 */
export const PLACE = {
  /** Tool panels opened from a toolbar: parameters. Top down, over the view cube. */
  tools:
    "pointer-events-none absolute bottom-[var(--chrome-bottom)] left-[var(--chrome-left)] right-[var(--chrome-right)] top-[var(--chrome-top)] z-20 flex flex-col items-start @max-[30rem]:left-[var(--chrome-margin)]",
  /** Inspection panels: section. Bottom up, below the view cube. */
  inspection:
    "pointer-events-none absolute bottom-[var(--chrome-bottom)] left-[var(--chrome-left)] right-[var(--chrome-right)] top-[var(--chrome-below-cube)] z-10 flex flex-col items-end justify-end @max-[30rem]:left-[var(--chrome-margin)]",
  /** The dimensions on the model: the band between the top and bottom bars, the full width. */
  dimensions: "pointer-events-none absolute inset-x-0 bottom-[var(--chrome-bottom)] top-[var(--chrome-top)] overflow-hidden",
  /** Inside `dimensions`: the whole canvas again, so what is drawn in it keeps canvas coordinates. */
  dimensionsCanvas: "absolute inset-x-0 -top-[var(--chrome-top)] -bottom-[var(--chrome-bottom)]",
} as const;

/**
 * A tool panel in its column: at the column's top left, as wide as it wants up to the column,
 * scrolling inside when taller. Positioned itself, so it floats over what's under it like a popover.
 */
export const PANEL = "pointer-events-auto absolute left-0 top-0 flex min-h-0 max-h-full max-w-full flex-col";

/**
 * The canvas above and below the band marked `data-chrome-band` (the top and bottom bars), as
 * rectangles relative to `origin` (the 3D view's box): what the dimension labels keep out of.
 */
export function outsideBand(root: Element, origin: DOMRect): { x: number; y: number; width: number; height: number }[] {
  const band = root.querySelector("[data-chrome-band]")?.getBoundingClientRect();
  if (!band || !band.height) return [];
  const top = band.top - origin.top;
  const bottom = band.bottom - origin.top;
  return [
    { x: 0, y: 0, width: origin.width, height: Math.max(0, top) },
    { x: 0, y: bottom, width: origin.width, height: Math.max(0, origin.height - bottom) },
  ];
}
