/**
 * Plain messages for the server's abuse limits (docs/security.md): rate limits, the daily
 * budget, size and upload-time limits. Their text comes from the server and is already written
 * for people, so the chat shows it as it is, without "Could not reach Claude".
 * The chat shows them through the run notices' UI (`limitNotice`, `RunNotice.tsx`), as a warning.
 */
import { ApiError, type LimitReason, type NoticeEvent } from "./session-api";

/** HTTP statuses whose `detail` is a limit message meant for people. */
export const LIMIT_STATUSES = new Set([403, 408, 413, 415, 429, 503]);

/** The server's message for a request a limit refused, or null for any other failure. */
export function limitMessage(e: unknown): string | null {
  return e instanceof ApiError && LIMIT_STATUSES.has(e.status) ? e.message : null;
}

/** Why a limit refused a request, as the notices' reason code. */
function limitReason(e: ApiError): LimitReason {
  if (e.status === 429) return "rate_limited";
  if (e.status === 503) return /budget|spend|today/i.test(e.message) ? "billing" : "overloaded";
  if (e.status === 413) return "too_large";
  return "unknown";
}

/** A limit's message as a warning notice for the chat, or null for any other failure. */
export function limitNotice(e: unknown): Omit<NoticeEvent, "type" | "message_id"> | null {
  const text = limitMessage(e);
  return text && e instanceof ApiError ? { kind: "warning", text, reason: limitReason(e) } : null;
}

/** Shown when the server took a secret or an email address out of the user's message. */
export const REMOVED_NOTE =
  "We removed what looked like a key, token or email address from your message before saving it or sending it to Claude, to keep it private.";

/** Whether the server's stored text differs from what was sent because something was removed. */
export function wasScrubbed(sent: string, stored: unknown): stored is { text: string } {
  const text = (stored as { text?: unknown } | null)?.text;
  return typeof text === "string" && text !== sent && text.includes("[removed:");
}
