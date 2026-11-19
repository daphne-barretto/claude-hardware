import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { CHOOSE_STANDARDS } from "../src/components/sites/claude-shell/data";

// The team standards picker on New: the sets come from the session API (standards.yaml),
// the chosen set shows on the button and goes with the session's first message.
type StandardsSet = { id: string; name: string; standards: { title: string }[] };
const sets = JSON.parse(readFileSync(resolve(__dirname, "../../fixtures/standards.json"), "utf8")) as StandardsSet[];

async function open(page: Page) {
  await page.goto("/new");
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  return page.getByRole("region", { name: "Chat" });
}

test("the menu lists the standards sets the API serves", async ({ page, request }) => {
  const served = (await (await request.get("/api/standards")).json()) as StandardsSet[];
  expect(served.map((s) => s.id)).toEqual(sets.map((s) => s.id));
  const chat = await open(page);
  await chat.getByRole("button", { name: CHOOSE_STANDARDS }).click();
  const menu = chat.getByRole("menu", { name: "Constraint set" });
  await expect(menu.getByRole("menuitemradio")).toHaveCount(served.length);
  for (const s of served) {
    await expect(menu.getByRole("menuitemradio", { name: new RegExp(s.name) })).toContainText(`${s.standards.length} constraints`);
  }
});

test("nothing is hardcoded: another list from the API is what the menu shows", async ({ page }) => {
  const other: StandardsSet[] = [
    { id: "set_a", name: "Set A", standards: [{ title: "One" }] },
    { id: "set_b", name: "Set B", standards: [{ title: "One" }, { title: "Two" }] },
  ].map((s) => ({ ...s, standards: s.standards.map((st, i) => ({ ...st, id: `s${i}`, description: st.title, check: `c${i}` })) }));
  await page.route("**/api/standards", (route) => route.fulfill({ json: other }));
  const chat = await open(page);
  await chat.getByRole("button", { name: CHOOSE_STANDARDS }).click();
  await expect(chat.getByRole("menuitemradio")).toHaveText([/Set A.*1 constraint$/, /Set B.*2 constraints/]);
});

test("without standards from the API the picker is hidden", async ({ page }) => {
  await page.route("**/api/standards", (route) => route.fulfill({ status: 500, json: { detail: "down" } }));
  const chat = await open(page);
  await expect(chat.getByText("Import STEP")).toBeVisible();
  await expect(chat.getByRole("button", { name: CHOOSE_STANDARDS })).toHaveCount(0);
});

test("choose standards on New, send a message, and the request carries the choice", async ({ page }) => {
  test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");
  const chosen = sets[0];
  const chat = await open(page);
  await chat.getByRole("button", { name: CHOOSE_STANDARDS }).click();
  await chat.getByRole("menuitemradio", { name: new RegExp(chosen.name) }).click();
  const button = chat.getByRole("button", { name: chosen.name });
  await expect(button).toBeVisible();
  await button.click();
  await expect(chat.getByRole("menuitemradio", { name: new RegExp(chosen.name) })).toHaveAttribute("aria-checked", "true");
  await page.keyboard.press("Escape");

  const sent = page.waitForRequest((r) => r.url().endsWith("/messages") && r.method() === "POST");
  await chat.getByRole("textbox", { name: "Message" }).fill("Update the case to our standards");
  await chat.getByRole("button", { name: "Send" }).click();
  const request = await sent;
  expect(request.postDataJSON()).toMatchObject({ text: "Update the case to our standards", standards: chosen.id });
  const sessionId = /sessions\/([^/]+)\/messages/.exec(request.url())![1];
  await expect
    .poll(async () => (await (await page.request.get(`/api/sessions/${sessionId}`)).json()).standards)
    .toBe(chosen.id);
});
