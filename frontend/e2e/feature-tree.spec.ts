import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type APIRequestContext, type Locator, type Page } from "@playwright/test";
import { openExample } from "./fixtures/sessions";

// The feature tree against the mock session API (docs/architecture.md). Parts,
// features, lines and parameters come from fixtures/, never from here.
test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");

type Face = { id: string; feature: string };
type Extras = { part: string; faces: Face[]; edges: Face[] };
type FeatureSource = { tag: string; part: string; lines: [number, number] };
type Source = { build_id: string; code: string; features: FeatureSource[] };
type Parameter = { name: string; value: number | string; unit: string | null; feature: string | null };
type Gltf = { nodes: { extras?: Extras }[] };

const raw = (name: string) => readFileSync(path.join(__dirname, "../../fixtures", name), "utf8");
const MESH = JSON.parse(raw("enclosure.gltf")) as Gltf;
const SOURCE = JSON.parse(raw("build-source.json")) as Source;
const PARAMS = JSON.parse(raw("parameters.json")) as Parameter[];
const BUILD = JSON.parse(raw("build.json")) as { id: string };
const SESSION = JSON.parse(raw("session.json")) as { prompt: string };
const PARTS = MESH.nodes.flatMap((n) => (n.extras?.part ? [n.extras] : []));
const FACES = PARTS.flatMap((p) => p.faces.map((f) => ({ ...f, part: p.part })));

/** Every feature: tagged on the model's faces or created in the code, by part. */
const FEATURES = new Map<string, Set<string>>(PARTS.map((p) => [p.part, new Set(p.faces.map((f) => f.feature))]));
for (const f of SOURCE.features) {
  if (!FEATURES.has(f.part)) FEATURES.set(f.part, new Set());
  FEATURES.get(f.part)!.add(f.tag);
}
const facesOf = (part: string, tag: string) => FACES.filter((f) => f.part === part && f.feature === tag).map((f) => f.id);
// A feature with faces of its own that the code creates, and one with several faces.
const MULTI = FACES.find((f) => f.feature !== f.part && facesOf(f.part, f.feature).length > 1)!;
const CODED = SOURCE.features.find((f) => f.tag !== MULTI.feature && facesOf(f.part, f.tag).length > 0)!;

async function openCanvas(page: Page, request: APIRequestContext) {
  const session = await openExample(request);
  await request.post(`/api/sessions/${session.id}/messages`, { data: { text: SESSION.prompt } });
  await expect
    .poll(async () => (await (await request.get(`/api/sessions/${session.id}/builds`)).json()).length, { timeout: 30_000 })
    .toBeGreaterThan(0);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/session/${session.id}`);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  const viewer = page.getByTestId("viewer");
  await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  const parts = page.getByRole("region", { name: "Parts" });
  // The code's features are in: the tree is complete.
  await expect(parts.locator("li[data-feature]")).toHaveCount([...FEATURES.values()].reduce((n, s) => n + s.size, 0));
  return { viewer, parts, canvas: page.getByRole("region", { name: "Canvas" }), chat: page.getByRole("region", { name: "Chat" }) };
}

const row = (parts: Locator, part: string, tag: string) => parts.locator(`li[data-part="${part}"] li[data-feature="${tag}"]`);
const featureButton = (parts: Locator, tag: string) =>
  parts.getByRole("button", { name: `Select feature: ${tag}`, exact: true });

test("every tagged feature appears, grouped by part, with its parameters", async ({ page, request }) => {
  const { parts } = await openCanvas(page, request);
  for (const [part, tags] of FEATURES) {
    const list = parts.getByRole("list", { name: `Features of ${part}`, exact: true });
    await expect(list.locator(":scope > li")).toHaveCount(tags.size);
    for (const tag of tags) {
      const r = row(parts, part, tag);
      await expect(r).toHaveCount(1);
      await expect(r).toHaveAttribute("data-faces", String(facesOf(part, tag).length));
      // Features with faces select them; the rest have nothing to select.
      await expect(featureButton(r, tag)).toHaveCount(facesOf(part, tag).length ? 1 : 0);
      for (const p of PARAMS.filter((x) => x.feature === tag)) {
        await expect(r.locator(`[data-param="${p.name}"]`)).toContainText(p.name);
      }
    }
  }
  // Each part row names its material (the design's, while the mesh doesn't name one per part).
  const material = PARAMS.find((p) => p.name === "material" && !p.feature);
  for (const part of FEATURES.keys()) {
    const material_ = parts.locator(`li[data-part="${part}"] > div`).getByTestId("part-material");
    if (material) await expect(material_).toHaveText(String(material.value));
  }
});

test("clicking a feature selects its faces; selecting a face highlights its feature", async ({ page, request }) => {
  const { parts, viewer, chat } = await openCanvas(page, request);
  const faces = facesOf(MULTI.part, MULTI.feature);
  await featureButton(parts, MULTI.feature).click();
  await expect(viewer).toHaveAttribute("data-selected", faces.join(","));
  await expect(chat.getByTestId("selection-chip")).toContainText(MULTI.feature);
  await expect(row(parts, MULTI.part, MULTI.feature)).toHaveAttribute("data-highlighted", "true");
  await expect(featureButton(parts, MULTI.feature)).toHaveAttribute("aria-current", "true");

  // A face picked on the model highlights its feature, and only that one.
  await page.keyboard.press("Escape");
  await expect(parts.locator("li[data-highlighted=true]")).toHaveCount(0);
  const box = (await viewer.boundingBox())!;
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  await expect.poll(async () => (await viewer.getAttribute("data-selected")) ?? "").not.toBe("");
  const picked = await viewer.getAttribute("data-selected");
  expect(picked, "the click picked one face of the model").not.toContain(",");
  const face = FACES.find((f) => f.id === picked)!;
  await expect(row(parts, face.part, face.feature)).toHaveAttribute("data-highlighted", "true");
  await expect(parts.locator("li[data-highlighted=true]")).toHaveCount(1);

  // Select by ID, the same: one face of a feature highlights it.
  await parts.getByRole("textbox", { name: "Select by ID" }).fill(faces[1]);
  await page.keyboard.press("Enter");
  await expect(viewer).toHaveAttribute("data-selected", faces[1]);
  await expect(row(parts, MULTI.part, MULTI.feature)).toHaveAttribute("data-highlighted", "true");
});

test("each entry links to its lines in the code view", async ({ page, request }) => {
  const { parts, canvas } = await openCanvas(page, request);
  const lines = SOURCE.code.split("\n");
  for (const f of [SOURCE.features[0], SOURCE.features[SOURCE.features.length - 1]]) {
    await row(parts, f.part, f.tag).getByRole("button", { name: `Lines of ${f.tag}`, exact: true }).click();
    const code = canvas.getByRole("region", { name: "Code" });
    const pre = code.getByTestId("code-source");
    await expect(pre).toHaveAttribute("data-lines", f.lines.join("-"));
    await expect(code.getByTestId("code-feature")).toContainText(f.tag);
    const marked = pre.locator("[data-marked]");
    await expect(marked).toHaveCount(f.lines[1] - f.lines[0] + 1);
    await expect(marked.first()).toHaveAttribute("data-line", String(f.lines[0]));
    expect(await marked.allTextContents()).toEqual(lines.slice(f.lines[0] - 1, f.lines[1]));
    await expect(marked.first()).toBeInViewport();
  }
  // The Code button opens the code with nothing marked.
  await canvas.getByRole("button", { name: "Code", exact: true }).click();
  await expect(canvas.getByRole("region", { name: "Code" })).toBeHidden();
  await canvas.getByRole("button", { name: "Code", exact: true }).click();
  await expect(canvas.getByTestId("code-source")).toHaveAttribute("data-lines", "");
  await expect(canvas.getByTestId("code-source").locator("[data-marked]")).toHaveCount(0);
});

test("the tree updates after a rebuild", async ({ page, request }) => {
  // The rebuild: the next build_ready brings a build whose mesh retags one feature's faces,
  // whose code moves every feature down a line, and whose parameters drive the new feature.
  const NEW = "rebuilt_feature";
  const mesh = JSON.parse(raw("enclosure.gltf")) as Gltf;
  for (const n of mesh.nodes) for (const f of n.extras?.faces ?? []) if (f.feature === MULTI.feature) f.feature = NEW;
  for (const n of mesh.nodes) for (const e of n.extras?.edges ?? []) if (e.feature === MULTI.feature) e.feature = NEW;
  const features = SOURCE.features
    .filter((f) => f.tag !== MULTI.feature)
    .map((f) => ({ ...f, lines: [f.lines[0] + 1, f.lines[1] + 1] }));
  features.push({ tag: NEW, part: MULTI.part, lines: [1, 1] });
  const rebuild = { ...JSON.parse(raw("build.json")), id: "build-rebuilt", mesh_url: "/rebuild/mesh", source_url: "/rebuild/source" };
  const source = { ...SOURCE, build_id: rebuild.id, code: `# rebuilt\n${SOURCE.code}`, features };
  const param = { name: `${NEW}_size`, value: 4, unit: "mm", locked: false, min: null, max: null, description: "", feature: NEW };

  let release!: () => void;
  const released = new Promise<void>((r) => (release = r));
  let rebuilt = false;
  await page.route("**/rebuild/mesh", (r) => r.fulfill({ contentType: "model/gltf+json", body: JSON.stringify(mesh) }));
  await page.route("**/rebuild/source", (r) => r.fulfill({ json: source }));
  await page.route(/\/api\/sessions\/[^/]+\/parameters$/, async (r) =>
    rebuilt ? r.fulfill({ json: [...PARAMS, param] }) : r.fallback(),
  );
  // The page's own stream, which page.route sees: without a shared worker the tab connects
  // itself (src/lib/session-stream.ts) instead of through the browser's shared one.
  await page.addInitScript(() => Reflect.deleteProperty(window, "SharedWorker"));
  await page.route(/\/api\/sessions\/[^/]+\/events/, async (r) => {
    await released;
    const data = JSON.stringify({ type: "build_ready", message_id: null, build: rebuild });
    await r.fulfill({ contentType: "text/event-stream", body: `id: 10000\nevent: build_ready\ndata: ${data}\n\n` });
  });

  const { parts, viewer } = await openCanvas(page, request);
  await expect(viewer).toHaveAttribute("data-build", BUILD.id);
  await expect(row(parts, MULTI.part, MULTI.feature)).toHaveCount(1);
  await expect(parts.locator(`li[data-feature="${NEW}"]`)).toHaveCount(0);

  rebuilt = true;
  release();
  await expect(viewer).toHaveAttribute("data-build", rebuild.id, { timeout: 30_000 });
  await expect(viewer).toHaveAttribute("data-state", "ready");
  const added = row(parts, MULTI.part, NEW);
  await expect(added).toHaveAttribute("data-faces", String(facesOf(MULTI.part, MULTI.feature).length));
  await expect(added.getByRole("button", { name: `Lines of ${NEW}`, exact: true })).toHaveText("L1");
  await expect(added.locator(`[data-param="${param.name}"]`)).toBeVisible();
  await expect(row(parts, MULTI.part, MULTI.feature)).toHaveCount(0);
  const moved = features.find((f) => f.tag === CODED.tag)!;
  await expect(row(parts, CODED.part, CODED.tag).getByRole("button", { name: `Lines of ${CODED.tag}`, exact: true })).toHaveText(
    moved.lines[0] === moved.lines[1] ? `L${moved.lines[0]}` : `L${moved.lines[0]}–${moved.lines[1]}`,
  );
  // The new feature selects its faces.
  await featureButton(parts, NEW).click();
  await expect(viewer).toHaveAttribute("data-selected", facesOf(MULTI.part, MULTI.feature).join(","));
});

test("part rows show and hide or isolate their part, with its features", async ({ page, request }) => {
  const { parts, viewer } = await openCanvas(page, request);
  const names = [...FEATURES.keys()].filter((p) => PARTS.some((x) => x.part === p));
  // Isolating a part hides every other listed part, the board revision's parts included.
  await expect(viewer).toHaveAttribute("data-board-parts", /./);
  const listed = [...PARTS.map((p) => p.part), ...((await viewer.getAttribute("data-board-parts")) ?? "").split(",")];
  for (const part of names) {
    const r = parts.locator(`li[data-part="${part}"]`);
    await parts.getByRole("button", { name: `Hide: ${part}`, exact: true }).click();
    await expect(viewer).toHaveAttribute("data-hidden", part);
    await expect(r.getByRole("list", { name: `Features of ${part}`, exact: true })).toBeVisible();
    await parts.getByRole("button", { name: `Hide: ${part}`, exact: true }).click();
    await expect(viewer).toHaveAttribute("data-hidden", "");

    await parts.getByRole("button", { name: `Isolate: ${part}`, exact: true }).click();
    await expect(viewer).toHaveAttribute("data-hidden", listed.filter((p) => p !== part).join(","));
    await parts.getByRole("button", { name: `Isolate: ${part}`, exact: true }).click();
    await expect(viewer).toHaveAttribute("data-hidden", "");
  }
});
