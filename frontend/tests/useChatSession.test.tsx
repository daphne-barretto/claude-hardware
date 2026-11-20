import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import type { SessionEvent } from "@/lib/session-api";

// The hook talks to the session API only through these calls; the test drives the stream.
const api = vi.hoisted(() => ({
  onEvent: null as ((event: SessionEvent) => void) | null,
  onError: null as (() => void) | null,
}));

vi.mock("@/lib/session-api", () => ({
  createSession: vi.fn(async () => ({ id: "session-1" })),
  postMessage: vi.fn(async () => ({})),
  uploadAttachment: vi.fn(),
  subscribe: vi.fn((_id: string, onEvent: (event: SessionEvent) => void, onError?: () => void) => {
    api.onEvent = onEvent;
    api.onError = onError ?? null;
    return () => {};
  }),
}));

import { REPLY_TIMEOUT_MS, useChatSession } from "@/components/sites/claude-shell/useChatSession";

const choices = { model: "default", effort: "default" } as never;

async function sendMessage() {
  const hook = renderHook(() => useChatSession());
  await act(async () => {
    await hook.result.current.send("make a box", [], choices);
  });
  return hook;
}

const errors = (hook: Awaited<ReturnType<typeof sendMessage>>) =>
  hook.result.current.messages.flatMap((m) => m.blocks ?? []).filter((b) => b.kind === "error");

beforeEach(() => {
  vi.useFakeTimers();
  api.onEvent = null;
  api.onError = null;
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useChatSession while waiting for a reply", () => {
  it("is waiting and busy right after a message is sent", async () => {
    const hook = await sendMessage();
    expect(hook.result.current.waiting).toBe(true);
    expect(hook.result.current.busy).toBe(true);
  });

  it("reports a reply that has not started in time and frees the composer", async () => {
    const hook = await sendMessage();
    act(() => vi.advanceTimersByTime(REPLY_TIMEOUT_MS - 1));
    expect(hook.result.current.busy).toBe(true);
    expect(errors(hook)).toHaveLength(0);

    act(() => vi.advanceTimersByTime(1));
    expect(hook.result.current.busy).toBe(false);
    expect(hook.result.current.waiting).toBe(false);
    expect(errors(hook)).toHaveLength(1);
    expect(errors(hook)[0]).toMatchObject({ text: expect.stringMatching(/send it again/) });
    expect((errors(hook)[0] as { text: string }).text).toMatch(/kept/);
  });

  it("says the session is gone when its stream can't come back, and frees the composer", async () => {
    const hook = await sendMessage();
    expect(api.onError).not.toBeNull();
    act(() => api.onError?.());
    expect(hook.result.current.busy).toBe(false);
    expect(errors(hook)).toHaveLength(0);
    const blocks = hook.result.current.messages.flatMap((m) => m.blocks ?? []);
    expect(blocks.some((b) => b.kind === "session_gone")).toBe(true);
  });

  it("stops waiting once the reply starts, with no error after the timeout", async () => {
    const hook = await sendMessage();
    act(() => api.onEvent?.({ type: "message_start", message_id: "m-1", role: "assistant" }));
    expect(hook.result.current.waiting).toBe(false);

    act(() => vi.advanceTimersByTime(REPLY_TIMEOUT_MS * 2));
    expect(errors(hook)).toHaveLength(0);
  });
});
