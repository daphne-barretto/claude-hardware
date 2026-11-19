import type { Page, Request } from "@playwright/test";
import { census, describeControl } from "./census";
import { visualChecks } from "./checks";
import { isOwned, ownerOf } from "./known";
import { expect, shot, test, writeFindings, type Finding } from "./live";
import { STATES, type QaState } from "./states";

// Every control in every QA state, used once (a click; typing for a text field; an arrow key for
// a slider), each from the state as it opens: it must do something a user can observe (the page,
// URL or a field changes, or a file chooser, dialog, popup, download or write request starts),
// with no console error, no failed request and no internal text on screen. The base states sweep
// every control; a tool state sweeps only the controls its tool adds.

const BASES = ["home", "new", "demo", "board", "question", "run", "changes", "boardless"];
/** Controls per test: the big states are split so the sweep runs in parallel. */
const CHUNKS: Record<string, number> = { board: 6, boardless: 6, question: 4, run: 4, changes: 4, demo: 3, new: 2 };
/** Requests that aren't a control's effect: telemetry, and the reads every page makes. */
const NOISE = /\/telemetry$/;

/** Controls that rightly do nothing in the state as it opens. */
const NOOP: [RegExp, string][] = [[/button "Home view"$/, "the view opens at the home view"]];

type Ctl = { role: string; name: string; nth: number; label: string; entry?: string };

async function controlsOf(page: Page): Promise<Ctl[]> {
  const seen = new Map<string, number>();
  const out: Ctl[] = [];
  for (const c of await census(page)) {
    const key = `${c.role}\0${c.name}`;
    const nth = seen.get(key) ?? 0;
    seen.set(key, nth + 1);
    if (c.disabled) continue;
    out.push({ role: c.role, name: c.name, nth, label: describeControl(c), entry: c.entry });
  }
  return out;
}

async function fingerprint(page: Page) {
  return page
    .evaluate(() => {
      const values = Array.from(document.querySelectorAll("input, textarea, select"), (el) => `${(el as HTMLInputElement).value}${(el as HTMLInputElement).checked ? "*" : ""}`);
      return [location.href, document.body.innerHTML, values.join("\0")].join("\n");
    })
    .catch(() => "navigating");
}

function baseOf(state: QaState) {
  return STATES.find((s) => s.id === state.id.split(".")[0])!;
}

const swept = STATES.filter((s) => BASES.includes(s.id) || s.id.startsWith("board.") || s.id.startsWith("boardless.") || s.id.startsWith("run."));

for (const state of swept) {
  const chunks = CHUNKS[state.id] ?? 1;
  for (let chunk = 0; chunk < chunks; chunk++)
    test(`${state.id}${chunks > 1 ? `#${chunk + 1}/${chunks}` : ""}: every control does something`, async ({ page, monitor }) => {
      test.setTimeout(900_000);
      const where = `${state.id}@laptop`;
      const findings: Finding[] = [];
      const results: { control: string; entry?: string; effect: string; problems: string[] }[] = [];
      let session = await state.create(page);
      await state.open(page, session);
      let controls = await controlsOf(page);
      if (!BASES.includes(state.id)) {
        // Only what the tool adds: open the base state on the same session and take its controls away.
        const base = baseOf(state);
        await base.open(page, session);
        const before = new Set((await controlsOf(page)).map((c) => `${c.role}\0${c.name}\0${c.nth}`));
        controls = controls.filter((c) => !before.has(`${c.role}\0${c.name}\0${c.nth}`));
      }
      // Many controls of one kind (check pins, parts, view cube corners) behave alike: three of
      // each inventory entry, or of each kind of control it lacks, stand for the rest.
      const kinds = new Map<string, number>();
      controls = controls.filter((c) => {
        const kind = c.entry?.replace(/\[\d+\]$/, "") ?? `${c.role}:${c.name.replace(/[\d.]+/g, "#").slice(0, 24)}`;
        kinds.set(kind, (kinds.get(kind) ?? 0) + 1);
        return kinds.get(kind)! <= 3;
      });
      controls = controls.filter((_, i) => i % chunks === chunk);
      page.on("dialog", (d) => void d.dismiss().catch(() => {}));
      const internal = async () => new Set((await visualChecks(page).catch(() => [])).filter((x) => x.kind === "internal-text").map((x) => x.detail));
      let dirty = false;
      let first = true;
      for (const c of controls) {
        if (dirty) session = await state.create(page);
        if (!first || !BASES.includes(state.id) || dirty) {
          try {
            await state.open(page, session);
          } catch (err) {
            findings.push({ where, kind: "unreachable", detail: `could not reopen the state for ${c.label}: ${(err as Error).message.split("\n")[0]}` });
            continue;
          }
        }
        first = false;
        dirty = false;
        const loc = page.getByRole(c.role as Parameters<Page["getByRole"]>[0], { name: c.name, exact: true }).nth(c.nth);
        if (!(await loc.isVisible().catch(() => false))) {
          results.push({ control: c.label, entry: c.entry, effect: "not visible (in the accessibility tree only)", problems: [] });
          continue;
        }
        // Choosing what is already chosen (a checked radio, the current page's link) is a no-op.
        const chosen = await loc
          .evaluate((el) => el.getAttribute("aria-checked") === "true" || ((el as HTMLInputElement).type === "radio" && (el as HTMLInputElement).checked) || el.getAttribute("aria-current") === "page")
          .catch(() => false);
        // A build card shows its build on the canvas: a no-op for the build already there.
        const card = /button "Build (build-\S+) /.exec(c.label)?.[1];
        const shown = card && (await page.getByTestId("viewer").getAttribute("data-build").catch(() => null)) === card;
        const noop = chosen ? "already chosen" : shown ? "its build is already on the canvas" : NOOP.find(([re]) => re.test(c.label))?.[1];
        if (noop) {
          results.push({ control: c.label, entry: c.entry, effect: `none expected: ${noop}`, problems: [] });
          continue;
        }
        const problemsBefore = monitor.problems().length;
        const events: string[] = [];
        const onRequest = (r: Request) => {
          if (r.method() !== "GET" && !NOISE.test(r.url())) {
            events.push(`${r.method()} ${new URL(r.url()).pathname}`);
            dirty = true;
          }
        };
        const onEvent = (what: string) => () => events.push(what);
        const handlers = { filechooser: onEvent("file chooser"), dialog: onEvent("dialog"), popup: onEvent("popup"), download: onEvent("download") };
        page.on("request", onRequest);
        for (const [e, h] of Object.entries(handlers)) page.on(e as "popup", h);
        await loc.hover({ timeout: 2_000 }).catch(() => {});
        await page.waitForTimeout(150);
        const shownBefore = await internal();
        const before = await fingerprint(page);
        const url = page.url();
        let failed = "";
        try {
          if (c.role === "textbox" || c.role === "searchbox" || c.role === "spinbutton") await loc.fill("12", { timeout: 5_000 });
          else if (c.role === "slider") {
            // To one end, or the other if it is there already.
            await loc.focus();
            const { value, min } = await loc.evaluate((el) => ({
              value: el.getAttribute("aria-valuenow") ?? (el as HTMLInputElement).value,
              min: el.getAttribute("aria-valuemin") ?? (el as HTMLInputElement).min ?? "0",
            }));
            await page.keyboard.press(Number(value) <= Number(min || 0) ? "End" : "Home");
          } else await loc.click({ timeout: 5_000 });
        } catch (err) {
          const text = (err as Error).message;
          // Under an open popover the click lands on the popover: the popover is the state's.
          const popover = /intercepts pointer events/.test(text) && (await page.locator("[aria-expanded=true]").count()) > 0;
          if (popover) {
            results.push({ control: c.label, entry: c.entry, effect: "none expected: under the state's open popover", problems: [] });
            page.off("request", onRequest);
            for (const [e, h] of Object.entries(handlers)) page.off(e as "popup", h);
            continue;
          }
          failed = text.split("\n")[0];
        }
        await page.waitForTimeout(700);
        const after = await fingerprint(page);
        page.off("request", onRequest);
        for (const [e, h] of Object.entries(handlers)) page.off(e as "popup", h);
        const problems: string[] = [];
        if (failed) problems.push(`the ${c.role === "textbox" ? "typing" : "click"} failed: ${failed}`);
        const effect = events.length ? events.join(", ") : after !== before || page.url() !== url ? (page.url() !== url ? `navigated to ${new URL(page.url()).pathname}` : "the page changed") : "";
        if (!effect && !failed) problems.push("no observable effect");
        problems.push(...monitor.problems().slice(problemsBefore));
        for (const v of await internal()) if (!shownBefore.has(v)) problems.push(`internal-text: ${v}`);
        results.push({ control: c.label, entry: c.entry, effect: effect || "none", problems });
        if (problems.length) {
          const screenshot = await shot(page, `${state.id}-${c.role}-${c.name}`.slice(0, 120));
          for (const p of problems)
            findings.push({ where, kind: p === "no observable effect" ? "dead" : p.startsWith("HTTP") ? "http" : p.startsWith("internal-text") ? "internal-text" : p.startsWith("the ") ? "unclickable" : "console", detail: `${c.label}: ${p}`, screenshot });
        }
      }
      // A finding seen before the click (in the state itself) is the sweep test's, not the control's.
      for (const f of findings) f.owner = ownerOf(f);
      writeFindings(`clicks-${state.id}-${chunk}`, findings, { state: state.id, results });
      for (const f of findings.filter(isOwned)) test.info().annotations.push({ type: `owned by ${f.owner}`, description: `${f.kind}: ${f.detail}` });
      expect(findings.filter((f) => !isOwned(f)).map((f) => `${f.kind}: ${f.detail}`)).toEqual([]);
    });
}
