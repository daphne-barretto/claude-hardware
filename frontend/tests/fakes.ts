import type { SessionEvent } from "@/lib/session-api";

/** A stand-in EventSource the test pushes events into. */
export class FakeEventSource {
  static all: FakeEventSource[] = [];
  static get last(): FakeEventSource | null {
    return FakeEventSource.all.at(-1) ?? null;
  }
  static reset() {
    FakeEventSource.all = [];
  }
  listeners: Record<string, ((e: MessageEvent) => void)[]> = {};
  closed = false;
  constructor(readonly url: string) {
    FakeEventSource.all.push(this);
  }
  addEventListener(type: string, fn: (e: MessageEvent) => void) {
    (this.listeners[type] ??= []).push(fn);
  }
  close() {
    this.closed = true;
  }
  /** Sends the event to every open stream of this session, as the server would (chat and canvas each have one). */
  emit(event: SessionEvent) {
    for (const source of FakeEventSource.all) {
      if (source.closed || source.url !== this.url) continue;
      for (const fn of source.listeners[event.type] ?? []) fn(new MessageEvent(event.type, { data: JSON.stringify(event) }));
    }
  }
}
