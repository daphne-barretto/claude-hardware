/**
 * Errors as people read them (QA fix brief 8): the server's own words when they are plain,
 * else a plain sentence. Never an HTTP status, a raw error body, an internal ID or a stack.
 * The chat says the same things (`errorText` in useChatSession); the abuse limits' messages
 * are already plain (`@/lib/limits`).
 */

export const OFFLINE = "Couldn't reach the server. Check your connection and try again.";
export const SERVER_TROUBLE = "The server had a problem with that. Try again in a moment.";
export const NOT_FOUND = "That isn't there any more. Reload the page to see the latest.";
export const REFUSED = "That didn't go through. Try again.";

/** Text that is internal, not for people: IDs, statuses, raw bodies, exception names, stacks. */
const INTERNAL =
  /\b(?:build|edit|comment|export|session|msg)-[0-9a-f]{6,}\b|\b(?:HTTP|status) \d{3}\b|^\d{3}\b|\b[45]\d\d (?:Not Found|Bad Request|Internal Server Error|Conflict|Unprocessable|Service Unavailable|Forbidden)|Internal Server Error|Traceback|\b\w*(?:Exception|Error):|\{"|\[object Object\]|\bundefined\b|Failed to fetch|NetworkError|Load failed/i;

/** A plain sentence for a failed response with no plain `detail`. */
export function statusSentence(status: number): string {
  if (status === 0) return OFFLINE;
  if (status === 404) return NOT_FOUND;
  if (status >= 500) return SERVER_TROUBLE;
  return REFUSED;
}

/** Whether a message is plain enough to show as it is. */
export const isPlain = (text: string) => !!text.trim() && !INTERNAL.test(text);

/** The message a failed response carries for people: its plain `detail`, else a sentence. */
export function responseMessage(status: number, detail: unknown): string {
  return typeof detail === "string" && isPlain(detail) ? detail : statusSentence(status);
}

/** What to show for an error: its message when plain, else `fallback` (or a plain sentence). */
export function shownError(e: unknown, fallback?: string): string {
  const text = e instanceof Error ? e.message : typeof e === "string" ? e : "";
  if (isPlain(text)) return text.trim();
  // fetch's own words for a network failure, per browser.
  if (/Failed to fetch|NetworkError|Load failed/i.test(text)) return OFFLINE;
  const status = /^(?:HTTP )?(\d{3})\b/.exec(text.trim())?.[1];
  return fallback ?? (status ? statusSentence(Number(status)) : REFUSED);
}

/** A failed response, with its status kept for the code and a plain message for people. */
export class ResponseError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

/** The error for a failed response: the server's plain `detail` (or validation messages), else a sentence. */
export async function failure(res: Response): Promise<ResponseError> {
  const body = (await res.json().catch(() => null)) as { detail?: unknown } | null;
  let detail = body?.detail;
  if (Array.isArray(detail))
    detail = detail
      .map((d: { msg?: unknown }) => d?.msg)
      .filter((m): m is string => typeof m === "string")
      .join("; ");
  return new ResponseError(res.status, responseMessage(res.status, detail));
}

/** `fetch`, with a dropped connection said plainly instead of "Failed to fetch". Aborts stay aborts. */
export const reach: typeof fetch = (input, init) =>
  globalThis.fetch(input, init).catch((e: unknown) => {
    if ((e as { name?: string } | null)?.name === "AbortError") throw e;
    throw new Error(OFFLINE);
  });
