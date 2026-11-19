import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { openExample } from "./fixtures/sessions";

// Direct editing and parameters against the mock session API, where setting a value
// locks the parameter and rebuilds (docs/architecture.md). Scenario values come from the fixtures.
test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");

type Parameter = { name: string; value: number | string; unit?: string | null; description?: string; locked: boolean };
type Report = { summary: { pass: number; fail: number; untested: number }; results: { title: string; status: string }[] };

const fixture = <T,>(name: string) => JSON.parse(readFileSync(path.join(__dirname, "../../fixtures", name), "utf8")) as T;
const PARAMS = fixture<Parameter[]>("parameters.json");
const BUILD = fixture<{ id: string }>("build.json");
const CHECKS = fixture<Report>("checks.json");
const SESSION = fixture<{ prompt: string }>("session.json");
const label = (p: Parameter) => p.description || p.name;
const LENGTH = PARAMS.find((p) => p.unit === "mm" && typeof p.value === "number")!;

/** A mock session whose recorded run has finished, opened on its canvas with the parameters panel. */
async function openParameters(page: Page, request: APIRequestContext) {
  const session = await openExample(request);
  await request.post(`/api/sessions/${session.id}/messages`, { data: { text: SESSION.prompt } });
  await expect
    .poll(async () => (await (await request.get(`/api/sessions/${session.id}`)).json()).status, { timeout: 30_000 })
    .not.toBe("running");
  await page.goto(`/session/${session.id}`);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  const viewer = page.getByTestId("viewer");
  await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  const canvas = page.getByRole("region", { name: "Canvas" });
  await canvas.getByRole("button", { name: "Parameters" }).click();
  const panel = canvas.getByRole("region", { name: "Parameters" });
  await expect(panel.getByRole("textbox")).toHaveCount(PARAMS.length);
  return { id: session.id as string, viewer, panel };
}

test("the panel lists every parameter with its value, unlocked", async ({ page, request }) => {
  const { panel } = await openParameters(page, request);
  for (const p of PARAMS) {
    const field = panel.getByRole("textbox", { name: label(p), exact: true });
    if (typeof p.value === "number") expect(Number(await field.inputValue())).toBeCloseTo(p.value);
    else await expect(field).toHaveValue(p.value);
  }
  await expect(panel.getByRole("button", { name: /^Unlock / })).toHaveCount(0);
});

test("typing a value locks it, rebuilds, and shows the failing checks", async ({ page, request }) => {
  const errors: string[] = [];
  page.on("pageerror", (err) => errors.push(err.message));
  const { id, viewer, panel } = await openParameters(page, request);
  const value = (LENGTH.value as number) + 1;
  const field = panel.getByRole("textbox", { name: label(LENGTH), exact: true });

  const patch = page.waitForRequest((r) => r.method() === "PATCH" && r.url().endsWith(`/parameters/${LENGTH.name}`));
  await field.fill(String(value));
  await field.press("Enter");
  expect((await patch).postDataJSON()).toEqual({ value });

  // Locked, with a lock.
  await expect(panel.locator(`[data-parameter="${LENGTH.name}"]`)).toHaveAttribute("data-locked", "true");
  await expect(panel.getByRole("button", { name: `Unlock ${label(LENGTH)}` })).toBeVisible();
  await expect(field).toHaveValue(String(value));

  // Rebuilt: a new build, shown on the canvas, with its checks.
  const rebuild = panel.getByRole("region", { name: "Rebuild" });
  await expect(rebuild).toHaveAttribute("data-state", "done", { timeout: 30_000 });
  const buildId = await rebuild.getAttribute("data-build");
  expect(buildId).not.toBe(BUILD.id);
  await expect(viewer).toHaveAttribute("data-build", buildId!);
  await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  const { summary } = CHECKS;
  await expect(rebuild.getByTestId("rebuild-summary")).toContainText(
    `${summary.pass} pass · ${summary.fail} fail · ${summary.untested} untested`,
  );
  const failing = rebuild.getByRole("list", { name: "Failing checks" }).getByRole("listitem");
  await expect(failing).toHaveCount(summary.fail);
  for (const r of CHECKS.results.filter((c) => c.status === "fail"))
    await expect(rebuild.getByText(r.title).first()).toBeVisible();

  const saved = (await (await request.get(`/api/sessions/${id}/parameters`)).json()) as Parameter[];
  expect(saved.find((p) => p.name === LENGTH.name)).toMatchObject({ value, locked: true });
  expect(errors).toEqual([]);
});

test("unlocking returns control to Claude", async ({ page, request }) => {
  const { id, panel } = await openParameters(page, request);
  const field = panel.getByRole("textbox", { name: label(LENGTH), exact: true });
  await field.fill(String((LENGTH.value as number) + 1));
  await field.press("Enter");
  const unlock = panel.getByRole("button", { name: `Unlock ${label(LENGTH)}` });
  await expect(unlock).toBeVisible();

  const patch = page.waitForRequest((r) => r.method() === "PATCH" && r.url().endsWith(`/parameters/${LENGTH.name}`));
  await unlock.click();
  expect((await patch).postDataJSON()).toEqual({ locked: false });
  await expect(unlock).toHaveCount(0);
  await expect(panel.locator(`[data-parameter="${LENGTH.name}"]`)).toHaveAttribute("data-locked", "false");
  await expect(panel.getByRole("status")).toContainText(`${label(LENGTH)} is Claude's again`);
  const saved = (await (await request.get(`/api/sessions/${id}/parameters`)).json()) as Parameter[];
  expect(saved.find((p) => p.name === LENGTH.name)?.locked).toBe(false);
});

test("a value that isn't a number is refused without an edit", async ({ page, request }) => {
  const { panel } = await openParameters(page, request);
  let patched = false;
  page.on("request", (r) => {
    if (r.method() === "PATCH") patched = true;
  });
  const field = panel.getByRole("textbox", { name: label(LENGTH), exact: true });
  await field.fill("wide");
  await field.press("Enter");
  await expect(field).toHaveAttribute("aria-invalid", "true");
  await expect(panel.getByText("Type a number.")).toBeVisible();
  await field.press("Escape");
  expect(Number(await field.inputValue())).toBeCloseTo(LENGTH.value as number);
  expect(patched).toBe(false);
  await expect(panel.locator(`[data-parameter="${LENGTH.name}"]`)).toHaveAttribute("data-locked", "false");
});
