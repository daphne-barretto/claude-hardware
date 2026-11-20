import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import type { CheckReport } from "@/components/canvas/api";
import { evidence } from "@/components/canvas/checks";
import { formatMapValue } from "@/components/canvas/HardwareCanvas";
import { lengthsInText, MM_PER_INCH } from "@/components/canvas/units";

// The mm/in toggle converts check values, limits and the lengths in their text.
const CHECKS = JSON.parse(readFileSync(path.join(__dirname, "../../fixtures/checks.json"), "utf8")) as CheckReport;

describe("evidence", () => {
  it("leaves no millimetres in the fixture report's values and limits in inches", () => {
    const withValues = CHECKS.results.filter((r) => r.measured || r.required);
    expect(withValues.some((r) => r.measured?.unit === "mm" || r.required?.unit === "mm")).toBe(true);
    for (const r of withValues) expect(evidence(r, "in")).not.toMatch(/mm/);
  });
});

describe("lengthsInText", () => {
  it("converts lengths written in mm and nothing else", () => {
    expect(lengthsInText("within ±0.2 mm, 25.4mm tall", "mm")).toBe("within ±0.2 mm, 25.4mm tall");
    expect(lengthsInText("within ±0.2 mm, 25.4mm tall", "in")).toBe("within ±0.0079 in, 1 in tall");
    expect(lengthsInText("turned 90°, area 4 mm2, 3 mmHg", "in")).toBe("turned 90°, area 4 mm2, 3 mmHg");
  });

  it("leaves no lengths in mm in the fixture report's summaries", () => {
    const summaries = CHECKS.results.map((r) => r.summary ?? "");
    expect(summaries.some((s) => /\d\s?mm\b/.test(s))).toBe(true);
    for (const s of summaries) expect(lengthsInText(s, "in")).not.toMatch(/\d\s?mm\b/);
  });
});

describe("formatMapValue", () => {
  it("formats lengths in the display unit and angles in degrees", () => {
    expect(formatMapValue(MM_PER_INCH, "mm", "in")).toBe("1.000 in");
    expect(formatMapValue(2, "mm", "mm")).toBe("2.0 mm");
    expect(formatMapValue(1.5, "deg", "in")).toBe("1.5°");
  });
});
