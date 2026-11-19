/**
 * Following session event streams with one connection per session for the whole browser: the
 * streams live in a shared worker (`./session-stream.worker.ts`) that every tab talks to, so ten
 * tabs on a session hold one connection, not forty. Where there is no shared worker (Chrome on
 * Android, tests) the tab shares one connection per session among its own followers.
 */
import { EVENT_TYPES } from "./event-types";
import { StreamHub, type StreamMessage } from "./stream-hub";
import type { FromWorker, ToWorker } from "./session-stream.worker";

type Listener = (message: StreamMessage) => void;

interface Follower {
  follow(path: string, listener: Listener): () => void;
  retryNow(): void;
}

/**
 * The shared worker's side, from this tab. Until the worker has started, what this tab says waits;
 * if it can't start, or fails before it is heard from, this tab connects by itself.
 */
class WorkerFollower implements Follower {
  private port: MessagePort | null = null;
  private waiting: ToWorker[] = [];
  private next = 1;
  private listeners = new Map<number, { path: string; listener: Listener; stop?: () => void }>();
  private heard = false;
  private fallback: Follower | null = null;

  constructor(worker: Promise<SharedWorker>) {
    worker.then(
      (w) => this.started(w),
      () => this.fallBack(),
    );
  }

  /** This tab connects by itself (the worker didn't start: blocked, or a script error). */
  private fallBack() {
    if (this.heard || this.fallback) return;
    this.fallback = new StreamHub(EVENT_TYPES);
    this.waiting = [];
    for (const entry of this.listeners.values()) entry.stop = this.fallback.follow(entry.path, entry.listener);
  }

  private started(worker: SharedWorker) {
    if (this.fallback) return;
    const port = worker.port;
    port.onmessage = (e: MessageEvent<FromWorker>) => {
      this.heard = true;
      this.listeners.get(e.data.id)?.listener(e.data.message);
    };
    worker.onerror = () => this.fallBack();
    port.start();
    this.port = port;
    // A lock this tab holds while it lives: the worker ends this tab's streams when it is released.
    const lock = `session-stream:${crypto.randomUUID?.() ?? Math.random().toString(36).slice(2)}`;
    if (typeof navigator !== "undefined" && "locks" in navigator) {
      void navigator.locks.request(lock, () => new Promise<void>(() => {})).catch(() => {});
      this.post({ op: "hello", lock });
    } else this.post({ op: "hello" });
    // Closing the tab (not a page kept for Back) ends its streams at once.
    window.addEventListener("pagehide", (e) => {
      if (!e.persisted) this.post({ op: "bye" });
    });
    for (const msg of this.waiting.splice(0)) port.postMessage(msg);
  }

  private post(msg: ToWorker) {
    if (this.fallback) return;
    if (this.port) this.port.postMessage(msg);
    else this.waiting.push(msg);
  }

  follow(path: string, listener: Listener) {
    const id = this.next++;
    const entry: { path: string; listener: Listener; stop?: () => void } = { path, listener };
    this.listeners.set(id, entry);
    if (this.fallback) entry.stop = this.fallback.follow(path, listener);
    else this.post({ op: "follow", id, path });
    return () => {
      if (!this.listeners.delete(id)) return;
      if (entry.stop) entry.stop();
      else this.post({ op: "unfollow", id });
    };
  }

  retryNow() {
    if (this.fallback) this.fallback.retryNow();
    else this.post({ op: "retry" });
  }
}

let follower: Follower | null = null;
/** Set by tests: connect in the tab even where a shared worker exists. */
let sharedWorkers = true;

function current(): Follower {
  if (follower) return follower;
  if (sharedWorkers && typeof window !== "undefined" && typeof SharedWorker !== "undefined") {
    return (follower = new WorkerFollower(import("./session-stream-worker").then((m) => m.startWorker())));
  }
  return (follower = new StreamHub(EVENT_TYPES));
}

/** Follows the event stream of the session at `path` (`/api/sessions/<id>`); returns the function that stops. */
export function followStream(path: string, listener: Listener): () => void {
  return current().follow(path, listener);
}

/** Tries every stream that can't reach the server again now. */
export function retryStreamsNow(): void {
  follower?.retryNow();
}

/** For tests: forget the streams' connections (and, with `false`, don't use a shared worker). */
export function resetStreams(useSharedWorker = true): void {
  follower = null;
  sharedWorkers = useSharedWorker;
}
