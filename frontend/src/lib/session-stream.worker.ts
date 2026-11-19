/// <reference lib="webworker" />
/**
 * The browser's one set of session event streams, shared by all its tabs (`./session-stream`):
 * each session's stream is opened once however many tabs follow it (`./stream-hub`).
 *
 * A tab says `follow` (with its own subscription ID and the session path) and `unfollow`; it gets
 * `{ id, message }` back for each subscription. A tab that goes away without saying so (closed,
 * crashed) releases the lock it named in `hello`, and its subscriptions end then.
 */
import { EVENT_TYPES } from "./event-types";
import { StreamHub, type StreamMessage } from "./stream-hub";

export type ToWorker =
  | { op: "hello"; lock?: string }
  | { op: "follow"; id: number; path: string }
  | { op: "unfollow"; id: number }
  | { op: "retry" }
  | { op: "bye" };
export type FromWorker = { id: number; message: StreamMessage };

declare const self: SharedWorkerGlobalScope;

const hub = new StreamHub(EVENT_TYPES);

self.onconnect = (e: MessageEvent) => {
  const port = e.ports[0];
  const followed = new Map<number, () => void>();
  const end = () => {
    for (const stop of followed.values()) stop();
    followed.clear();
  };
  port.onmessage = (m: MessageEvent<ToWorker>) => {
    const msg = m.data;
    if (msg.op === "hello") {
      // Granted once the tab holding it is gone.
      if (msg.lock && "locks" in navigator) void navigator.locks.request(msg.lock, async () => end());
    } else if (msg.op === "follow") {
      followed.get(msg.id)?.();
      followed.set(
        msg.id,
        hub.follow(msg.path, (message) => port.postMessage({ id: msg.id, message } satisfies FromWorker)),
      );
    } else if (msg.op === "unfollow") {
      followed.get(msg.id)?.();
      followed.delete(msg.id);
    } else if (msg.op === "retry") hub.retryNow();
    else if (msg.op === "bye") end();
  };
  port.start();
};
