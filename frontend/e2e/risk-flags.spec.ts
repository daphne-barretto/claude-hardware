import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test } from "@playwright/test";

// Risk flags in chat: each flagged risk shows with Accept and Ignore for its fix, apart
// from the change set; selecting it shows its location on the canvas. Runs against the mock
// session API, whose recorded run ends with the fixture change set and risk flags.
test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");

const fixture = (name: string) => JSON.parse(readFileSync(resolve(__dirname, "../../fixtures", name), "utf8"));
type Flag = { id: string; message: string; location: { feature?: string | null; point: number[] } };
const flags = (fixture("risk-flags.json") as { flags: Flag[] }).flags;
const target = (p: number[]) => p.map((v) => Math.round(v * 10) / 10 || 0).join(",");

test("flagged risks show in chat with Accept and Ignore, and on the canvas", async ({ page, request }) => {
  expect(flags.length).toBeGreaterThan(0);
  const session = await (await request.post("/api/sessions", { data: {} })).json();
  await page.goto(`/session/${encodeURIComponent(session.id)}`);
  const chat = page.getByRole("region", { name: "Chat" });
  const canvas = page.getByRole("region", { name: "Canvas" });
  await chat.getByRole("button", { name: "Load next revision" }).click();

  const risks = chat.getByRole("region", { name: "Flagged risks" });
  await expect(risks).toBeVisible({ timeout: 30_000 });
  // Apart from the change set: its own block, not a change in the proposal.
  await expect(chat.getByRole("list", { name: "Proposed changes" })).toBeVisible();
  await expect(chat.getByRole("list", { name: "Proposed changes" }).getByText(flags[0].message)).toHaveCount(0);
  await expect(risks.getByRole("listitem")).toHaveCount(flags.length);
  const [first] = flags;
  const subject = first.location.feature!;
  await expect(risks.getByRole("button", { name: `Accept the fix for ${subject}` })).toBeVisible();
  await expect(risks.getByRole("button", { name: `Ignore the fix for ${subject}` })).toBeVisible();

  // Selecting the flag shows where it is: the view centres on its point and its faces are selected.
  const viewer = canvas.getByTestId("viewer");
  await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  await risks.getByRole("button", { name: first.message }).click();
  await expect(viewer).toHaveAttribute("data-target", target(first.location.point));
  await expect(chat.getByRole("button", { name: "Clear selection" })).toBeVisible();

  // Accepting the fix reviews it through the session API and rebuilds.
  const builds = async () => (await (await request.get(`/api/sessions/${encodeURIComponent(session.id)}/builds`)).json()).length;
  const before = await builds();
  const review = page.waitForRequest((r) => r.url().endsWith("/risks") && r.method() === "POST");
  await risks.getByRole("button", { name: `Accept the fix for ${subject}` }).click();
  expect((await review).postDataJSON()).toEqual({ accept: [first.id] });
  const accept = risks.getByRole("button", { name: `Accept the fix for ${subject}` });
  await expect(accept).toHaveAttribute("aria-pressed", "true");
  await expect(accept).toHaveAttribute("title", "You accepted this fix.");
  await expect.poll(builds).toBe(before + 1);

  // Ignoring it takes it back.
  await risks.getByRole("button", { name: `Ignore the fix for ${subject}` }).click();
  await expect(risks.getByRole("button", { name: `Ignore the fix for ${subject}` })).toHaveAttribute("aria-pressed", "true");
  await expect(risks.locator(`[data-risk-id="${first.id}"]`)).toContainText("Fix ignored");
});
