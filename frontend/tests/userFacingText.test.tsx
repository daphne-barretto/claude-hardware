import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import type { Build } from "@/components/canvas/api";
import { getMap } from "@/components/canvas/api";
import { buildName, buildTitle, restoredText } from "@/components/canvas/history";
import { checkTitle, proofText, type Change } from "@/components/canvas/changes";
import type { ChatMessage } from "@/components/sites/claude-shell/chat";
import { ChatMessages } from "@/components/sites/claude-shell/ChatMessages";
import { shownError } from "@/lib/errors";

// QA fix briefs 1, 2 and 8: nothing the user reads shows a build ID, a
// check ID or a raw HTTP status.

const build = (id: string, revision = "2.0") =>
  ({ id, board: { name: "b-parasite", revision }, checks: { pass: 3, fail: 1, untested: 0, not_applicable: 0 } }) as Build;
const builds = [build("build-0a1b2c3d4e5f", "1.2"), build("build-afe3a5476288")];

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("build names", () => {
  it("names a build by its place in the session, never its ID", () => {
    expect(buildName(builds[1], builds)).toBe("Build 2");
    expect(buildTitle(builds[1], builds)).toBe("Build 2 · b-parasite 2.0");
    expect(buildTitle({ ...builds[0], board: null } as unknown as Build, builds)).toBe("Build 1");
    expect(buildName(build("build-ffffffffffff"), builds)).toBe("Build");
    expect(restoredText(builds[0].id, builds)).toBe("Restored from Build 1");
    expect(restoredText("build-gone", builds)).toBe("Restored from an earlier build");
  });

  it("shows a chat build card as Build N, without the ID", () => {
    const messages = [{ id: "m-1", role: "assistant", blocks: [{ kind: "build", build: builds[1] }] }] as ChatMessage[];
    render(<ChatMessages messages={messages} busy={false} onAnswer={() => {}} builds={builds} />);
    const card = screen.getByTestId("build-ready");
    expect(card.textContent).toContain("Build 2 · b-parasite 2.0");
    expect(card.textContent).not.toContain("build-");
    expect(card.getAttribute("aria-label") ?? "").not.toContain("build-");
  });
});

describe("check names in a change", () => {
  const link = { id: "wall_thickness", check: "wall_thickness", title: "Minimum wall thickness", effect: "fixes" as const, before: "fail" as const, alone: "pass" as const };
  const change = (reason: string, checks = [link]) =>
    ({ id: "params.wall", title: "Change wall from 1 to 2", reason, status: "proposed", delta: {}, checks }) as unknown as Change;

  it("names checks by their titles", () => {
    expect(proofText(change("Changed by Claude."))).toBe("Fixes Minimum wall thickness");
    expect(checkTitle({ id: "opening_alignment:Q4-sensor_window", check: "opening_alignment" })).toBe("Opening alignment (Q4 sensor window)");
  });

  it("says it once: nothing under a reason that already names the check", () => {
    expect(proofText(change("Fixes Minimum wall thickness."))).toBeNull();
  });

  it("says a change no check depends on is that, not 0 of 0", () => {
    const messages = [
      {
        id: "m-1",
        role: "assistant",
        blocks: [{ kind: "changes", changeSet: { changes: [change("Changed by Claude.", [])], checks: { pass: 1, fail: 0, untested: 0 } } }],
      },
    ] as unknown as ChatMessage[];
    render(<ChatMessages messages={messages} busy={false} onAnswer={() => {}} />);
    const row = screen.getByText("Change wall from 1 to 2").parentElement!;
    expect(row.textContent).toContain("no check depends on it alone");
    expect(row.textContent).not.toContain("0 of 0");
  });
});

describe("errors in canvas panels", () => {
  it("says a server error in a plain sentence, not its HTTP status", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("Internal Server Error", { status: 500, statusText: "Internal Server Error" })));
    const err = await getMap("session-1", { id: "build-1" }, "wall_thickness").catch((e: Error) => e);
    expect(err).toBeInstanceOf(Error);
    expect((err as Error).message).not.toMatch(/500|Internal Server Error/);
    expect((err as Error).message).toMatch(/server/i);
  });

  it("keeps the server's own plain words", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: "That face is too small to sketch on." }, { status: 409 })));
    const err = await getMap("session-1", { id: "build-1" }, "wall_thickness").catch((e: Error) => e);
    expect((err as Error).message).toBe("That face is too small to sketch on.");
  });

  it("says a dropped connection plainly", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("Failed to fetch"))));
    const err = await getMap("session-1", { id: "build-1" }, "wall_thickness").catch((e: Error) => e);
    expect((err as Error).message).not.toMatch(/Failed to fetch/);
    expect((err as Error).message).toMatch(/reach the server/);
  });

  it("never shows internal text", () => {
    expect(shownError(new Error("500 Internal Server Error"))).not.toMatch(/500/);
    expect(shownError(new Error("no build build-afe3a5476288"))).not.toMatch(/build-/);
    expect(shownError(new Error('{"detail":[{"loc":["body"]}]}'))).not.toMatch(/detail/);
    expect(shownError(new Error("HTTP 404"))).not.toMatch(/404/);
    expect(shownError(new TypeError("Failed to fetch"))).toMatch(/reach the server/);
    expect(shownError(new Error("That name is taken."))).toBe("That name is taken.");
  });
});
