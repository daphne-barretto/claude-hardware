import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { changesHeading, featureChangeDetail, featureChangeText, parameterChangeText } from "../src/components/canvas/history";
import type { Build, BuildDiff, BuildSource, Parameter } from "../src/components/canvas/api";
import { openExample } from "./fixtures/sessions";

// Revisions, compare, history and code against the mock session API, which serves
// the builds of both revisions and the diff between them (docs/architecture.md). Scenario values
// come from the fixture files, never from here.
test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");

const fixture = <T>(name: string) => JSON.parse(readFileSync(path.join(__dirname, "../../fixtures", name), "utf8")) as T;
const LATEST = fixture<Build>("build.json");
const PREVIOUS = fixture<Build>("previous-build.json");
const DIFF = fixture<BuildDiff>("diff.json");
const SOURCE = fixture<BuildSource>("build-source.json");
const SESSION = fixture<{ prompt: string }>("session.json");
const rev = (b: Build) => `v${b.board!.revision}`;

/** A mock session whose recorded run has finished, so it has the builds on both revisions. */
async function sessionWithBuilds(request: APIRequestContext): Promise<string> {
  const session = await openExample(request);
  await request.post(`/api/sessions/${session.id}/messages`, { data: { text: SESSION.prompt } });
  await expect
    .poll(async () => (await (await request.get(`/api/sessions/${session.id}/builds`)).json()).length, {
      timeout: 30_000,
    })
    .toBe(2);
  return session.id;
}

async function open(page: Page, id: string) {
  await page.goto(`/session/${id}`);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  const viewer = page.getByTestId("viewer");
  await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  return { viewer, canvas: page.getByRole("region", { name: "Canvas" }) };
}

test("the revision menu lists the builds of both revisions", async ({ page, request }) => {
  const { canvas, viewer } = await open(page, await sessionWithBuilds(request));
  await expect(viewer).toHaveAttribute("data-build", LATEST.id);
  await canvas.getByRole("button", { name: "Revision" }).click();
  const items = canvas.getByRole("menu", { name: "Revisions" }).getByRole("menuitemradio");
  await expect(items).toHaveText([new RegExp(`${rev(LATEST)}.*Build 2`), new RegExp(`${rev(PREVIOUS)}.*Build 1`)]);
  await items.nth(1).click();
  await expect(viewer).toHaveAttribute("data-build", PREVIOUS.id);
  await expect(canvas.getByRole("button", { name: "Revision" })).toHaveText(rev(PREVIOUS));
});

test("compare shows both revisions with the diff between them", async ({ page, request }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const { canvas, viewer } = await open(page, await sessionWithBuilds(request));
  const compare = canvas.getByRole("button", { name: `Compare with ${rev(PREVIOUS)}` });
  await expect(compare).toHaveAttribute("aria-pressed", "false");
  await expect(page.getByTestId("compare-viewer")).toHaveCount(0);

  const diffRequest = page.waitForRequest((r) => r.url().includes(`/builds/${LATEST.id}/diff?against=${PREVIOUS.id}`));
  await compare.click();
  await diffRequest;
  await expect(compare).toHaveAttribute("aria-pressed", "true");
  await expect(compare).toHaveText(`Comparing with ${rev(PREVIOUS)}`);

  // Both revisions, side by side: the older on the left, the shown build on the right.
  const left = page.getByTestId("compare-viewer");
  await expect(left).toHaveAttribute("data-build", PREVIOUS.id);
  await expect(left).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  await expect(viewer).toHaveAttribute("data-build", LATEST.id);
  await expect(viewer).toHaveAttribute("data-compare", PREVIOUS.id);
  await expect(page.getByTestId("compare-label-old")).toHaveText(`${rev(PREVIOUS)} · Build 1`);
  await expect(page.getByTestId("compare-label-new")).toHaveText(`${rev(LATEST)} · Build 2`);
  const l = (await left.boundingBox())!;
  const r = (await viewer.boundingBox())!;
  expect(l.x + l.width).toBeLessThanOrEqual(r.x + 1);
  expect(Math.abs(l.width - r.width)).toBeLessThan(2);

  // The change list is the build diff: one row per feature change, then the parameters.
  const changes = page.getByRole("region", { name: "Changes" });
  await expect(changes.getByTestId("changes-heading")).toHaveText(changesHeading(DIFF, PREVIOUS));
  const rows = changes.getByRole("list", { name: "Feature changes" }).getByRole("listitem");
  await expect(rows).toHaveCount(DIFF.features.length);
  for (const [i, c] of DIFF.features.entries()) {
    await expect(rows.nth(i)).toHaveAttribute("data-tag", c.tag);
    await expect(rows.nth(i)).toContainText(featureChangeText(c));
    const detail = featureChangeDetail(c, "mm");
    if (detail) await expect(rows.nth(i)).toContainText(detail);
  }
  const params = changes.getByRole("list", { name: "Parameter changes" }).getByRole("listitem");
  await expect(params).toHaveCount(DIFF.parameters.length);
  await expect(params.first()).toHaveText(parameterChangeText(DIFF.parameters[0]));

  // Inches convert the distances.
  await canvas.getByRole("radiogroup", { name: "Units" }).getByRole("radio", { name: "in" }).click();
  const moved = DIFF.features.findIndex((c) => c.change.includes("moved"));
  await expect(rows.nth(moved)).toContainText(featureChangeDetail(DIFF.features[moved], "in"));

  await compare.click();
  await expect(compare).toHaveAttribute("aria-pressed", "false");
  await expect(left).toHaveCount(0);
  await expect(changes).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("undo in the history restores a previous build", async ({ page, request }) => {
  const id = await sessionWithBuilds(request);
  // A hand edit makes a third build, the latest.
  const params = (await (await request.get(`/api/sessions/${id}/parameters`)).json()) as Parameter[];
  const param = params.find((p) => typeof p.value === "number")!;
  await request.patch(`/api/sessions/${id}/parameters/${param.name}`, { data: { value: (param.value as number) + 1 } });
  const builds = (await (await request.get(`/api/sessions/${id}/builds`)).json()) as Build[];
  expect(builds).toHaveLength(3);
  const edited = builds[2];

  const { canvas, viewer } = await open(page, id);
  await expect(viewer).toHaveAttribute("data-build", edited.id);
  await canvas.getByRole("button", { name: "History" }).click();
  const history = page.getByRole("region", { name: "History" });
  const entries = history.getByRole("list", { name: "Builds" }).getByRole("listitem");
  // The timeline, newest first.
  await expect(entries).toHaveCount(3);
  for (const [i, b] of [...builds].reverse().entries()) await expect(entries.nth(i)).toHaveAttribute("data-build", b.id);
  await expect(entries.first()).toContainText("Latest");
  await expect(entries.first().getByRole("button", { name: /^Restore/ })).toHaveCount(0);

  const restore = history.getByRole("button", { name: `Restore ${rev(LATEST)} · Build 2` });
  await expect(restore).toBeEnabled();
  const posted = page.waitForRequest((r) => r.method() === "POST" && r.url().endsWith(`/builds/${LATEST.id}/restore`));
  await restore.click();
  await posted;

  // A new build of the restored design, which the canvas shows.
  await expect(viewer).toHaveAttribute("data-restored-from", LATEST.id);
  const restoredId = await viewer.getAttribute("data-build");
  expect([...builds.map((b) => b.id)]).not.toContain(restoredId);
  await expect(entries).toHaveCount(4);
  await expect(entries.first()).toHaveAttribute("data-build", restoredId!);
  await expect(entries.first().getByTestId("history-restored")).toHaveText("Restored from Build 2");
  const after = (await (await request.get(`/api/sessions/${id}/parameters`)).json()) as Parameter[];
  expect(after.find((p) => p.name === param.name)!.value).toBe(param.value);

  // Show puts an earlier build on the canvas without restoring it.
  await history.getByRole("button", { name: `Show ${rev(PREVIOUS)} · Build 1` }).click();
  await expect(viewer).toHaveAttribute("data-build", PREVIOUS.id);
  await expect(history.locator(`li[data-build="${PREVIOUS.id}"]`)).toHaveAttribute("data-shown", "true");
});

test("the code view shows the source of the build on the canvas", async ({ page, request }) => {
  const { canvas, viewer } = await open(page, await sessionWithBuilds(request));
  await canvas.getByRole("button", { name: "Revision" }).click();
  await canvas.getByRole("menuitemradio", { name: new RegExp(PREVIOUS.id) }).click();
  await expect(viewer).toHaveAttribute("data-build", PREVIOUS.id);
  await canvas.getByRole("button", { name: "Code", exact: true }).click();
  const code = page.getByRole("region", { name: "Code" });
  await expect(code.getByTestId("code-file")).toHaveText(`${SOURCE.filename} · ${SOURCE.backend} · ${rev(PREVIOUS)}`);
  await expect(code.getByTestId("code-source")).toHaveAttribute("data-build", PREVIOUS.id);
  const lines = SOURCE.code.split("\n").filter((line) => line.trim());
  await expect(code.getByTestId("code-source")).toContainText(lines[0]);
  await expect(code.getByTestId("code-source")).toContainText(lines[lines.length - 1].trim());
});
