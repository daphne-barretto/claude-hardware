import { expect, test } from "@playwright/test";

test("the frontend loads the home page", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (err) => errors.push(err.message));

  await page.goto("/");

  await expect(page).toHaveTitle("Claude Hardware");
  await expect(page.getByPlaceholder("How can I help you today?")).toBeVisible();
  expect(errors).toEqual([]);
});
