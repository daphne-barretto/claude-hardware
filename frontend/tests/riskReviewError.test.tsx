import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, renderHook, screen } from "@testing-library/react";

// Regression: a failed Accept or Ignore on a risk flag added a raw error line to the chat on
// every click ("no risk flags: no run has flagged a risk yet", three times over). It now shows
// one plain-language note on the flag itself, and clicking again doesn't repeat it.
const api = vi.hoisted(() => ({ review: vi.fn() }));

vi.mock("@/lib/session-api", async (original) => ({
  ...(await original<typeof import("@/lib/session-api")>()),
  createSession: vi.fn(async () => ({ id: "session-1" })),
  subscribe: vi.fn(() => () => {}),
  reviewRisks: api.review,
}));

import { ApiError } from "@/lib/session-api";
import { useChatSession } from "@/components/sites/claude-shell/useChatSession";
import { ChatMessages } from "@/components/sites/claude-shell/ChatMessages";
import type { ChatMessage } from "@/components/sites/claude-shell/chat";

afterEach(() => {
  cleanup();
  api.review.mockReset();
});

const flags = {
  contract: "risk-flags/1",
  build: "build-1",
  flags: [
    {
      id: "thin:wall",
      kind: "thin",
      message: "A thin wall, on the base.",
      standard: null,
      value: 1,
      limit: 2,
      unit: "mm",
      location: { part: "base", feature: null, component: null, faces: [], point: null },
      status: "proposed",
      fix: { title: "Thicken the wall", edits: {}, changes: [], resolves: true, checks: [] },
    },
  ],
};

describe("a failed risk review", () => {
  it("shows one plain note on the flag and adds nothing to the chat, however often it is clicked", async () => {
    api.review.mockRejectedValue(new ApiError(404, "no risk flags: no run has flagged a risk yet"));
    const hook = renderHook(() => useChatSession("session-1"));
    await act(async () => {}); // the session's history has loaded (or failed to)
    const before = hook.result.current.messages.length;
    for (let i = 0; i < 3; i++) {
      await act(async () => {
        await hook.result.current.reviewRisk("thin:wall", "accept");
      });
    }
    expect(hook.result.current.messages).toHaveLength(before);
    const error = hook.result.current.riskError;
    expect(error).toMatchObject({ flagId: "thin:wall" });
    expect(error?.text).not.toMatch(/no risk flags|no run/);

    const messages: ChatMessage[] = [
      { id: "m1", role: "assistant", text: "", blocks: [{ kind: "risks", riskFlags: flags } as never] },
    ];
    render(
      <ChatMessages messages={messages} busy={false} onAnswer={() => {}} onReviewRisk={() => {}} riskError={error} />,
    );
    const alerts = screen.getAllByRole("alert");
    expect(alerts).toHaveLength(1);
    expect(alerts[0].textContent).toBe(error?.text);
    expect(screen.queryByText(/no risk flags/)).toBeNull();
  });

  it("clears the note once a review goes through", async () => {
    api.review.mockRejectedValueOnce(new ApiError(409, "another review is still rebuilding"));
    api.review.mockResolvedValueOnce(flags);
    const hook = renderHook(() => useChatSession("session-1"));
    await act(async () => {
      await hook.result.current.reviewRisk("thin:wall", "reject");
    });
    expect(hook.result.current.riskError).not.toBeNull();
    await act(async () => {
      await hook.result.current.reviewRisk("thin:wall", "reject");
    });
    expect(hook.result.current.riskError).toBeNull();
  });
});
