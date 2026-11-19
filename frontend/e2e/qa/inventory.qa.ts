import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import type { Page } from "@playwright/test";
import { vars } from "../inventory/app-inventory";
import { knownBug, loadInventory, preparePage, runBehaviour, sessions, waitReady, type Inventory } from "../inventory/ui-inventory";
import { isOwned, ownerOf } from "./known";
import { MOCK_ONLY } from "./mockOnly";
import { answer, exampleSession, expect, send, test, writeFindings, type Finding } from "./live";

// frontend/e2e/ui-inventory.yaml on the real backend: every entry's interaction performed and its
// expected result checked (the same checks `make e2e` runs on the mock API), with the sessions
// made on the real backend with the scripted Claude. The scripted Claude asks its own question
// (backend/app/e2e_api.py), so the templates take that one; entries that need what only the
// mock's recorded runs make (a conflict, risk flags, Claude's find) are listed in mockOnly.ts
// and stay covered by `make e2e`.

const e2eApi = readFileSync(resolve(__dirname, "../../../backend/app/e2e_api.py"), "utf8");
// On /new the session has no board, so the scripted Claude asks what to design (`script()`).
const QUESTION = /"question": "(What should I design\?)"/.exec(e2eApi)![1];
const OPTIONS = Array.from(e2eApi.matchAll(/\{"id": "(\w+)", "label": "([^"]+)", "description": "([^"]+)"\}/g))
  .map(([, id, label, description]) => ({ id, label, description }))
  .filter((o) => o.id === "box" || o.id === "bracket");

// A `mock_session` session on the real backend has two builds, as the mock's recorded run
// does: the example's existing enclosure (the build on the older revision) and its build on the
// new revision, which the run checked. The real backend numbers builds from 1; the fixtures
// from 0.
const live: Inventory = loadInventory(resolve(__dirname, "../ui-inventory.yaml"), {
  ...vars,
  question: { ...vars.question, text: QUESTION, options: OPTIONS },
  build: { ...vars.build, id: "build-2" },
  build_source: { ...vars.build_source, build_id: "build-2" },
  previous_build: { ...vars.previous_build, id: "build-1" },
  // A session started on /new has no board here, and is named from its first request
  // (`title_for` in backend/app/titles.py on new.question's message).
  session: { ...vars.session, title: "Check the enclosure against the new" },
});
// On /new the real backend starts a session without a board, so the canvas controls of a board
// session are tested in the board example's session instead of after the first message on New.
for (const e of live.entries)
  if (/^new\.(question|answered)$/.test(e.test_in) && e.states.includes("session") && !/^(question|constraint)\./.test(e.id))
    e.test_in = "session";

/** A `mock_session` step on the real backend: the board example, sent `text`, its question answered. */
sessions.open = async (page: Page, inv: Inventory, text: string) => {
  const id = await exampleSession(page);
  const status = await send(page, id, text);
  if (status === "waiting_for_answer") await answer(page, id, 0);
  await page.goto(`/session/${encodeURIComponent(id)}`);
  await waitReady(page, inv);
};

test.beforeEach(async ({ context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]).catch(() => {});
});

for (const entry of live.entries) {
  const what = entry.role === "shortcut" || entry.role === "pointer" ? entry.key : `${entry.role} "${entry.name}"`;
  test(`${entry.id}: ${what} in ${entry.test_in}`, async ({ page, context }) => {
    test.setTimeout(300_000);
    const mockOnly = MOCK_ONLY.find((m) => m.entries.test(entry.id) || m.states?.test(entry.test_in));
    test.skip(!!mockOnly, `covered on the mock API only: ${mockOnly?.why}`);
    test.fail(entry.bug?.fails === "behaviour", knownBug(entry.bug));
    const state = live.states.find((s) => s.id === entry.test_in);
    const errors = await preparePage(page, context, live, state);
    // Exports and rebuilds are real here, so results get longer to show than on the mock.
    const problems = await runBehaviour(page, live, entry, errors, 30_000);
    const findings = problems.map((p) => ({ where: `${entry.test_in}@laptop`, kind: "behaviour", detail: p }));
    for (const f of findings) Object.assign(f, { owner: ownerOf(f) });
    writeFindings(`inventory-${entry.id}`, findings, { entry: entry.id, state: entry.test_in });
    const owned = findings.filter((f) => isOwned(f as Finding));
    if (owned.length && owned.length === findings.length) test.fail(true, `owned by ${(owned[0] as { owner?: string }).owner}`);
    expect(problems).toEqual([]);
  });
}
