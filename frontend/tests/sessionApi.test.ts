import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { lockedMode, subscribe } from "@/lib/session-api";
import { resetStreams } from "@/lib/session-stream";

/** A stand-in EventSource; it calls its listeners directly, so their exceptions reach the test. */
class FakeEventSource {
  static last: FakeEventSource | null = null;
  listeners: Record<string, ((e: Event) => void)[]> = {};
  closed = false;
  constructor(readonly url: string) {
    FakeEventSource.last = this;
  }
  addEventListener(type: string, fn: (e: Event) => void) {
    (this.listeners[type] ??= []).push(fn);
  }
  close() {
    this.closed = true;
  }
  dispatchEvent(event: Event) {
    for (const fn of this.listeners[event.type] ?? []) fn(event);
  }
}

beforeEach(() => {
  // Each test opens its own connection: the session's shared one from an earlier test is forgotten.
  resetStreams();
  FakeEventSource.last = null;
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("subscribe", () => {
  it("ignores a connection error (an error event without data)", () => {
    const onEvent = vi.fn();
    subscribe("s-1", onEvent);
    const source = FakeEventSource.last!;
    expect(source.url).toBe("/api/sessions/s-1/events");
    expect(() => source.dispatchEvent(new Event("error"))).not.toThrow();
    expect(onEvent).not.toHaveBeenCalled();
  });

  it("passes a session error event with JSON data to onEvent", () => {
    const onEvent = vi.fn();
    subscribe("s-1", onEvent);
    const payload = { type: "error", message: "Build failed" };
    FakeEventSource.last!.dispatchEvent(new MessageEvent("error", { data: JSON.stringify(payload) }));
    expect(onEvent).toHaveBeenCalledWith(payload);
  });

  it("closes the stream when unsubscribed", () => {
    const close = subscribe("s-1", vi.fn());
    close();
    expect(FakeEventSource.last!.closed).toBe(true);
  });
});

describe("lockedMode", () => {
  it("sends every message in Auto mode, whatever was chosen before", () => {
    expect(lockedMode({ text: "Hi", mode: "manual" })).toMatchObject({ mode: "auto" });
    expect(lockedMode({ text: "Hi", mode: "auto" })).toMatchObject({ mode: "auto" });
  });
});
