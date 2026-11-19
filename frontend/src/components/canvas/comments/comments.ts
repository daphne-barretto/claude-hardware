import type { Comment, CommentReply, SessionEvent } from "@/lib/session-api";
import { sessionRoute } from "@/lib/share";
import type { Change, ChangeSet, Point3 } from "../changes";
// `fetch` here says a dropped connection plainly, and `failure` a failed response (QA fix brief 8).
import { failure, reach as fetch } from "@/lib/errors";

/**
 * Comments pinned to geometry: pinned to a face, threaded, and sent to Claude when the
 * user wants Claude to act. The session API places each pin on the shown build (the face is
 * found again by its stable ID after a rebuild), streams every change to a comment as a
 * `comment` event, and links the changes a run makes for a comment to it.
 */

export type { Comment, CommentReply };

/** Mentioning Claude in a comment or reply sends the thread to Claude, like "Send to Claude". */
export function mentionsClaude(text: string): boolean {
  return /(^|[^\w@])@claude\b/i.test(text);
}

/** A part name as a title: `lid` becomes "Lid". */
export function partLabel(part: string | null | undefined): string {
  if (!part) return "the model";
  const words = part.replace(/[_-]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** A feature tag in words: `opening_Q4` is "opening for Q4", `boss_2` is "boss 2". */
export function featureLabel(tag: string): string {
  const m = /^([a-z]+)_(.+)$/.exec(tag);
  if (!m) return tag.replace(/_/g, " ");
  return /^\d+$/.test(m[2]) ? `${m[1]} ${m[2]}` : `${m[1]} for ${m[2]}`;
}

/** Where a comment is pinned, as the thread's header says it: "Pinned to Lid · opening for Q4". */
export function pinnedTo(comment: Pick<Comment, "part" | "feature" | "location">): string {
  const part = comment.location?.part ?? comment.part;
  const head = `Pinned to ${partLabel(part)}`;
  const feature = comment.feature && comment.feature !== part ? featureLabel(comment.feature) : null;
  return feature ? `${head} · ${feature}` : head;
}

/** The pins to draw: open comments that have a place on the shown build. */
export function visiblePins(comments: Comment[]): (Comment & { location: NonNullable<Comment["location"]> })[] {
  return comments.filter((c): c is Comment & { location: NonNullable<Comment["location"]> } => !c.resolved && !!c.location);
}

/** The changes in the change set made for a comment ("answering this comment"). */
export function changesFor(changeSet: ChangeSet | null, commentId: string): Change[] {
  return changeSet?.changes.filter((c) => c.comment === commentId) ?? [];
}

/** Undo for a comment: rejects its applied changes, which rebuilds without them. Null when none is applied. */
export function undoReview(changes: Change[]): { accept: string[]; reject: string[] } | null {
  const applied = changes.filter((c) => c.status === "accepted").map((c) => c.id);
  return applied.length ? { accept: [], reject: applied } : null;
}

export type ThreadState = "note" | "working" | "waiting" | "answered" | "failed" | "resolved";

/** Where a thread stands, for its badge in the comments list. `waiting`: the run answering it is
 * paused on Claude's question to the user, so nothing is being worked on until they answer. */
export function threadState(comment: Comment, waiting = false): ThreadState {
  if (comment.resolved) return "resolved";
  if (!comment.request) return "note";
  if (comment.request.status === "running") return waiting ? "waiting" : "working";
  return comment.request.status;
}

/** Whether a reason sends are blocked means Claude's question is open (`useChatSession`). */
export const isAnswerFirst = (why: string | undefined) => !!why?.startsWith("Answer Claude's question");

export const THREAD_LABEL: Record<ThreadState, string> = {
  note: "Note",
  working: "Claude is working",
  waiting: "Waiting for your answer",
  answered: "Claude replied",
  failed: "Claude couldn't finish",
  resolved: "Resolved",
};

/** "now", "2m", "3h", "4d": how long ago, as the thread shows it. */
export function timeAgo(iso: string, now = Date.now()): string {
  const s = Math.max(0, (now - Date.parse(iso)) / 1000);
  if (s < 45) return "now";
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

/** The point to pin a new comment at, from what a click hit. */
export type Draft = { face: string; part: string; point: Point3 };

/** Adds or replaces a comment in a list, keeping the order they were made in. */
export function withComment(comments: Comment[], comment: Comment): Comment[] {
  const i = comments.findIndex((c) => c.id === comment.id);
  if (i < 0) return [...comments, comment];
  return comments.map((c, j) => (j === i ? comment : c));
}

/** A question Claude asked in a comment run, still waiting for its answer. */
export type ThreadQuestion = {
  messageId: string;
  questionId: string;
  text: string;
  options: { id: string; label: string; description?: string | null }[];
};

/**
 * Tracks the questions of runs, by message ID, from the event stream: a question stays open
 * until more of its run arrives (the answer resumed it) or the run ends.
 */
export function applyQuestion(open: Record<string, ThreadQuestion>, event: SessionEvent): Record<string, ThreadQuestion> {
  const mid = "message_id" in event ? event.message_id : null;
  if (!mid) return open;
  if (event.type === "question") {
    return { ...open, [mid]: { messageId: mid, questionId: event.question_id, text: event.text, options: event.options } };
  }
  if (!(mid in open) || event.type === "comment" || event.type === "build_ready") return open;
  const rest = { ...open };
  delete rest[mid];
  return rest;
}

// --- Session API --------------------------------------------------------------------------------

const base = (sessionId: string) => `/api${sessionRoute(sessionId)}/comments`;

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) throw await failure(res);
  return (await res.json()) as T;
}

/** Every comment, resolved ones included, with its pin located on `buildId` (default: the latest). */
export function listComments(sessionId: string, buildId?: string): Promise<Comment[]> {
  const q = buildId ? `?${new URLSearchParams({ build_id: buildId })}` : "";
  return fetch(`${base(sessionId)}${q}`).then((r) => json<Comment[]>(r));
}

export function createComment(sessionId: string, body: { build_id: string; face_id: string; point: Point3; text: string }): Promise<Comment> {
  return fetch(base(sessionId), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then((r) => json<Comment>(r));
}

export function replyToComment(sessionId: string, commentId: string, text: string): Promise<Comment> {
  return fetch(`${base(sessionId)}/${encodeURIComponent(commentId)}/replies`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  }).then((r) => json<Comment>(r));
}

export function resolveComment(sessionId: string, commentId: string, resolved: boolean): Promise<Comment> {
  return fetch(`${base(sessionId)}/${encodeURIComponent(commentId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ resolved }),
  }).then((r) => json<Comment>(r));
}
