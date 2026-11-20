import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { constraintItem, openConstraints, pickCheck } from "./fixtures/constraints";
import { openExample } from "./fixtures/sessions";

// The canvas on real builds: the real backend (backend/app/e2e_api.py: the session API,
// geometry, checks and analysis maps, with only Claude scripted) serves the glTF and the check
// results, not the contract fixtures. The dev server sends requests carrying the
// cookie `e2e-backend=live` to it (playwright.config.ts). Every expected value is read from the
// real API or from `demos/b-parasite/CHANGES.md`, never written here.
test.skip(!!process.env.E2E_BASE_URL, "the scripted backend runs only with the local dev servers");

const repo = (...parts: string[]) => resolve(__dirname, "../..", ...parts);
// The scripted Claude moves the openings `demos/b-parasite/CHANGES.md` lists as moved when a message starts
// with this (`MOVE_OPENINGS` in backend/app/e2e_api.py).
const MOVE_OPENINGS = "Move the openings";
const CHANGES = readFileSync(repo("demos/b-parasite/CHANGES.md"), "utf8");
/** Components whose opening moved between the revisions, from the table in CHANGES.md. */
const MOVED = CHANGES.split("\n")
  .filter((l) => l.startsWith("| `"))
  .map((l) => l.split("|").map((c) => c.trim()))
  .filter((cells) => cells[4].startsWith("moved"))
  .map((cells) => cells[1].replaceAll("`", ""));

type Build = { id: string; mesh_url: string; checks_url: string };
type Gltf = { nodes: { extras?: { part?: string; faces?: { id: string; feature: string }[] } }[] };
type Result = { id: string; status: string; location?: { point: [number, number, number] } | null };
type Report = { build_id: string; results: Result[] };
/** The viewer's orbit centre is reported in board coordinates, rounded to 0.1 mm. */
const target = (p: [number, number, number]) =>
  p
    .map((v) => {
      const r = Math.round(v * 10) / 10;
      return r === 0 ? 0 : r;
    })
    .join(",");

test.beforeEach(async ({ context, baseURL }) => {
  await context.addCookies([{ name: "e2e-backend", value: "live", url: baseURL! }]);
});

const get = async <T>(request: APIRequestContext, url: string) => {
  const res = await request.get(url);
  expect(res.ok(), `${url}: ${res.status()}`).toBe(true);
  return (await res.json()) as T;
};

/** A new session on the real backend, once its first build (the existing enclosure) is ready. */
async function liveSession(request: APIRequestContext) {
  const health = await get<{ mock?: boolean }>(request, "/api/health");
  expect(health.mock).toBeUndefined();
  const session = await openExample(request);
  let builds: Build[] = [];
  await expect
    .poll(async () => (builds = await get<Build[]>(request, `/api/sessions/${session.id}/builds`)).length, {
      timeout: 60_000,
    })
    .toBeGreaterThan(0);
  return { id: session.id, build: builds[0] };
}

async function openCanvas(page: Page, id: string) {
  await page.goto(`/session/${id}`);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  const viewer = page.getByTestId("viewer");
  await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  return { viewer, canvas: page.getByRole("region", { name: "Canvas" }) };
}

const faces = (mesh: Gltf) => mesh.nodes.flatMap((n) => n.extras?.faces ?? []);

test("the canvas loads a real build's glTF from the API and fits it", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (err) => errors.push(err.message));
  const { id, build } = await liveSession(page.request);
  const meshLoaded = page.waitForResponse((r) => new URL(r.url()).pathname === build.mesh_url);
  const { viewer, canvas } = await openCanvas(page, id);
  const response = await meshLoaded;
  expect(response.ok()).toBe(true);

  // What the canvas got is the real backend's tessellation of the build.
  const served = (await response.json()) as Gltf;
  expect(served).toEqual(await get<Gltf>(page.request, build.mesh_url));
  await expect(viewer).toHaveAttribute("data-build", build.id);
  await expect(viewer).toHaveAttribute("data-fits", "true");
  // Every part of the real mesh is in the parts panel.
  const parts = [...new Set(served.nodes.map((n) => n.extras?.part).filter(Boolean))] as string[];
  expect(parts.length).toBeGreaterThan(0);
  const list = canvas.getByRole("list", { name: "Part list" });
  for (const part of parts) await expect(list.locator(`[data-part="${part}"]`)).toBeAttached();
  // A click on the middle of the model picks one of the real mesh's faces.
  const box = (await viewer.boundingBox())!;
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  const ids = new Set(faces(served).map((f) => f.id));
  await expect.poll(async () => (await viewer.getAttribute("data-selected")) ?? "").not.toBe("");
  expect(ids.has((await viewer.getAttribute("data-selected"))!)).toBe(true);
  expect(errors).toEqual([]);
});

test("check pins come from the real check results and a failure moves the camera to it", async ({ page }) => {
  const { id, build } = await liveSession(page.request);
  const report = await get<Report>(page.request, build.checks_url);
  expect(report.build_id).toBe(build.id);
  const located = report.results.filter((r) => r.status !== "untested" && r.location);
  const failed = located.filter((r) => r.status === "fail");
  expect(failed.length).toBeGreaterThan(0);

  const { viewer, canvas } = await openCanvas(page, id);
  await expect(canvas.getByTestId("check-pin")).toHaveCount(located.length);
  for (const r of located) {
    await expect(canvas.locator(`[data-testid=check-pin][data-check="${r.id}"]`)).toHaveAttribute("data-status", r.status);
  }
  // The Constraints panel has each result's constraint (or check), failing where one of its places fails.
  await openConstraints(canvas);
  for (const r of located) await expect(constraintItem(canvas, r)).toBeVisible();
  for (const r of failed) await expect(constraintItem(canvas, r)).toHaveAttribute("data-status", "fail");

  const [first, second = first] = failed;
  await pickCheck(canvas, first, { close: true });
  await expect(viewer).toHaveAttribute("data-target", target(first.location!.point));
  await expect(canvas.getByTestId("check-card")).toHaveAttribute("data-check", first.id);

  await canvas.getByTestId("check-card").getByRole("button", { name: "Close" }).click();
  await canvas.locator(`[data-testid=check-pin][data-check="${second.id}"]`).click();
  await expect(viewer).toHaveAttribute("data-target", target(second.location!.point));
  await expect(canvas.getByTestId("check-card")).toHaveAttribute("data-check", second.id);
});

test("a rebuild that moves an opening keeps the selected face's ID", async ({ page }) => {
  test.setTimeout(120_000);
  expect(MOVED.length).toBeGreaterThan(0);
  const { id, build } = await liveSession(page.request);
  const before = await get<Gltf>(page.request, build.mesh_url);
  const feature = `opening_${MOVED[0]}`;
  const face = faces(before).find((f) => f.feature === feature);
  expect(face, `a face of ${feature} on ${build.id}`).toBeTruthy();

  const { viewer, canvas } = await openCanvas(page, id);
  // Select the opening's feature: its faces, this one among them.
  await canvas.getByRole("region", { name: "Parts" }).getByRole("button", { name: `Select feature: ${feature}`, exact: true }).click();
  await expect(viewer).toHaveAttribute("data-selected", new RegExp(`(^|,)${face!.id}(,|$)`));
  await expect(page.getByTestId("selection-chip")).toBeVisible();

  // Claude moves the openings; the rebuild streams in and the canvas shows it.
  const posted = await page.request.post(`/api/sessions/${id}/messages`, { data: { text: MOVE_OPENINGS } });
  expect(posted.ok()).toBe(true);
  let rebuilt: Build | undefined;
  await expect
    .poll(
      async () => {
        const builds = await get<Build[]>(page.request, `/api/sessions/${id}/builds`);
        const status = (await get<{ status: string }>(page.request, `/api/sessions/${id}`)).status;
        rebuilt = status === "idle" ? builds.at(-1) : undefined;
        return rebuilt?.id ?? build.id;
      },
      { timeout: 90_000 },
    )
    .not.toBe(build.id);
  await expect(viewer).toHaveAttribute("data-build", rebuilt!.id, { timeout: 30_000 });
  await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 30_000 });

  // The opening moved...
  const diff = await get<{ features: { tag: string; change: string; distance: number | null }[] }>(
    page.request,
    `/api/sessions/${id}/builds/${rebuilt!.id}/diff?against=${build.id}`,
  );
  const moved = diff.features.find((f) => f.tag === feature);
  expect(moved?.change).toMatch(/moved/);
  expect(moved!.distance!).toBeGreaterThan(0);
  // ...and its face kept its ID: still selected in the canvas, and on the new mesh.
  const after = await get<Gltf>(page.request, rebuilt!.mesh_url);
  expect(faces(after).find((f) => f.id === face!.id)?.feature).toBe(feature);
  await expect(viewer).toHaveAttribute("data-selected", new RegExp(`(^|,)${face!.id}(,|$)`));
  await expect(page.getByTestId("selection-chip")).toBeVisible();
});
