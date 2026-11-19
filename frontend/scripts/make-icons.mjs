// Generates the browser tab and home-screen icons from the press-kit Claude mark.
//
//   cd frontend && node scripts/make-icons.mjs
//
// Source: public/claude-logo.svg (see the repository README, "Attribution").
// Outputs, all in src/app/ where Next.js picks them up as file-based icons:
// - icon.svg       byte-for-byte copy of the mark
// - apple-icon.png 180x180, the mark with padding on the app background (iOS fills
//                  transparency with black, so the tile colour is set explicitly)
// - favicon.ico    16, 32 and 48 px PNG frames with a transparent background
// The mark itself is never redrawn or recoloured; Chromium (via Playwright) only rasterises it.
import { copyFileSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const source = path.join(root, "public/claude-logo.svg");
const appDir = path.join(root, "src/app");
const APP_BACKGROUND = "#191919"; // --cs-bg in src/app/globals.css

const svg = readFileSync(source, "utf8");
const dataUrl = `data:image/svg+xml;base64,${Buffer.from(svg).toString("base64")}`;

async function render(page, size, { padding = 0, background = null } = {}) {
  await page.setViewportSize({ width: size, height: size });
  const inner = size - 2 * padding;
  await page.setContent(
    `<html><body style="margin:0;background:${background ?? "transparent"}">` +
      `<img src="${dataUrl}" width="${inner}" height="${inner}" style="display:block;margin:${padding}px">` +
      `</body></html>`,
  );
  await page.locator("img").evaluate((img) => img.decode());
  return page.screenshot({ omitBackground: background === null, clip: { x: 0, y: 0, width: size, height: size } });
}

// ICO container holding PNG frames (supported by every current browser).
function ico(frames) {
  const header = Buffer.alloc(6);
  header.writeUInt16LE(0, 0);
  header.writeUInt16LE(1, 2);
  header.writeUInt16LE(frames.length, 4);
  let offset = 6 + 16 * frames.length;
  const entries = frames.map(({ size, png }) => {
    const e = Buffer.alloc(16);
    e.writeUInt8(size >= 256 ? 0 : size, 0);
    e.writeUInt8(size >= 256 ? 0 : size, 1);
    e.writeUInt16LE(1, 4); // colour planes
    e.writeUInt16LE(32, 6); // bits per pixel
    e.writeUInt32LE(png.length, 8);
    e.writeUInt32LE(offset, 12);
    offset += png.length;
    return e;
  });
  return Buffer.concat([header, ...entries, ...frames.map((f) => f.png)]);
}

const browser = await chromium.launch();
try {
  const page = await browser.newPage({ deviceScaleFactor: 1 });
  copyFileSync(source, path.join(appDir, "icon.svg"));
  writeFileSync(
    path.join(appDir, "apple-icon.png"),
    await render(page, 180, { padding: 30, background: APP_BACKGROUND }),
  );
  const frames = [];
  for (const size of [16, 32, 48]) frames.push({ size, png: await render(page, size) });
  writeFileSync(path.join(appDir, "favicon.ico"), ico(frames));
} finally {
  await browser.close();
}
console.log("Wrote src/app/icon.svg, src/app/apple-icon.png and src/app/favicon.ico");
