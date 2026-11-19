import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test, type Locator, type Page } from "@playwright/test";
import { openConstraints } from "./fixtures/constraints";
import { CHOOSE_STANDARDS } from "../src/components/sites/claude-shell/data";
import { EDITOR_LABEL, MANAGE_STANDARDS } from "../src/components/sites/claude-shell/standards/StandardsEditor";

// The standards editor (docs/constraints.md): "Manage constraint sets…" under the constraint set picker opens
// it; the built-in set is read-only, a duplicate is edited and saved, and a rule is added from the
// catalogue. In a session on the real backend (backend/app/e2e_api.py: real builds and checks,
// only Claude scripted), switching to the edited set rechecks the design: the wall thickness
// constraint in the canvas's Constraints panel turns from pass to fail, and the chat says what changed. Values come
// from the API (standards.yaml and the catalogue), never written here.
test.skip(!!process.env.E2E_BASE_URL, "the mock and scripted backends run only with the local dev servers");

const SHOTS = resolve(__dirname, "../test-results/screenshots/standards-editor");
mkdirSync(SHOTS, { recursive: true });
const shot = (page: Page, name: string) => page.screenshot({ path: resolve(SHOTS, `${name}.png`) });

type Rule = { id: string; check: string; title: string; value: number | string; unit?: string | null; label: string };
type StandardsSet = { id: string; name: string; built_in: boolean; standards: Rule[] };
type Kind = { check: string; title: string };
// A design from scratch the scripted Claude builds for real: an open box, walls at its default.
const BOX = "make a box 40 x 30 x 20 mm";

async function builtIn(page: Page): Promise<StandardsSet> {
  const sets = (await (await page.request.get("/api/standards")).json()) as StandardsSet[];
  return sets.find((s) => s.built_in)!;
}

async function openEditor(page: Page, trigger: Locator): Promise<Locator> {
  await trigger.click();
  await page.getByRole("menu", { name: "Constraint set" }).getByRole("menuitem", { name: MANAGE_STANDARDS }).click();
  const dialog = page.getByRole("dialog", { name: EDITOR_LABEL });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByTestId("rule-label").first()).toBeVisible();
  return dialog;
}

test("on New: the built-in set is read-only; a duplicate is edited, a rule added, and the copy chosen", async ({ page }) => {
  await page.goto("/new");
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  const team = await builtIn(page);
  const wall = team.standards.find((s) => s.check === "wall_thickness")!;
  const chat = page.getByRole("region", { name: "Chat" });
  const dialog = await openEditor(page, chat.getByRole("button", { name: CHOOSE_STANDARDS }));

  await expect(dialog.getByText(/read-only/)).toBeVisible();
  await expect(dialog.getByTestId("rule-label")).toHaveText(team.standards.map((s) => new RegExp(`^${escape(s.label)}`)));
  await shot(page, "built-in-read-only");

  await dialog.getByRole("button", { name: "Duplicate" }).click();
  const value = dialog.getByRole("textbox", { name: `${wall.title} value` });
  await expect(value).toHaveValue(String(wall.value));
  await value.fill("3");
  await expect(dialog.locator(`li[data-check="${wall.check}"]`).getByTestId("rule-label")).toContainText("3.0 mm");
  // A rule out of the check's range is caught before it is sent.
  await value.fill("0.01");
  await expect(dialog.locator(`li[data-check="${wall.check}"]`).getByRole("alert")).toContainText("At least");
  await value.fill("3");
  await shot(page, "duplicate-edited");
  await save(dialog);

  // Remove a rule, then add it back from the catalogue.
  const kinds = (await (await page.request.get("/api/standards/kinds")).json()) as Kind[];
  const material = kinds.find((k) => k.check === "material")!;
  const materialRule = team.standards.find((s) => s.check === "material")!;
  await dialog.getByRole("button", { name: `Remove ${materialRule.title}` }).click();
  await dialog.getByRole("button", { name: "Add rule" }).click();
  const picker = dialog.getByRole("region", { name: "Add a rule" });
  await picker.getByRole("button", { name: "Request a new kind of check" }).click();
  await expect(picker.getByText(/needs new code/)).toBeVisible();
  await shot(page, "add-rule-catalogue");
  await picker.getByRole("button", { name: `Add ${material.title}` }).click();
  await expect(dialog.locator(`li[data-check="${material.check}"]`)).toBeVisible();
  await save(dialog);

  await dialog.getByRole("button", { name: "Use in this chat" }).click();
  await expect(dialog).toBeHidden();
  await expect(chat.getByRole("button", { name: `${team.name} (copy)` })).toBeVisible();
});

test("in a session: switching to an edited set rechecks the design and the Constraints panel shows the change", async ({ page, context, baseURL }) => {
  test.setTimeout(240_000);
  await context.addCookies([{ name: "e2e-backend", value: "live", url: baseURL! }]);
  const created = await page.request.post("/api/sessions", { data: {} });
  expect(created.status()).toBe(201);
  const { id } = (await created.json()) as { id: string };
  expect((await page.request.post(`/api/sessions/${id}/messages`, { data: { text: BOX } })).status()).toBe(202);
  await expect
    .poll(async () => ((await (await page.request.get(`/api/sessions/${id}/builds`)).json()) as unknown[]).length, { timeout: 120_000 })
    .toBeGreaterThan(0);

  const team = await builtIn(page);
  const wall = team.standards.find((s) => s.check === "wall_thickness")!;
  await page.goto(`/session/${id}`);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  const canvas = page.getByRole("region", { name: "Canvas" });
  await expect(canvas.getByTestId("check-pins")).toBeAttached({ timeout: 60_000 });
  await openConstraints(canvas);
  const wallCheck = canvas.locator(`[data-testid=constraint-item][data-constraint="${wall.id}"]`);
  await expect(wallCheck).toHaveAttribute("data-status", "pass", { timeout: 60_000 });

  const chat = page.getByRole("region", { name: "Chat" });
  const header = chat.getByRole("button", { name: `Constraint set: ${team.name}` });
  await expect(header).toBeEnabled({ timeout: 60_000 });
  const dialog = await openEditor(page, header);
  await dialog.getByRole("button", { name: "Duplicate" }).click();
  const value = dialog.getByRole("textbox", { name: `${wall.title} value` });
  await value.fill("3");
  await save(dialog);
  await dialog.getByRole("button", { name: "Use in this chat" }).click();
  await expect(dialog).toBeHidden();

  await expect(canvas.getByTestId("check-pins")).toBeAttached({ timeout: 60_000 });
  await openConstraints(canvas);
  await expect(wallCheck).toHaveAttribute("data-status", "fail", { timeout: 120_000 });
  await expect(chat.getByText(/Now failing/)).toBeVisible({ timeout: 60_000 });
  await expect(chat.getByRole("button", { name: `Constraint set: ${team.name} (copy)` })).toBeVisible();
  await expect(chat.getByRole("button", { name: /^Constraint set:/ })).toBeEnabled({ timeout: 60_000 });
  await shot(page, "session-rechecked");

  // Back to the editor: the copy is the set in use. Remove a rule, add it back from the
  // catalogue, and save: the session rechecks against the saved set.
  const again = await openEditor(page, chat.getByRole("button", { name: /^Constraint set:/ }));
  await expect(again.getByText("This chat uses this set.")).toBeVisible();
  const kinds = (await (await page.request.get("/api/standards/kinds")).json()) as Kind[];
  const draft = kinds.find((k) => k.check === "draft_angle")!;
  await again.getByRole("button", { name: `Remove ${team.standards.find((s) => s.check === draft.check)!.title}` }).click();
  await again.getByRole("button", { name: "Add rule" }).click();
  await again.getByRole("region", { name: "Add a rule" }).getByRole("button", { name: `Add ${draft.title}` }).click();
  await expect(again.locator(`li[data-check="${draft.check}"]`)).toBeVisible();
  await shot(page, "session-editor");
  await save(again);
  await again.getByRole("button", { name: "Close" }).click();
  await expect(again).toBeHidden();
  await expect(chat.getByText(/You updated the constraint set/)).toBeVisible({ timeout: 120_000 });
});

/** Saves the set and waits until the editor shows the saved version (nothing left to discard). */
async function save(dialog: Locator) {
  await dialog.getByRole("button", { name: "Save changes" }).click();
  await expect(dialog.getByRole("button", { name: "Discard changes" })).toBeHidden();
  await expect(dialog.getByRole("button", { name: "Save changes" })).toBeDisabled();
}

function escape(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
