/**
 * Which check pins the canvas draws: only failed checks by default, every pin, or none. The pin
 * of the check the user selected (in the strip, on a pin, or "Show on model") is drawn whatever
 * the filter says. Untested and not-applicable results never get a pin. The choice is kept per
 * browser.
 */

export type PinFilter = "failed" | "all" | "none";

export const PIN_FILTERS: PinFilter[] = ["failed", "all", "none"];
export const PIN_FILTER_LABELS: Record<PinFilter, string> = { failed: "Failed", all: "All", none: "None" };
export const DEFAULT_PIN_FILTER: PinFilter = "failed";
export const PIN_FILTER_KEY = "claude-hardware.check-pins";

/** Statuses that have a place on the model worth a pin. */
const PINNED = new Set(["pass", "fail"]);

/** Whether a result gets a pin at all: it was tested and applies. */
export function hasPin(r: { status: string }): boolean {
  return PINNED.has(r.status);
}

/** Whether a result's pin is drawn under `filter`; `revealed` pins (the selected check) always are. */
export function pinShown(r: { id: string; status: string }, filter: PinFilter, revealed: ReadonlySet<string>): boolean {
  if (!hasPin(r)) return false;
  if (revealed.has(r.id)) return true;
  return filter === "all" || (filter === "failed" && r.status === "fail");
}

/** The results whose pins are drawn, in their order. */
export function shownPins<T extends { id: string; status: string }>(
  results: T[],
  filter: PinFilter,
  revealed: ReadonlySet<string> = new Set(),
): T[] {
  return results.filter((r) => pinShown(r, filter, revealed));
}

const isFilter = (v: unknown): v is PinFilter => typeof v === "string" && (PIN_FILTERS as string[]).includes(v);

function storage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

/** The filter this browser chose last, or the default. Never throws (storage can be blocked). */
export function loadPinFilter(store: Storage | null = storage()): PinFilter {
  try {
    const v = store?.getItem(PIN_FILTER_KEY);
    return isFilter(v) ? v : DEFAULT_PIN_FILTER;
  } catch {
    return DEFAULT_PIN_FILTER;
  }
}

/** Remembers the filter for this browser; a blocked storage just doesn't remember. */
export function savePinFilter(filter: PinFilter, store: Storage | null = storage()): void {
  try {
    store?.setItem(PIN_FILTER_KEY, filter);
  } catch {
    // Private windows and blocked site data: the choice lasts for this page only.
  }
}
