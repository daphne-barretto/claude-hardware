import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { openExample } from "./fixtures/sessions";

// Color maps against the mock session API, which serves the contract fixtures
// (docs/architecture.md). Scenario values come from the fixture files, never from here.
test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");

type Spot = { id: string; message: string; location: { point: number[]; part?: string | null; faces?: string[] } };
type AnalysisMap = {
  type: string;
  standard: string;
  legend: { min: number; max: number; limit?: number | null; unit: "mm" | "deg" };
  parts: { values: (number | null)[] }[];
  flagged?: Spot[];
};

const fixture = (name: string) => JSON.parse(readFileSync(path.join(__dirname, "../../fixtures", name), "utf8"));
const BUILD = fixture("build.json") as { maps: string[] };
const SESSION = fixture("session.json") as { prompt: string };
const MAPS = BUILD.maps.map((t) => fixture(`maps/${t}.json`) as AnalysisMap);
const RESULTS = (fixture("checks.json") as { results: { standard?: string | null; title: string }[] }).results;
const LABELS: Record<string, string> = { wall_thickness: "Wall thickness", draft: "Draft" };

const fmt = (v: number, unit: "mm" | "deg") => (unit === "deg" ? `${v.toFixed(1)}°` : `${v.toFixed(1)} mm`);

/** A mock session whose recorded run has finished (it has the fixture build and is no longer running). */
async function finishedSession(request: APIRequestContext): Promise<string> {
  const session = await openExample(request);
  await request.post(`/api/sessions/${session.id}/messages`, { data: { text: SESSION.prompt } });
  await expect
    .poll(async () => (await (await request.get(`/api/sessions/${session.id}`)).json()).status, { timeout: 30_000 })
    .not.toBe("running");
  return session.id;
}

type Question = { question_id: string; options: { id: string }[] };
const QUESTION = readFileSync(path.join(__dirname, "../../fixtures/session-events.jsonl"), "utf8")
  .split("\n")
  .filter(Boolean)
  .map((line) => JSON.parse(line).event)
  .find((e) => e.type === "question") as Question;

/**
 * Answers the recorded run's question, so it is no longer open: fix requests post text, which
 * the session API takes only once no question waits.
 */
async function answered(request: APIRequestContext, id: string) {
  await request.post(`/api/sessions/${id}/messages`, {
    data: { text: "", answer: { question_id: QUESTION.question_id, option_id: QUESTION.options[0].id } },
  });
  await expect
    .poll(async () => (await (await request.get(`/api/sessions/${id}`)).json()).status, { timeout: 30_000 })
    .toBe("idle");
  return id;
}

async function openCanvas(page: Page, request: APIRequestContext, answer = false) {
  const finished = await finishedSession(request);
  const id = answer ? await answered(request, finished) : finished;
  await page.goto(`/session/${id}`);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  const viewer = page.getByTestId("viewer");
  await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  return { id, viewer, canvas: page.getByRole("region", { name: "Canvas" }) };
}

async function pickMap(page: Page, label: string) {
  const canvas = page.getByRole("region", { name: "Canvas" });
  await canvas.getByRole("button", { name: "Analysis maps" }).click();
  await canvas.getByRole("menu", { name: "Analysis maps" }).getByRole("menuitemradio", { name: label }).click();
}

for (const map of MAPS) {
  test(`the ${map.type} map colors the model and its legend matches the map's values`, async ({ page, request }) => {
    const errors: string[] = [];
    page.on("pageerror", (err) => errors.push(err.message));
    const { viewer, canvas } = await openCanvas(page, request);
    const gl = canvas.locator("canvas");
    const plain = await gl.screenshot();

    await pickMap(page, LABELS[map.type]);
    await expect(viewer).toHaveAttribute("data-map", map.type);
    // Every vertex of every part got a color from the map.
    const vertices = map.parts.reduce((n, p) => n + p.values.length, 0);
    await expect(viewer).toHaveAttribute("data-map-vertices", String(vertices));
    await expect.poll(async () => (await gl.screenshot()).equals(plain)).toBe(false);

    const legend = canvas.getByRole("region", { name: `${LABELS[map.type]} legend` });
    // The standard by its title as the checks name it, never its ID (fix brief 10).
    const title = RESULTS.find((r) => r.standard === map.standard)?.title;
    const header = legend.locator("p").first();
    if (title) await expect(header).toContainText(title);
    await expect(header).not.toContainText(map.standard);
    await expect(legend.getByTestId("legend-min")).toHaveText(fmt(map.legend.min, map.legend.unit));
    await expect(legend.getByTestId("legend-max")).toHaveText(fmt(map.legend.max, map.legend.unit));
    const { limit, min, max } = map.legend;
    if (limit != null) {
      await expect(legend.getByTestId("legend-limit-label")).toHaveText(`limit ${fmt(limit, map.legend.unit)}`);
      const position = Number(await legend.getByTestId("legend-limit").getAttribute("data-position"));
      expect(position).toBeCloseTo((limit - min) / (max - min));
    }
    const spots = legend.getByRole("list", { name: "Flagged spots" }).getByRole("listitem");
    await expect(spots).toHaveCount(map.flagged?.length ?? 0);
    for (const spot of map.flagged ?? []) await expect(legend).toContainText(spot.message);

    // Back to the part colors.
    await pickMap(page, "No map");
    await expect(viewer).toHaveAttribute("data-map", "");
    await expect(legend).toBeHidden();
    expect(errors).toEqual([]);
  });
}

test("the legend follows the mm/in toggle for length maps", async ({ page, request }) => {
  const map = MAPS.find((m) => m.legend.unit === "mm");
  test.skip(!map, "no length map in the fixtures");
  const { canvas } = await openCanvas(page, request);
  await pickMap(page, LABELS[map!.type]);
  await canvas.getByRole("radiogroup", { name: "Units" }).getByRole("radio", { name: "in" }).click();
  await expect(canvas.getByTestId("legend-max")).toHaveText(`${(map!.legend.max / 25.4).toFixed(3)} in`);
});

test("the map legend matches the reference screenshot", async ({ page, request }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const map = MAPS[0];
  await openCanvas(page, request);
  await pickMap(page, LABELS[map.type]);
  await expect(page.getByTestId("viewer")).toHaveAttribute("data-map", map.type);
  await expect(page.getByRole("region", { name: "Canvas" })).toHaveScreenshot("maps.png", { maxDiffPixelRatio: 0.02 });
});

test("a flagged spot is sent to Claude as a fix request with its faces selected", async ({ page, request }) => {
  const map = MAPS.find((m) => (m.flagged ?? []).length > 0)!;
  const spot = map.flagged![0];
  const { id, canvas } = await openCanvas(page, request, true);
  await pickMap(page, LABELS[map.type]);
  const item = canvas.locator(`[data-spot="${spot.id}"]`);
  const fix = item.getByRole("button", { name: "Ask Claude to fix" });
  await expect(fix).toBeEnabled({ timeout: 30_000 });

  const posted = page.waitForRequest((r) => r.method() === "POST" && r.url().endsWith(`/api/sessions/${id}/messages`));
  await fix.click();
  const body = (await posted).postDataJSON();
  expect(body.text).toContain(spot.message);
  expect(body.selection.faces).toEqual(spot.location.faces);
  expect(body.selection.point).toEqual(spot.location.point);
  await expect(item.getByRole("button", { name: "Sent to Claude" })).toBeDisabled();

  // The mock API stored it on the canvas's session with the selection, and the chat shows it.
  await expect
    .poll(async () => {
      const messages = (await (await request.get(`/api/sessions/${id}/messages`)).json()) as {
        role: string;
        text: string;
        selection?: { faces: string[] } | null;
      }[];
      return messages.find((m) => m.role === "user" && m.text.includes(spot.message))?.selection?.faces;
    })
    .toEqual(spot.location.faces);
  const chat = page.getByRole("region", { name: "Chat" });
  await expect(chat.getByText(spot.message).first()).toBeVisible();
  await expect(chat.getByTestId("selection-chip").first()).toBeVisible();
});

test("disabled flagged-spot buttons say why they are disabled", async ({ page, request }) => {
  const map = MAPS.find((m) => (m.flagged ?? []).length > 1);
  test.skip(!map, "no map with two flagged spots in the fixtures");
  const [first, second] = map!.flagged!;
  const { id, canvas } = await openCanvas(page, request, true);
  await pickMap(page, LABELS[map!.type]);
  const sentItem = canvas.locator(`[data-spot="${first.id}"]`);
  const otherItem = canvas.locator(`[data-spot="${second.id}"]`);
  await expect(sentItem.getByRole("button", { name: "Ask Claude to fix" })).toBeEnabled({ timeout: 30_000 });

  // Hold the fix request so Claude stays busy while the buttons are checked.
  let release!: () => void;
  const held = new Promise<void>((resolve) => (release = resolve));
  await page.route(`**/api/sessions/${id}/messages`, async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    await held;
    await route.fallback();
  });
  await sentItem.getByRole("button", { name: "Ask Claude to fix" }).click();

  const sent = sentItem.getByRole("button", { name: "Sent to Claude" });
  await expect(sent).toBeDisabled();
  await expect(sent).toHaveAttribute("title", /sent to Claude/i);
  const waiting = otherItem.getByRole("button", { name: "Ask Claude to fix" });
  await expect(waiting).toBeDisabled();
  await expect(waiting).toHaveAttribute("title", /Claude is still replying/i);

  release();
  await expect(waiting).toBeEnabled({ timeout: 30_000 });
  await expect(waiting).not.toHaveAttribute("title", /.+/);
  await expect(sent).toHaveAttribute("title", /sent to Claude/i);
});
