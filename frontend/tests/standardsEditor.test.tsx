import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { StandardsSet } from "@/lib/session-api";
import type { CheckKind } from "@/lib/standards-api";
import { StandardsEditor, EDITOR_LABEL, NEW_KIND_EXPLAINED } from "@/components/sites/claude-shell/standards/StandardsEditor";
import {
  availableKinds,
  changed,
  copyName,
  newDraft,
  plainLabel,
  toDraft,
  toInput,
  valueProblem,
  withValue,
} from "@/components/sites/claude-shell/standards/rules";

// The standards editor (docs/constraints.md). The sets and the catalogue are the generated
// fixtures (fixtures/standards.json, fixtures/standards-kinds.json, from standards.yaml).
const fixture = <T,>(name: string) => JSON.parse(readFileSync(resolve(__dirname, "../../fixtures", name), "utf8")) as T;
const builtIn = fixture<StandardsSet[]>("standards.json")[0];
const kinds = fixture<CheckKind[]>("standards-kinds.json");
const kindOf = (check: string) => kinds.find((k) => k.check === check)!;
const wall = builtIn.standards.find((s) => s.check === "wall_thickness")!;

describe("rules", () => {
  it("reads a rule in plain language from its kind's phrase", () => {
    const draft = toDraft(wall);
    expect(plainLabel(draft, kindOf(wall.check))).toBe(wall.label);
    expect(plainLabel({ ...draft, value: "3" }, kindOf(wall.check))).toBe(kindOf(wall.check).phrase.replace("{value}", "3.0 mm"));
  });

  it("says early when a value is out of the check's range", () => {
    const kind = kindOf(wall.check);
    const draft = toDraft(wall);
    expect(valueProblem(draft, kind)).toBeNull();
    expect(valueProblem({ ...draft, value: "abc" }, kind)).toMatch(/number in mm/);
    expect(valueProblem({ ...draft, value: String(kind.min! / 2) }, kind)).toMatch(/At least/);
    expect(valueProblem({ ...draft, value: String(kind.max! * 2) }, kind)).toMatch(/At most/);
  });

  it("sends numbers as numbers and leaves the rest for the API to name", () => {
    const kind = kindOf(wall.check);
    expect(toInput({ ...toDraft(wall), value: "3" }, kind).value).toBe(3);
    expect(toInput({ ...toDraft(wall), value: "thick" }, kind).value).toBe("thick");
    expect(toInput({ ...toDraft(wall), aliases: "wall, walls ," }, kind).aliases).toEqual(["wall", "walls"]);
  });

  it("keeps a description that states the value in step with it", () => {
    const kind = kindOf(wall.check);
    const draft = { ...toDraft(wall), description: "No wall thinner than 2.0 mm.", stated: "2", value: "2" };
    expect(withValue(draft, "3", kind)).toMatchObject({ value: "3", description: "No wall thinner than 3.0 mm." });
    expect(withValue({ ...draft, description: "Thin walls crack." }, "3", kind)).toEqual({ value: "3" });
  });

  it("offers only the kinds a set lacks, and starts them from the built-in rule", () => {
    const drafts = builtIn.standards.map(toDraft).filter((d) => d.check !== "material");
    expect(availableKinds(kinds, drafts).map((k) => k.check)).toEqual(["material"]);
    expect(newDraft(kindOf("material")).value).toBe(String(kindOf("material").suggested!.value));
  });

  it("names copies so they never clash, and knows when a set changed", () => {
    expect(copyName("A", [{ name: "A" }])).toBe("A (copy)");
    expect(copyName("A", [{ name: "A" }, { name: "a (copy)" }])).toBe("A (copy 2)");
    const drafts = builtIn.standards.map(toDraft);
    expect(changed(builtIn, builtIn.name, drafts)).toBe(false);
    expect(changed(builtIn, builtIn.name, drafts.map((d, i) => (i ? d : { ...d, enabled: false })))).toBe(true);
  });
});

describe("StandardsEditor", () => {
  let sets: StandardsSet[];
  let requests: { method: string; url: string; body?: unknown }[];

  beforeEach(() => {
    const copy: StandardsSet = { ...builtIn, id: "set_copy", name: `${builtIn.name} (copy)`, built_in: false };
    sets = [builtIn];
    requests = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit = {}) => {
        const method = init.method ?? "GET";
        const body = init.body ? JSON.parse(String(init.body)) : undefined;
        requests.push({ method, url, body });
        const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status });
        if (url === "/api/standards/kinds") return json(kinds);
        if (url === "/api/standards" && method === "GET") return json(sets);
        if (url === "/api/standards" && method === "POST") {
          sets = [...sets, copy];
          return json(copy, 201);
        }
        if (url === "/api/standards/set_copy" && method === "PATCH") {
          if ((body as { standards: { value: unknown }[] }).standards.some((r) => r.value === 99)) {
            return json({ detail: "too thick", problems: [{ rule: 3, field: "value", message: "Too thick for the check." }] }, 422);
          }
          return json(copy);
        }
        return json({ detail: "no" }, 404);
      }),
    );
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  const open = async (onUse = vi.fn(), onSaved = vi.fn()) => {
    render(<StandardsEditor initial={null} active={builtIn.id} onUse={onUse} onSaved={onSaved} onClose={vi.fn()} />);
    const dialog = await screen.findByRole("dialog", { name: EDITOR_LABEL });
    await within(dialog).findAllByTestId("rule-label");
    return dialog;
  };

  it("shows the built-in set read-only, in plain language", async () => {
    const dialog = await open();
    expect(within(dialog).getByText(/read-only/)).toBeTruthy();
    expect(within(dialog).queryByRole("textbox", { name: `${wall.title} value` })).toBeNull();
    expect(within(dialog).queryByRole("button", { name: "Save changes" })).toBeNull();
    expect(within(dialog).getAllByTestId("rule-label")).toHaveLength(builtIn.standards.length);
  });

  it("duplicates the built-in set, edits a value and saves the whole set", async () => {
    const onSaved = vi.fn();
    const dialog = await open(vi.fn(), onSaved);
    fireEvent.click(within(dialog).getByRole("button", { name: "Duplicate" }));
    const value = await within(dialog).findByRole("textbox", { name: `${wall.title} value` });
    const save = within(dialog).getByRole("button", { name: "Save changes" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    fireEvent.change(value, { target: { value: "3" } });
    expect(within(dialog).getAllByTestId("rule-label")[3].textContent).toContain("3.0 mm");
    fireEvent.click(save);
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    const patch = requests.find((r) => r.method === "PATCH")!;
    const sent = (patch.body as { standards: { check: string; value: unknown }[] }).standards;
    expect(sent.find((r) => r.check === wall.check)!.value).toBe(3);
    expect(sent).toHaveLength(builtIn.standards.length);
    expect(requests.find((r) => r.method === "POST")!.body).toEqual({ name: `${builtIn.name} (copy)`, duplicate_of: builtIn.id });
  });

  it("shows the API's problems next to the rule", async () => {
    const dialog = await open();
    fireEvent.click(within(dialog).getByRole("button", { name: "Duplicate" }));
    const value = await within(dialog).findByRole("textbox", { name: `${wall.title} value` });
    fireEvent.change(value, { target: { value: "99" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save changes" }));
    expect(await within(dialog).findByText("Too thick for the check.")).toBeTruthy();
  });

  it("adds a rule from the catalogue and explains that new kinds need code", async () => {
    const dialog = await open();
    fireEvent.click(within(dialog).getByRole("button", { name: "Duplicate" }));
    const material = await within(dialog).findByRole("button", { name: `Remove ${builtIn.standards.find((s) => s.check === "material")!.title}` });
    fireEvent.click(material);
    fireEvent.click(within(dialog).getByRole("button", { name: "Add rule" }));
    const picker = within(dialog).getByRole("region", { name: "Add a rule" });
    fireEvent.click(within(picker).getByRole("button", { name: "Request a new kind of check" }));
    expect(within(picker).getByText(NEW_KIND_EXPLAINED)).toBeTruthy();
    fireEvent.click(within(picker).getByRole("button", { name: `Add ${kindOf("material").title}` }));
    expect(within(dialog).getAllByTestId("rule-label")).toHaveLength(builtIn.standards.length);
  });

  it("turns a rule off and uses a set in the chat", async () => {
    const onUse = vi.fn();
    const dialog = await open(onUse);
    fireEvent.click(within(dialog).getByRole("button", { name: "Duplicate" }));
    const toggle = await within(dialog).findByRole("switch", { name: `${wall.title} on` });
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-checked")).toBe("false");
    expect(within(dialog).getAllByTestId("rule-label")[3].textContent).toContain("off, not checked");
    fireEvent.click(within(dialog).getByRole("button", { name: "Discard changes" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Use in this chat" }));
    expect(onUse).toHaveBeenCalledWith("set_copy");
  });
});
