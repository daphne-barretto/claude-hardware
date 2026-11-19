import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import type { ChatMessage } from "@/components/sites/claude-shell/chat";
import { ChatMessages } from "@/components/sites/claude-shell/ChatMessages";
import { subscribe } from "@/lib/session-api";
import { resetStreams } from "@/lib/session-stream";

// Each test opens its own connection: the session's shared one from an earlier test is forgotten.
beforeEach(() => resetStreams());

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const sent: ChatMessage[] = [{ id: "u-1", role: "user", text: "make a box" }];

describe("while Claude has not started replying", () => {
  it("shows that Claude is working right after a message is sent", () => {
    render(<ChatMessages messages={sent} busy waiting onAnswer={() => {}} />);
    const status = screen.getByRole("status");
    expect(status.textContent).toMatch(/Thinking/);
  });

  it("shows nothing extra once nothing is pending", () => {
    render(<ChatMessages messages={sent} busy={false} onAnswer={() => {}} />);
    expect(screen.queryByRole("status")).toBeNull();
  });
});

class FakeEventSource {
  static last: FakeEventSource;
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 2;
  readyState = 1;
  onerror: ((e: Event) => void) | null = null;
  listeners: Record<string, ((e: Event) => void)[]> = {};
  constructor(public url: string) {
    FakeEventSource.last = this;
  }
  addEventListener(type: string, fn: (e: Event) => void) {
    (this.listeners[type] ??= []).push(fn);
  }
  close() {
    this.readyState = 2;
  }
}

describe("the session event stream", () => {
  it("reports a stream the server closed because the session is gone (a 404 no session)", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "no session session-1" }), { status: 404 })));
    const onError = vi.fn();
    subscribe("session-1", () => {}, onError);
    FakeEventSource.last.readyState = FakeEventSource.CLOSED;
    FakeEventSource.last.onerror?.(new Event("error"));
    await vi.waitFor(() => expect(onError).toHaveBeenCalledTimes(1));
  });

  it("does not report a dropped connection the browser is retrying", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const onError = vi.fn();
    const onStatus = vi.fn();
    subscribe("session-1", () => {}, onError, onStatus);
    FakeEventSource.last.readyState = FakeEventSource.CONNECTING;
    FakeEventSource.last.onerror?.(new Event("error"));
    expect(onError).not.toHaveBeenCalled();
    expect(onStatus).toHaveBeenLastCalledWith("reconnecting");
  });

  it("ignores the browser's own connection error event, which has no data", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const onEvent = vi.fn();
    subscribe("session-1", onEvent);
    expect(() => FakeEventSource.last.listeners.error.forEach((fn) => fn(new Event("error")))).not.toThrow();
    expect(onEvent).not.toHaveBeenCalled();
  });
});
