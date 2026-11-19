import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import path from "node:path";
import { answerOpenQuestion } from "./inventory/ui-inventory";

// The parts panel's Select filters and box select explain themselves: a hint says how to
// select in the current mode until the first selection, hovering the model previews what a click
// would pick in that mode, and box select says how to use it and that Esc stops it.

test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");

const SESSION = JSON.parse(readFileSync(path.resolve(__dirname, "../../fixtures/session.json"), "utf8")) as { prompt: string };

async function openSession(page: Page) {
  const session = (await (await page.request.post("/api/sessions", { data: {} })).json()) as { id: string };
  const api = `/api/sessions/${session.id}`;
  await page.request.post(`${api}/messages`, { data: { text: SESSION.prompt } });
  await expect.poll(async () => ((await (await page.request.get(api)).json()) as { status: string }).status, { timeout: 30_000 }).not.toBe("running");
  await answerOpenQuestion(page, api);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/session/${session.id}`);
  const viewer = page.getByTestId("viewer");
  await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  return { viewer, parts: page.getByRole("region", { name: "Parts" }) };
}

test("the Select filters say what a click picks, preview it on hover, and box select says how to stop", async ({ page }) => {
  const { viewer, parts } = await openSession(page);
  const hint = parts.getByTestId("select-hint");
  const mode = (name: string) => parts.getByRole("radiogroup", { name: "Select" }).getByRole("radio", { name });
  await expect(hint).toHaveText("Click the model to select a face");
  await expect(mode("Face")).toHaveAttribute("title", "Click a face on the model");
  await expect(parts.getByRole("button", { name: "Box select" })).toHaveAttribute("title", "Drag a box on the model to select several");

  // Hovering the model previews what a click would pick: a face, then the whole part.
  const box = (await viewer.boundingBox())!;
  const [x, y, w, h] = ((await viewer.getAttribute("data-model-rect")) ?? "").split(",").map(Number);
  const at = { x: box.x + x + w * 0.6, y: box.y + y + h * 0.5 };
  const hover = async () => {
    await page.mouse.move(at.x - 3, at.y);
    await page.mouse.move(at.x, at.y);
  };
  await hover();
  await expect(viewer).toHaveAttribute("data-hover", /^face:/);
  await mode("Part").click();
  await expect(hint).toHaveText("Click the model to select a part");
  await hover();
  await expect(viewer).toHaveAttribute("data-hover", /^part:/);
  await mode("Edge").click();
  await expect(hint).toHaveText("Click the model to select an edge");

  // Box select: visibly on, says how to use it and how to stop; Esc stops it.
  await mode("Face").click();
  await parts.getByRole("button", { name: "Box select" }).click();
  await expect(parts.getByRole("button", { name: "Box select" })).toHaveAttribute("aria-pressed", "true");
  await expect(viewer).toHaveAttribute("data-box", "true");
  await expect(viewer).toHaveClass(/cursor-crosshair/);
  await expect(hint).toHaveText("Drag on the model · Esc to stop");
  await hover();
  await expect(viewer).toHaveAttribute("data-hover", /^face:/); // a click still picks in box select
  await viewer.focus();
  await page.keyboard.press("Escape");
  await expect(viewer).toHaveAttribute("data-box", "false");
  await expect(hint).toHaveText("Click the model to select a face");

  // Once something is selected, the hint has done its job.
  await page.mouse.click(at.x, at.y);
  await expect(viewer).not.toHaveAttribute("data-selected", "");
  await expect(hint).toHaveCount(0);
});
