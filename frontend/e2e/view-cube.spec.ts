import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type APIRequestContext, type Locator, type Page } from "@playwright/test";
import { emptySession, openExample } from "./fixtures/sessions";

// The view cube, its turn, roll and home buttons, the turntable orbit, the axis triad and the
// view label. Runs against the mock session API.
test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");

const SESSION = JSON.parse(readFileSync(path.join(__dirname, "../../fixtures/session.json"), "utf8")) as {
  prompt: string;
};

/** The 26 targets, as the design names them. */
const ORDER = ["Front", "Back", "Right", "Left", "Top", "Bottom"];
const AXES: [string, string][] = [
  ["Right", "Left"],
  ["Back", "Front"],
  ["Top", "Bottom"],
];
const TARGETS = (() => {
  const all: { key: string; label: string; faces: number }[] = [];
  for (const x of [-1, 0, 1])
    for (const y of [-1, 0, 1])
      for (const z of [-1, 0, 1]) {
        const names = [x, y, z].flatMap((c, i) => (c ? [AXES[i][c > 0 ? 0 : 1]] : []));
        if (!names.length) continue;
        names.sort((a, b) => ORDER.indexOf(a) - ORDER.indexOf(b));
        const kind = ["face", "edge", "corner"][names.length - 1];
        const joined = names.join("-");
        all.push({ key: joined.toLowerCase(), label: kind === "face" ? joined : `${joined} ${kind}`, faces: names.length });
      }
  return all;
})();
const FACES = TARGETS.filter((t) => t.faces === 1);
/** The data-view the canvas reports for a target: the home corner is "iso". */
const viewOf = (key: string) => (key === "front-right-top" ? "iso" : key);

const TURNS = {
  up: "Rotate to the face above",
  down: "Rotate to the face below",
  left: "Rotate to the face on the left",
  right: "Rotate to the face on the right",
};
const ROLLS = { cw: "Roll 90 degrees clockwise", ccw: "Roll 90 degrees counterclockwise" };

async function openCanvas(page: Page, request: APIRequestContext) {
  const session = await openExample(request);
  await request.post(`/api/sessions/${session.id}/messages`, { data: { text: SESSION.prompt } });
  await expect
    .poll(async () => (await (await request.get(`/api/sessions/${session.id}/builds`)).json()).length, {
      timeout: 30_000,
    })
    .toBeGreaterThan(0);
  await page.goto(`/session/${session.id}`);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  // On a phone the chat comes first: switch to the canvas.
  const showCanvas = page.getByRole("region", { name: "Chat" }).getByRole("button", { name: "Show canvas" });
  if (await showCanvas.isVisible()) await showCanvas.click();
  const viewer = page.getByTestId("viewer");
  await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  const canvas = page.getByRole("region", { name: "Canvas" });
  const cube = canvas.getByTestId("view-cube");
  await expect(cube).toBeVisible();
  return { viewer, canvas, cube };
}

const target = (cube: Locator, label: string) => cube.getByRole("button", { name: label, exact: true });

/**
 * Snaps to a target with the cube. Only targets on faces turned toward the camera can be
 * clicked, so this goes home first and, for a target home can't see, on to the
 * Back-Left-Bottom corner, from where every target but the home corner shows.
 */
async function snap(cube: Locator, label: string) {
  await cube.getByRole("button", { name: "Home view" }).click();
  if ((await target(cube, label).count()) === 0) {
    await target(cube, "Right-Bottom edge").click();
    await target(cube, "Back-Left-Bottom corner").click();
  }
  await target(cube, label).click();
}
const attr = async (l: Locator, name: string) => (await l.getAttribute(name)) ?? "";
const num = async (l: Locator, name: string) => Number(await attr(l, name));

/** Settled at a view: not animating, the expected view, no roll and Z up on screen. */
async function expectLevelView(viewer: Locator, view: string) {
  await expect(viewer).toHaveAttribute("data-animating", "false");
  await expect(viewer).toHaveAttribute("data-view", view);
  await expect(viewer).toHaveAttribute("data-roll", "0");
  // Looking straight down or up, Z points at the viewer and has no direction on screen.
  await expect(viewer).toHaveAttribute("data-up-angle", view === "top" || view === "bottom" ? "" : "0");
}

/** Drags with the left button from (x, y) through the given offsets. */
async function drag(page: Page, x: number, y: number, path: [number, number][], steps = 1) {
  await page.mouse.move(x, y);
  await page.mouse.down();
  for (const [dx, dy] of path) await page.mouse.move(x + dx, y + dy, { steps });
  await page.mouse.up();
}

/** Offsets that go round a circle of radius r, `laps` times, from its leftmost point. */
function circles(r: number, laps: number): [number, number][] {
  const out: [number, number][] = [];
  for (let i = 1; i <= laps * 36; i++) {
    const a = (i * 10 * Math.PI) / 180;
    out.push([r - r * Math.cos(a), r * Math.sin(a)]);
  }
  return out;
}

async function center(l: Locator): Promise<[number, number]> {
  const b = (await l.boundingBox())!;
  return [b.x + b.width / 2, b.y + b.height / 2];
}

/** A point on the canvas away from the cube, the toolbar and the labels. */
async function canvasPoint(viewer: Locator): Promise<[number, number]> {
  const b = (await viewer.boundingBox())!;
  // Clear of the parts panel, which overlays the canvas's left side.
  const parts = await viewer.page().getByRole("region", { name: "Parts" }).boundingBox();
  const x = Math.max(b.x + b.width * 0.4, parts ? parts.x + parts.width + 24 : 0);
  return [x, b.y + b.height * 0.55];
}

test.describe("snapping (reduced motion)", () => {
  test.use({ reducedMotion: "reduce" });

  test("every face, edge and corner snaps to its view with Z up", async ({ page, request }) => {
    test.setTimeout(120_000);
    const { viewer, cube } = await openCanvas(page, request);
    // From home the front, right and top faces show; from the Back-Left-Bottom corner the
    // others do. Between them every target is on a face that can be clicked.
    const bases: [string, string[]][] = [
      ["iso", []],
      ["back-left-bottom", ["Right-Bottom edge", "Back-Left-Bottom corner"]],
    ];
    const done = new Set<string>();
    for (const [base, path] of bases) {
      for (const t of TARGETS) {
        if (done.has(t.key)) continue;
        await cube.getByRole("button", { name: "Home view" }).click();
        for (const step of path) await target(cube, step).click();
        await expectLevelView(viewer, viewOf(base));
        if ((await target(cube, t.label).count()) === 0) continue;
        await target(cube, t.label).click();
        await expectLevelView(viewer, viewOf(t.key));
        await expect(target(cube, t.label)).toHaveAttribute("aria-pressed", "true");
        await expect(page.getByTestId("view-label")).toContainText(
          t.key === "front-right-top" ? "Front-Right-Top corner" : t.label,
        );
        done.add(t.key);
      }
    }
    expect([...done].sort()).toEqual(TARGETS.map((t) => t.key).sort());
  });

  test("top and bottom views put the front edge at the bottom of the screen", async ({ page, request }) => {
    const { viewer, cube } = await openCanvas(page, request);
    await snap(cube, "Top");
    await expectLevelView(viewer, "top");
    // Azimuth 0 looks from the front; 180 from the back, so upside down the front is at the bottom.
    await expect(viewer).toHaveAttribute("data-azimuth", "0");
    await snap(cube, "Bottom");
    await expectLevelView(viewer, "bottom");
    await expect(viewer).toHaveAttribute("data-azimuth", "180");
  });

  test("hovering an edge or corner highlights it on every face that holds it", async ({ page, request }) => {
    const { cube } = await openCanvas(page, request);
    const label = page.getByTestId("view-label");
    // From home: every target on the front, right and top faces.
    const visible = TARGETS.filter((t) => t.key.split("-").some((n) => ["front", "right", "top"].includes(n)));
    for (const t of visible) {
      const control = target(cube, t.label);
      await control.hover();
      const cells = cube.locator(`[data-target="${t.key}"]`);
      await expect(cells, t.label).toHaveCount(t.faces);
      for (let i = 0; i < t.faces; i++) await expect(cells.nth(i), t.label).toHaveAttribute("data-hovered", "true");
      await expect(cube.locator("[data-hovered]"), t.label).toHaveCount(t.faces);
      await expect(label).toContainText(`click for ${t.label}`);
    }
    await page.mouse.move(5, 5);
    await expect(cube.locator("[data-hovered]")).toHaveCount(0);
    await expect(label).not.toContainText("click for");
  });

  test("chevrons, rolls and home work from every face view", async ({ page, request }) => {
    test.setTimeout(120_000);
    const { viewer, cube } = await openCanvas(page, request);
    const faceKeys = FACES.map((f) => f.key);
    for (const face of FACES) {
      for (const [dir, name] of Object.entries(TURNS)) {
        await cube.getByRole("button", { name: "Home view" }).click();
        await expectLevelView(viewer, "iso");
        // Reach the face with the cube from home or, for the hidden ones, by its neighbour.
        if ((await target(cube, face.label).count()) === 0) {
          await target(cube, { Back: "Right", Left: "Front", Bottom: "Front" }[face.label]!).click();
          await cube.getByRole("button", { name: { Back: TURNS.right, Left: TURNS.left, Bottom: TURNS.down }[face.label]! }).click();
        } else await target(cube, face.label).click();
        await expectLevelView(viewer, face.key);
        await cube.getByRole("button", { name }).click();
        await expect(viewer).toHaveAttribute("data-roll", "0");
        const view = await attr(viewer, "data-view");
        expect(faceKeys, `${face.label} ${dir} lands on a face`).toContain(view);
        expect(view, `${face.label} ${dir} moves`).not.toBe(face.key);
      }
      // Rolls turn about the line of sight and come back; home levels everything.
      await cube.getByRole("button", { name: ROLLS.cw }).click();
      await expect(viewer).toHaveAttribute("data-roll", "90");
      await cube.getByRole("button", { name: ROLLS.cw }).click();
      await expect(viewer).toHaveAttribute("data-roll", "180");
      await cube.getByRole("button", { name: ROLLS.ccw }).click();
      await cube.getByRole("button", { name: ROLLS.ccw }).click();
      await expect(viewer).toHaveAttribute("data-roll", "0");
      await cube.getByRole("button", { name: ROLLS.ccw }).click();
      await expect(viewer).toHaveAttribute("data-roll", "-90");
      await cube.getByRole("button", { name: "Home view" }).click();
      await expectLevelView(viewer, "iso");
      await expect(page.getByTestId("view-label")).toContainText("View: Home");
    }
  });

  test("the neighbouring faces from Front are the ones on that side", async ({ page, request }) => {
    const { viewer, cube } = await openCanvas(page, request);
    for (const [dir, to] of [
      ["up", "top"],
      ["down", "bottom"],
      ["left", "left"],
      ["right", "right"],
    ] as const) {
      await snap(cube, "Front");
      await expectLevelView(viewer, "front");
      await cube.getByRole("button", { name: TURNS[dir] }).click();
      await expectLevelView(viewer, to);
    }
  });

  test("keyboard: arrows turn, Shift with Left or Right rolls, 0 goes home, H does not", async ({ page, request }) => {
    const { viewer, cube } = await openCanvas(page, request);
    await target(cube, "Front").click();
    await expectLevelView(viewer, "front");
    await viewer.focus();
    await page.keyboard.press("ArrowUp");
    await expectLevelView(viewer, "top");
    await page.keyboard.press("ArrowDown");
    await expectLevelView(viewer, "front");
    await page.keyboard.press("ArrowRight");
    await expectLevelView(viewer, "right");
    await page.keyboard.press("ArrowLeft");
    await expectLevelView(viewer, "front");
    await page.keyboard.press("Shift+ArrowRight");
    await expect(viewer).toHaveAttribute("data-roll", "90");
    await expect(viewer).toHaveAttribute("data-view", "front");
    // Rolled 90° clockwise, the face above on screen is Left.
    await page.keyboard.press("ArrowUp");
    await expect(viewer).toHaveAttribute("data-view", "left");
    await expect(viewer).toHaveAttribute("data-roll", "90");
    await page.keyboard.press("Shift+ArrowLeft");
    await expectLevelView(viewer, "left");
    // H is Hide selection in the canvas keymap: with nothing selected the view stays put.
    await page.keyboard.press("h");
    await expectLevelView(viewer, "left");
    await page.keyboard.press("0");
    await expectLevelView(viewer, "iso");
    // From the home corner the arrows still turn.
    await page.keyboard.press("ArrowRight");
    await expect(viewer).not.toHaveAttribute("data-view", "iso");
    await page.keyboard.press("0");
    await expectLevelView(viewer, "iso");
    // With focus on a cube button too.
    await cube.getByRole("button", { name: "Home view" }).focus();
    await page.keyboard.press("ArrowLeft");
    await expect(viewer).not.toHaveAttribute("data-view", "iso");
  });

  test("a cube target works from the keyboard", async ({ page, request }) => {
    const { viewer, cube } = await openCanvas(page, request);
    await target(cube, "Front-Right edge").focus();
    await page.keyboard.press("Enter");
    await expectLevelView(viewer, "front-right");
  });

  // Regression: on main, after a face snap the camera looked straight at it, and the cube left
  // nothing else to click.
  test("after snapping to a face, every cube target, button, drag and zoom still responds", async ({ page, request }) => {
    test.setTimeout(120_000);
    const { viewer, cube } = await openCanvas(page, request);
    const snapFront = async () => {
      await snap(cube, "Front");
      await expectLevelView(viewer, "front");
    };
    await snapFront();
    // Straight on, the front face holds nine targets: the face, four edges and four corners.
    const onFront = TARGETS.filter((t) => t.key.split("-").includes("front"));
    expect(onFront).toHaveLength(9);
    for (const t of onFront) {
      await expect(target(cube, t.label), t.label).toHaveCount(1);
      await target(cube, t.label).click();
      await expectLevelView(viewer, viewOf(t.key));
      await snapFront();
    }
    for (const name of [...Object.values(TURNS), ...Object.values(ROLLS), "Home view"]) {
      const before = `${await attr(viewer, "data-view")}/${await attr(viewer, "data-roll")}`;
      await cube.getByRole("button", { name }).click();
      await expect.poll(async () => `${await attr(viewer, "data-view")}/${await attr(viewer, "data-roll")}`, name).not.toBe(before);
      await snapFront();
    }
    // Dragging the cube and the model orbits.
    const [cx, cy] = await center(cube);
    await drag(page, cx, cy, [[40, 10]], 5);
    await expect(viewer).toHaveAttribute("data-view", "custom");
    await snapFront();
    const [mx, my] = await canvasPoint(viewer);
    await drag(page, mx, my, [[40, 10]], 5);
    await expect(viewer).toHaveAttribute("data-view", "custom");
    await snapFront();
    // The wheel zooms.
    const zoom = await num(viewer, "data-zoom");
    await page.mouse.move(mx, my);
    await page.mouse.wheel(0, -400);
    await expect.poll(() => num(viewer, "data-zoom")).toBeGreaterThan(zoom);
  });

  test("a press that moves under 4 px is a click, more is a drag", async ({ page, request }) => {
    const { viewer, cube } = await openCanvas(page, request);
    const front = target(cube, "Front");
    // On the cube: 3 px snaps...
    let [x, y] = await center(front);
    await drag(page, x, y, [[3, 0]]);
    await expectLevelView(viewer, "front");
    await cube.getByRole("button", { name: "Home view" }).click();
    await expectLevelView(viewer, "iso");
    // ...5 px orbits and never snaps.
    [x, y] = await center(front);
    await drag(page, x, y, [[5, 0]]);
    await expect(viewer).toHaveAttribute("data-view", "custom");
    await expect(page.getByTestId("view-label")).toContainText("View: Free orbit");
    // On the model: 3 px leaves the view alone, 5 px orbits.
    await cube.getByRole("button", { name: "Home view" }).click();
    await expectLevelView(viewer, "iso");
    [x, y] = await canvasPoint(viewer);
    await drag(page, x, y, [[0, 3]]);
    await page.waitForTimeout(100);
    await expectLevelView(viewer, "iso");
    await drag(page, x, y, [[0, 5]]);
    await expect(viewer).toHaveAttribute("data-view", "custom");
  });

  test("vertical drag stops at straight down and straight up", async ({ page, request }) => {
    const { viewer, cube } = await openCanvas(page, request);
    const [px, y] = await canvasPoint(viewer);
    // Start clear of the Parts panel, which covers the top left of the canvas.
    const parts = (await page.getByRole("region", { name: "Parts" }).boundingBox())!;
    const x = Math.max(px, parts.x + parts.width + 40);
    // Down tilts the camera over the top: it stops looking straight down.
    await drag(page, x, y - 200, [[0, 400]], 10);
    await expect(viewer).toHaveAttribute("data-elevation", "90");
    await drag(page, x, y - 200, [[0, 400]], 10);
    await expect(viewer).toHaveAttribute("data-elevation", "90");
    await expect(viewer).toHaveAttribute("data-roll", "0");
    // Up tilts it under: it stops looking straight up.
    for (let i = 0; i < 3; i++) await drag(page, x, y + 200, [[0, -400]], 10);
    await expect(viewer).toHaveAttribute("data-elevation", "-90");
    await expect(viewer).toHaveAttribute("data-roll", "0");
    // The cube drags the same way.
    const [cx, cy] = await center(cube);
    for (let i = 0; i < 3; i++) await drag(page, cx, cy, [[0, 300]], 10);
    await expect(viewer).toHaveAttribute("data-elevation", "90");
  });

  // Regression: dragging in circles must leave the model level.
  for (const on of ["cube", "model"] as const) {
    test(`dragging the ${on} in several full circles leaves roll at 0° and Z up`, async ({ page, request }) => {
      const { viewer, cube } = await openCanvas(page, request);
      const [x, y] = on === "cube" ? await center(cube) : await canvasPoint(viewer);
      await drag(page, x - 30, y, circles(30, 3), 1);
      await expect(viewer).toHaveAttribute("data-animating", "false");
      await expect(viewer).toHaveAttribute("data-roll", "0");
      await expect(viewer).toHaveAttribute("data-up-angle", "0");
      // The triad agrees: Z points straight up on screen.
      const z = page.getByTestId("axis-triad").locator('[data-axis="Z"]');
      expect(Number(await attr(z, "data-x"))).toBeCloseTo(0, 0);
      expect(Number(await attr(z, "data-y"))).toBeLessThan(0);
    });
  }

  test("right-drag and Shift-drag pan, the wheel zooms", async ({ page, request }) => {
    const { viewer } = await openCanvas(page, request);
    const [x, y] = await canvasPoint(viewer);
    let before = await attr(viewer, "data-target");
    await page.mouse.move(x, y);
    await page.mouse.down({ button: "right" });
    await page.mouse.move(x + 80, y + 30, { steps: 5 });
    await page.mouse.up({ button: "right" });
    await expect.poll(() => attr(viewer, "data-target")).not.toBe(before);
    await expect(viewer).toHaveAttribute("data-view", "iso");
    before = await attr(viewer, "data-target");
    await page.keyboard.down("Shift");
    await drag(page, x, y, [[-60, 20]], 5);
    await page.keyboard.up("Shift");
    await expect.poll(() => attr(viewer, "data-target")).not.toBe(before);
    await expect(viewer).toHaveAttribute("data-view", "iso");
    const zoom = await num(viewer, "data-zoom");
    await page.mouse.move(x, y);
    await page.mouse.wheel(0, 400);
    await expect.poll(() => num(viewer, "data-zoom")).toBeLessThan(zoom);
  });

  test("the axis triad turns with the view, nearest axis on top", async ({ page, request }) => {
    const { viewer, cube } = await openCanvas(page, request);
    const triad = page.getByTestId("axis-triad");
    const axis = (n: string) => triad.locator(`[data-axis="${n}"]`);
    await snap(cube, "Front");
    await expectLevelView(viewer, "front");
    // Front: X right, Z up, Y straight into the screen (drawn first, furthest away).
    await expect(axis("X")).toHaveAttribute("data-x", "20.0");
    await expect(axis("Z")).toHaveAttribute("data-y", "-20.0");
    await expect(triad).toHaveAttribute("data-order", /^Y/);
    await snap(cube, "Top");
    await expectLevelView(viewer, "top");
    // Top: Y up on screen (the back is at the top), Z toward the viewer, drawn last.
    await expect(axis("Y")).toHaveAttribute("data-y", "-20.0");
    await expect(triad).toHaveAttribute("data-order", /Z$/);
  });
});

/** The triad's axis positions on screen, as one string. */
const triadOf = (page: Page) =>
  page.evaluate(() =>
    [...document.querySelectorAll("[data-testid=axis-triad] [data-axis]")]
      .map((g) => `${g.getAttribute("data-x")},${g.getAttribute("data-y")}`)
      .join(" "),
  );

/**
 * Snaps, turns, rolls and home animate over about 450 ms, and a drag follows at once. The
 * page runs on Playwright's clock, paused, and the test moves time on itself, so the check
 * samples the animation's progress at known instants and does not depend on how fast the
 * machine is.
 */
async function expectMotion(page: Page, request: APIRequestContext) {
  await page.clock.install();
  const { viewer, cube } = await openCanvas(page, request);
  await page.clock.pauseAt(await page.evaluate(() => Date.now() + 1000));
  const progress = () => attr(viewer, "data-animation-progress");
  for (const name of ["Front", TURNS.up, ROLLS.cw, "Home view"]) {
    const control = name === "Front" ? target(cube, name) : cube.getByRole("button", { name });
    const start = await triadOf(page);
    await control.click();
    // The animation is under way after the first frame, and its progress follows time.
    let elapsed = 0;
    const between: string[] = [];
    for (const at of [100, 225, 350, 425]) {
      await page.clock.runFor(at - elapsed);
      elapsed = at;
      await expect(viewer, `${name} at ${at} ms`).toHaveAttribute("data-animating", "true");
      // Frames come every 16 ms, so the last one may be up to a frame behind.
      await expect
        .poll(async () => Number(await progress()) * 450, { message: `${name} progress at ${at} ms` })
        .toBeGreaterThanOrEqual(at - 20);
      expect(Number(await progress()) * 450, `${name} progress at ${at} ms`).toBeLessThanOrEqual(at + 3);
      between.push(await triadOf(page));
    }
    // Done a frame or so after 450 ms.
    await page.clock.runFor(500 - elapsed);
    await expect(viewer, name).toHaveAttribute("data-animating", "false");
    await expect(viewer, name).toHaveAttribute("data-animation-progress", "");
    // The triad moves with the view, through in-between positions.
    const end = await triadOf(page);
    expect(start, name).not.toBe(end);
    expect(
      between.some((t) => t !== start && t !== end),
      name,
    ).toBe(true);
  }
  // A drag follows the pointer with no animation.
  const [x, y] = await canvasPoint(viewer);
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + 40, y, { steps: 4 });
  await expect(viewer).toHaveAttribute("data-animating", "false");
  await expect(viewer).toHaveAttribute("data-view", "custom");
  await page.mouse.up();
}

test.describe("motion", () => {
  test("snaps, turns, rolls and home animate over about 450 ms; drags follow at once", async ({ page, request }) => {
    await expectMotion(page, request);
  });

  test("the animation check holds with the CPU throttled", async ({ page, request }) => {
    // Everything, page load included, runs 20 times slower.
    test.setTimeout(180_000);
    const cdp = await page.context().newCDPSession(page);
    await cdp.send("Emulation.setCPUThrottlingRate", { rate: 20 });
    await expectMotion(page, request);
  });

  test("reduced motion removes the animation", async ({ page, request }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    const { viewer, cube } = await openCanvas(page, request);
    const seen = await page.evaluate(() => {
      const el = document.querySelector("[data-testid=viewer]")!;
      const log: string[] = [];
      new MutationObserver(() => log.push(el.getAttribute("data-animating") ?? "")).observe(el, {
        attributes: true,
        attributeFilter: ["data-animating"],
      });
      (window as unknown as { __animLog: string[] }).__animLog = log;
      return true;
    });
    expect(seen).toBe(true);
    await target(cube, "Front").click();
    await expectLevelView(viewer, "front");
    await cube.getByRole("button", { name: ROLLS.cw }).click();
    await expect(viewer).toHaveAttribute("data-roll", "90");
    const log = await page.evaluate(() => (window as unknown as { __animLog: string[] }).__animLog);
    expect(log).not.toContain("true");
  });
});

test.describe("layout and styling", () => {
  test.use({ reducedMotion: "reduce" });

  for (const size of [
    { name: "desktop", width: 1440, height: 900 },
    { name: "phone", width: 390, height: 844 },
  ]) {
    test(`${size.name}: sizes, placement and button styling match the design`, async ({ page, request }) => {
      await page.setViewportSize({ width: size.width, height: size.height });
      const { viewer, canvas, cube } = await openCanvas(page, request);
      const box = (await cube.boundingBox())!;
      expect(box.width).toBe(150);
      expect(box.height).toBe(150);
      // Directly under the toolbar, right edges aligned (on a phone the toolbar is wider than
      // the canvas; the cube keeps to the canvas's 12 px margin).
      const bar = (await canvas.getByRole("toolbar", { name: "Canvas controls" }).boundingBox())!;
      const vb0 = (await viewer.boundingBox())!;
      expect(box.x + box.width).toBeCloseTo(Math.min(bar.x + bar.width, vb0.x + vb0.width - 12), 0);
      expect(box.y).toBeGreaterThanOrEqual(bar.y + bar.height);
      expect(box.y - (bar.y + bar.height)).toBeLessThanOrEqual(12);
      // The cube is 66 px.
      const face = cube.locator('[data-face="Front"]');
      expect(await face.evaluate((el) => (el as HTMLElement).offsetWidth)).toBe(66);
      expect(await face.locator("[data-target]").first().evaluate((el) => (el as HTMLElement).offsetWidth)).toBe(14);
      // Seven icon-only buttons: 32 px hit areas, 16 px icons, no border or background.
      const names = [...Object.values(TURNS), ...Object.values(ROLLS), "Home view"];
      for (const name of names) {
        const b = cube.getByRole("button", { name });
        const bb = (await b.boundingBox())!;
        expect([bb.width, bb.height], name).toEqual([32, 32]);
        const style = await b.evaluate((el) => {
          const s = getComputedStyle(el);
          const svg = el.querySelector("svg")!.getBoundingClientRect();
          return { bg: s.backgroundColor, border: s.borderTopWidth, color: s.color, icon: [svg.width, svg.height] };
        });
        expect(style.bg, name).toBe("rgba(0, 0, 0, 0)");
        expect(style.border, name).toBe("0px");
        expect(style.icon, name).toEqual([16, 16]);
        // Muted, darkening on hover with no hover tile.
        await b.hover();
        await expect
          .poll(() => b.evaluate((el) => getComputedStyle(el).color), name)
          .not.toBe(style.color);
        expect(await b.evaluate((el) => getComputedStyle(el).backgroundColor), name).toBe("rgba(0, 0, 0, 0)");
      }
      // Focus shows a ring.
      await cube.getByRole("button", { name: "Home view" }).focus();
      await page.keyboard.press("Shift+Tab");
      await page.keyboard.press("Tab");
      expect(await cube.getByRole("button", { name: "Home view" }).evaluate((el) => getComputedStyle(el).boxShadow)).not.toBe(
        "none",
      );
      // Faces carry their axis tint: X red (Right), Y green (Front), Z blue (Top).
      const tint = (name: string) =>
        cube.locator(`[data-face="${name}"] [data-target="${name.toLowerCase()}"]`).evaluate((el) => getComputedStyle(el).backgroundColor);
      expect(await tint("Right")).toBe("rgba(226, 75, 74, 0.15)");
      expect(await tint("Front")).toBe("rgba(99, 153, 34, 0.15)");
      expect(await tint("Top")).toBe("rgba(55, 138, 221, 0.15)");
      await target(cube, "Front").hover();
      expect(await tint("Front")).toBe("rgba(99, 153, 34, 0.4)");
      await target(cube, "Front-Right edge").hover();
      expect(
        await cube.locator('[data-target="front-right"]').first().evaluate((el) => getComputedStyle(el).backgroundColor),
      ).toBe("rgba(136, 135, 128, 0.34)");
      // The triad (64 px) bottom-left, above the view label (12 px text).
      const triad = (await page.getByTestId("axis-triad").boundingBox())!;
      const label = page.getByTestId("view-label");
      const lb = (await label.boundingBox())!;
      const vb = (await viewer.boundingBox())!;
      expect([triad.width, triad.height]).toEqual([64, 64]);
      expect(triad.x - vb.x).toBeLessThan(20);
      expect(triad.y + triad.height).toBeLessThanOrEqual(lb.y + 1);
      expect(await label.evaluate((el) => getComputedStyle(el).fontSize)).toBe("12px");
      await expect(label).toContainText("View: Home");
      await page.mouse.move(5, 5);
      await page.screenshot({ path: test.info().outputPath(`view-cube-${size.name}.png`) });
    });
  }

  test("the empty canvas shows no cube, triad or view label", async ({ page, request }) => {
    const session = await emptySession(request);
    await page.goto(`/session/${session.id}`);
    await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
    await expect(page.getByTestId("hardware-canvas")).toHaveAttribute("data-empty", "true");
    await expect(page.getByTestId("view-cube")).toHaveCount(0);
    await expect(page.getByTestId("axis-triad")).toHaveCount(0);
    await expect(page.getByTestId("view-label")).toHaveCount(0);
  });
});

// Visual baselines: home, a face, an edge and a corner view.
test.describe("visual baselines", () => {
  test.use({ reducedMotion: "reduce" });

  test("home, face, edge and corner views match the reference screenshots", async ({ page, request }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    const { viewer, canvas, cube } = await openCanvas(page, request);
    const shot = { maxDiffPixelRatio: 0.02, animations: "disabled" as const };
    await page.mouse.move(5, 5);
    await expect(canvas).toHaveScreenshot("view-home.png", shot);
    for (const [label, key, file] of [
      ["Front", "front", "view-face.png"],
      ["Front-Right edge", "front-right", "view-edge.png"],
      ["Front-Left-Top corner", "front-left-top", "view-corner.png"],
    ]) {
      await snap(cube, label);
      await expectLevelView(viewer, key);
      await page.mouse.move(5, 5);
      await expect(canvas).toHaveScreenshot(file, shot);
    }
  });
});
