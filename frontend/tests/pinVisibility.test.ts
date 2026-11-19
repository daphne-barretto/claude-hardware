import { describe, expect, it } from "vitest";
import {
  DEFAULT_PIN_FILTER,
  hasPin,
  loadPinFilter,
  PIN_FILTER_KEY,
  pinShown,
  savePinFilter,
  shownPins,
} from "@/components/canvas/pinVisibility";

// Pin visibility (check pins): failed only by default, all, or none; the selected check's pin
// always shows; untested and not-applicable results never get a pin.
const results = [
  { id: "a", status: "pass" },
  { id: "b", status: "fail" },
  { id: "c", status: "untested" },
  { id: "d", status: "not_applicable" },
  { id: "e", status: "pass" },
];
const ids = (rs: { id: string }[]) => rs.map((r) => r.id);

function memory(): Storage {
  const m = new Map<string, string>();
  return {
    getItem: (k) => m.get(k) ?? null,
    setItem: (k, v) => void m.set(k, v),
    removeItem: (k) => void m.delete(k),
    clear: () => m.clear(),
    key: (i) => [...m.keys()][i] ?? null,
    get length() {
      return m.size;
    },
  };
}

describe("check pin visibility", () => {
  it("shows only failed pins by default", () => {
    expect(DEFAULT_PIN_FILTER).toBe("failed");
    expect(ids(shownPins(results, DEFAULT_PIN_FILTER))).toEqual(["b"]);
  });

  it("shows every tested result with All, and none with None", () => {
    expect(ids(shownPins(results, "all"))).toEqual(["a", "b", "e"]);
    expect(ids(shownPins(results, "none"))).toEqual([]);
  });

  it("always shows the selected check's pin, whatever the filter", () => {
    expect(ids(shownPins(results, "failed", new Set(["e"])))).toEqual(["b", "e"]);
    expect(ids(shownPins(results, "none", new Set(["a"])))).toEqual(["a"]);
  });

  it("never gives untested or not-applicable results a pin, even selected", () => {
    expect(hasPin({ status: "untested" })).toBe(false);
    expect(hasPin({ status: "not_applicable" })).toBe(false);
    expect(pinShown(results[2], "all", new Set(["c"]))).toBe(false);
    expect(pinShown(results[3], "all", new Set(["d"]))).toBe(false);
  });

  it("remembers the choice per browser and survives blocked storage", () => {
    const store = memory();
    expect(loadPinFilter(store)).toBe("failed");
    savePinFilter("all", store);
    expect(store.getItem(PIN_FILTER_KEY)).toBe("all");
    expect(loadPinFilter(store)).toBe("all");
    store.setItem(PIN_FILTER_KEY, "bogus");
    expect(loadPinFilter(store)).toBe("failed");
    const blocked = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    } as unknown as Storage;
    expect(loadPinFilter(blocked)).toBe("failed");
    expect(() => savePinFilter("none", blocked)).not.toThrow();
    expect(loadPinFilter(null)).toBe("failed");
  });
});
