import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type Locator, type Page } from "@playwright/test";
import { LONG_PASTE_CHARS } from "../src/components/sites/claude-shell/data";

// The composer behaves like the claude.ai one: the text takes the full width
// above a row of controls, grows line by line up to half the viewport and then scrolls
// inside, wraps long words, sends on Enter, turns long pastes into a chip, and goes back to
// one line after sending. Checked on home, New and a session page (the mock API's fixture
// session, docs/architecture.md), at 1024 and 1440 px wide.
const session = JSON.parse(readFileSync(path.join(__dirname, "../../fixtures/session.json"), "utf8"));
const screens = [
  { name: "home", path: "/" },
  { name: "new", path: "/new" },
  { name: "session", path: `/session/${session.id}` },
];
const widths = [1024, 1440];
const height = 900;

async function open(page: Page, path: string) {
  await page.goto(path);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
}

/** The composer's message box, the box around it, and its controls. */
function composer(page: Page) {
  const box = page.getByRole("textbox", { name: "Message" });
  const frame = box.locator("../..");
  return {
    box,
    add: frame.getByRole("button", { name: "Add", exact: true }),
    send: frame.getByRole("button", { name: "Send" }),
  };
}

async function metrics(box: Locator) {
  return box.evaluate((el: HTMLTextAreaElement) => ({
    height: el.getBoundingClientRect().height,
    clientHeight: el.clientHeight,
    scrollHeight: el.scrollHeight,
    clientWidth: el.clientWidth,
    scrollWidth: el.scrollWidth,
    overflowY: getComputedStyle(el).overflowY,
    lineHeight: parseFloat(getComputedStyle(el).lineHeight),
  }));
}

async function bottom(l: Locator) {
  const b = (await l.boundingBox())!;
  return b.y + b.height;
}

async function paste(box: Locator, text: string) {
  await box.evaluate((el, t) => {
    el.focus();
    const data = new DataTransfer();
    data.setData("text/plain", t);
    el.dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
  }, text);
}

for (const width of widths) {
  test.describe(`at ${width} px`, () => {
    test.use({ viewport: { width, height } });

    for (const screen of screens) {
      test(`${screen.name}: full-width text above the controls, growing to half the viewport`, async ({ page }, info) => {
        await open(page, screen.path);
        const { box, add, send } = composer(page);
        await expect(send).toBeDisabled();
        const one = await metrics(box);
        expect(one.height).toBeLessThan(one.lineHeight * 2);

        // Grows line by line, with the controls staying on their own row below the text.
        await box.click();
        let last = one.height;
        for (let i = 1; i <= 4; i++) {
          await page.keyboard.type(`Line ${i} of a longer request`);
          await page.keyboard.press("Shift+Enter");
          const m = await metrics(box);
          expect(m.height).toBeGreaterThan(last);
          expect(m.overflowY).toBe("hidden");
          expect(m.scrollHeight).toBeLessThanOrEqual(m.clientHeight);
          last = m.height;
          expect((await add.boundingBox())!.y).toBeGreaterThanOrEqual(await bottom(box));
          expect((await send.boundingBox())!.y).toBeGreaterThanOrEqual(await bottom(box));
        }
        const boxRect = (await box.boundingBox())!;
        expect((await add.boundingBox())!.x).toBeLessThan(boxRect.x + 40);
        expect((await send.boundingBox())!.x).toBeGreaterThan(boxRect.x + boxRect.width - 60);
        await expect(send).toBeEnabled();
        await page.screenshot({ path: info.outputPath(`composer-${screen.name}-${width}.png`) });

        // Past half the viewport it stops growing and scrolls inside.
        await box.fill(Array.from({ length: 60 }, (_, i) => `Line ${i + 1}`).join("\n"));
        const tall = await metrics(box);
        expect(tall.height).toBeLessThanOrEqual(height / 2 + 1);
        expect(tall.height).toBeGreaterThan(height / 2 - tall.lineHeight);
        expect(tall.scrollHeight).toBeGreaterThan(tall.clientHeight);
        expect(tall.overflowY).toBe("auto");
        expect((await add.boundingBox())!.y).toBeGreaterThanOrEqual(await bottom(box));
        expect((await send.boundingBox())!.y + 32).toBeLessThanOrEqual(height);
        await page.screenshot({ path: info.outputPath(`composer-${screen.name}-${width}-max.png`) });

        // Long words and URLs wrap rather than scroll sideways.
        await box.fill(`See https://example.com/${"a".repeat(400)} and ${"b".repeat(300)}`);
        const wrapped = await metrics(box);
        expect(wrapped.scrollWidth).toBeLessThanOrEqual(wrapped.clientWidth);
        expect(wrapped.height).toBeGreaterThan(wrapped.lineHeight * 2);
      });
    }

    test("chat: Enter sends and the box goes back to one line with focus kept", async ({ page }, info) => {
      test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");
      await open(page, "/new");
      const chat = page.getByRole("region", { name: "Chat" });
      const { box } = composer(page);
      await box.click();
      await page.keyboard.type("Add a drain hole");
      await page.keyboard.press("Shift+Enter");
      await page.keyboard.type("near the lowest corner");
      await expect(box).toHaveValue("Add a drain hole\nnear the lowest corner");

      // Enter that confirms an IME composition does not send.
      await box.evaluate((el) =>
        el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", isComposing: true, bubbles: true, cancelable: true })),
      );
      await expect(box).toHaveValue("Add a drain hole\nnear the lowest corner");

      await page.keyboard.press("Enter");
      await expect(page).toHaveURL(/\/session\/[^/]+$/);
      await expect(chat.getByText("near the lowest corner").last()).toBeVisible();
      await expect(box).toHaveValue("");
      await expect(box).toBeFocused();
      const m = await metrics(box);
      expect(m.height).toBeLessThan(m.lineHeight * 2);
      await page.screenshot({ path: info.outputPath(`composer-sent-${width}.png`) });
    });

    test("chat: a long paste becomes a chip, a short one stays in the box", async ({ page }, info) => {
      await open(page, "/new");
      const { box, send } = composer(page);
      const long = "Sample notes, pasted\n".repeat(Math.ceil(LONG_PASTE_CHARS / 20));
      await paste(box, long);
      const chip = page.getByTestId("pasted-chip");
      await expect(chip).toBeVisible();
      await expect(chip).toContainText(`Pasted content · ${Math.ceil(LONG_PASTE_CHARS / 20)} lines`);
      await expect(box).toHaveValue("");
      await expect(send).toBeEnabled();
      // The chip sits above the text.
      expect(await bottom(chip)).toBeLessThanOrEqual((await box.boundingBox())!.y);
      await page.screenshot({ path: info.outputPath(`composer-pasted-${width}.png`) });

      // A short paste goes into the box as usual (no chip).
      await page.getByRole("button", { name: "Remove pasted content" }).click();
      await expect(chip).toHaveCount(0);
      await box.focus();
      await paste(box, "a short note");
      await expect(chip).toHaveCount(0);
    });
  });
}
