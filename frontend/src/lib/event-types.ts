import type { SessionEvent } from "./session-api";

/** Every event type a session's stream sends (backend `EVENT_TYPES`). */
export const EVENT_TYPES: SessionEvent["type"][] = [
  "message_start",
  "text",
  "progress",
  "question",
  "build_ready",
  "changes_ready",
  "constraints",
  "plan",
  "risks_ready",
  "found",
  "message_end",
  "error",
  "comment",
  "queue",
  "notice",
  "title",
  "user_message",
];
