import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// The shell's text tokens (globals.css) against the surfaces they sit on meet WCAG AA for
// normal text (4.5:1). The QA sweep's contrast check found --cs-faint at 3.5:1.

const css = readFileSync(resolve(__dirname, "../src/app/globals.css"), "utf8");

function token(name: string): string {
  const m = css.match(new RegExp(`${name}:\\s*([^;]+);`));
  if (!m) throw new Error(`no ${name} in globals.css`);
  return m[1].trim();
}

type Rgb = [number, number, number];

function hex(value: string): Rgb {
  const h = value.replace("#", "");
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)) as Rgb;
}

/** An `rgba(...)` token composited over an opaque background. */
function over(value: string, bg: Rgb): Rgb {
  const m = value.match(/rgba\(\s*(\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\s*\)/);
  if (!m) return hex(value);
  const a = Number(m[4]);
  return [1, 2, 3].map((i, k) => Math.round(Number(m[i]) * a + bg[k] * (1 - a))) as Rgb;
}

function luminance([r, g, b]: Rgb): number {
  const lin = (c: number) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

function contrast(a: Rgb, b: Rgb): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

describe("shell text contrast", () => {
  const surfaces = ["--cs-bg", "--cs-sidebar", "--cs-surface-2", "--cs-surface"];
  for (const text of ["--cs-text", "--cs-muted", "--cs-faint"])
    for (const surface of surfaces)
      it(`${text} on ${surface} is at least 4.5:1`, () => {
        const bg = hex(token(surface));
        expect(contrast(over(token(text), bg), bg)).toBeGreaterThanOrEqual(4.5);
      });
});
