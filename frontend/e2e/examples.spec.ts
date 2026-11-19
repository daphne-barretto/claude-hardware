import { expect, test, type Page } from "@playwright/test";
import { emptySession } from "./fixtures/sessions";

// Sessions start empty. New offers no examples: ready-made work is the demos in Recents,
// which open as the browser's own sessions (demos.spec.ts).

function consoleErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
  page.on("pageerror", (e) => errors.push(e.message));
  return errors;
}

test("New offers no examples: the demos in Recents are the ready-made work", async ({ page }) => {
  const errors = consoleErrors(page);
  await page.goto("/new");
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  const chat = page.getByRole("region", { name: "Chat" });
  await expect(chat.getByRole("button", { name: "Import STEP" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Examples" })).toHaveCount(0);
  await expect(chat).not.toContainText("Or open an example");
  await expect(chat.getByRole("button", { name: /^Open example / })).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("a new session without an example has no board and an empty canvas", async ({ page, request }) => {
  const session = await emptySession(request);
  expect(session.board).toBeNull();
  const builds = await (await request.get(`/api/sessions/${session.id}/builds`)).json();
  expect(builds).toEqual([]);

  const errors = consoleErrors(page);
  await page.goto(`/session/${session.id}`);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  await expect(page.getByTestId("hardware-canvas")).toHaveAttribute("data-empty", "true");
  await expect(page.getByRole("region", { name: "Canvas" })).toContainText("This canvas has no parts yet");
  await expect(page.getByRole("region", { name: "Examples" })).toHaveCount(0);
  await expect(page.locator("body")).not.toContainText(/undefined|null/);
  expect(errors).toEqual([]);
});
