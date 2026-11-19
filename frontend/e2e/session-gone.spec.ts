import { expect, test, type Page } from "@playwright/test";

// A chat whose session is no longer on the server, against the mock API: the chat says
// so and offers a new chat, and the new chat is a working session.
test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");

async function open(page: Page, path: string) {
  await page.goto(path);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
}

test("a chat whose session is gone offers a new chat, which opens a working session", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (err) => errors.push(err.message));
  const gone = `session-gone-${Date.now()}`;
  expect((await page.request.get(`/api/sessions/${gone}`)).status()).toBe(404);

  await open(page, `/session/${gone}`);
  const chat = page.getByRole("region", { name: "Chat" });
  await chat.getByRole("textbox", { name: "Message" }).fill("Make the lid thicker");
  await chat.getByRole("button", { name: "Send" }).click();
  await expect(chat.getByText(/This chat is no longer on the server/)).toBeVisible();
  await expect(chat.getByText(/Could not reach Claude/)).toHaveCount(0);

  const created = page.waitForResponse((r) => r.url().endsWith("/api/sessions") && r.request().method() === "POST");
  await chat.getByRole("button", { name: "Start a new chat" }).click();
  const session = (await (await created).json()) as { id: string };
  await expect(page).toHaveURL(`/session/${encodeURIComponent(session.id)}`);
  expect(session.id).not.toBe(gone);

  // The new chat works: a message gets Claude's reply.
  const fresh = page.getByRole("region", { name: "Chat" });
  await expect(fresh.getByText(/no longer on the server/)).toHaveCount(0);
  await fresh.getByRole("textbox", { name: "Message" }).fill("Make an enclosure for this board");
  await fresh.getByRole("button", { name: "Send" }).click();
  await expect(fresh.getByTestId("assistant-message").first()).toBeVisible({ timeout: 30_000 });
  await expect(fresh.getByTestId("assistant-message").first()).not.toContainText(/no longer on the server|Could not reach Claude/);
  expect(errors).toEqual([]);
});
