"use client";

import { useLayoutEffect, type RefObject } from "react";

const MARGIN = 8;

/**
 * How far to move a popover sideways, and how wide it may be, to keep it inside `bounds` (the
 * canvas on screen) with a margin: one anchored to a toolbar button near the right edge moves
 * left by its overflow, and one wider than the canvas narrows to it.
 */
export function fitInside(
  box: { left: number; width: number },
  bounds: { left: number; right: number },
  margin = MARGIN,
): { dx: number; maxWidth: number } {
  const left = bounds.left + margin;
  const right = bounds.right - margin;
  const maxWidth = Math.max(0, right - left);
  const width = Math.min(box.width, maxWidth);
  let dx = 0;
  if (box.left + width > right) dx = right - (box.left + width);
  if (box.left + dx < left) dx = left - box.left;
  return { dx, maxWidth };
}

/**
 * Keeps an absolutely placed popover inside the canvas it opened in (the
 * `[data-testid="hardware-canvas"]` slot, else the window), placed again when the window
 * resizes. On a phone the canvas is the screen.
 */
export function useFitInside(ref: RefObject<HTMLElement | null>) {
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const place = () => {
      el.style.translate = "";
      el.style.maxWidth = "";
      const host = el.closest('[data-testid="hardware-canvas"]')?.getBoundingClientRect();
      const bounds = {
        left: Math.max(host?.left ?? 0, 0),
        right: Math.min(host?.right ?? window.innerWidth, window.innerWidth),
      };
      const box = el.getBoundingClientRect();
      const { dx, maxWidth } = fitInside(box, bounds);
      if (box.width > maxWidth) el.style.maxWidth = `${maxWidth}px`;
      if (dx) el.style.translate = `${dx}px 0`;
    };
    place();
    window.addEventListener("resize", place);
    return () => window.removeEventListener("resize", place);
  }, [ref]);
}

/** A rectangle on screen. */
export type Band = { left: number; top: number; right: number; bottom: number };

/**
 * Where a card anchored to a point on the model goes, inside `band` (the canvas between its top
 * bar and its bottom bar): beside the point, on its right where the card fits, else on its left,
 * else wherever it fits sideways; level with the point a little above it, moved up (or flipped
 * above the point) so it ends inside the band. `maxHeight` is the band's height: a taller card
 * scrolls inside.
 */
export function placeNear(
  anchor: { x: number; y: number },
  size: { width: number; height: number },
  band: Band,
  { gap = 22, lift = 40, margin = MARGIN } = {},
): { left: number; top: number; maxHeight: number; maxWidth: number } {
  const left0 = band.left + margin;
  const right0 = band.right - margin;
  const top0 = band.top + margin;
  const bottom0 = band.bottom - margin;
  const maxWidth = Math.max(0, right0 - left0);
  const maxHeight = Math.max(0, bottom0 - top0);
  const width = Math.min(size.width, maxWidth);
  const height = Math.min(size.height, maxHeight);
  let left = anchor.x + gap;
  if (left + width > right0) left = anchor.x - gap - width; // flip to the left of the point
  left = Math.min(Math.max(left, left0), right0 - width);
  let top = anchor.y - lift;
  if (top + height > bottom0) top = Math.min(top, anchor.y - gap - height); // flip above the point
  top = Math.min(Math.max(top, top0), bottom0 - height);
  return { left, top, maxHeight, maxWidth };
}

/** The canvas's band for cards on screen: the canvas slot less its top and bottom bars (`chromeLayout`). */
export function canvasBand(el: Element): Band {
  const host = el.closest('[data-testid="hardware-canvas"]') as HTMLElement | null;
  const r = host?.getBoundingClientRect() ?? { left: 0, top: 0, right: window.innerWidth, bottom: window.innerHeight };
  const px = (name: string) => {
    if (!host) return 0;
    const probe = document.createElement("div");
    probe.style.cssText = `position:absolute;visibility:hidden;height:var(${name},0px)`;
    host.appendChild(probe);
    const h = probe.getBoundingClientRect().height;
    probe.remove();
    return h;
  };
  return {
    left: Math.max(r.left, 0),
    right: Math.min(r.right, window.innerWidth),
    top: Math.max(r.top, 0) + px("--chrome-top"),
    bottom: Math.min(r.bottom, window.innerHeight) - px("--chrome-bottom"),
  };
}

/**
 * Keeps a card anchored to a point on the model (`anchor`, in the coordinates of the card's
 * positioned parent) inside the canvas band, placed again whenever the point or the card's size
 * changes or the window resizes. The card is a flex column whose body scrolls (`min-h-0` and
 * `overflow-y-auto` on the body), so its header and close button stay in view.
 */
export function useAnchoredCard(
  ref: RefObject<HTMLElement | null>,
  anchor: { x: number; y: number } | null,
  options: { gap?: number; lift?: number } = {},
) {
  const { gap, lift } = options;
  const x = anchor?.x ?? null;
  const y = anchor?.y ?? null;
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el || x === null || y === null) return;
    const place = () => {
      const parent = (el.offsetParent as HTMLElement | null)?.getBoundingClientRect() ?? { left: 0, top: 0 };
      const band = canvasBand(el);
      el.style.maxHeight = "";
      const size = { width: el.offsetWidth, height: el.scrollHeight };
      const p = placeNear({ x: x + parent.left, y: y + parent.top }, size, band, { gap, lift });
      el.style.left = `${p.left - parent.left}px`;
      el.style.top = `${p.top - parent.top}px`;
      el.style.maxHeight = `${p.maxHeight}px`;
      el.style.maxWidth = `${p.maxWidth}px`;
    };
    place();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(place);
    observer?.observe(el);
    window.addEventListener("resize", place);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", place);
    };
  }, [ref, x, y, gap, lift]);
}
