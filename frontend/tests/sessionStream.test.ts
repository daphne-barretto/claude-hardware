import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { adoptStored, applyEvent, fromStored, type ChatMessage } from "@/components/sites/claude-shell/chat";
import { subscribe, type Message, type SessionEvent } from "@/lib/session-api";
import { resetStreams } from "@/lib/session-stream";
import { StreamHub } from "@/lib/stream-hub";

// One event stream per session for everyone following it (the chat, the canvas, comments, the
// change set), in every tab: a connection each stalls the page over HTTP/1.1 (docs/security.md).

class FakeEventSource {
  static all: FakeEventSource[] = [];
  static CONNECTING = 0;
  readyState = 1;
  closed = false;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  listeners: Record<string, ((e: MessageEvent) => void)[]> = {};
  constructor(readonly url: string) {
    FakeEventSource.all.push(this);
  }
  addEventListener(type: string, fn: (e: MessageEvent) => void) {
    (this.listeners[type] ??= []).push(fn);
  }
  close() {
    this.closed = true;
  }
  emit(id: number, event: SessionEvent) {
    for (const fn of this.listeners[event.type] ?? [])
      fn(new MessageEvent(event.type, { data: JSON.stringify(event), lastEventId: String(id) }));
  }
}
const open = () => FakeEventSource.all.filter((s) => !s.closed);

beforeEach(() => {
  FakeEventSource.all = [];
  resetStreams();
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const start: SessionEvent = { type: "message_start", message_id: "m-2", role: "assistant" };
const text: SessionEvent = { type: "text", message_id: "m-2", delta: "Hello" };

describe("a session's event stream", () => {
  it("is one connection however many follow it, and closes when the last one stops", () => {
    const chat = vi.fn();
    const canvas = vi.fn();
    const stopChat = subscribe("s-1", chat);
    const stopCanvas = subscribe("s-1", canvas);
    expect(open()).toHaveLength(1);
    open()[0].emit(1, start);
    expect(chat).toHaveBeenCalledWith(start);
    expect(canvas).toHaveBeenCalledWith(start);
    stopChat();
    expect(open()).toHaveLength(1);
    stopCanvas();
    expect(open()).toHaveLength(0);
  });

  it("gives a later follower every event from the start, once", () => {
    subscribe("s-1", () => {});
    const source = open()[0];
    source.emit(1, start);
    source.emit(2, text);
    source.emit(2, text); // a reconnect repeats it
    const later = vi.fn();
    subscribe("s-1", later);
    expect(later.mock.calls.map(([e]) => e)).toEqual([start, text]);
    expect(open()).toHaveLength(1);
  });

  it("keeps sessions apart", () => {
    const one = vi.fn();
    subscribe("s-1", one);
    subscribe("s-2", () => {});
    expect(open().map((s) => s.url)).toEqual(["/api/sessions/s-1/events", "/api/sessions/s-2/events"]);
    open()[1].emit(1, start);
    expect(one).not.toHaveBeenCalled();
  });

  it("tells every follower that the session is gone, and a later one too", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "no session s-1" }), { status: 404 })));
    const hub = new StreamHub(["text"]);
    const first = vi.fn();
    hub.follow("/api/sessions/s-1", first);
    const source = open()[0];
    source.readyState = 2;
    source.onerror?.();
    await vi.waitFor(() => expect(first).toHaveBeenCalledWith({ kind: "gone" }));
    const later = vi.fn();
    hub.follow("/api/sessions/s-1", later);
    expect(later).toHaveBeenCalledWith({ kind: "gone" });
  });
});

const stored = (m: Partial<Message> & { id: string }): Message => ({ role: "user", text: "", created_at: "2026-01-01T00:00:00Z", ...m });

describe("the user's messages on the stream", () => {
  it("shows a message sent from another tab", () => {
    const message = stored({ id: "m-1", text: "make a box" });
    const after = applyEvent([], { type: "user_message", message_id: "m-1", message });
    expect(after).toEqual(fromStored([message]));
  });

  it("doesn't repeat a message the chat already shows (loaded, or resent)", () => {
    const message = stored({ id: "m-1", text: "make a box" });
    const loaded = fromStored([message]);
    expect(applyEvent(loaded, { type: "user_message", message_id: "m-1", message })).toBe(loaded);
  });

  it("gives this tab's own message, shown when sent, its stored ID", () => {
    const shown: ChatMessage[] = [{ id: "local-0", role: "user", text: "make a box" }];
    const message = stored({ id: "m-1", text: "make a box" });
    expect(applyEvent(shown, { type: "user_message", message_id: "m-1", message })).toEqual([{ ...shown[0], id: "m-1" }]);
    // An answer is matched by its option, as its stored text is empty.
    const answer = { question_id: "q-1", option_id: "a" };
    const answered: ChatMessage[] = [{ id: "local-1", role: "user", text: "Option A", answer }];
    const event: SessionEvent = { type: "user_message", message_id: "m-3", message: stored({ id: "m-3", answer }) };
    expect(applyEvent(answered, event).map((m) => m.id)).toEqual(["m-3"]);
  });

  it("keeps one copy when the stored words differ (a secret taken out) and the event came first", () => {
    const shown: ChatMessage[] = [{ id: "local-0", role: "user", text: "key sk-123" }];
    const message = stored({ id: "m-1", text: "key [removed: key]" });
    const both = applyEvent(shown, { type: "user_message", message_id: "m-1", message });
    expect(both).toHaveLength(2);
    expect(adoptStored(both, "local-0", "m-1").map((m) => [m.id, m.text])).toEqual([["m-1", "key [removed: key]"]]);
    expect(adoptStored(shown, "local-0", "m-1").map((m) => m.id)).toEqual(["m-1"]);
  });
});
