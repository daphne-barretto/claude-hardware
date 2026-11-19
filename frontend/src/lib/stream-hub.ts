/**
 * One connection per session event stream, shared by everyone who follows it (the chat, the
 * canvas, comments, the change set): a browser allows only 6 connections per host over HTTP/1.1,
 * so a connection each, in every tab, stalls the page (docs/security.md). A hub runs in the
 * browser's shared worker (`./session-stream.worker.ts`), so every tab shares it, or in the tab
 * when there is no shared worker (`./session-stream.ts`).
 *
 * A stream keeps the events it has seen, so whoever follows it later gets them all from the start,
 * as a stream of its own would. A dropped connection is reconnected from the last event seen
 * (`Last-Event-ID`, or `after` when the browser gave up and a new stream is opened), so no event
 * is lost or repeated; while it can't reach the server it keeps trying, with backoff.
 */

/** The event stream's connection: live, dropped and coming back, or the server can't be reached. */
export type StreamStatus = "connected" | "reconnecting" | "offline";

/** What a follower of a stream hears: an event (its JSON as sent), the connection, or that the session is gone (a 404). */
export type StreamMessage =
  | { kind: "event"; type: string; data: string }
  | { kind: "status"; status: StreamStatus }
  | { kind: "gone" };

/** How long to wait before each manual reconnect (ms), the last repeated. */
export const RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000, 15000];

type Listener = (message: StreamMessage) => void;

class Stream {
  /** The events seen so far, for those who follow the stream later. */
  private seen: { type: string; data: string }[] = [];
  private lastId = 0;
  private source: EventSource | null = null;
  private failures = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private status: StreamStatus | null = null;
  private gone = false;
  private closed = false;
  readonly listeners = new Set<Listener>();

  constructor(
    private readonly path: string,
    private readonly types: readonly string[],
  ) {}

  add(listener: Listener) {
    this.listeners.add(listener);
    for (const e of this.seen) listener({ kind: "event", ...e });
    if (this.status) listener({ kind: "status", status: this.status });
    if (this.gone) listener({ kind: "gone" });
    else if (!this.source && !this.timer && !this.closed) this.open();
  }

  close() {
    this.closed = true;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.source?.close();
    this.source = null;
  }

  /** Tries at once, when the server can't be reached (the banner's Retry now). */
  retryNow() {
    if (this.closed || this.gone || this.source) return;
    if (this.timer) clearTimeout(this.timer);
    void this.check();
  }

  private send(message: StreamMessage) {
    for (const listener of [...this.listeners]) listener(message);
  }

  private setStatus(next: StreamStatus) {
    if (next === this.status) return;
    this.status = next;
    this.send({ kind: "status", status: next });
  }

  private open() {
    if (this.closed) return;
    // A new stream picks up after the last event seen; the browser's own reconnects send Last-Event-ID.
    const source = new EventSource(this.lastId ? `${this.path}/events?after=${this.lastId}` : `${this.path}/events`);
    this.source = source;
    source.onopen = () => {
      this.failures = 0;
      this.setStatus("connected");
    };
    for (const type of this.types) {
      source.addEventListener(type, (e) => {
        // EventSource fires its own `error`, without data, when the connection drops (and then
        // reconnects by itself). Only session events carry JSON data.
        const message = e as MessageEvent;
        if (typeof message.data !== "string") return;
        const id = Number(message.lastEventId);
        if (Number.isFinite(id) && id > 0) {
          if (id <= this.lastId) return; // already seen
          this.lastId = id;
        }
        this.setStatus("connected");
        const event = { type, data: message.data };
        this.seen.push(event);
        this.send({ kind: "event", ...event });
      });
    }
    source.onerror = () => {
      if (this.closed || this.source !== source) return;
      if (source.readyState === EventSource.CONNECTING) {
        this.setStatus("reconnecting");
        return;
      }
      // The browser gave up (the server answered with an error, or isn't there): find out why.
      source.close();
      this.source = null;
      void this.check();
    };
  }

  private retryLater() {
    const delay = RECONNECT_DELAYS_MS[Math.min(this.failures, RECONNECT_DELAYS_MS.length - 1)];
    this.failures += 1;
    this.timer = setTimeout(() => void this.check(), delay);
  }

  private async check() {
    if (this.closed) return;
    this.timer = null;
    let res: Response | null = null;
    try {
      res = await fetch(this.path);
    } catch {
      res = null;
    }
    if (this.closed) return;
    if (res?.status === 404) {
      let detail = "";
      try {
        detail = (await res.json())?.detail ?? "";
      } catch {
        // Not JSON: not the API's "no session".
      }
      if (/^no session\b/.test(detail)) {
        this.gone = true;
        this.send({ kind: "gone" });
        return;
      }
    }
    if (res?.ok) {
      this.setStatus("reconnecting");
      this.open();
      return;
    }
    this.setStatus("offline");
    this.retryLater();
  }
}

/** The streams followed here, by session path (`/api/sessions/<id>`); a stream closes when its last follower leaves. */
export class StreamHub {
  private streams = new Map<string, Stream>();

  constructor(private readonly types: readonly string[]) {}

  /** Follows the stream of the session at `path`; returns the function that stops following it. */
  follow(path: string, listener: Listener): () => void {
    let stream = this.streams.get(path);
    if (!stream) this.streams.set(path, (stream = new Stream(path, this.types)));
    stream.add(listener);
    let followed = true;
    return () => {
      if (!followed) return;
      followed = false;
      stream.listeners.delete(listener);
      if (stream.listeners.size === 0 && this.streams.get(path) === stream) {
        stream.close();
        this.streams.delete(path);
      }
    };
  }

  retryNow() {
    for (const stream of this.streams.values()) stream.retryNow();
  }

  /** How many connections are open or opening (for tests). */
  get size() {
    return this.streams.size;
  }
}
