import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, type BrowserContext, type Download, type Locator, type Page, type Request } from "@playwright/test";
import { parse } from "yaml";
import { openExample } from "../fixtures/sessions";

// The UI interaction inventory (frontend/e2e/ui-inventory.yaml): loading it, reaching
// each screen state, and the three checks run on it. Coverage compares the controls in the
// accessibility tree with the inventory, behaviour performs each entry's interaction and
// checks its expected result, and hygiene checks how disabled controls look. Every check
// returns a list of problems (empty = pass) so the fixture tests can prove they fail.

/** Roles a user can interact with. Anything else in the accessibility tree is content. */
export const INTERACTIVE_ROLES = new Set([
  "button",
  "link",
  "textbox",
  "searchbox",
  "combobox",
  "checkbox",
  "radio",
  "switch",
  "slider",
  "spinbutton",
  "menuitem",
  "menuitemradio",
  "menuitemcheckbox",
  "option",
  "tab",
  "treeitem",
]);

/** Keyboard shortcuts and pointer gestures: tested for behaviour, but not in the accessibility tree. */
const UNLISTED_ROLES = new Set(["shortcut", "pointer"]);

export type Step =
  | { click: string }
  | { click_on: string; modifiers?: ("Shift" | "Alt" | "Control" | "ControlOrMeta")[]; position?: [number, number] }
  | { fill: string; text: string }
  | { press: string }
  /** Focuses an entry, for a keyboard interaction with a control the pointer can't reach. */
  | { focus: string }
  /**
   * Creates a session on the mock session API, sends `text` as its first message, waits until
   * the recorded run has finished with a build, and opens /session/{id}.
   */
  | { mock_session: string }
  /** Pastes text into an entry (a synthetic paste event carrying it), `repeat` times over. */
  | { paste: string; text: string; repeat?: number }
  | { attach: string; files: string[] }
  | { mouse: [number, number] }
  /**
   * Drags with the left button inside a locator, from `from` to `to` (fractions of its box,
   * 0 to 1), holding `modifiers`.
   */
  | { drag_on: string; from: [number, number]; to: [number, number]; modifiers?: ("Shift" | "Alt" | "Control" | "Meta")[] }
  | { wait: string; enabled?: boolean; attribute?: { name: string; value: string }; timeout?: number }
  | { goto: string };

export type Expectation =
  /** The path (and #fragment) after the interaction, exactly, or a /regex/. */
  | { url: string }
  | { visible: string }
  | { hidden: string }
  | { attribute: { of?: string; name: string; value: string } }
  | { value: { of?: string; equals?: string; contains?: string } }
  | { focused: string }
  | { filechooser: { multiple?: boolean; accept?: string; directory?: boolean; files?: string[] } }
  | { request: { method: string; url: string; body?: unknown } }
  | { clipboard: "page-url" }
  /** A download starts, named like the control's text. */
  | { download: true };

export type State = {
  id: string;
  screen: string;
  url: string;
  steps: Step[];
  /** Needs the mock session API (docs/architecture.md), so it is skipped against staging. */
  mock_api: boolean;
  /** A console error the state expects (a substring), not counted as a failure. */
  allow_console?: string;
};

/** A known defect: `behaviour` makes the behaviour test an expected failure; `hygiene` is reported, not failed. */
export type Bug = { fails: "behaviour" | "hygiene"; note?: string };

export type Entry = {
  id: string;
  screen: string;
  role: string;
  name: string;
  within?: string;
  key?: string;
  /** Which of several controls with the same role and name the behaviour test uses (default 0). */
  nth?: number;
  states: string[];
  action: Step[];
  before: Step[];
  expect: Expectation[];
  bug?: Bug;
  mock_api: boolean;
  test_in: string;
  /**
   * A list that comes from the session API (Recents, search results): zero or more items,
   * so coverage doesn't require it, and the behaviour test uses the first item.
   */
  dynamic: boolean;
  /**
   * `hidden`: the control is not shown at phone width (below 768 px), where the canvas or
   * artifact already fills the screen. The phone checks (e2e/phone.spec.ts) skip it, and the
   * states reached through it.
   */
  phone?: "hidden";
};

export type Inventory = {
  ready?: string;
  speech: string;
  states: State[];
  entries: Entry[];
};

type RawEntry = Omit<Entry, "states" | "action" | "before" | "mock_api" | "test_in" | "id"> & {
  id: string;
  each?: string;
  states: string[];
  except?: string[];
  action?: Step[];
  before?: Step[];
  test_in?: string;
  mock_api?: boolean;
  dynamic?: boolean;
};

type RawState = { id: string; screen?: string; url?: string; from?: string; steps?: Step[]; mock_api?: boolean; allow_console?: string };

// ---------------------------------------------------------------------------------------
// Loading and templating

type Vars = Record<string, unknown>;

function lookup(vars: Vars, path: string): unknown {
  let v: unknown = vars;
  for (const key of path.split(".")) {
    if (v === null || v === undefined) break;
    v = (v as Record<string, unknown>)[key];
  }
  if (v === undefined) throw new Error(`ui-inventory: unknown template value {${path}}`);
  return v;
}

const escapeRegex = (s: string) => s.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&");

/** Replaces {a.b.0.c} with values from vars. Inside a /regex/ name, values are escaped. */
export function fill(text: string, vars: Vars): string {
  const sub = (part: string, isRegex: boolean) =>
    part.replace(/\{([a-zA-Z_][\w.]*)\}/g, (_, path: string) => {
      const v = String(lookup(vars, path));
      return isRegex ? escapeRegex(v) : v;
    });
  if (/^\/.*\/$/.test(text)) return sub(text, true);
  // A locator chain: only its quoted "/regex/" names are regexes.
  return text
    .split(/("\/.*?\/")/)
    .map((part) => sub(part, /^"\/.*\/"$/.test(part)))
    .join("");
}

function fillDeep<T>(value: T, vars: Vars): T {
  if (typeof value === "string") return fill(value, vars) as T;
  if (Array.isArray(value)) return value.map((v) => fillDeep(v, vars)) as T;
  if (value && typeof value === "object")
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, fillDeep(v, vars)])) as T;
  return value;
}

/** Glob match for state patterns: `*` matches any run of characters. */
function glob(pattern: string, id: string) {
  return new RegExp(`^${pattern.split("*").map(escapeRegex).join(".*")}$`).test(id);
}

export function loadInventory(file: string, vars: Vars = {}): Inventory {
  const raw = parse(readFileSync(file, "utf8")) as {
    ready?: string;
    speech?: string;
    states: RawState[];
    entries: RawEntry[];
  };
  const problems: string[] = [];

  const rawStates = new Map(raw.states.map((s) => [s.id, s]));
  if (rawStates.size !== raw.states.length) problems.push("duplicate state ids");
  const resolveState = (id: string, seen: string[] = []): State => {
    const s = rawStates.get(id);
    if (!s) throw new Error(`ui-inventory: unknown state ${id}`);
    if (seen.includes(id)) throw new Error(`ui-inventory: state cycle ${[...seen, id].join(" > ")}`);
    if (!s.from) {
      // A state can start from a mock session instead of a url.
      if (!s.url && !(s.steps?.[0] && "mock_session" in s.steps[0]))
        throw new Error(`ui-inventory: state ${id} needs url, from or a first mock_session step`);
      return {
        id,
        screen: s.screen ?? id,
        url: s.url ? fill(s.url, vars) : "",
        steps: fillDeep(s.steps ?? [], vars),
        mock_api: !!s.mock_api,
        ...(s.allow_console ? { allow_console: s.allow_console } : {}),
      };
    }
    const parent = resolveState(s.from, [...seen, id]);
    return {
      id,
      screen: s.screen ?? parent.screen,
      url: parent.url,
      steps: [...parent.steps, ...fillDeep(s.steps ?? [], vars)],
      mock_api: parent.mock_api || !!s.mock_api,
      ...((s.allow_console ?? parent.allow_console) ? { allow_console: s.allow_console ?? parent.allow_console } : {}),
    };
  };
  const states = raw.states.map((s) => resolveState(s.id));
  const stateIds = states.map((s) => s.id);

  const entries: Entry[] = [];
  for (const e of raw.entries) {
    const items: [string, Vars][] = e.each
      ? (lookup(vars, e.each) as unknown[]).map((it, i) => [`${e.id}[${i}]`, { ...vars, it }])
      : [[e.id, vars]];
    for (const [id, v] of items) {
      const where = e.states.flatMap((p) => stateIds.filter((s) => glob(p, s)));
      const inStates = stateIds.filter((s) => where.includes(s) && !(e.except ?? []).some((p) => glob(p, s)));
      if (!inStates.length) problems.push(`${id}: its states match no state`);
      if (!e.expect?.length) problems.push(`${id}: no expected result`);
      if (e.phone !== undefined && e.phone !== "hidden") problems.push(`${id}: phone must be hidden`);
      if (e.bug && !["behaviour", "hygiene"].includes(e.bug.fails))
        problems.push(`${id}: bug needs fails: behaviour or hygiene`);
      if (UNLISTED_ROLES.has(e.role) ? !e.action?.length : !INTERACTIVE_ROLES.has(e.role))
        problems.push(`${id}: role ${e.role} is not interactive (or a shortcut without an action)`);
      const testIn = e.test_in ?? inStates[0];
      if (testIn && !stateIds.includes(testIn)) problems.push(`${id}: test_in ${testIn} is not a state`);
      const defaultAction: Step[] =
        e.role === "textbox" || e.role === "searchbox" ? [{ fill: "self", text: "Inventory check" }] : [{ click: "self" }];
      entries.push({
        id,
        screen: e.screen,
        role: e.role,
        name: fill(String(e.name ?? ""), v),
        within: e.within ? fill(e.within, v) : undefined,
        key: e.key,
        nth: e.nth,
        states: inStates,
        action: fillDeep(e.action ?? defaultAction, v),
        before: fillDeep(e.before ?? [], v),
        expect: fillDeep(e.expect ?? [], v),
        bug: e.bug,
        mock_api: !!e.mock_api || !!states.find((s) => s.id === testIn)?.mock_api,
        test_in: testIn,
        dynamic: !!e.dynamic,
        ...(e.phone ? { phone: e.phone } : {}),
      });
    }
  }
  if (new Set(entries.map((e) => e.id)).size !== entries.length) problems.push("duplicate entry ids");
  // Steps refer to entries by id; check they all resolve.
  const ids = new Set(entries.map((e) => e.id));
  const refs = (steps: Step[]) =>
    steps.flatMap((s) => ["click", "fill", "attach", "paste", "focus"].map((k) => (s as Record<string, unknown>)[k] as string | undefined));
  for (const s of states) for (const r of refs(s.steps)) if (r && !ids.has(r)) problems.push(`state ${s.id}: unknown entry ${r}`);
  for (const e of entries)
    for (const r of refs([...e.action, ...e.before])) if (r && r !== "self" && !ids.has(r)) problems.push(`${e.id}: unknown entry ${r}`);
  if (problems.length) throw new Error(`ui-inventory: ${file} is invalid:\n${problems.join("\n")}`);

  return { ready: raw.ready, speech: raw.speech ?? "dictated words", states, entries };
}

// ---------------------------------------------------------------------------------------
// Locators

/** A name in the inventory: an exact string, or /regex/. */
function nameMatcher(name: string): string | RegExp {
  const m = /^\/(.*)\/$/.exec(name);
  return m ? new RegExp(m[1]) : name;
}

/** A url in the inventory: an exact path, or /regex/ (for session pages, whose IDs vary). */
function urlMatcher(url: string): RegExp {
  const m = nameMatcher(url);
  return typeof m === "string" ? new RegExp(`^${escapeRegex(m)}$`) : m;
}

function nameMatches(pattern: string, name: string) {
  const m = nameMatcher(pattern);
  return typeof m === "string" ? m === name : m.test(name);
}

type Part = { role: string; name?: string };

/** `region "Chat" >> button "Send"`, `text "Hello"`, `menu`. */
function parseSpec(spec: string): Part[] {
  return spec.split(" >> ").map((p) => {
    const m = /^([a-z]+)(?:\s+"(.*)")?$/.exec(p.trim());
    if (!m) throw new Error(`ui-inventory: bad locator "${spec}"`);
    return { role: m[1], name: m[2] };
  });
}

function partLocator(root: Page | Locator, { role, name }: Part): Locator {
  if (role === "text") return root.getByText(nameMatcher(name ?? ""));
  if (role === "testid") return root.getByTestId(name ?? "");
  return root.getByRole(role as Parameters<Page["getByRole"]>[0], name === undefined ? {} : { name: nameMatcher(name), exact: true });
}

export function specLocator(page: Page, spec: string): Locator {
  return parseSpec(spec).reduce<Page | Locator>((root, part) => partLocator(root, part), page) as Locator;
}

export function entryLocator(page: Page, entry: Entry): Locator {
  const root = entry.within ? specLocator(page, entry.within) : page;
  const loc = partLocator(root, { role: entry.role, name: entry.name });
  if (entry.nth !== undefined) return loc.nth(entry.nth);
  return entry.dynamic ? loc.first() : loc;
}

// ---------------------------------------------------------------------------------------
// Page setup and reaching states

/**
 * Prepares a page for the checks: records console errors and unhandled rejections, grants
 * the clipboard, and replaces speech recognition with a stub that "hears" `speech` when it
 * stops (and the microphone with a generated tone), so dictation behaves the same in every
 * browser.
 */
export async function preparePage(page: Page, context: BrowserContext, inv: Inventory, state?: State) {
  const errors: string[] = [];
  page.on("pageerror", (err) => errors.push(`page error: ${err.message}`));
  page.on("console", (msg) => {
    if (msg.type() !== "error") return;
    if (state?.allow_console && msg.text().includes(state.allow_console)) return;
    errors.push(`console error: ${msg.text()}`);
  });
  await context.grantPermissions(["clipboard-read", "clipboard-write"]).catch(() => {});
  await page.addInitScript((speech: string) => {
    window.addEventListener("unhandledrejection", (e) => {
      const r = e.reason as { message?: string } | undefined;
      console.error(`unhandled rejection: ${r?.message ?? String(e.reason)}`);
    });
    class StubRecognition {
      continuous = false;
      interimResults = false;
      lang = "";
      onresult: ((e: unknown) => void) | null = null;
      onend: (() => void) | null = null;
      onerror: (() => void) | null = null;
      start() {}
      stop() {
        this.onresult?.({ results: [Object.assign([{ transcript: speech }], { isFinal: true })] });
        this.onend?.();
      }
      abort() {
        this.onend?.();
      }
    }
    const w = window as unknown as Record<string, unknown>;
    w.SpeechRecognition = StubRecognition;
    w.webkitSpeechRecognition = StubRecognition;
    // The level meter listens to the microphone: give it a quiet tone instead of a prompt.
    if (navigator.mediaDevices)
      navigator.mediaDevices.getUserMedia = async () => {
        const audio = new AudioContext();
        const tone = audio.createOscillator();
        const out = audio.createMediaStreamDestination();
        tone.connect(out);
        tone.start();
        return out.stream;
      };
  }, inv.speech);
  return errors;
}

export function entryById(inv: Inventory, id: string) {
  const e = inv.entries.find((x) => x.id === id);
  if (!e) throw new Error(`ui-inventory: unknown entry ${id}`);
  return e;
}

/** `timeout` bounds clicks and fills, so an unreachable control fails the step instead of hanging. */
export async function runStep(page: Page, inv: Inventory, step: Step, self?: Entry, timeout?: number) {
  const target = (id: string) => entryLocator(page, id === "self" && self ? self : entryById(inv, id));
  if ("click" in step) await target(step.click).click({ timeout });
  else if ("click_on" in step)
    await specLocator(page, step.click_on)
      .first()
      .click({ timeout, modifiers: step.modifiers, position: step.position && { x: step.position[0], y: step.position[1] } });
  else if ("fill" in step) await target(step.fill).fill(step.text, { timeout });
  else if ("press" in step) await page.keyboard.press(step.press);
  else if ("focus" in step) await target(step.focus).focus();
  else if ("mock_session" in step) await sessions.open(page, inv, step.mock_session);
  else if ("paste" in step)
    await target(step.paste).evaluate((node, text) => {
      (node as HTMLElement).focus();
      const data = new DataTransfer();
      data.setData("text/plain", text);
      node.dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
    }, step.text.repeat(step.repeat ?? 1));
  else if ("mouse" in step) await page.mouse.click(step.mouse[0], step.mouse[1]);
  else if ("drag_on" in step) {
    const box = await specLocator(page, step.drag_on).first().boundingBox({ timeout });
    if (!box) throw new Error(`ui-inventory: ${step.drag_on} is not visible`);
    const at = ([fx, fy]: [number, number]) => [box.x + fx * box.width, box.y + fy * box.height] as const;
    for (const m of step.modifiers ?? []) await page.keyboard.down(m);
    await page.mouse.move(...at(step.from));
    await page.mouse.down();
    await page.mouse.move(...at(step.to), { steps: 8 });
    await page.mouse.up();
    for (const m of step.modifiers ?? []) await page.keyboard.up(m);
  }
  else if ("goto" in step) await page.goto(step.goto);
  else if ("wait" in step) {
    const loc = specLocator(page, step.wait).first();
    const timeout = step.timeout ?? 30_000;
    if (step.enabled) await expect(loc).toBeEnabled({ timeout });
    else if (step.attribute) await expect(loc).toHaveAttribute(step.attribute.name, nameMatcher(step.attribute.value), { timeout });
    else await expect(loc).toBeVisible({ timeout });
  } else if ("attach" in step) {
    const chooser = page.waitForEvent("filechooser");
    await target(step.attach).click();
    await (await chooser).setFiles(step.files.map((name) => ({ name, mimeType: "text/plain", buffer: Buffer.from(name) })));
  }
}

export async function waitReady(page: Page, inv: Inventory) {
  if (inv.ready) await expect(page.locator(inv.ready)).toBeAttached({ timeout: 30_000 });
}

/** A mock-API session whose recorded run has finished with a build, opened at /session/{id}. */
/** Answers the recorded run's question (fixtures/session-events.jsonl) and waits for the reply. */
export async function answerOpenQuestion(page: Page, path: string) {
  const { status } = (await (await page.request.get(path)).json()) as { status: string };
  if (status !== "waiting_for_answer") return;
  const question = readFileSync(resolve(__dirname, "../../../fixtures/session-events.jsonl"), "utf8")
    .split("\n")
    .filter(Boolean)
    .map((line) => (JSON.parse(line) as { event: { type: string; question_id?: string; options?: { id: string }[] } }).event)
    .find((e) => e.type === "question")!;
  const answer = { question_id: question.question_id, option_id: question.options![0].id };
  expect((await page.request.post(`${path}/messages`, { data: { text: "", answer } })).ok()).toBe(true);
  await expect
    .poll(async () => ((await (await page.request.get(path)).json()) as { status: string }).status, { timeout: 30_000 })
    .toBe("idle");
}

/**
 * How a `mock_session` step makes its session: on the mock session API by default; the QA
 * harness (e2e/qa/) swaps in the real backend's.
 */
export const sessions = { open: (page: Page, inv: Inventory, text: string) => openMockSession(page, inv, text) };

async function openMockSession(page: Page, inv: Inventory, text: string) {
  const session = await openExample(page.request);
  const path = `/api/sessions/${encodeURIComponent(session.id)}`;
  await page.request.post(`${path}/messages`, { data: { text } });
  await expect
    .poll(
      async () => {
        const { status } = (await (await page.request.get(path)).json()) as { status: string };
        const builds = (await (await page.request.get(`${path}/builds`)).json()) as unknown[];
        return status !== "running" && builds.length > 0;
      },
      { timeout: 30_000 },
    )
    .toBe(true);
  // The run waits on its question, and the session API takes only the answer then:
  // answer it, so the states can send messages. The conflict run's question stays open: its
  // states are about that question.
  const { conflict_prompt } = JSON.parse(
    readFileSync(resolve(__dirname, "../../../fixtures/session.json"), "utf8"),
  ) as { conflict_prompt: string };
  if (text !== conflict_prompt) await answerOpenQuestion(page, path);
  await page.goto(`/session/${encodeURIComponent(session.id)}`);
  await waitReady(page, inv);
}

export async function reachState(page: Page, inv: Inventory, state: State) {
  if (state.url) {
    await page.goto(state.url);
    await waitReady(page, inv);
  }
  for (const step of state.steps) await runStep(page, inv, step);
}

// ---------------------------------------------------------------------------------------
// Coverage

export type Control = { role: string; name: string; ancestors: Part[]; disabled: boolean };

/** Parses Playwright's ARIA snapshot (YAML-like, indented by depth) into its interactive controls. */
export function parseControls(snapshot: string): Control[] {
  const controls: Control[] = [];
  const stack: { indent: number; part: Part }[] = [];
  for (const raw of snapshot.split("\n")) {
    // Names with a colon come YAML-quoted: - 'button "Hide: base" [pressed]'
    const quoted = /^(\s*)- '((?:[^']|'')*)'(:.*)?$/.exec(raw);
    const line = quoted ? `${quoted[1]}- ${quoted[2].replace(/''/g, "'")}${quoted[3] ?? ""}` : raw;
    const m = /^(\s*)- ([a-z]+)(?: "((?:[^"\\]|\\.)*)")?((?: \[[^\]]+\])*)(?::.*)?$/.exec(line);
    if (!m) continue;
    const indent = m[1].length;
    const role = m[2];
    const name = m[3] === undefined ? "" : (JSON.parse(`"${m[3]}"`) as string);
    while (stack.length && stack[stack.length - 1].indent >= indent) stack.pop();
    if (INTERACTIVE_ROLES.has(role))
      controls.push({ role, name, ancestors: stack.map((s) => s.part), disabled: /\[disabled\]/.test(m[4]) });
    stack.push({ indent, part: { role, name } });
  }
  return controls;
}

/** The controls a user can reach: inside an open modal dialog only, as assistive tech sees it. */
export async function collectControls(page: Page): Promise<Control[]> {
  const modal = page.locator('[aria-modal="true"]');
  const root = (await modal.count()) ? modal.last() : page.locator("body");
  return parseControls(await root.ariaSnapshot());
}

function withinMatches(within: string | undefined, ancestors: Part[]) {
  if (!within) return true;
  // Each part of the chain must be an ancestor, in order.
  let i = 0;
  for (const want of parseSpec(within)) {
    while (i < ancestors.length && !(ancestors[i].role === want.role && (want.name === undefined || nameMatches(want.name, ancestors[i].name ?? ""))))
      i++;
    if (i === ancestors.length) return false;
    i++;
  }
  return true;
}

export function entryMatches(entry: Entry, c: Control) {
  return entry.role === c.role && nameMatches(entry.name, c.name) && withinMatches(entry.within, c.ancestors);
}

const describe = (c: Control) =>
  [...c.ancestors.filter((a) => a.name).map((a) => `${a.role} "${a.name}"`), `${c.role} "${c.name}"`].join(" >> ");

/** Every control in the state is in the inventory, and every entry for the state is there. */
export function compareCoverage(inv: Inventory, state: State, controls: Control[]): string[] {
  const listed = inv.entries.filter((e) => e.states.includes(state.id) && !UNLISTED_ROLES.has(e.role));
  const problems: string[] = [];
  for (const c of controls)
    if (!listed.some((e) => entryMatches(e, c))) problems.push(`${state.id}: ${describe(c)} is not in the inventory`);
  for (const e of listed)
    if (!e.dynamic && !controls.some((c) => entryMatches(e, c)))
      problems.push(`${state.id}: inventory entry ${e.id} (${e.role} "${e.name}") no longer exists`);
  return problems;
}

// ---------------------------------------------------------------------------------------
// Hygiene

/** Disabled controls must look disabled (dimmed or a not-allowed cursor) and say why (title or description). */
export async function checkHygiene(page: Page, inv: Inventory, state: State, controls: Control[]): Promise<string[]> {
  const problems: string[] = [];
  const seen = new Map<string, number>();
  for (const c of controls.filter((x) => x.disabled)) {
    const key = `${c.role}\0${c.name}`;
    const n = seen.get(key) ?? 0;
    seen.set(key, n + 1);
    const el = page.getByRole(c.role as Parameters<Page["getByRole"]>[0], { name: c.name, exact: true, disabled: true }).nth(n);
    const { opacity, cursor, why } = await el.evaluate((node) => {
      let opacity = 1;
      for (let n: Element | null = node; n; n = n.parentElement) opacity *= Number(getComputedStyle(n).opacity);
      const described = (node.getAttribute("aria-describedby") ?? "")
        .split(/\s+/)
        .map((id) => document.getElementById(id)?.textContent?.trim() ?? "")
        .join(" ")
        .trim();
      const titled = node.closest("[title]")?.getAttribute("title")?.trim() ?? "";
      return { opacity, cursor: getComputedStyle(node).cursor, why: described || titled };
    });
    const known = inv.entries.find((e) => e.states.includes(state.id) && entryMatches(e, c))?.bug;
    const tag = known?.fails === "hygiene" ? ` (${knownBug(known)})` : "";
    if (opacity > 0.6 && cursor !== "not-allowed")
      problems.push(`${state.id}: disabled ${describe(c)} does not look disabled${tag}`);
    if (!why) problems.push(`${state.id}: disabled ${describe(c)} does not say why it is disabled${tag}`);
  }
  return problems;
}

/** How a problem or an expected failure names a known bug: "known bug", with its note if it has one. */
export function knownBug(bug: Bug | undefined) {
  return bug?.note ? `known bug: ${bug.note}` : "known bug";
}

/** Splits hygiene problems into failures and those a known bug in the inventory already covers. */
export function splitKnown(problems: string[]) {
  const isKnown = (p: string) => / \(known bug(: .*)?\)$/.test(p);
  return {
    failures: problems.filter((p) => !isKnown(p)),
    known: problems.filter(isKnown),
  };
}

// ---------------------------------------------------------------------------------------
// Behaviour

/** An assertion's message without colours or the call log. */
function brief(err: unknown) {
  const text = (err as Error).message.replace(/\u001b\[[0-9;]*m/g, "");
  return text.split(/\n\s*Call log:/)[0].split("\n").map((l) => l.trim()).filter(Boolean).join(" | ");
}

async function fingerprint(page: Page, withFocus = false) {
  return page
    .evaluate((withFocus) => {
      const values = Array.from(document.querySelectorAll("input, textarea, select"), (el) => (el as HTMLInputElement).value);
      // Focus only for shortcuts: clicking any button focuses it, dead or not, but a key press
      // moves focus only when its handler does.
      const active = document.activeElement;
      const focus = withFocus && active ? `${active.tagName}#${Array.from(document.querySelectorAll("*")).indexOf(active)}` : "";
      return [location.href, document.body.innerHTML, values.join("\0"), focus].join("\n");
    }, withFocus)
    .catch(() => "navigating");
}

/**
 * Performs the entry's interaction in its test state and checks each expected result.
 * Fails on a missed expectation, on no observable effect (no DOM, URL or value change, no
 * focus change for a shortcut, and no file chooser, dialog, popup, download or write request),
 * and on console errors or unhandled rejections.
 */
export async function runBehaviour(page: Page, inv: Inventory, entry: Entry, errors: string[], timeout = 5_000): Promise<string[]> {
  const problems: string[] = [];
  const state = inv.states.find((s) => s.id === entry.test_in)!;
  await reachState(page, inv, state);
  for (const step of entry.before) await runStep(page, inv, step, entry);

  // Hover first, so hover-only changes don't count as the click's effect.
  // (A control the pointer can't reach is skipped here; its action then fails if it needs the pointer.)
  if (!UNLISTED_ROLES.has(entry.role)) await entryLocator(page, entry).hover({ timeout: 2_000 }).catch(() => {});
  await page.waitForTimeout(100);
  // The control's text, for a download named after it.
  const name = entry.expect.some((x) => "download" in x) ? await entryLocator(page, entry).textContent() : null;
  const withFocus = entry.role === "shortcut";
  const before = await fingerprint(page, withFocus);
  const urlBefore = page.url();

  const events: string[] = [];
  const requests: Request[] = [];
  const downloads: Download[] = [];
  const onRequest = (r: Request) => {
    requests.push(r);
    if (r.method() !== "GET") events.push(`request ${r.method()} ${r.url()}`);
  };
  const onChooser = () => events.push("filechooser");
  const onDialog = () => events.push("dialog");
  const onPopup = () => events.push("popup");
  const onDownload = (d: Download) => {
    downloads.push(d);
    events.push("download");
  };
  page.on("request", onRequest);
  page.on("filechooser", onChooser);
  page.on("dialog", onDialog);
  page.on("popup", onPopup);
  page.on("download", onDownload);

  try {
    const chooserSpec = entry.expect.find((x): x is Extract<Expectation, { filechooser: unknown }> => "filechooser" in x);
    const chooser = chooserSpec ? page.waitForEvent("filechooser", { timeout }).catch(() => null) : null;
    try {
      for (const step of entry.action) await runStep(page, inv, step, entry, timeout);
    } catch (err) {
      problems.push(`${entry.id}: the interaction failed: ${brief(err)}`);
      return [...problems, ...errors.map((e) => `${entry.id}: ${e}`)];
    }

    if (chooserSpec) {
      const fc = await chooser;
      const want = chooserSpec.filechooser;
      if (!fc) problems.push(`${entry.id}: no file chooser opened`);
      else {
        if (want.multiple !== undefined && fc.isMultiple() !== want.multiple)
          problems.push(`${entry.id}: file chooser multiple is ${fc.isMultiple()}`);
        if (want.accept !== undefined) {
          const accept = (await fc.element().getAttribute("accept")) ?? "";
          if (!accept.includes(want.accept)) problems.push(`${entry.id}: file chooser accepts "${accept}", not "${want.accept}"`);
        }
        if (want.directory !== undefined) {
          const dir = await fc.element().evaluate((el) => (el as HTMLInputElement).webkitdirectory);
          if (dir !== want.directory) problems.push(`${entry.id}: file chooser directory is ${dir}`);
        }
        if (want.files)
          await fc.setFiles(want.files.map((name) => ({ name, mimeType: "text/plain", buffer: Buffer.from(name) })));
      }
    }

    const target = (of?: string) => (!of || of === "self" ? entryLocator(page, entry) : specLocator(page, of).first());
    for (const exp of entry.expect) {
      try {
        if ("url" in exp)
          await expect
            .poll(() => {
              const u = new URL(page.url());
              return u.pathname + u.hash;
            }, { timeout })
            .toMatch(urlMatcher(exp.url));
        else if ("visible" in exp) await expect(specLocator(page, exp.visible).first()).toBeVisible({ timeout });
        else if ("hidden" in exp) await expect(specLocator(page, exp.hidden)).toHaveCount(0, { timeout });
        else if ("attribute" in exp)
          await expect(target(exp.attribute.of)).toHaveAttribute(exp.attribute.name, nameMatcher(exp.attribute.value), { timeout });
        else if ("value" in exp) {
          const loc = target(exp.value.of);
          if (exp.value.equals !== undefined) await expect(loc).toHaveValue(exp.value.equals, { timeout });
          if (exp.value.contains !== undefined) await expect(loc).toHaveValue(new RegExp(escapeRegex(exp.value.contains)), { timeout });
        } else if ("focused" in exp) await expect(target(exp.focused)).toBeFocused({ timeout });
        else if ("request" in exp) {
          const want = exp.request;
          await expect
            .poll(
              () =>
                requests.some((r) => {
                  if (r.method() !== want.method || !new RegExp(want.url).test(r.url())) return false;
                  if (want.body === undefined) return true;
                  try {
                    expect(r.postDataJSON()).toMatchObject(want.body as Record<string, unknown>);
                    return true;
                  } catch {
                    return false;
                  }
                }),
              { timeout },
            )
            .toBe(true);
        } else if ("download" in exp) {
          await expect.poll(() => downloads.length, { timeout }).toBeGreaterThan(0);
          expect(downloads.map((d) => d.suggestedFilename())).toContain(name?.trim());
        } else if ("clipboard" in exp)
          await expect.poll(() => page.evaluate(() => navigator.clipboard.readText()), { timeout }).toBe(urlBefore);
      } catch (err) {
        problems.push(`${entry.id}: expected ${JSON.stringify(exp)}: ${brief(err)}`);
      }
    }

    await page.waitForTimeout(100);
    if (!events.length && (await fingerprint(page, withFocus)) === before && page.url() === urlBefore)
      problems.push(`${entry.id}: no observable effect`);
  } finally {
    page.off("request", onRequest);
    page.off("filechooser", onChooser);
    page.off("dialog", onDialog);
    page.off("popup", onPopup);
    page.off("download", onDownload);
  }
  return [...problems, ...errors.map((e) => `${entry.id}: ${e}`)];
}
