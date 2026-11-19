import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

// The tab and home-screen icons are derived from the press-kit mark by scripts/make-icons.mjs.
const root = path.resolve(__dirname, "..");
const MARK = "public/claude-logo.svg";
const APP_BACKGROUND = "#191919"; // the apple-icon tile, as set in scripts/make-icons.mjs
// sha256 of the cloner template's default src/app/favicon.ico (the Next.js "N" icon).
const TEMPLATE_FAVICON = "2b8ad2d33455a8f736fc3a8ebf8f0bdea8848ad4c0db48a2833bd0f9cd775932";

const dataUrl = (mime: string, bytes: Buffer) => `data:${mime};base64,${bytes.toString("base64")}`;

// The PNG frames inside an ICO file.
function icoFrames(ico: Buffer) {
  expect(ico.readUInt16LE(2), "ICO type").toBe(1);
  const frames: { size: number; png: Buffer }[] = [];
  for (let i = 0; i < ico.readUInt16LE(4); i++) {
    const e = 6 + 16 * i;
    const png = ico.subarray(ico.readUInt32LE(e + 12), ico.readUInt32LE(e + 12) + ico.readUInt32LE(e + 8));
    expect(png.subarray(1, 4).toString(), "frame is PNG").toBe("PNG");
    frames.push({ size: ico.readUInt8(e) || 256, png });
  }
  return frames;
}

// Mean per-channel RGBA difference (0-255) between an image and the mark drawn on a
// size x size canvas with the given padding and background.
async function diffFromMark(
  page: Page,
  image: string,
  size: number,
  { padding = 0, background = "" }: { padding?: number; background?: string } = {},
) {
  const mark = dataUrl("image/svg+xml", readFileSync(path.join(root, MARK)));
  return page.evaluate(
    async ({ image, mark, size, padding, background }) => {
      const load = async (src: string) => {
        const img = new Image();
        img.src = src;
        await img.decode();
        return img;
      };
      const pixels = (draw: (ctx: CanvasRenderingContext2D) => void) => {
        const c = document.createElement("canvas");
        c.width = c.height = size;
        const ctx = c.getContext("2d")!;
        draw(ctx);
        return ctx.getImageData(0, 0, size, size).data;
      };
      const [a, b] = [await load(image), await load(mark)];
      if (a.naturalWidth !== size || a.naturalHeight !== size) return Infinity;
      const got = pixels((ctx) => ctx.drawImage(a, 0, 0));
      const want = pixels((ctx) => {
        if (background) {
          ctx.fillStyle = background;
          ctx.fillRect(0, 0, size, size);
        }
        ctx.drawImage(b, padding, padding, size - 2 * padding, size - 2 * padding);
      });
      let sum = 0;
      for (let i = 0; i < got.length; i++) sum += Math.abs(got[i] - want[i]);
      return sum / got.length;
    },
    { image, mark, size, padding, background },
  );
}

test("the page's icon links point to the Claude mark", async ({ page }) => {
  await page.goto("/");
  const icons = page.locator('head link[rel="icon"]');
  const hrefs = await icons.evaluateAll((links) => links.map((l) => l.getAttribute("href") ?? ""));
  const svgIcon = hrefs.find((h) => h.startsWith("/icon.svg"));
  expect(svgIcon, `rel=icon hrefs: ${hrefs.join(", ")}`).toBeTruthy();
  await expect(page.locator(`head link[rel="icon"][href="${svgIcon}"]`)).toHaveAttribute("type", "image/svg+xml");
  expect(hrefs.some((h) => h.startsWith("/favicon.ico"))).toBe(true);
  const apple = await page.locator('head link[rel="apple-touch-icon"]').getAttribute("href");
  expect(apple).toMatch(/^\/apple-icon\.png/);

  // Each linked icon is served, and the SVG is the press-kit mark itself.
  const mark = readFileSync(path.join(root, MARK));
  const svg = await page.request.get(svgIcon!);
  expect(svg.headers()["content-type"]).toContain("image/svg+xml");
  expect(Buffer.from(await svg.body()).equals(mark)).toBe(true);
  for (const href of [apple!, hrefs.find((h) => h.startsWith("/favicon.ico"))!]) {
    const res = await page.request.get(href);
    expect(res.ok(), href).toBe(true);
    const body = Buffer.from(await res.body());
    expect(createHash("sha256").update(body).digest("hex"), href).not.toBe(TEMPLATE_FAVICON);
  }
});

test("icon.svg, apple-icon.png and favicon.ico are rendered from the press-kit mark", async ({ page }) => {
  const app = (f: string) => readFileSync(path.join(root, "src/app", f));
  expect(app("icon.svg").equals(readFileSync(path.join(root, MARK)))).toBe(true);

  // Pixel comparison against the mark rendered by the browser: no redrawn or recoloured mark.
  const apple = dataUrl("image/png", app("apple-icon.png"));
  expect(await diffFromMark(page, apple, 180, { padding: 30, background: APP_BACKGROUND })).toBeLessThan(2);

  const frames = icoFrames(app("favicon.ico"));
  expect(frames.map((f) => f.size)).toEqual([16, 32, 48]);
  for (const { size, png } of frames) {
    expect(await diffFromMark(page, dataUrl("image/png", png), size), `${size}px frame`).toBeLessThan(4);
  }

  // How they were made is recorded next to the mark's source.
  expect(existsSync(path.join(root, "scripts/make-icons.mjs"))).toBe(true);
  const readme = readFileSync(path.join(root, "..", "README.md"), "utf8");
  expect(readme).toContain("frontend/scripts/make-icons.mjs");
});

test("the template's default favicon is gone", () => {
  const hash = (f: string) => createHash("sha256").update(readFileSync(f)).digest("hex");
  for (const f of ["src/app/favicon.ico", "public/favicon.ico"]) {
    const p = path.join(root, f);
    if (existsSync(p)) expect(hash(p), f).not.toBe(TEMPLATE_FAVICON);
  }
});
