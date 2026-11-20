import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

// New, chat, canvas and Recents connected through sessions, against the mock
// session API (docs/architecture.md). Scenario values come from the fixture files.
test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");

const fixture = (name: string) => readFileSync(path.join(__dirname, "../../fixtures", name), "utf8");
const BUILD = JSON.parse(fixture("build.json")) as { id: string; board: { revision: string } };
const SESSION = JSON.parse(fixture("session.json")) as { title: string; prompt: string };
type Recorded = { type: string; text?: string; options?: { label: string }[] };
const recorded = fixture("session-events.jsonl")
  .split("\n")
  .filter(Boolean)
  .map((line) => JSON.parse(line).event as Recorded);
const progressLines = recorded.filter((e) => e.type === "progress").map((e) => e.text!);
const question = recorded.find((e) => e.type === "question")!;

async function open(page: Page, path: string) {
  await page.goto(path);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
}

/** Starts a session from New and waits until its URL is the session's page. Returns its ID. */
async function startFromNew(page: Page, text: string) {
  await page.getByRole("complementary", { name: "Sidebar" }).getByRole("link", { name: "New", exact: true }).click();
  await expect(page).toHaveURL(/\/new$/);
  const chat = page.getByRole("region", { name: "Chat" });
  await chat.getByRole("textbox", { name: "Message" }).fill(text);
  await chat.getByRole("button", { name: "Send" }).click();
  await expect(page).toHaveURL(/\/session\/[^/]+$/);
  return decodeURIComponent(new URL(page.url()).pathname.split("/").pop()!);
}

test("New → message → build on the canvas → reload keeps it → Recents", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (err) => errors.push(err.message));
  await open(page, "/");
  const id = await startFromNew(page, SESSION.prompt);
  const chat = page.getByRole("region", { name: "Chat" });
  const viewer = page.getByTestId("viewer");

  // The same workspace keeps streaming, and the canvas follows the same session.
  await expect(chat.getByText(SESSION.prompt)).toBeVisible();
  await expect(chat.getByTestId("build-ready")).toBeVisible({ timeout: 30_000 });
  await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  await expect(viewer).toHaveAttribute("data-build", BUILD.id);
  await expect(chat.getByRole("group", { name: question.text })).toBeVisible({ timeout: 30_000 });
  await expect(chat.getByTestId("assistant-message")).not.toHaveAttribute("aria-busy");

  // The build card shows its build.
  await chat.getByTestId("build-ready").click();
  await expect(viewer).toHaveAttribute("data-build", BUILD.id);

  // The session is in Recents, marked as the current page.
  const recents = page.getByRole("list", { name: "Recents" });
  const item = recents.locator(`a[data-session="${id}"]`);
  await expect(item).toHaveAttribute("href", `/session/${id}`);
  await expect(item).toHaveAttribute("aria-current", "page");
  await expect(item).toHaveText(SESSION.title);

  // A reload shows the same chat, the pending question and the build on the canvas.
  await page.reload();
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  await expect(chat.getByText(SESSION.prompt)).toBeVisible();
  await expect(chat.getByTestId("progress-line")).toHaveText(progressLines);
  await expect(chat.getByTestId("build-ready")).toHaveAttribute("data-build", BUILD.id);
  const asked = chat.getByRole("group", { name: question.text });
  await expect(asked.getByRole("button")).toHaveCount(question.options!.length);
  await expect(asked.getByRole("button").first()).toBeEnabled();
  await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  await expect(viewer).toHaveAttribute("data-build", BUILD.id);
  await expect(item).toHaveAttribute("aria-current", "page");

  // The answer is part of the session too: after another reload it is still answered.
  const option = question.options![0];
  await asked.getByRole("button", { name: new RegExp(option.label) }).click();
  await expect(chat.getByTestId("assistant-message")).toHaveCount(2);
  await page.reload();
  await expect(chat.getByTestId("assistant-message")).toHaveCount(2, { timeout: 30_000 });
  await expect(asked.getByRole("button", { name: new RegExp(option.label) })).toHaveAttribute("aria-pressed", "true");
  expect(errors).toEqual([]);
});

test("the session page replaces /new in the history", async ({ page }) => {
  await open(page, "/");
  await startFromNew(page, SESSION.prompt);
  await page.goBack();
  await expect(page).toHaveURL(/\/$/);
});

test("Recents lists sessions newest first and opens them", async ({ page }) => {
  await open(page, "/");
  const older = await startFromNew(page, SESSION.prompt);
  const newer = await startFromNew(page, SESSION.prompt);
  const recents = page.getByRole("list", { name: "Recents" });
  const ids = async () => recents.getByRole("link").evaluateAll((links) => links.map((l) => l.getAttribute("data-session")));
  await expect.poll(async () => (await ids()).includes(older) && (await ids()).includes(newer)).toBe(true);
  const order = await ids();
  expect(order.indexOf(newer)).toBeLessThan(order.indexOf(older));

  // Recents open the session page, which loads that session's chat.
  await recents.locator(`a[data-session="${older}"]`).click();
  await expect(page).toHaveURL(new RegExp(`/session/${older}$`));
  await expect(page.getByRole("region", { name: "Chat" }).getByText(SESSION.prompt)).toBeVisible();
  await expect(recents.locator('[aria-current="page"]')).toHaveCount(1);
  await expect(recents.locator(`a[data-session="${older}"]`)).toHaveAttribute("aria-current", "page");
});

test("the sidebar keeps the user's choice across navigation", async ({ page, request }) => {
  const created = await (await request.post("/api/sessions", { data: {} })).json();
  await open(page, "/new");
  await page.getByRole("button", { name: "Hide sidebar" }).click();
  await expect(page.getByRole("complementary", { name: "Sidebar" })).toHaveCount(0);

  await page.getByRole("button", { name: "Show sidebar" }).click();
  await page.locator(`a[data-session="${created.id}"]`).click();
  await expect(page).toHaveURL(new RegExp(`/session/${created.id}$`));
  await page.getByRole("button", { name: "Hide sidebar" }).click();
  // Search opens another page without the sidebar.
  await page.keyboard.press("Control+k");
  await page.getByRole("dialog", { name: "Search sessions" }).getByRole("button").first().click();
  await expect(page).toHaveURL(/\/session\/[^/]+$/);
  await expect(page.getByRole("complementary", { name: "Sidebar" })).toHaveCount(0);
});
