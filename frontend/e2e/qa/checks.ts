import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import type { Page } from "@playwright/test";
import { parse } from "yaml";

// The visual checks run in every QA state: each returns a list of
// problems as text, empty when the state passes. They run in the page, on what is on screen.

export type Visual = { kind: string; detail: string };

const repo = resolve(__dirname, "../../..");
/**
 * The IDs of the standards (`standards.yaml`) and the check names (`fixtures/checks.json`) that
 * are identifiers, not words ("min_draft", "wall_thickness"; not "material"): people read their
 * titles instead (QA fix brief 2).
 */
export const CHECK_IDS = [
  ...(parse(readFileSync(resolve(repo, "standards.yaml"), "utf8")) as { standards: { id: string; check?: string }[] }).standards.flatMap((s) => [s.id, s.check ?? ""]),
  ...(JSON.parse(readFileSync(resolve(repo, "fixtures/checks.json"), "utf8")) as { results: { check: string }[] }).results.map((r) => r.check),
].filter((id, i, all) => id.includes("_") && all.indexOf(id) === i);

/**
 * Overlaps, clipping and off-screen controls, horizontal scroll, text cut off, readable
 * contrast and internal text shown to the user, measured in the page.
 */
export async function visualChecks(page: Page): Promise<Visual[]> {
  return page.evaluate((checkIds: string[]) => {
    const out: { kind: string; detail: string }[] = [];
    const vw = document.documentElement.clientWidth;
    const vh = document.documentElement.clientHeight;
    const INTERACTIVE =
      'button, a[href], input:not([type=hidden]), textarea, select, [role=button], [role=link], [role=menuitem], [role=menuitemradio], [role=menuitemcheckbox], [role=tab], [role=slider], [role=radio], [role=checkbox], [role=switch], [role=option], [contenteditable=true]';

    const visible = (el: Element) => {
      const r = el.getBoundingClientRect();
      if (r.width < 1 || r.height < 1) return false;
      for (let n: Element | null = el; n; n = n.parentElement) {
        const s = getComputedStyle(n);
        if (s.display === "none" || s.visibility === "hidden" || Number(s.opacity) === 0) return false;
      }
      return true;
    };
    /** A short, readable name for an element: its role and accessible-ish name. */
    const label = (el: Element) => {
      const role = el.getAttribute("role") ?? el.tagName.toLowerCase();
      const name =
        el.getAttribute("aria-label") ??
        el.getAttribute("title") ??
        (el as HTMLInputElement).placeholder ??
        (el.textContent ?? "").trim().replace(/\s+/g, " ").slice(0, 50);
      const region = el.closest("[role=region][aria-label], [role=dialog][aria-label], aside[aria-label], [role=toolbar][aria-label]");
      const where = region && region !== el ? ` in ${region.getAttribute("role") ?? region.tagName.toLowerCase()} "${region.getAttribute("aria-label")}"` : "";
      return `${role} "${name}"${where}`;
    };
    /** The rectangle of `el` that its scrolling or clipping ancestors leave visible. */
    const clipRect = (el: Element) => {
      let { left, top, right, bottom } = el.getBoundingClientRect();
      for (let n = el.parentElement; n && n !== document.body; n = n.parentElement) {
        const s = getComputedStyle(n);
        if (/(hidden|auto|scroll|clip)/.test(s.overflow + s.overflowX + s.overflowY)) {
          const p = n.getBoundingClientRect();
          left = Math.max(left, p.left);
          top = Math.max(top, p.top);
          right = Math.min(right, p.right);
          bottom = Math.min(bottom, p.bottom);
        }
      }
      return { left, top, right, bottom };
    };
    const inScroller = (el: Element) => {
      for (let n = el.parentElement; n && n !== document.body; n = n.parentElement) {
        const s = getComputedStyle(n);
        if (/(auto|scroll)/.test(s.overflow + s.overflowX + s.overflowY) && (n.scrollHeight > n.clientHeight + 1 || n.scrollWidth > n.clientWidth + 1))
          return true;
      }
      return false;
    };

    // Controls on screen. Elements inside a WebGL canvas aren't in the DOM; inputs that are
    // visually hidden behind a styled label (sr-only radios, file inputs) are skipped.
    const controls = Array.from(document.querySelectorAll(INTERACTIVE)).filter((el) => {
      if (!visible(el)) return false;
      const r = el.getBoundingClientRect();
      if (r.width <= 2 && r.height <= 2) return false; // sr-only
      if (el.closest("[aria-hidden=true]")) return false;
      return true;
    });

    // 1. Horizontal scroll on the page.
    const se = document.scrollingElement ?? document.documentElement;
    if (se.scrollWidth > se.clientWidth + 1) out.push({ kind: "hscroll", detail: `the page scrolls sideways: ${se.scrollWidth}px wide in ${se.clientWidth}px` });

    // 2. Controls off screen or clipped by a container that doesn't scroll to them.
    // Controls that a container clips away entirely are out of sight but still take focus.
    const hidden: string[] = [];
    for (const el of controls) {
      const r = el.getBoundingClientRect();
      const cr = clipRect(el);
      if (!inScroller(el) && (cr.right - cr.left <= 0 || cr.bottom - cr.top <= 0 || cr.left >= vw || cr.top >= vh || cr.right <= 0 || cr.bottom <= 0)) {
        hidden.push(label(el));
        continue;
      }
      const off = r.right > vw + 1 || r.left < -1 || r.bottom > vh + 1 || r.top < -1;
      if (off && !inScroller(el)) {
        out.push({ kind: "offscreen", detail: `${label(el)} is off screen at (${Math.round(r.left)},${Math.round(r.top)}) ${Math.round(r.width)}x${Math.round(r.height)}` });
        continue;
      }
      const c = clipRect(el);
      const shown = Math.max(0, c.right - c.left) * Math.max(0, c.bottom - c.top);
      if (!inScroller(el) && shown < r.width * r.height * 0.6 && shown >= 0)
        out.push({ kind: "clipped", detail: `${label(el)} is cut off by its container (${Math.round((100 * shown) / (r.width * r.height))}% shown)` });
    }

    if (hidden.length)
      out.push({ kind: "offscreen", detail: `${hidden.length} controls are out of sight but still focusable, such as ${hidden.slice(0, 3).join(", ")}` });

    // 3. Covered controls: the control's centre is under something that isn't part of it.
    for (const el of controls) {
      if (hidden.includes(label(el))) continue;
      const r = el.getBoundingClientRect();
      const c = clipRect(el);
      const x = Math.min(Math.max((r.left + r.right) / 2, c.left + 1), c.right - 1);
      const y = Math.min(Math.max((r.top + r.bottom) / 2, c.top + 1), c.bottom - 1);
      if (x < 0 || y < 0 || x > vw || y > vh || c.right <= c.left || c.bottom <= c.top) continue;
      const top = document.elementFromPoint(x, y);
      if (!top || el.contains(top) || top.contains(el)) continue;
      // A label for the input, or the input's own styled wrapper.
      if (top instanceof HTMLLabelElement && (top.control === el || top.contains(el))) continue;
      if (Array.from((el as HTMLInputElement).labels ?? []).some((l) => l.contains(top))) continue;
      // A disclosure's popover (opened by a control that says it is expanded) covers what's
      // under it on purpose.
      const popover = Array.from(document.querySelectorAll("[aria-expanded=true]")).some((b) => {
        const id = b.getAttribute("aria-controls");
        const panel = id ? document.getElementById(id) : null;
        if (panel?.contains(top)) return true;
        // Or the panel that follows it, positioned over the page.
        const next = b.nextElementSibling ?? b.parentElement?.nextElementSibling;
        return !!next && next.contains(top) && /absolute|fixed/.test(getComputedStyle(next).position);
      });
      if (popover) continue;
      // A tool's panel, opened from its button (expanded or pressed), floats over the 3D view,
      // the view cube and the toolbars on purpose, like any popover.
      // (A section nested in a tool's panel, such as its hand edits, belongs to that panel.)
      const floating = (n: Element | null) => !!n && /absolute|fixed/.test(getComputedStyle(n).position);
      let panel = top.closest("section[aria-label], [role=region][aria-label]");
      while (panel && !floating(panel) && !floating(panel.parentElement)) panel = panel.parentElement?.closest("section[aria-label], [role=region][aria-label]") ?? null;
      const anyOpen = document.querySelector("[aria-expanded=true], [aria-pressed=true]") !== null;
      if (panel && !panel.contains(el) && anyOpen) continue;
      // An open modal or popover covers what's under it on purpose.
      const coverer = top.closest("[role=dialog], [role=menu], [role=listbox], [aria-modal=true], [data-radix-popper-content-wrapper], [data-state=open]");
      if (coverer && !coverer.contains(el)) {
        // A dialog (modal or a sheet such as the shortcuts or a comment thread) sits over what's
        // under it on purpose.
        if (coverer.closest("[role=dialog]")) continue;
        out.push({ kind: "overlap", detail: `${label(el)} is covered by ${label(coverer)}` });
        continue;
      }
      // A full-screen backdrop (a drawer's or a dialog's) covers the page on purpose.
      const tr = top.getBoundingClientRect();
      if (tr.width >= vw - 2 && tr.height >= vh - 2) continue;
      out.push({ kind: "overlap", detail: `${label(el)} is covered by ${label(top.closest(INTERACTIVE) ?? top)}` });
    }

    // 4. Floating panels overlapping each other: positioned regions, toolbars, dialogs and
    // menus that aren't nested in one another.
    const panels = Array.from(
      document.querySelectorAll("[role=region][aria-label], [role=toolbar][aria-label], [role=dialog], [role=menu], [role=group][aria-label], [data-testid=check-pin], [data-testid=change-pin]"),
    ).filter((el) => {
      if (!visible(el)) return false;
      const pos = getComputedStyle(el).position;
      if (pos !== "absolute" && pos !== "fixed" && pos !== "sticky") {
        // A panel positioned through its wrapper counts too.
        const p = el.parentElement && getComputedStyle(el.parentElement).position;
        if (p !== "absolute" && p !== "fixed") return false;
      }
      const r = el.getBoundingClientRect();
      return r.width < vw - 2 || r.height < vh - 2;
    });
    for (let i = 0; i < panels.length; i++)
      for (let j = i + 1; j < panels.length; j++) {
        const a = panels[i];
        const b = panels[j];
        if (a.contains(b) || b.contains(a)) continue;
        const ra = a.getBoundingClientRect();
        const rb = b.getBoundingClientRect();
        const w = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left);
        const h = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
        if (w > 4 && h > 4) out.push({ kind: "overlap", detail: `${label(a)} overlaps ${label(b)} by ${Math.round(w)}x${Math.round(h)}px` });
      }

    // 5. Text cut off: a label, button or heading whose text overflows its box with no
    // ellipsis (cut mid-word), or with an ellipsis and no way to read the rest (title).
    const texts = Array.from(document.querySelectorAll("button, a, label, h1, h2, h3, h4, [role=menuitem], [role=menuitemradio], [role=tab], th, td, li > span, p")).filter(visible);
    for (const el of texts) {
      const h = el as HTMLElement;
      const s = getComputedStyle(h);
      if (!(h.textContent ?? "").trim()) continue;
      const over = h.scrollWidth > h.clientWidth + 1 && /(hidden|clip)/.test(s.overflow + s.overflowX);
      const overY = h.scrollHeight > h.clientHeight + 2 && /(hidden|clip)/.test(s.overflow + s.overflowY) && s.webkitLineClamp === "none";
      if (!over && !overY) continue;
      if (s.textOverflow === "ellipsis" || s.webkitLineClamp !== "none") {
        if (!h.title && !h.closest("[title]") && !h.getAttribute("aria-label"))
          out.push({ kind: "truncated", detail: `${label(h)}: text ends in an ellipsis with no title to read the rest` });
      } else out.push({ kind: "truncated", detail: `${label(h)}: text is cut off mid-word` });
    }

    // 6. Contrast of text against the colour behind it (WCAG AA: 4.5, or 3 for large text).
    const parse = (c: string) => {
      const m = c.match(/rgba?\(([^)]+)\)/);
      if (!m) return null;
      const [r, g, b, a = 1] = m[1].split(/[ ,/]+/).filter(Boolean).map(Number);
      return { r, g, b, a };
    };
    const lum = ({ r, g, b }: { r: number; g: number; b: number }) => {
      const f = (v: number) => {
        v /= 255;
        return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
      };
      return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
    };
    const seen = new Set<string>();
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const text = (node.textContent ?? "").trim();
      const el = node.parentElement;
      if (!text || !el || !visible(el) || seen.has(text)) continue;
      if (el.closest("[disabled], [aria-disabled=true], [aria-hidden=true], canvas, svg")) continue;
      const s = getComputedStyle(el);
      const fg = parse(s.color);
      if (!fg) continue;
      // The first opaque background behind the text; over the 3D canvas it can't be known.
      let bg: { r: number; g: number; b: number; a: number } | null = null;
      let overCanvas = false;
      for (let n: Element | null = el; n; n = n.parentElement) {
        const c = parse(getComputedStyle(n).backgroundColor);
        if (c && c.a >= 0.9) {
          bg = c;
          break;
        }
        if (n.querySelector(":scope > canvas")) overCanvas = true;
        if (getComputedStyle(n).backgroundImage !== "none") overCanvas = true;
      }
      if (overCanvas && !bg) continue;
      bg ??= { r: 255, g: 255, b: 255, a: 1 };
      // Text drawn translucent blends with the background.
      const opacity = (() => {
        let o = fg.a;
        for (let n: Element | null = el; n; n = n.parentElement) o *= Number(getComputedStyle(n).opacity);
        return o;
      })();
      const mix = (f: number, b: number) => f * opacity + b * (1 - opacity);
      const shown = { r: mix(fg.r, bg.r), g: mix(fg.g, bg.g), b: mix(fg.b, bg.b) };
      const [l1, l2] = [lum(shown), lum(bg)].sort((a, b) => b - a);
      const ratio = (l1 + 0.05) / (l2 + 0.05);
      const size = parseFloat(s.fontSize);
      const large = size >= 24 || (size >= 18.66 && Number(s.fontWeight) >= 700);
      if (ratio < (large ? 3 : 4.5)) {
        seen.add(text);
        out.push({ kind: "contrast", detail: `"${text.slice(0, 40)}" has contrast ${ratio.toFixed(2)}:1 (${s.color} on rgb(${bg.r}, ${bg.g}, ${bg.b}))` });
      }
    }

    // 7. Internal text shown to the user: stack traces, raw IDs, JS values, HTTP details.
    const shown = document.body.innerText;
    const INTERNAL: [RegExp, string][] = [
      [/Traceback \(most recent call last\)|^\s+at \S+ \(.*:\d+:\d+\)|File ".*", line \d+/m, "a stack trace"],
      [/\[object Object\]|\bundefined\b|\bNaN\b/, "a raw JavaScript value"],
      [/\b(session-[0-9a-f]{6,}|session-\d+|msg-\d+|q-\d+)\b/, "a raw session, message or question ID"],
      [/\b(?:build|edit|comment|export)-[0-9a-f]{8,}\b/, "a raw build/edit/comment ID"],
      [/\b(?:HTTP|status) [45]\d\d\b|\b[45]\d\d (?:Not Found|Bad Request|Internal Server Error|Conflict|Unprocessable)/i, "an HTTP status"],
      [/\{"detail":/, "a raw API error body"],
      [/\b[a-z]+(?:_[a-z]+){2,}\b/, "a snake_case identifier"],
      [new RegExp(`\\b(?:${checkIds.join("|")})\\b`), "a check or standard ID"],
    ];
    for (const [re, what] of INTERNAL) {
      const all = new RegExp(re.source, re.flags.includes("g") ? re.flags : `${re.flags}g`);
      // A constraint's source line, in its detail in the Constraints bar, names the constraint's
      // key on purpose ("Constraint heat_set_inserts in …").
      const m = Array.from(shown.matchAll(all)).find(
        (x) => !/^ in /.test(shown.slice(x.index! + x[0].length)) || !/constraint $/i.test(shown.slice(Math.max(0, x.index! - 11), x.index!)),
      );
      if (m && m.index !== undefined) {
        const at = Math.max(0, m.index - 30);
        out.push({ kind: "internal-text", detail: `shows ${what}: "…${shown.slice(at, m.index + m[0].length + 30).replace(/\s+/g, " ")}…"` });
      }
    }
    return out;
  }, CHECK_IDS);
}

/** Spinners and "Loading" text still on screen after `ms`. */
export async function stuckLoading(page: Page, ms = 15_000): Promise<Visual[]> {
  const find = () =>
    page.evaluate(() => {
      const hits: string[] = [];
      for (const el of Array.from(document.querySelectorAll("[aria-busy=true], [role=progressbar], .animate-spin, [data-state=loading]"))) {
        const r = el.getBoundingClientRect();
        if (r.width && r.height) hits.push(`${el.tagName.toLowerCase()} ${el.getAttribute("aria-label") ?? el.getAttribute("data-testid") ?? ""}`.trim());
      }
      const text = document.body.innerText;
      for (const m of text.matchAll(/(Loading[^\n]{0,40}|Building[^\n]{0,40}|Connecting[^\n]{0,30})/g)) hits.push(`text "${m[1]}"`);
      return hits;
    });
  const deadline = Date.now() + ms;
  let hits = await find();
  while (hits.length && Date.now() < deadline) {
    await page.waitForTimeout(500);
    hits = await find();
  }
  return hits.map((h) => ({ kind: "loading", detail: `${h} still loading after ${ms / 1000}s` }));
}

/** Tabs through the first `n` focus stops: each must show a focus ring (outline or box shadow). */
export async function focusChecks(page: Page, n = 12): Promise<Visual[]> {
  const out: Visual[] = [];
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  const seen = new Set<string>();
  for (let i = 0; i < n; i++) {
    await page.keyboard.press("Tab");
    const r = await page.evaluate(() => {
      const el = document.activeElement as HTMLElement | null;
      if (!el || el === document.body) return null;
      const s = getComputedStyle(el);
      const ring =
        (s.outlineStyle !== "none" && parseFloat(s.outlineWidth) > 0) ||
        (s.boxShadow !== "none" && s.boxShadow !== "") ||
        el.matches(":focus-visible") === false;
      // A ring drawn by a wrapper that reacts to :focus-within also counts.
      let wrapped = false;
      for (let n = el.parentElement, k = 0; n && k < 3; n = n.parentElement, k++) {
        const p = getComputedStyle(n);
        if (n.matches(":focus-within") && ((p.outlineStyle !== "none" && parseFloat(p.outlineWidth) > 0) || p.boxShadow !== "none")) wrapped = true;
      }
      const name = el.getAttribute("aria-label") ?? el.getAttribute("title") ?? (el.textContent ?? "").trim().slice(0, 40);
      return { key: `${el.tagName}:${name}`, name: `${el.getAttribute("role") ?? el.tagName.toLowerCase()} "${name}"`, ring: ring || wrapped };
    });
    if (!r || seen.has(r.key)) continue;
    seen.add(r.key);
    if (!r.ring) out.push({ kind: "focus", detail: `${r.name} shows no focus ring when tabbed to` });
  }
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  return out;
}
