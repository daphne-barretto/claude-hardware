import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test } from "@playwright/test";
import { openExample } from "./fixtures/sessions";

// Loading the next board revision: Claude starts the work without the user typing, and
// its proposal shows in the chat and on the canvas. Runs against the mock session API, whose
// load plays the scripted run and ends with the fixture change set (docs/architecture.md).
test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");

const fixture = (name: string) => JSON.parse(readFileSync(resolve(__dirname, "../../fixtures", name), "utf8"));
const changeSet = fixture("change-set.json") as { changes: { title: string }[] };
const build = fixture("build.json") as { board: { name: string; revision: string } };

test("loading the next revision produces a proposal in the chat and on the canvas", async ({ page, request }) => {
  const session = await openExample(request);
  expect(session.next_revision).toEqual(build.board);
  await page.goto(`/session/${encodeURIComponent(session.id)}`);

  const chat = page.getByRole("region", { name: "Chat" });
  const canvas = page.getByRole("region", { name: "Canvas" });
  const composer = chat.getByRole("textbox");
  await expect(canvas.getByTestId("viewer")).toHaveCount(0);

  await chat.getByRole("button", { name: "Load next revision" }).click();

  // The load is the user's message; nothing was typed. It shows at once, then as the server
  // stored it ("Loaded … (was …)"), as every tab and a reload show it.
  const load = `${build.board.name} revision ${build.board.revision}`;
  await expect(chat.getByTestId("user-message").filter({ hasText: load })).toHaveCount(1);
  await expect(chat.getByText(`Loaded ${load}`)).toBeVisible({ timeout: 30_000 });
  await expect(composer).toHaveValue("");
  // The proposal in the chat: every change, with the checks behind it.
  const proposed = chat.getByRole("list", { name: "Proposed changes" });
  await expect(proposed).toBeVisible({ timeout: 30_000 });
  await expect(proposed.getByRole("listitem")).toHaveCount(changeSet.changes.length);
  for (const c of changeSet.changes) await expect(proposed.getByText(c.title)).toBeVisible();
  // The proposed build on the canvas.
  await expect(canvas.getByTestId("viewer")).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  // Loaded: the control is gone, and the session has nothing more to load.
  await expect(chat.getByRole("button", { name: "Load next revision" })).toHaveCount(0);
  const after = await (await request.get(`/api/sessions/${encodeURIComponent(session.id)}`)).json();
  expect(after.next_revision).toBeNull();
  expect(after.latest_build_id).toBe(fixture("change-set.json").build);
});
