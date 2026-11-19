import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, renderHook, screen } from "@testing-library/react";
import type { MessageQueue, QueuedMessage, SessionEvent } from "@/lib/session-api";
import {
  EMPTY_QUEUE,
  PAUSED_REASON,
  applyQueueEvent,
  edited,
  moved,
  queueHeading,
  sentMessage,
} from "@/components/sites/claude-shell/messageQueue";
import { QueuedMessages } from "@/components/sites/claude-shell/QueuedMessages";
import type { ChatMessage } from "@/components/sites/claude-shell/chat";

// The hook talks to the session API only through these calls; the test drives the stream.
const api = vi.hoisted(() => ({ onEvent: null as ((event: SessionEvent) => void) | null }));

vi.mock("@/lib/session-api", async (original) => ({
  ...(await original<typeof import("@/lib/session-api")>()),
  createSession: vi.fn(async () => ({ id: "session-1" })),
  listMessages: vi.fn(async () => []),
  postMessage: vi.fn(async () => ({})),
  queueMessage: vi.fn(async (_sid: string, body: unknown) => ({ id: "queued-9", created_at: "", message: body })),
  updateQueued: vi.fn(async () => ({})),
  removeQueued: vi.fn(async () => ({})),
  sendQueued: vi.fn(async () => ({})),
  stopRun: vi.fn(async () => ({})),
  uploadAttachment: vi.fn(),
  subscribe: vi.fn((_id: string, onEvent: (event: SessionEvent) => void) => {
    api.onEvent = onEvent;
    return () => {};
  }),
}));

import * as sessionApi from "@/lib/session-api";
import { useChatSession } from "@/components/sites/claude-shell/useChatSession";

const item = (id: string, text: string, extra: Partial<QueuedMessage["message"]> = {}): QueuedMessage => ({
  id,
  created_at: "2026-01-01T00:00:00Z",
  message: { text, mode: "auto", ...extra },
});
const queueOf = (items: QueuedMessage[], paused = false): MessageQueue => ({ items, paused });
const event = (queue: MessageQueue, sent?: string, message_id?: string): Extract<SessionEvent, { type: "queue" }> => ({
  type: "queue",
  queue,
  ...(sent ? { sent, message_id } : {}),
});

describe("the chat's queue state", () => {
  const a = item("q-1", "First", {
    attachments: [{ name: "notes.txt", media_type: "text/plain", url: "/x" }],
    selection: { faces: ["lid.top_1"], edges: [] },
  });
  const b = item("q-2", "Second");

  it("follows the queue from its events", () => {
    const { queue, messages } = applyQueueEvent(EMPTY_QUEUE, [], event(queueOf([a, b])));
    expect(queue.items.map((q) => q.id)).toEqual(["q-1", "q-2"]);
    expect(messages).toEqual([]);
  });

  it("shows a sent message as the user's, with its files and selection, once", () => {
    const before = queueOf([a, b]);
    const sent = event(queueOf([b]), "q-1", "msg-3");
    const { queue, messages } = applyQueueEvent(before, [], sent);
    expect(queue.items).toEqual([b]);
    expect(messages).toEqual([
      { id: "msg-3", role: "user", text: "First", files: ["notes.txt"], selection: { faces: ["lid.top_1"], edges: [] } },
    ]);
    // A reopened session resends the event after loading the stored message: no second copy.
    expect(applyQueueEvent(before, messages, sent).messages).toHaveLength(1);
    expect(sentMessage(b, "msg-4")).toMatchObject({ id: "msg-4", text: "Second", selection: null });
  });

  it("moves, edits and removes queued messages", () => {
    const q = queueOf([a, b]);
    expect(moved(q, "q-2", -1)).toEqual({ items: [b, a], position: 0 });
    expect(moved(q, "q-1", -1)).toBeNull();
    expect(moved(q, "q-2", 1)).toBeNull();
    expect(edited(q, "q-2", "Changed").items[1].message.text).toBe("Changed");
    expect(edited(q, "q-1", null).items).toEqual([b]);
  });
});

describe("useChatSession with a queue", () => {
  beforeEach(() => {
    api.onEvent = null;
    vi.clearAllMocks();
  });

  async function working() {
    const hook = renderHook(() => useChatSession());
    await act(async () => {
      await hook.result.current.send("Take your time", [], { model: "m", effort: "low", mode: "auto" });
    });
    act(() => api.onEvent?.({ type: "message_start", message_id: "msg-2", role: "assistant" }));
    return hook;
  }

  it("queues a message sent while Claude works instead of posting it", async () => {
    const hook = await working();
    expect(hook.result.current.queues).toBe(true);
    await act(async () => {
      await hook.result.current.send("And then this", [], { model: "m", effort: "low", mode: "manual" });
    });
    expect(sessionApi.postMessage).toHaveBeenCalledTimes(1);
    expect(sessionApi.queueMessage).toHaveBeenCalledWith(
      "session-1",
      expect.objectContaining({ text: "And then this", mode: "manual", attachments: [] }),
    );
    // Not in the transcript until it is sent: the queue shows it.
    expect(hook.result.current.messages.filter((m) => m.role === "user").map((m) => m.text)).toEqual(["Take your time"]);

    const queued = item("queued-1", "And then this");
    act(() => api.onEvent?.(event(queueOf([queued]))));
    expect(hook.result.current.queue.items).toEqual([queued]);

    act(() => api.onEvent?.({ type: "message_end", message_id: "msg-2" }));
    act(() => api.onEvent?.(event(queueOf([]), "queued-1", "msg-3")));
    act(() => api.onEvent?.({ type: "message_start", message_id: "msg-4", role: "assistant" }));
    const order = hook.result.current.messages.map((m: ChatMessage) => `${m.role}:${m.id}`);
    expect(order.slice(-3)).toEqual(["assistant:msg-2", "user:msg-3", "assistant:msg-4"]);
    expect(hook.result.current.queue.items).toEqual([]);
  });

  it("edits, moves and removes at once, then asks the server", async () => {
    const hook = await working();
    act(() => api.onEvent?.(event(queueOf([item("q-1", "A"), item("q-2", "B")]))));
    await act(async () => hook.result.current.moveQueued("q-2", -1));
    expect(hook.result.current.queue.items.map((q) => q.id)).toEqual(["q-2", "q-1"]);
    expect(sessionApi.updateQueued).toHaveBeenCalledWith("session-1", "q-2", { position: 0 });
    await act(async () => hook.result.current.editQueued("q-1", "A2"));
    expect(hook.result.current.queue.items[1].message.text).toBe("A2");
    expect(sessionApi.updateQueued).toHaveBeenCalledWith("session-1", "q-1", { text: "A2" });
    await act(async () => hook.result.current.removeQueued("q-2"));
    expect(hook.result.current.queue.items.map((q) => q.id)).toEqual(["q-1"]);
    expect(sessionApi.removeQueued).toHaveBeenCalledWith("session-1", "q-2");
  });

  it("sends the next message anyway and stops the run", async () => {
    const hook = await working();
    act(() => api.onEvent?.(event(queueOf([item("q-1", "A"), item("q-2", "B")], true))));
    expect(hook.result.current.queue.paused).toBe(true);
    await act(async () => hook.result.current.sendAnyway());
    expect(sessionApi.sendQueued).toHaveBeenCalledWith("session-1", "q-1");
    await act(async () => hook.result.current.stop());
    expect(sessionApi.stopRun).toHaveBeenCalledWith("session-1");
  });
});

describe("QueuedMessages", () => {
  afterEach(cleanup);
  const handlers = () => ({ onEdit: vi.fn(), onMove: vi.fn(), onRemove: vi.fn(), onSendAnyway: vi.fn() });

  it("shows nothing for an empty queue", () => {
    const { container } = render(<QueuedMessages queue={EMPTY_QUEUE} {...handlers()} />);
    expect(container.innerHTML).toBe("");
  });

  it("lists the queue with its count and each message's controls", () => {
    const h = handlers();
    render(<QueuedMessages queue={queueOf([item("q-1", "A"), item("q-2", "B")])} {...h} />);
    expect(screen.getByRole("heading", { name: queueHeading(2) })).toBeTruthy();
    expect(screen.getByText("Queued · 2")).toBeTruthy();
    // The first can't go up and the last can't go down: those buttons aren't there.
    expect(screen.getAllByRole("button", { name: "Move queued message up" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Move queued message down" })).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Move queued message up" }));
    expect(h.onMove).toHaveBeenCalledWith("q-2", -1);
    fireEvent.click(screen.getAllByRole("button", { name: "Remove queued message" })[0]);
    expect(h.onRemove).toHaveBeenCalledWith("q-1");
    expect(screen.queryByRole("button", { name: "Send anyway" })).toBeNull();
  });

  it("edits a queued message in place", () => {
    const h = handlers();
    render(<QueuedMessages queue={queueOf([item("q-1", "A")])} {...h} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit queued message" }));
    const box = screen.getByRole("textbox", { name: "Edit queued message" });
    fireEvent.change(box, { target: { value: "" } });
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(box, { target: { value: "A, edited" } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(h.onEdit).toHaveBeenCalledWith("q-1", "A, edited");
    expect(screen.queryByRole("textbox", { name: "Edit queued message" })).toBeNull();
  });

  it("says a question paused it and offers Send anyway", () => {
    const h = handlers();
    render(<QueuedMessages queue={queueOf([item("q-1", "A")], true)} {...h} />);
    expect(screen.getByRole("status").textContent).toBe(PAUSED_REASON);
    fireEvent.click(screen.getByRole("button", { name: "Send anyway" }));
    expect(h.onSendAnyway).toHaveBeenCalled();
  });
});
