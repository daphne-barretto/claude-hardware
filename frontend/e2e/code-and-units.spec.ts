import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { expandConstraint, openConstraints } from "./fixtures/constraints";

// Editing the code and units everywhere, against the mock session API: a code edit
// rebuilds and rechecks (or is refused with its line), and the mm/in toggle converts every
// displayed length. Scenario values come from the fixtures.
test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");

type Value = { value: number | string; unit?: string | null; kind?: string };
type Report = {
  summary: { pass: number; fail: number; untested: number };
  results: { id: string; check: string; standard?: string | null; title: string; status: string; measured?: Value | null; required?: Value | null; summary: string }[];
};

const fixture = <T,>(name: string) => JSON.parse(readFileSync(path.join(__dirname, "../../fixtures", name), "utf8")) as T;
const SOURCE = fixture<{ build_id: string; code: string }>("build-source.json");
const CHECKS = fixture<Report>("checks.json");
const SESSION = fixture<{ prompt: string }>("session.json");
const MM_LENGTH = /\d\s?mm\b/;

async function openCanvas(page: Page, request: APIRequestContext) {
  const session = await (await request.post("/api/sessions", { data: {} })).json();
  await request.post(`/api/sessions/${session.id}/messages`, { data: { text: SESSION.prompt } });
  await expect
    .poll(async () => (await (await request.get(`/api/sessions/${session.id}`)).json()).status, { timeout: 30_000 })
    .not.toBe("running");
  await page.goto(`/session/${session.id}`);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  const viewer = page.getByTestId("viewer");
  await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  return { id: session.id as string, viewer, canvas: page.getByRole("region", { name: "Canvas" }) };
}

async function openCode(page: Page, request: APIRequestContext) {
  const opened = await openCanvas(page, request);
  await opened.canvas.getByRole("button", { name: "Code" }).click();
  const panel = opened.canvas.getByRole("region", { name: "Code" });
  const editor = panel.getByRole("textbox", { name: "Generator code" });
  await expect(editor).toHaveValue(SOURCE.code);
  return { ...opened, panel, editor };
}

test("editing the code rebuilds and rechecks, and the canvas shows the rebuild", async ({ page, request }) => {
  const errors: string[] = [];
  page.on("pageerror", (err) => errors.push(err.message));
  const { id, viewer, panel, editor } = await openCode(page, request);
  const rebuildButton = panel.getByRole("button", { name: "Rebuild" });
  await expect(rebuildButton).toBeDisabled();
  await expect(rebuildButton).toHaveAttribute("title", /Edit the code/);

  const code = `${SOURCE.code}\n# Edited by hand\n`;
  await editor.fill(code);
  await expect(editor).toHaveAttribute("data-edited", "true");
  const put = page.waitForRequest((r) => r.method() === "PUT" && r.url().endsWith(`/sessions/${id}/source`));
  await rebuildButton.click();
  expect((await put).postDataJSON()).toEqual({ build_id: SOURCE.build_id, code });

  // The rebuild is shown, with its checks, and its source is the edited code.
  const s = CHECKS.summary;
  await expect(panel.getByTestId("code-rebuilt")).toContainText(`${s.pass} pass · ${s.fail} fail · ${s.untested} untested`);
  await expect(viewer).not.toHaveAttribute("data-build", SOURCE.build_id);
  const rebuilt = await viewer.getAttribute("data-build");
  await expect(editor).toHaveAttribute("data-build", rebuilt!);
  await expect(editor).toHaveValue(code);
  await expect(editor).toHaveAttribute("data-edited", "false");
  await expect(viewer).toHaveAttribute("data-state", "ready");
  const builds = await (await request.get(`/api/sessions/${id}/builds`)).json();
  expect(builds.map((b: { id: string }) => b.id)).toContain(rebuilt);
  const checks = await (await request.get(`/api/sessions/${id}/builds/${rebuilt}/checks`)).json();
  expect(checks.summary).toEqual(s);
  expect(errors).toEqual([]);
});

test("a syntax error shows inline with its line, and the edit is kept", async ({ page, request }) => {
  const { viewer, canvas, panel, editor } = await openCode(page, request);
  const lines = SOURCE.code.split("\n");
  // Break the last `def` line: drop its colon.
  const index = lines.findLastIndex((l) => /^def .*:\s*$/.test(l));
  const broken = [...lines.slice(0, index), lines[index].replace(/:\s*$/, ""), ...lines.slice(index + 1)].join("\n");
  await editor.fill(broken);
  await panel.getByRole("button", { name: "Rebuild" }).click();

  const alert = panel.getByRole("alert");
  await expect(alert).toContainText(`Line ${index + 1}: Syntax error`);
  await expect(alert).toContainText("Your edit is kept");
  await expect(editor).toHaveValue(broken);
  await expect(editor).toHaveAttribute("aria-invalid", "true");
  const marker = panel.locator('[data-error-line="true"]');
  await expect(marker).toHaveText(String(index + 1));
  // The editor scrolls with its line numbers (never on its own), to the error line.
  await expect(marker).toBeInViewport();
  expect(await editor.evaluate((el) => el.scrollTop)).toBe(0);
  await expect(viewer).toHaveAttribute("data-build", SOURCE.build_id);

  // Closing the panel doesn't lose the edit either; discarding it does.
  await page.keyboard.press("Escape");
  await expect(panel).toBeHidden();
  await canvas.getByRole("button", { name: "Code" }).click();
  await expect(editor).toHaveValue(broken);
  await panel.getByRole("button", { name: "Discard edits" }).click();
  await expect(editor).toHaveValue(SOURCE.code);
  await expect(panel.getByRole("alert")).toBeHidden();
});

test("inches convert the model size, check values and limits, and the build note", async ({ page, request }) => {
  const { canvas } = await openCanvas(page, request);
  const units = canvas.getByRole("radiogroup", { name: "Units" });

  // Check results: values, limits and summaries.
  const failing = CHECKS.results.filter((r) => r.status === "fail");
  const withLength = failing.find((r) => r.required?.unit === "mm" && typeof r.required.value === "number")!;
  // Its place in the Constraints panel: measured value and limit.
  await openConstraints(canvas);
  const failed = await expandConstraint(canvas, withLength);
  const row = failed.locator(`[data-check="${withLength.id}"]`);
  await expect(row.getByTestId("check-values")).toContainText(" mm");
  // Units are in View settings; then back to the Constraints panel.
  await canvas.getByRole("button", { name: "View settings" }).click();
  await units.getByRole("radio", { name: "in" }).click();
  await openConstraints(canvas);
  await expandConstraint(canvas, withLength);
  await expect(row.getByTestId("check-values")).toContainText(" in");
  await expect(row.getByTestId("check-values")).not.toContainText("mm");
  await expect(row.getByTestId("check-values")).toContainText(String(Number(((withLength.required!.value as number) / 25.4).toFixed(4))));
  for (const text of await failed.allTextContents()) expect(text).not.toMatch(MM_LENGTH);
  await expect(canvas.getByTestId("model-size")).toHaveText(/ in$/);

  // The build note is exported in inches; the request says so.
  await canvas.getByRole("button", { name: "Export" }).click();
  const exportPanel = canvas.getByRole("region", { name: "Export" });
  await expect(exportPanel.getByTestId("export-note-units")).toContainText("lengths in inches");
  const post = page.waitForRequest((r) => r.method() === "POST" && r.url().endsWith("/exports"));
  await exportPanel.getByRole("button", { name: /^Download/ }).click();
  expect((await post).postDataJSON()).toMatchObject({ units: "in" });
  const note = exportPanel.getByRole("list", { name: "Exported files" }).getByRole("link", { name: "build-note.md" });
  const text = await (await request.get((await note.getAttribute("href"))!)).text();
  expect(text).toContain("Units: in.");
  for (const line of text.split("\n").filter((l) => l.startsWith("| "))) expect(line).not.toMatch(MM_LENGTH);
});
