import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import type { Build, CheckReport } from "@/components/canvas/api";
import {
  failingChecks,
  formatParameter,
  parseParameter,
  rebuiltBuild,
  withParameter,
  type Parameter,
} from "@/components/canvas/parameters";
import { MM_PER_INCH } from "@/components/canvas/units";

// Parameters, read at run time from the fixtures (generated from demos/b-parasite/ and standards.yaml).
const fixture = <T,>(name: string) => JSON.parse(readFileSync(path.join(__dirname, "../../fixtures", name), "utf8")) as T;
const PARAMS = fixture<Parameter[]>("parameters.json");
const BUILD = fixture<Build>("build.json");
const CHECKS = fixture<CheckReport>("checks.json");
const length = PARAMS.find((p) => p.unit === "mm" && typeof p.value === "number")!;
const angle = PARAMS.find((p) => p.unit === "deg")!;
const text = PARAMS.find((p) => typeof p.value === "string")!;

describe("parameter values", () => {
  it("shows lengths in the chosen unit and other values as they are", () => {
    expect(Number(formatParameter(length, "mm"))).toBeCloseTo(length.value as number);
    expect(Number(formatParameter(length, "in"))).toBeCloseTo((length.value as number) / MM_PER_INCH, 3);
    expect(Number(formatParameter(angle, "in"))).toBeCloseTo(angle.value as number);
    expect(formatParameter(text, "mm")).toBe(text.value);
  });

  it("turns typed text into a value in the parameter's unit", () => {
    expect(parseParameter(" 3.5 ", length, "mm")).toEqual({ value: 3.5 });
    expect(parseParameter("1", length, "in")).toEqual({ value: MM_PER_INCH });
    expect(parseParameter("2", angle, "in")).toEqual({ value: 2 });
    expect(parseParameter("cast aluminium", text, "mm")).toEqual({ value: "cast aluminium" });
  });

  it("rejects empty, non-numeric and out-of-range values", () => {
    expect(parseParameter("", length, "mm")).toHaveProperty("error");
    expect(parseParameter("wide", length, "mm")).toHaveProperty("error");
    const bounded = { ...length, min: 1, max: 4 };
    expect(parseParameter("0.5", bounded, "mm")).toEqual({ error: "At least 1 mm." });
    expect(parseParameter("5", bounded, "mm")).toEqual({ error: "At most 4 mm." });
  });

  it("replaces an updated parameter by name, keeping the order", () => {
    const updated = { ...length, value: 9, locked: true };
    const next = withParameter(PARAMS, updated);
    expect(next.map((p) => p.name)).toEqual(PARAMS.map((p) => p.name));
    expect(next.find((p) => p.name === length.name)).toEqual(updated);
  });
});

describe("rebuild after a hand edit", () => {
  it("is the first build that was not there before the edit", () => {
    const rebuilt = { ...BUILD, id: `${BUILD.id}-edit-1` };
    const later = { ...BUILD, id: `${BUILD.id}-edit-2` };
    expect(rebuiltBuild([BUILD], [BUILD.id])).toBeNull();
    expect(rebuiltBuild([BUILD, rebuilt, later], [BUILD.id])).toBe(rebuilt);
  });

  it("lists the failing checks of the report", () => {
    const failing = failingChecks(CHECKS);
    expect(failing).toHaveLength(CHECKS.summary.fail);
    expect(failing.every((r) => r.status === "fail")).toBe(true);
  });
});
