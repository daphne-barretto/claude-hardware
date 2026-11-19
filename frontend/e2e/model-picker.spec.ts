import { expect, test, type Page } from "@playwright/test";
import { DEFAULT_CHOICES, MODELS, MORE_MODELS } from "../src/components/sites/claude-shell/data";

// "More models" in the model picker: a submenu of the catalogue's additional models
// (contracts/models.json); the choice shows on the picker and goes with the next message.
test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");

const SCREENSHOTS = process.env.SCREENSHOT_DIR;
const defaultName = MODELS.find((m) => m.id === DEFAULT_CHOICES.model)!.name;

async function open(page: Page, path: string) {
  await page.goto(path);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
}

test("More models opens a submenu, selects a model and sends it with the next message", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (err) => errors.push(err.message));
  await open(page, "/new");
  const chat = page.getByRole("region", { name: "Chat" });
  const pick = MORE_MODELS.at(-1)!;

  // The picker button is named after the chosen model, so find it by its menu instead.
  const picker = chat.locator('button[aria-haspopup="menu"]').filter({ hasText: defaultName });
  await expect(picker).toHaveCount(1);
  await picker.click();
  const more = chat.getByRole("menuitem", { name: /More models/ });
  await more.click();
  const submenu = chat.getByRole("menu", { name: "More models" });
  await expect(submenu.getByRole("menuitemradio")).toHaveCount(MORE_MODELS.length);
  if (SCREENSHOTS) await page.screenshot({ path: `${SCREENSHOTS}/more-models-submenu.png` });

  // Escape closes the submenu and returns focus to "More models"; the keyboard reopens it.
  await page.keyboard.press("Escape");
  await expect(submenu).toBeHidden();
  await expect(more).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(submenu).toBeVisible();
  await submenu.getByRole("menuitemradio", { name: new RegExp(pick.name) }).focus();
  await page.keyboard.press("Enter");
  await expect(chat.getByRole("menu")).toHaveCount(0);
  const chosen = chat.locator('button[aria-haspopup="menu"]').filter({ hasText: pick.name });
  await expect(chosen).toHaveCount(1);
  await expect(chosen).not.toContainText(defaultName);
  if (SCREENSHOTS) await chat.screenshot({ path: `${SCREENSHOTS}/more-models-selected.png` });

  const sent = page.waitForRequest((r) => r.url().endsWith("/messages") && r.method() === "POST");
  const accepted = page.waitForResponse((r) => r.url().endsWith("/messages") && r.request().method() === "POST");
  await chat.getByRole("textbox", { name: "Message" }).fill("Check the enclosure");
  await chat.getByRole("button", { name: "Send" }).click();
  expect((await sent).postDataJSON()).toMatchObject({ text: "Check the enclosure", model: pick.id });
  expect((await accepted).status()).toBe(202);
  expect(errors).toEqual([]);
});
