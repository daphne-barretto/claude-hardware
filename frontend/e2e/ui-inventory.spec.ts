import { expect, test } from "@playwright/test";
import {
  checkHygiene,
  collectControls,
  compareCoverage,
  knownBug,
  preparePage,
  reachState,
  runBehaviour,
  splitKnown,
} from "./inventory/ui-inventory";
import { inv } from "./inventory/app-inventory";

// Every interactive element on every screen and state, against frontend/e2e/ui-inventory.yaml.
// Runs in `make e2e`: in CI, and in QA against staging.

// The mock session API runs only with the local dev servers (playwright.config.ts).
const mockApi = !process.env.E2E_BASE_URL;

test.describe("UI inventory: coverage and hygiene", () => {
  for (const state of inv.states) {
    test(`${state.screen} / ${state.id}: every control is in the inventory`, async ({ page, context }) => {
      test.skip(state.mock_api && !mockApi, "needs the mock session API");
      const errors = await preparePage(page, context, inv, state);
      await reachState(page, inv, state);
      const controls = await collectControls(page);
      const hygiene = splitKnown(await checkHygiene(page, inv, state, controls));
      for (const known of hygiene.known) test.info().annotations.push({ type: "known bug", description: known });
      expect([...compareCoverage(inv, state, controls), ...hygiene.failures, ...errors]).toEqual([]);
    });
  }
});

test.describe("UI inventory: behaviour", () => {
  for (const entry of inv.entries) {
    const what = entry.role === "shortcut" || entry.role === "pointer" ? entry.key : `${entry.role} "${entry.name}"`;
    test(`${entry.id}: ${what} in ${entry.test_in}`, async ({ page, context }) => {
      test.skip(entry.mock_api && !mockApi, "needs the mock session API");
      test.fail(entry.bug?.fails === "behaviour", knownBug(entry.bug));
      const errors = await preparePage(page, context, inv, inv.states.find((s) => s.id === entry.test_in));
      expect(await runBehaviour(page, inv, entry, errors)).toEqual([]);
    });
  }
});
