import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

// Dictation works like claude.ai's: a recording bar with a level meter, a timer,
// cancel and keep in place of the idle controls; live words, dimmed until final; insertion at
// the cursor; Escape cancels, Enter keeps; a message when the microphone is blocked. Checked
// in the composers on home, New and a session page (the mock API's fixture session,
// docs/architecture.md), at 1024 and 1440 px wide.
//
// The browser's speech recognition is replaced by a stub the test drives through
// `window.__speech`, and the microphone by a generated tone so the meter moves.
const session = JSON.parse(readFileSync(path.join(__dirname, "../../fixtures/session.json"), "utf8"));
const screens = [
  { name: "home", path: "/" },
  { name: "new", path: "/new" },
  { name: "session", path: `/session/${session.id}` },
];
const widths = [1024, 1440];

async function open(page: Page, path: string, width: number, { blocked = false } = {}) {
  await page.setViewportSize({ width, height: 900 });
  await page.addInitScript((blocked: boolean) => {
    type Handler = ((e: unknown) => void) | null;
    class StubRecognition {
      continuous = false;
      interimResults = false;
      lang = "";
      onresult: Handler = null;
      onend: (() => void) | null = null;
      onerror: Handler = null;
      start() {
        (window as unknown as { __speech: StubRecognition }).__speech = this;
        if (blocked)
          setTimeout(() => {
            this.onerror?.({ error: "not-allowed" });
            this.onend?.();
          }, 0);
      }
      stop() {
        setTimeout(() => this.onend?.(), 0);
      }
      abort() {}
      hear(results: [string, boolean][]) {
        this.onresult?.({ results: results.map(([t, isFinal]) => Object.assign([{ transcript: t }], { isFinal })) });
      }
    }
    const w = window as unknown as Record<string, unknown>;
    w.SpeechRecognition = StubRecognition;
    w.webkitSpeechRecognition = StubRecognition;
    if (navigator.mediaDevices)
      navigator.mediaDevices.getUserMedia = async () => {
        const audio = new AudioContext();
        const tone = audio.createOscillator();
        const out = audio.createMediaStreamDestination();
        tone.connect(out);
        tone.start();
        return out.stream;
      };
  }, blocked);
  await page.goto(path);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  const box = page.getByRole("textbox", { name: "Message" });
  const frame = box.locator("../..");
  return {
    box,
    frame,
    dictate: frame.getByRole("button", { name: "Dictate" }),
    stopMic: frame.getByRole("button", { name: "Stop dictation" }),
    cancel: frame.getByRole("button", { name: "Cancel dictation" }),
    keep: frame.getByRole("button", { name: "Keep dictation" }),
    add: frame.getByRole("button", { name: "Add", exact: true }),
    send: frame.getByRole("button", { name: "Send" }),
    meter: frame.getByRole("meter", { name: "Microphone level" }),
    timer: frame.getByRole("timer", { name: "Recording time" }),
    interim: frame.getByTestId("dictation-interim"),
    hear: (results: [string, boolean][]) =>
      page.evaluate((r) => (window as unknown as { __speech: { hear: (r: [string, boolean][]) => void } }).__speech.hear(r), results),
  };
}

for (const width of widths) {
  for (const screen of screens) {
    test(`${screen.name} ${width}px: recording bar, live text, keep at the cursor`, async ({ page }, info) => {
      const c = await open(page, screen.path, width);
      await c.box.fill("Make the wall thicker");
      await c.box.evaluate((el: HTMLTextAreaElement) => el.setSelectionRange(13, 13));
      await page.screenshot({ path: info.outputPath(`dictation-${screen.name}-${width}-idle.png`) });

      await c.dictate.click();
      await expect(c.cancel).toBeVisible();
      await expect(c.keep).toBeVisible();
      await expect(c.meter).toBeVisible();
      await expect(c.add).toBeHidden();
      await expect(c.stopMic).toHaveAttribute("aria-pressed", "true");
      await expect(c.timer).toHaveText("0:00");
      await expect(c.timer).toHaveText("0:01", { timeout: 3000 });
      // The meter follows the microphone.
      await expect.poll(() => c.meter.getAttribute("aria-valuenow")).not.toBe("0");
      await page.screenshot({ path: info.outputPath(`dictation-${screen.name}-${width}-recording.png`) });

      await c.hear([["two", true], ["millimetres", false]]);
      await expect(c.box).toHaveValue("Make the wall two millimetres thicker");
      await expect(c.interim).toHaveText(" millimetres");
      const dim = await c.interim.evaluate((el) => getComputedStyle(el).color);
      const normal = await c.frame.getByTestId("dictation-final").evaluate((el) => getComputedStyle(el).color);
      expect(dim).not.toBe(normal);
      await page.screenshot({ path: info.outputPath(`dictation-${screen.name}-${width}-interim.png`) });

      await c.hear([["two", true], ["millimetres", true]]);
      await expect(c.interim).toHaveText("");
      await c.keep.click();
      await expect(c.dictate).toBeVisible();
      await expect(c.box).toHaveValue("Make the wall two millimetres thicker");
      await expect(c.box).toBeFocused();
      expect(await c.box.evaluate((el: HTMLTextAreaElement) => el.selectionStart)).toBe("Make the wall two millimetres".length);
    });
  }
}

for (const screen of screens) {
  test(`${screen.name}: Escape cancels only the dictated text, Enter keeps it`, async ({ page }) => {
    const c = await open(page, screen.path, 1440);
    await c.box.fill("Keep this");
    await c.dictate.click();
    await c.hear([["and drop this", true]]);
    await expect(c.box).toHaveValue("Keep this and drop this");
    await page.keyboard.press("Escape");
    await expect(c.box).toHaveValue("Keep this");
    await expect(c.dictate).toBeVisible();

    await c.dictate.click();
    await c.hear([["and this too", true]]);
    await page.keyboard.press("Enter");
    await expect(c.dictate).toBeVisible();
    await expect(c.box).toHaveValue("Keep this and this too");
  });

  test(`${screen.name}: the cancel button removes only the dictated text`, async ({ page }) => {
    const c = await open(page, screen.path, 1440);
    await c.box.fill("Keep this");
    await c.dictate.click();
    await c.hear([["drop this", false]]);
    await c.cancel.click();
    await expect(c.box).toHaveValue("Keep this");
  });

  test(`${screen.name}: blocked microphone shows how to allow it`, async ({ page }, info) => {
    const c = await open(page, screen.path, 1440, { blocked: true });
    await c.dictate.click();
    const alert = page.getByRole("alert").filter({ hasText: "Microphone access is blocked" });
    await expect(alert).toBeVisible();
    await expect(alert).toContainText("allow the microphone");
    await expect(c.dictate).toBeVisible();
    await page.screenshot({ path: info.outputPath(`dictation-${screen.name}-blocked.png`) });
  });
}

test("New: sending while listening stops dictation and sends what was heard", async ({ page }) => {
  test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");
  const c = await open(page, "/new", 1440);
  await c.dictate.click();
  await c.hear([["add a drain", true], ["hole", false]]);
  await c.send.click();
  await expect(c.dictate).toBeVisible();
  await expect(page.getByRole("region", { name: "Chat" }).getByText("Add a drain hole")).toBeVisible();
  await expect(c.box).toHaveValue("");
});
