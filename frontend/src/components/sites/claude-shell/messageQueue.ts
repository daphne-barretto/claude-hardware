// The chat's message queue (backend/app/message_queue.py): messages sent while Claude works wait
// in the session's queue and are sent one at a time, in order, when each run ends. The queue
// lives on the server; every tab follows it from `queue` events.
import type { MessageQueue, QueuedMessage, SessionEvent } from "@/lib/session-api";
import type { ChatMessage } from "./chat";

export const EMPTY_QUEUE: MessageQueue = { items: [], paused: false };

/** "Queued · 2": the heading over the queued messages. */
export const queueHeading = (n: number) => `Queued · ${n}`;

/** Why the queue waits while Claude's question is open. */
export const PAUSED_REASON = "Paused until you answer Claude's question";

/** What "Send anyway" does, as its title says. */
export const SEND_ANYWAY_NOTE = "Send the next message now and leave Claude's question unanswered";

/** The Send button's title while Claude works: sending queues the message. */
export const QUEUE_NOTE = "Claude is working: this message waits in the queue and is sent when the reply ends";

/** The user message a queued message becomes once it is sent (`message_id`: its stored ID). */
export function sentMessage(item: QueuedMessage, messageId: string): ChatMessage {
  const files = item.message.attachments?.map((a) => a.name) ?? [];
  return {
    id: messageId,
    role: "user",
    text: item.message.text,
    ...(files.length ? { files } : {}),
    selection: item.message.selection ?? null,
  };
}

/**
 * Applies a `queue` event: the new queue, and the chat with the message it sent (if any) as the
 * user's message, once (a resent stream repeats the events of stored messages).
 */
export function applyQueueEvent(
  queue: MessageQueue,
  messages: ChatMessage[],
  event: Extract<SessionEvent, { type: "queue" }>,
): { queue: MessageQueue; messages: ChatMessage[] } {
  if (!event.sent || !event.message_id || messages.some((m) => m.id === event.message_id)) {
    return { queue: event.queue, messages };
  }
  const item = queue.items.find((q) => q.id === event.sent);
  if (!item) return { queue: event.queue, messages };
  return { queue: event.queue, messages: [...messages, sentMessage(item, event.message_id)] };
}

/** The queue after a queued message this tab just added (the event may come before or after). */
export function withQueued(queue: MessageQueue, item: QueuedMessage): MessageQueue {
  return queue.items.some((q) => q.id === item.id) ? queue : { ...queue, items: [...queue.items, item] };
}

/** The queue with `id` moved by `by` places (-1: up), or null when it can't move that way. */
export function moved(queue: MessageQueue, id: string, by: -1 | 1): { items: QueuedMessage[]; position: number } | null {
  const from = queue.items.findIndex((q) => q.id === id);
  const to = from + by;
  if (from < 0 || to < 0 || to >= queue.items.length) return null;
  const items = [...queue.items];
  const [item] = items.splice(from, 1);
  items.splice(to, 0, item);
  return { items, position: to };
}

/** The queue with one item's text changed, or removed (`text` null), as the server will have it. */
export function edited(queue: MessageQueue, id: string, text: string | null): MessageQueue {
  const items =
    text === null
      ? queue.items.filter((q) => q.id !== id)
      : queue.items.map((q) => (q.id === id ? { ...q, message: { ...q.message, text } } : q));
  return { ...queue, items };
}
