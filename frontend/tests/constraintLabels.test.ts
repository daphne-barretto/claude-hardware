import { describe, expect, it } from "vitest";
import { readdirSync } from "node:fs";
import { resolve } from "node:path";
import type { Constraint } from "@/lib/session-api";
import type { CheckReport, CheckResult } from "@/components/canvas/api";
import { evidence } from "@/components/canvas/checks";
import { constraintGroups, requiredLabel, requiredValue } from "@/components/canvas/constraintGroups";
import type { LengthUnit } from "@/components/canvas/units";
import { readRepoFile } from "./fixtures";

// Every row of the Constraints panel reads as the constraint's name and its required value (with
// its unit), and every place under it as a measured value against that requirement: never
// "None", an empty label or a raw expression such as "> 0 after 0.201 mm". Checked on the mock
// API's report and on every build of the recorded sample sessions (real check runs), with the
// constraints each session had, in both display units.

type Case = { name: string; report: CheckReport; constraints: Constraint[] };

const samplesDir = resolve(__dirname, "../../demos/sessions");
const samples = readdirSync(samplesDir, { withFileTypes: true }).filter((d) => d.isDirectory());

/** The constraints a recorded session ended with (the last event that lists them). */
function sessionConstraints(id: string): Constraint[] {
  const session = JSON.parse(readRepoFile(`demos/sessions/${id}/session.json`)) as { events: { event: { constraints?: Constraint[] } }[] };
  return session.events.map((e) => e.event.constraints).filter((c): c is Constraint[] => !!c).at(-1) ?? [];
}

const cases: Case[] = [
  { name: "mock checks", report: JSON.parse(readRepoFile("fixtures/checks.json")), constraints: [] },
  ...samples.flatMap((s) => {
    const builds = readdirSync(resolve(samplesDir, s.name, "builds"));
    return builds.map((b) => ({
      name: `${s.name}/${b}`,
      report: JSON.parse(readRepoFile(`demos/sessions/${s.name}/builds/${b}/checks.json`)) as CheckReport,
      constraints: sessionConstraints(s.name),
    }));
  }),
];

// Text a person reads as "no value", or a comparison instead of words.
const PLACEHOLDER = /^(none|null|nil|undefined|n\/?a|nan)?$/i;
const EXPRESSION = /^[<>=!]/;
const UNIT_TEXT: Record<LengthUnit, Record<string, string>> = {
  mm: { mm: "mm", mm2: "mm²", mm3: "mm³", deg: "°" },
  in: { mm: "in", mm2: "in²", mm3: "in³", deg: "°" },
};
const loose = (t: string) => t.toLowerCase().replace(/s$/, "");

function expectHuman(text: string, what: string) {
  expect(PLACEHOLDER.test(text.trim()), `${what}: "${text}"`).toBe(false);
  expect(EXPRESSION.test(text.trim()), `${what}: "${text}"`).toBe(false);
  expect(text, what).not.toMatch(/\b(None|null|undefined|NaN)\b/);
}

describe.each(["mm", "in"] as LengthUnit[])("Constraints panel labels (%s)", (unit) => {
  it.each(cases.map((c) => [c.name, c] as const))("%s: every row names its constraint and required value", (_, c) => {
    const groups = constraintGroups(c.constraints, c.report, unit);
    expect(groups.length).toBeGreaterThan(0);
    for (const g of groups) {
      expectHuman(g.label, `row ${g.key}`);
      // The name is in the label, unless the value is the name ("Molded ABS").
      expect(g.label.startsWith(g.title) || loose(g.label) === loose(g.title), `row ${g.key}: "${g.label}" lacks "${g.title}"`).toBe(true);
      const required = g.constraint ? null : g.results.find((r) => r.required)?.required;
      if (required) {
        const value = requiredValue(required, unit);
        expectHuman(value, `required of ${g.key}`);
        expect(g.label.toLowerCase(), `row ${g.key}`).toContain(value.toLowerCase());
        if (required.unit) expect(g.label, `row ${g.key}`).toContain(UNIT_TEXT[unit][required.unit]);
      }
    }
  });

  it.each(cases.map((c) => [c.name, c] as const))("%s: every place reads as a measured value against the requirement", (_, c) => {
    for (const r of c.report.results.filter((x: CheckResult) => x.measured && x.required)) {
      const text = evidence(r, unit)!;
      expect(text, r.id).toMatch(/^Measured .+ · (limit|min|max|requires) .+/);
      const [measured, required] = text.split(" · ");
      expectHuman(measured.replace(/^Measured /, ""), `measured of ${r.id}`);
      expectHuman(required.replace(/^(limit|min|max|requires) /, ""), `required of ${r.id}`);
    }
  });
});

describe("requiredLabel", () => {
  it("keeps the name of a spec whose value is not the name", () => {
    expect(requiredLabel("Board held", { kind: "spec", value: "a stop within 0.2 mm" }, "mm")).toBe("Board held: a stop within 0.2 mm");
    expect(requiredLabel("Board held", { kind: "spec", value: "a stop within 25.4 mm" }, "in")).toBe("Board held: a stop within 1 in");
  });

  it("reads as the value when the value is the name", () => {
    expect(requiredLabel("Molded ABS", { kind: "spec", value: "molded ABS" }, "mm")).toBe("Molded ABS");
    expect(requiredLabel("M3 heat-set inserts", { kind: "spec", value: "M3 heat-set insert" }, "mm")).toBe("M3 heat-set insert");
  });

  it("is the name alone with no value or an empty one", () => {
    expect(requiredLabel("Drop", null, "mm")).toBe("Drop");
    expect(requiredLabel("Board held", { kind: "spec", value: " " }, "mm")).toBe("Board held");
  });

  it("names a check from a result that has a value when the first has none", () => {
    const base = { check: "x", title: "Board fits", standard: null, summary: "s", covers: "c", location: null } as const;
    const report = {
      contract: "check-results/1",
      build_id: "b",
      board: null,
      summary: { pass: 1, fail: 0, untested: 1, not_applicable: 0 },
      results: [
        { ...base, id: "x:a", status: "untested", measured: null, required: null },
        { ...base, id: "x:b", status: "pass", measured: { value: 0, unit: "mm3" }, required: { kind: "max", value: 0, unit: "mm3" } },
      ],
    } as unknown as CheckReport;
    expect(constraintGroups([], report, "mm")[0].label).toBe("Board fits ≤ 0 mm³");
  });
});
