import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import type { Constraint, StandardsSet } from "@/lib/session-api";
import { removeConstraint, updateConstraint } from "@/lib/session-api";
import type { CheckReport } from "@/components/canvas/api";
import { ConstraintsPanel, sourceText } from "@/components/canvas/ConstraintsPanel";
import { ConstraintsContext, type ConstraintsValue } from "@/components/canvas/constraintsContext";
import { constraintGroups, requiredLabel } from "@/components/canvas/constraintGroups";
import { standardsSetName } from "@/components/sites/claude-shell/useStandardsSetName";
import { readRepoFile } from "./fixtures";

// The scenario comes from the fixtures (fixtures/checks.json, fixtures/standards.json), so the
// tests hold for any board and set.
const report = JSON.parse(readRepoFile("fixtures/checks.json")) as CheckReport;
const sets = JSON.parse(readRepoFile("fixtures/standards.json")) as StandardsSet[];
const builtIn = sets.find((s) => s.built_in)!;
const setName = standardsSetName(sets, null)!;

/** A constraint per rule of the built-in set that the report checks, as the spec gives them. */
const constraints: Constraint[] = builtIn.standards
  .filter((s) => report.results.some((r) => r.standard === s.id && r.status !== "not_applicable"))
  .map((s) => {
    const kind = s.kind ?? "spec";
    const value = s.value ?? s.title;
    const unit = s.unit ?? null;
    return {
      id: s.id,
      title: s.title,
      label: requiredLabel(s.title, { kind, value, unit } as never, "mm"),
      kind,
      value,
      unit,
      check: s.check,
      source: "standard",
      quote: null,
      message_id: null,
      standard_value: value,
    } as Constraint;
  });
const numeric = constraints.find((c) => c.kind !== "spec")!;
const failing = report.results.find((r) => r.status === "fail" && r.standard && constraints.some((c) => c.id === r.standard))!;
// A check with no constraint behind it and more than one place (the board's openings).
const boardChecks = report.results.filter((r) => !r.standard && r.location);
const multi = boardChecks.find((r) => boardChecks.filter((x) => x.check === r.check).length > 1)!;
const multiResults = report.results.filter((r) => !r.standard && r.check === multi.check);

function ctx(over: Partial<ConstraintsValue> = {}): ConstraintsValue {
  return { constraints, onChange: vi.fn(), disabledReason: null, setName, ...over };
}

type PanelProps = Partial<Parameters<typeof ConstraintsPanel>[0]>;

function renderBar(value: ConstraintsValue, props: PanelProps = {}) {
  return render(
    <ConstraintsContext.Provider value={value}>
      <ConstraintsPanel unit="mm" {...props} report={props.report === undefined ? report : props.report} />
    </ConstraintsContext.Provider>,
  );
}

const items = () => within(screen.getByRole("list", { name: "Constraints" })).getAllByRole("button");
const item = (key: string) => document.querySelector<HTMLButtonElement>(`[data-testid="constraint-item"][data-constraint="${key}"]`)!;

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("constraint groups", () => {
  it("groups the results by constraint, and the rest by standard or check", () => {
    const groups = constraintGroups(constraints, report, "mm");
    for (const c of constraints) {
      const g = groups.find((x) => x.key === c.id)!;
      expect(g.constraint).toBe(c);
      expect(g.results.map((r) => r.id)).toEqual(report.results.filter((r) => r.standard === c.id).map((r) => r.id));
    }
    const board = groups.find((g) => g.key === multi.check)!;
    expect(board.constraint).toBeNull();
    expect(board.results.map((r) => r.id)).toEqual(multiResults.map((r) => r.id));
    // Every result is in exactly one group.
    expect(groups.flatMap((g) => g.results).length).toBe(report.results.length);
  });

  it("orders failing, then untested, then passing, then not applicable", () => {
    const rank = { fail: 0, untested: 1, unchecked: 1, pass: 2, not_applicable: 3 };
    const order = constraintGroups(constraints, report, "mm").map((g) => rank[g.status]);
    expect(order).toEqual([...order].sort((a, b) => a - b));
    expect(order[0]).toBe(0);
  });

  it("counts the places a check tested", () => {
    const board = constraintGroups(constraints, report, "mm").find((g) => g.key === multi.check)!;
    expect(board.places).toBe(multiResults.filter((r) => r.status === "pass" || r.status === "fail").length);
  });

  it("is not checked yet with no report", () => {
    const groups = constraintGroups(constraints, null, "mm");
    expect(groups.map((g) => g.key)).toEqual(constraints.map((c) => c.id));
    expect(new Set(groups.map((g) => g.status))).toEqual(new Set(["unchecked"]));
  });
});

describe("Constraints panel", () => {
  it("is a list of constraints, failing first, each named with its status and places", () => {
    renderBar(ctx());
    expect(screen.getByRole("region", { name: "Constraints" })).toBeTruthy();
    const first = items()[0];
    expect(first.getAttribute("data-status")).toBe("fail");
    expect(first.getAttribute("aria-label")).toMatch(/, failing/);
    const board = item(multi.check);
    expect(board.textContent).toContain(`${multiResults.length} places`);
    expect(board.getAttribute("aria-label")).toContain(`${multiResults.length} places`);
    expect(item(numeric.id).textContent).toContain(numeric.label);
    // Not applicable ones wait behind their own button.
    const na = report.results.filter((r) => r.status === "not_applicable");
    if (na.length) {
      expect(screen.queryByRole("button", { name: new RegExp(`${na[0].title}.*not applicable`) })).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: `${na.length} not applicable` }));
      expect(screen.getAllByRole("button", { name: /, not applicable$/ }).length).toBeGreaterThan(0);
      expect(items().at(-1)!.getAttribute("data-status")).toBe("not_applicable");
    }
  });

  it("expands a row with the source and the places; a place selects it on the model", () => {
    const onSelect = vi.fn();
    renderBar(ctx(), { onSelect });
    const key = failing.standard!;
    const c = constraints.find((x) => x.id === key)!;
    fireEvent.click(item(key));
    expect(item(key).getAttribute("aria-expanded")).toBe("true");
    const detail = screen.getByRole("region", { name: `Constraint: ${c.label}` });
    expect(within(detail).getByTestId("constraint-source").textContent).toBe(sourceText(c, setName));
    expect(within(detail).getByTestId("constraint-source").textContent).toContain(builtIn.name);
    const place = detail.querySelector<HTMLButtonElement>(`button[data-check="${failing.id}"]`)!;
    expect(place.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(place);
    expect(onSelect).toHaveBeenCalledWith(failing);
  });

  it("collapses on Escape and returns focus to its row", () => {
    renderBar(ctx());
    fireEvent.click(item(numeric.id));
    const detail = screen.getByTestId("constraint-detail");
    fireEvent.keyDown(within(detail).getByRole("button", { name: `Edit ${numeric.title}` }), { key: "Escape" });
    expect(screen.queryByTestId("constraint-detail")).toBeNull();
    expect(document.activeElement).toBe(item(numeric.id));
  });

  it("shows a check with no constraint, with its results and nothing to edit", () => {
    renderBar(ctx());
    fireEvent.click(item(multi.check));
    const detail = screen.getByTestId("constraint-detail");
    expect(within(detail).queryByRole("button", { name: /^Edit / })).toBeNull();
    expect(within(detail).getByRole("list", { name: "Places checked" }).children.length).toBe(multiResults.length);
  });

  it("edits a value with the same request as the chip editor did", async () => {
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(numeric), { status: 200 }));
    // The workspace's `changeConstraint` (useChatSession): PATCH a value, DELETE the user's value.
    const onChange = vi.fn((id: string, value?: number | string) =>
      value === undefined ? removeConstraint("s1", id) : updateConstraint("s1", id, value),
    );
    renderBar(ctx({ onChange }));
    fireEvent.click(item(numeric.id));
    fireEvent.click(screen.getByRole("button", { name: `Edit ${numeric.title}` }));
    const input = screen.getByRole("textbox", { name: numeric.title }) as HTMLInputElement;
    expect(input.value).toBe(String(numeric.value));
    fireEvent.change(input, { target: { value: "3.5" } });
    fireEvent.click(screen.getByRole("button", { name: `Save ${numeric.title}` }));
    expect(onChange).toHaveBeenCalledWith(numeric.id, 3.5);
    expect(fetch).toHaveBeenCalledWith(`/api/sessions/s1/constraints/${numeric.id}`, expect.objectContaining({ method: "PATCH", body: JSON.stringify({ value: 3.5 }) }));
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("cancels an edit with Escape, keeping the detail open", () => {
    const onChange = vi.fn();
    renderBar(ctx({ onChange }));
    fireEvent.click(item(numeric.id));
    fireEvent.click(screen.getByRole("button", { name: `Edit ${numeric.title}` }));
    fireEvent.keyDown(screen.getByRole("textbox", { name: numeric.title }), { key: "Escape" });
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.getByTestId("constraint-detail")).toBeTruthy();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("goes back to the set's value for a value from the user's words", () => {
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(numeric), { status: 200 }));
    const own: Constraint = { ...numeric, source: "user", quote: "my words", value: 9 };
    const onChange = vi.fn((id: string, value?: number | string) =>
      value === undefined ? removeConstraint("s1", id) : updateConstraint("s1", id, value),
    );
    renderBar(ctx({ constraints: constraints.map((c) => (c.id === own.id ? own : c)), onChange }));
    fireEvent.click(item(own.id));
    expect(screen.getByTestId("constraint-source").textContent).toBe("From your words: “my words”");
    fireEvent.click(screen.getByRole("button", { name: "Back to the set's value" }));
    expect(onChange).toHaveBeenCalledWith(own.id);
    expect(fetch).toHaveBeenCalledWith(`/api/sessions/s1/constraints/${own.id}`, expect.objectContaining({ method: "DELETE" }));
  });

  it("names an edit and the set's value", () => {
    const edited: Constraint = { ...numeric, source: "edit", value: 9 };
    renderBar(ctx({ constraints: [edited] }));
    fireEvent.click(item(edited.id));
    expect(screen.getByTestId("constraint-source").textContent).toMatch(/^Edited by you\. Set value: /);
    expect(screen.getByTestId("constraint-source").textContent).toContain(setName);
  });

  it("shows the constraints before the first build, not checked yet, and editable", () => {
    const onChange = vi.fn();
    renderBar(ctx({ onChange }), { report: null });
    expect(items().length).toBe(constraints.length);
    for (const b of items()) expect(b.getAttribute("aria-label")).toMatch(/not checked yet$/);
    fireEvent.click(items()[0]);
    expect(screen.getByTestId("constraint-detail").textContent).toContain("Not checked yet");
    fireEvent.click(screen.getByRole("button", { name: `Edit ${constraints[0].title}` }));
    fireEvent.submit(screen.getByTestId("constraint-editor"));
    expect(onChange).toHaveBeenCalledWith(constraints[0].id, constraints[0].value);
  });

  it("says when nothing has been checked yet", () => {
    renderBar(ctx(), { report: null });
    expect(screen.getByTestId("checks-summary").textContent).toMatch(/^Not checked yet/);
    renderBar(ctx(), { report: null, onRun: () => {} });
    expect(screen.getAllByTestId("checks-summary")[1].textContent).toBe("Running checks…");
  });

  it("disables editing on a read-only link, saying why", () => {
    const own: Constraint = { ...numeric, source: "user", quote: "my words" };
    renderBar(ctx({ constraints: [own], disabledReason: "Shared links are read-only" }));
    fireEvent.click(item(own.id));
    const edit = screen.getByRole("button", { name: `Edit ${own.title}` }) as HTMLButtonElement;
    const back = screen.getByRole("button", { name: "Back to the set's value" }) as HTMLButtonElement;
    expect(edit.disabled).toBe(true);
    expect(back.disabled).toBe(true);
    expect(edit.title).toBe("Shared links are read-only");
    expect(screen.getByTestId("constraint-detail").textContent).toContain("Shared links are read-only");
  });

  it("sums up the checks, filters the pins, and runs the checks again", () => {
    const onPinFilter = vi.fn();
    const onRun = vi.fn();
    renderBar(ctx(), { against: "Against a board", pinFilter: "failed", onPinFilter, onRun, run: 2 });
    const panel = screen.getByRole("region", { name: "Constraints" });
    expect(within(panel).getByTestId("checks-summary").textContent).toMatch(new RegExp(`^${report.summary.pass} pass`));
    expect(panel.textContent).toContain("Against a board");
    const pins = within(panel).getByRole("radiogroup", { name: "Check pins" });
    expect(within(pins).getByRole("radio", { name: "Failed" }).getAttribute("aria-checked")).toBe("true");
    fireEvent.click(within(pins).getByRole("radio", { name: "All" }));
    expect(onPinFilter).toHaveBeenCalledWith("all");
    // Checks run on every build, so there is no "Run again"; only a failed load can be retried.
    expect(within(panel).queryByRole("button", { name: /Run again|Retry/ })).toBeNull();
  });

  it("offers Retry only when the checks couldn't be loaded", () => {
    const onRun = vi.fn();
    renderBar(ctx(), { error: "Couldn't load the checks.", onRun });
    const panel = screen.getByRole("region", { name: "Constraints" });
    fireEvent.click(within(panel).getByRole("button", { name: "Retry" }));
    expect(onRun).toHaveBeenCalled();
  });
});
