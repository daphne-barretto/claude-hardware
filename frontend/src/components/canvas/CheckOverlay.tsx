"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Check, Crosshair, X } from "lucide-react";
import type { CheckReport } from "./api";
import {
  checkLabel,
  covered,
  evidence,
  locatedResults,
  placePins,
  type LocatedResult,
  type ScreenPoint,
  type ScreenRect,
} from "./checks";
import { lengthsInText, type LengthUnit } from "./units";
import { CLICK_SLOP_PX } from "./viewer";
import { pinShown, type PinFilter } from "./pinVisibility";
import { useAnchoredCard } from "./fitPanel";

const CARD_WIDTH = 300;

/** The panels pins never draw over: elements in the canvas marked `data-pin-cover` (opaque, above the pin layer). */
export const PIN_COVER = "data-pin-cover";
/** What a pin stays under: the panels, and any open dialog (a comment thread, the shortcuts sheet), but not the check's own card. */
const COVERS = `[${PIN_COVER}], [role=dialog]:not([data-testid=check-card])`;
/** Half a pin's size, the larger (failed) one, px: a pin is under a cover when any of it is. */
const PIN_RADIUS = 10;

/** The boxes of the covers over the pin layer, in its coordinates. */
export function coverRects(layer: HTMLElement): ScreenRect[] {
  const root = layer.parentElement;
  if (!root) return [];
  const origin = layer.getBoundingClientRect();
  return Array.from(root.querySelectorAll(COVERS), (el) => el.getBoundingClientRect())
    .filter((r) => r.width > 0 && r.height > 0)
    .map((r) => ({ x: r.left - origin.left, y: r.top - origin.top, width: r.width, height: r.height }));
}

/**
 * A pin at each located check result the filter shows (`filter`; the `revealed` ones always),
 * on the model where the check applies. The layer is its own stacking context, rendered before
 * the canvas panels, so the panels always draw over it; a pin whose spot lies under a panel
 * (parts panel, view cube, toolbar, an open menu) or an open dialog stays on its spot under it,
 * rather than being moved off the model, and is hidden there (nothing of it shows, nor takes focus).
 */
export function CheckPins({
  report,
  filter,
  revealed,
  selectedId,
  onSelect,
  toScreen,
  onWheel,
  onDrag,
  showPins = true,
}: {
  report: CheckReport;
  filter: PinFilter;
  /** Pins shown whatever the filter: the selected check, one the chat asked to show. */
  revealed: ReadonlySet<string>;
  selectedId: string | null;
  onSelect: (result: LocatedResult) => void;
  /**
   * Projects a result's location (its board point, on its part) to viewport pixels; changes
   * identity whenever the camera moves or the parts move apart.
   */
  toScreen: (location: LocatedResult["location"]) => ScreenPoint | null;
  /** Wheel events over a pin, for the viewer to zoom with as if they were over the model. */
  onWheel: (e: WheelEvent) => void;
  /** A drag that starts on a pin: the press and the first move past the click slop, for the viewer to orbit or pan with. */
  onDrag: (down: PointerEvent, move: PointerEvent) => void;
  /** False while a click on the model means something else (measuring), so no pin is in the way. */
  showPins?: boolean;
}) {
  // A press on a pin: a click selects the result, a drag is handed to the viewer.
  const press = useRef<{ down: PointerEvent; dragged: boolean } | null>(null);
  const layerRef = useRef<HTMLDivElement>(null);
  const [layout, setLayout] = useState<{ width: number; height: number; covers: ScreenRect[] }>({ width: 0, height: 0, covers: [] });
  // The view's size and the panels over it, after every render and whenever the canvas changes.
  const measure = useCallback(() => {
    const layer = layerRef.current;
    if (!layer) return;
    const next = { width: layer.clientWidth, height: layer.clientHeight, covers: coverRects(layer) };
    setLayout((prev) => (JSON.stringify(prev) === JSON.stringify(next) ? prev : next));
  }, []);
  useLayoutEffect(measure);
  useEffect(() => {
    const root = layerRef.current?.parentElement;
    if (!root) return;
    const observer = new MutationObserver(measure);
    observer.observe(root, { subtree: true, childList: true, attributes: true, attributeFilter: ["class", "style"] });
    window.addEventListener("resize", measure);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [measure]);

  const located = locatedResults(report).filter((r) => pinShown(r, filter, revealed));
  // Pins stay on their spot; one on (or near) another's moves at most a pin's width. The
  // selected one, then failures, keep their place and draw on top.
  const rank = (r: LocatedResult) => (r.id === selectedId ? 0 : r.status === "fail" ? 1 : 2);
  const pins = placePins(
    located
      .map((r, i) => ({ r, i, at: toScreen(r.location) }))
      .filter((p): p is { r: LocatedResult; i: number; at: ScreenPoint } => !!p.at)
      .sort((a, b) => rank(a.r) - rank(b.r) || a.i - b.i)
      .map(({ r, at }) => ({ id: r.id, at })),
    layout.width ? { width: layout.width, height: layout.height } : undefined,
  );

  return (
    <div
      ref={layerRef}
      data-testid="check-pins"
      className="pointer-events-none absolute inset-0 isolate overflow-hidden"
      onWheel={(e) => onWheel(e.nativeEvent)}
    >
      {showPins && located.map((r) => {
        const at = pins.get(r.id);
        if (!at) return null;
        // Under a panel or a dialog: kept in place but not shown, so it neither draws over it nor takes focus.
        const hidden = covered(at, layout.covers, PIN_RADIUS);
        const fail = r.status === "fail";
        return (
          <button
            key={r.id}
            type="button"
            data-testid="check-pin"
            // Dimension labels keep clear of pins as they do of panels (Dimensions, canvasLayout).
            data-canvas-panel
            data-check={r.id}
            data-status={r.status}
            data-covered={hidden ? "true" : undefined}
            aria-label={`${checkLabel(r)}: ${r.status}`}
            aria-pressed={r.id === selectedId}
            title={checkLabel(r)}
            onPointerDown={(e) => {
              // Keep the moves coming to the pin until the drag is handed over.
              e.currentTarget.setPointerCapture?.(e.pointerId);
              press.current = { down: e.nativeEvent, dragged: false };
            }}
            onPointerMove={(e) => {
              const p = press.current;
              if (!p || p.dragged || p.down.pointerId !== e.pointerId) return;
              if (Math.hypot(e.clientX - p.down.clientX, e.clientY - p.down.clientY) < CLICK_SLOP_PX) return;
              p.dragged = true;
              onDrag(p.down, e.nativeEvent);
            }}
            onClick={() => {
              const dragged = press.current?.dragged;
              press.current = null;
              if (!dragged) onSelect(r);
            }}
            style={{ left: at.x, top: at.y, zIndex: r.id === selectedId ? 3 : fail ? 2 : 1 }}
            className={`${hidden ? "invisible" : "pointer-events-auto"} absolute grid -translate-x-1/2 -translate-y-1/2 place-items-center rounded-full border-2 border-[#1f1f1f] font-bold shadow aria-pressed:ring-2 aria-pressed:ring-white ${
              fail ? "size-5 bg-[var(--cs-accent)] text-[11px] text-[#1f1f1f]" : "size-3.5 bg-[#5fae7a]"
            }`}
          >
            {fail && "!"}
          </button>
        );
      })}
    </div>
  );
}

/**
 * Check results on the model, apart from the pins (`CheckPins`): a card for the selected result.
 * The results are listed by constraint in the Constraints panel (`ConstraintsPanel`).
 * Selecting a result asks the canvas to move the camera to it (`onSelect`).
 */
export function CheckOverlay({
  report,
  unit,
  selectedId,
  onSelect,
  toScreen,
  width,
}: {
  report: CheckReport;
  unit: LengthUnit;
  selectedId: string | null;
  onSelect: (result: LocatedResult | null) => void;
  /** Projects a result's location (its board point, on its part) to viewport pixels. */
  toScreen: (location: LocatedResult["location"]) => ScreenPoint | null;
  width: number;
}) {
  const located = locatedResults(report);
  const selected = located.find((r) => r.id === selectedId) ?? null;
  const selectedAt = selected ? toScreen(selected.location) : null;
  // Beside its pin, inside the canvas between its bars (`useAnchoredCard`).
  const cardRef = useRef<HTMLElement>(null);
  // Below and right of the pin, as before; above it where there is no room below.
  useAnchoredCard(cardRef, selectedAt, { gap: 20, lift: -16 });

  return (
    <>
      {selected && (
        <section
          role="dialog"
          aria-label={checkLabel(selected)}
          data-testid="check-card"
          data-check={selected.id}
          ref={cardRef}
          style={
            selectedAt
              ? { left: Math.max(8, Math.min(selectedAt.x + 20, width - CARD_WIDTH - 8)), top: selectedAt.y + 16 }
              : { left: 12, top: 56 }
          }
          className="cs-scroll absolute z-30 overflow-y-auto rounded-xl border border-[var(--cs-border)] bg-[#262626] p-3 text-[13px] shadow-xl"
        >
          <div style={{ width: CARD_WIDTH - 26 }} className="max-w-full">
            <div className="flex items-start gap-2">
              <p
                className={`flex flex-1 items-center gap-1.5 text-[12px] ${selected.status === "fail" ? "text-[var(--cs-accent)]" : "text-[#5fae7a]"}`}
              >
                {selected.status === "fail" ? <X className="size-3.5" /> : <Check className="size-3.5" />}
                {checkLabel(selected)} · {selected.status}
              </p>
              <button
                type="button"
                aria-label="Close"
                onClick={() => onSelect(null)}
                className="grid size-5 place-items-center rounded text-[var(--cs-muted)] hover:bg-[var(--cs-hover)] hover:text-[var(--cs-text)]"
              >
                <X className="size-3.5" />
              </button>
            </div>
            <p className="mt-1 font-semibold leading-snug">{lengthsInText(selected.summary, unit)}</p>
            {evidence(selected, unit) && (
              <p data-testid="check-evidence" className="mt-1 text-[var(--cs-muted)]">
                {evidence(selected, unit)}
              </p>
            )}
            <p className="mt-1 text-[12px] text-[var(--cs-muted)]">{selected.covers}</p>
            <button
              type="button"
              onClick={() => onSelect(selected)}
              className="mt-2 flex h-7 items-center gap-1.5 rounded-md bg-[var(--cs-surface)] px-2.5 hover:bg-[#3a3a3a]"
            >
              <Crosshair className="size-3.5" />
              Show on model
            </button>
          </div>
        </section>
      )}

    </>
  );
}
