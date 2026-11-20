"use client";

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Check, ListChecks, MessageSquare, MessageSquarePlus, Send, Undo2, X } from "lucide-react";
import type { Build } from "../api";
import { reviewChanges } from "../api";
import type { Change, ChangeSet, Point3 } from "../changes";
import type { ScreenPoint } from "../checks";
import {
  changesFor,
  createComment,
  mentionsClaude,
  pinnedTo,
  replyToComment,
  resolveComment,
  THREAD_LABEL,
  threadState,
  timeAgo,
  undoReview,
  visiblePins,
  type Comment,
  type ThreadQuestion } from "./comments";
import { OPEN_THREAD, type Conversation } from "@/lib/session-api";

/** A conversation's open question, as the thread shows it. */
const threadQuestion = (c: Conversation | undefined): ThreadQuestion | undefined =>
  c?.question
    ? { messageId: c.question.message_id, questionId: c.question.question_id, text: c.question.text, options: c.question.options }
    : undefined;
import { useComments, type CommentTool } from "./useComments";
import { shownError } from "@/lib/errors";
import { useAnchoredCard } from "../fitPanel";

const CARD_WIDTH = 312;
/** How far above its point a comment pin sits: its tail touches the face. */
const PIN_LIFT = 14;

const card = "absolute z-30 rounded-xl border border-[var(--cs-border)] bg-[#262626] p-3 text-[13px] shadow-xl";
const btn =
  "flex h-7 items-center gap-1.5 rounded-md px-2.5 text-[13px] enabled:hover:bg-[var(--cs-hover)] disabled:cursor-not-allowed disabled:opacity-50";
const primary =
  "flex h-7 items-center gap-1.5 rounded-md bg-[var(--cs-text)] px-2.5 text-[13px] text-[#1f1f1f] enabled:hover:bg-white disabled:cursor-not-allowed disabled:opacity-50";

type Props = {
  sessionId: string;
  /** The shown build: new comments are pinned on it and every pin is placed on it. */
  build: Build | undefined;
  /** The model is loaded, so pins can be placed. */
  ready: boolean;
  tool: CommentTool;
  /** Projects a board point to viewport pixels; changes identity whenever the camera moves. */
  toScreen: (point: Point3) => ScreenPoint | null;
  width: number;
  height: number;
  changeSet: ChangeSet | null;
  onReviewed: (set: ChangeSet) => void;
  /** Sends comments to Claude: one run answers each in its thread. */
  onSend?: (ids: string[]) => void | Promise<void>;
  /** Answers a question Claude asked in a comment's run. */
  onAnswer?: (questionId: string, optionId: string) => void | Promise<void>;
  /** Claude is busy: reviews wait, and sends go to the queue. */
  busy: boolean;
  /** Why nothing can be sent at all (a read-only share link, a session that is gone). Claude's
   * open question never blocks a send: Claude is told it was left open. */
  blocked?: string;
  onFocus: (point: Point3) => void;
  /** Where the comment tools go (the canvas's bottom bar); none: centred above it. */
  toolbarSlot?: HTMLElement | null;
};

/**
 * Comments on the canvas: the comment tool and the comments list, a pin per open comment
 * on its face, the draft of a new comment, and the open thread with its replies, Claude's
 * answer, the changes made for it (accept, reject, Undo), Send to Claude and Resolve.
 */
export function CommentsLayer(props: Props) {
  const slotted = (node: React.ReactNode) => (props.toolbarSlot ? createPortal(node, props.toolbarSlot) : node);
  const { sessionId, build, ready, tool, toScreen, width, height, busy, blocked } = props;
  const { comments, questions, conversations, put } = useComments(sessionId, build?.id);
  const open = comments.find((c) => c.id === tool.openId) ?? null;
  const pins = ready ? visiblePins(comments) : [];
  const notes = comments.filter((c) => !c.resolved && threadState(c) === "note");
  const sendWhy = !props.onSend ? "Not connected to Claude" : blocked;
  // Sent while Claude works: it waits in the queue and goes when the run ends.
  const sendNote = busy ? "Claude is working: it is sent when the run ends" : undefined;
  // A thread waits on the user while its run's question is open.
  const asks = (c: Comment) => !!c.request && c.request.message_id in questions;
  const stateOf = (c: Comment) => threadState(c, asks(c), conversations[c.id]);

  // "Open thread" from the main chat (a thread's report or question).
  useEffect(() => {
    const onOpen = (e: Event) => {
      const id = (e as CustomEvent<string>).detail;
      const c = comments.find((x) => x.id === id);
      if (!c) return;
      tool.setListOpen(false);
      tool.open(id);
      if (c.location && !c.resolved) props.onFocus(c.location.point);
    };
    window.addEventListener(OPEN_THREAD, onOpen);
    return () => window.removeEventListener(OPEN_THREAD, onOpen);
  }, [comments, tool, props]);

  // Escape cancels a draft, then closes the thread.
  useEffect(() => {
    if (!tool.draft && !tool.openId && !tool.listOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      tool.place(null);
      tool.open(null);
      tool.setListOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [tool]);

  const send = async (ids: string[]) => {
    if (sendWhy || !ids.length) return;
    await props.onSend?.(ids);
  };

  const at = (p: Point3) => toScreen(p);
  // Next to the pin: on its right where the card fits, else on its left, so the pin stays in view.
  // A first guess: `useAnchoredCard` then keeps the card inside the canvas between its bars.
  const place = (q: ScreenPoint) => ({
    left: Math.max(8, q.x + 22 + CARD_WIDTH <= width - 8 ? q.x + 22 : q.x - 22 - CARD_WIDTH),
    top: Math.max(8, Math.min(q.y - 40, height - 340)),
  });
  const openAt = open?.location && !open.resolved ? at(open.location.point) : null;
  const draftAt = tool.draft ? at(tool.draft.point) : null;

  return (
    <>
      <div data-testid="comment-pins" className="pointer-events-none absolute inset-0 overflow-hidden">
        {pins.map((c) => {
          const q = at(c.location.point);
          if (!q) return null;
          const state = stateOf(c);
          return (
            <button
              key={c.id}
              type="button"
              data-testid="comment-pin"
              data-comment={c.id}
              data-face={c.location.faces?.[0] ?? ""}
              data-state={state}
              data-x={Math.round(q.x)}
              data-y={Math.round(q.y)}
              aria-label={`Comment: ${c.text}`}
              aria-pressed={tool.openId === c.id}
              title={`${c.author}: ${c.text}`}
              onClick={() => tool.open(tool.openId === c.id ? null : c.id)}
              style={{ left: q.x, top: q.y - PIN_LIFT, zIndex: 3 }}
              className={`pointer-events-auto absolute grid size-6 -translate-x-1/2 -translate-y-1/2 place-items-center rounded-full rounded-bl-none border-2 border-[#1f1f1f] text-[11px] font-bold text-[#1f1f1f] shadow aria-pressed:ring-2 aria-pressed:ring-white ${
                state === "working" ? "animate-pulse bg-[#d9b25f]" : state === "waiting" || state === "queued" ? "bg-[#d9b25f]" : "bg-[#8fd1c5]"
              }`}
            >
              {c.author.charAt(0).toUpperCase()}
            </button>
          );
        })}
        {tool.draft && draftAt && (
          <span
            data-testid="comment-draft-pin"
            style={{ left: draftAt.x, top: draftAt.y - PIN_LIFT }}
            className="absolute grid size-6 -translate-x-1/2 -translate-y-1/2 place-items-center rounded-full rounded-bl-none border-2 border-dashed border-[#8fd1c5] bg-[#1f1f1f]"
          >
            <MessageSquarePlus className="size-3 text-[#8fd1c5]" />
          </span>
        )}
      </div>

      {tool.draft && build && (
        <DraftCard
          key={`${tool.draft.face}/${tool.draft.point.join(",")}`}
          style={draftAt ? place(draftAt) : { left: 12, top: 56 }}
          anchor={draftAt}
          sendWhy={sendWhy}
          sendNote={sendNote}
          onCancel={() => tool.place(null)}
          onSave={async (text, toClaude) => {
            const d = tool.draft!;
            const made = await createComment(sessionId, { build_id: build.id, face_id: d.face, point: d.point, text });
            put(made);
            tool.place(null);
            tool.open(made.id);
            if (toClaude || mentionsClaude(text)) await send([made.id]);
          }}
        />
      )}

      {open && (
        <Thread
          key={open.id}
          comment={open}
          style={openAt ? place(openAt) : { left: 12, top: 56 }}
          anchor={openAt}
          sessionId={sessionId}
          changes={changesFor(props.changeSet, open.id)}
          question={threadQuestion(conversations[open.id]) ?? (open.request ? questions[open.request.message_id] : undefined)}
          conversation={conversations[open.id]}
          busy={busy}
          sendWhy={sendWhy}
          sendNote={sendNote}
          onClose={() => tool.open(null)}
          onPut={put}
          onSend={() => send([open.id])}
          onAnswer={props.onAnswer}
          onReviewed={props.onReviewed}
        />
      )}

      {/* The comment tools: comment on the model, and the comments list. */}
      {slotted(
      <div
        role="toolbar"
        aria-label="Comment tools"
        data-pin-cover=""
        className={`pointer-events-auto flex h-9 items-center gap-0.5 rounded-xl border border-[var(--cs-border)] bg-[#262626] px-1 shadow-lg ${
          props.toolbarSlot ? "shrink-0" : "absolute bottom-[4.5rem] left-1/2 z-20 -translate-x-1/2"
        }`}
      >
        <button
          type="button"
          aria-label="Comment"
          aria-pressed={tool.active}
          title={tool.active ? "Click a face to pin a comment (C)" : "Comment (C)"}
          onClick={() => {
            tool.toggle();
            tool.place(null);
          }}
          className="grid size-7 place-items-center rounded-md text-[var(--cs-muted)] hover:bg-[var(--cs-hover)] hover:text-[var(--cs-text)] aria-pressed:bg-[#2c4a7a] aria-pressed:text-[#9cc3ff]"
        >
          <MessageSquare className="size-4" />
        </button>
        <button
          type="button"
          aria-label="Comments"
          aria-expanded={tool.listOpen}
          title="All comments, resolved ones included"
          onClick={() => tool.setListOpen(!tool.listOpen)}
          className="flex h-7 items-center gap-1 rounded-md px-1.5 text-[13px] text-[var(--cs-muted)] hover:bg-[var(--cs-hover)] hover:text-[var(--cs-text)] aria-expanded:bg-[var(--cs-active)] aria-expanded:text-[var(--cs-text)]"
        >
          <ListChecks className="size-4" />
          <span data-testid="comment-count">{comments.filter((c) => !c.resolved).length}</span>
        </button>
        {tool.active && <span className="whitespace-nowrap px-1.5 text-[12px] text-[var(--cs-muted)]">Click a face to comment</span>}
      </div>,
      )}

      {tool.listOpen && (
        <section
          aria-label="Comments list"
          className="absolute bottom-[7.5rem] left-1/2 z-30 w-80 -translate-x-1/2 rounded-xl border border-[var(--cs-border)] bg-[#262626] p-2 text-[13px] shadow-xl"
        >
          <div className="flex items-center gap-2 px-1 pb-1.5">
            <h2 className="flex-1 font-semibold">Comments</h2>
            {notes.length > 1 && (
              <button
                type="button"
                disabled={!!sendWhy}
                title={sendWhy ?? sendNote ?? "One run answers each note in its own thread"}
                onClick={() => send(notes.map((c) => c.id))}
                className={btn}
              >
                <Send className="size-3.5" />
                Send {notes.length} to Claude
              </button>
            )}
          </div>
          {comments.length === 0 ? (
            <p className="px-1 pb-1 text-[var(--cs-muted)]">No comments yet. Pick the comment tool, then click a face.</p>
          ) : (
            <ul aria-label="Comment threads" className="cs-scroll flex max-h-72 flex-col overflow-y-auto">
              {comments.map((c) => {
                const state = stateOf(c);
                return (
                  <li key={c.id} data-comment={c.id} data-state={state}>
                    <button
                      type="button"
                      onClick={() => {
                        tool.open(c.id);
                        tool.setListOpen(false);
                        if (c.location && !c.resolved) props.onFocus(c.location.point);
                      }}
                      className={`flex w-full flex-col rounded-md px-2 py-1.5 text-left hover:bg-[var(--cs-hover)] ${c.resolved ? "opacity-60" : ""}`}
                    >
                      <span className="truncate">{c.text}</span>
                      <span className="text-[11px] text-[var(--cs-muted)]">
                        {state === "queued" && conversations[c.id]?.queued_behind_title
                          ? `Queued behind “${conversations[c.id]?.queued_behind_title}”`
                          : THREAD_LABEL[state]}{" "}
                        · {pinnedTo(c).replace(/^Pinned to /, "")}
                        {!c.location && !c.resolved ? " · not on this build" : ""}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      )}
    </>
  );
}

function DraftCard({
  style,
  anchor,
  sendWhy,
  sendNote,
  onCancel,
  onSave,
}: {
  style: React.CSSProperties;
  /** The pin's point on screen: the card stays beside it, inside the canvas. */
  anchor: ScreenPoint | null;
  sendWhy: string | undefined;
  sendNote: string | undefined;
  onCancel: () => void;
  onSave: (text: string, toClaude: boolean) => Promise<void>;
}) {
  const ref = useRef<HTMLElement>(null);
  useAnchoredCard(ref, anchor);
  const [text, setText] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const save = (toClaude: boolean) => {
    if (!text.trim() || saving) return;
    setSaving(true);
    setError(null);
    onSave(text.trim(), toClaude)
      .catch((e: Error) => setError(shownError(e)))
      .finally(() => setSaving(false));
  };
  return (
    <section ref={ref} role="dialog" aria-label="New comment" style={{ ...style, width: CARD_WIDTH }} className={`${card} cs-scroll overflow-y-auto`}>
      <textarea
        aria-label="Comment text"
        autoFocus
        rows={3}
        value={text}
        placeholder="Add a comment…"
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) save(false);
        }}
        className="w-full resize-none rounded-md bg-[var(--cs-surface-2)] px-2 py-1.5 outline-none focus:ring-1 focus:ring-[var(--cs-accent)] placeholder:text-[var(--cs-faint)]"
      />
      {error && (
        <p role="alert" className="mt-1 text-[12px] text-[var(--cs-accent)]">
          {error}
        </p>
      )}
      <div className="mt-2 flex items-center justify-end gap-1">
        <button type="button" onClick={onCancel} className={btn}>
          Cancel
        </button>
        <button
          type="button"
          disabled={!text.trim() || saving}
          title={text.trim() ? "Pin it as a note: Claude doesn't act on it" : "Type a comment first"}
          onClick={() => save(false)}
          className={btn}
        >
          Comment
        </button>
        <button
          type="button"
          disabled={!text.trim() || saving || !!sendWhy}
          title={sendWhy ?? (text.trim() ? (sendNote ?? "Claude answers in this thread") : "Type a comment first")}
          onClick={() => save(true)}
          className={primary}
        >
          <Send className="size-3.5" />
          Send to Claude
        </button>
      </div>
    </section>
  );
}

function Thread({
  comment,
  style,
  anchor,
  sessionId,
  changes,
  question,
  conversation,
  busy,
  sendWhy,
  sendNote,
  onClose,
  onPut,
  onSend,
  onAnswer,
  onReviewed,
}: {
  comment: Comment;
  style: React.CSSProperties;
  /** The pin's point on screen: the card stays beside it, inside the canvas. */
  anchor: ScreenPoint | null;
  sessionId: string;
  changes: Change[];
  question: ThreadQuestion | undefined;
  /** The thread's conversation with Claude, as the server says it stands. */
  conversation: Conversation | undefined;
  busy: boolean;
  sendWhy: string | undefined;
  sendNote: string | undefined;
  onClose: () => void;
  onPut: (c: Comment) => void;
  onSend: () => Promise<void>;
  onAnswer?: (questionId: string, optionId: string) => void | Promise<void>;
  onReviewed: (set: ChangeSet) => void;
}) {
  const ref = useRef<HTMLElement>(null);
  useAnchoredCard(ref, anchor);
  const [reply, setReply] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [answered, setAnswered] = useState<string | null>(null);
  const state = threadState(comment, !!question, conversation);
  // Waiting on Claude's question, or its turn, counts as in progress: it can't be resolved yet.
  const working = state === "working" || state === "waiting" || state === "queued";
  const undo = undoReview(changes);
  const reviewWhy = busy ? "Claude is working: review when the run has finished" : pending ? "Applying" : undefined;

  const act = (label: string, run: () => Promise<unknown>) => {
    setPending(label);
    setError(null);
    run()
      .catch((e: Error) => setError(shownError(e)))
      .finally(() => setPending(null));
  };
  const review = (body: { accept: string[]; reject: string[] }, label: string) =>
    act(label, () => reviewChanges(sessionId, body).then(onReviewed));
  const postReply = (toClaude: boolean) => {
    const text = reply.trim();
    if (!text) return toClaude ? act("send", onSend) : undefined;
    act(toClaude ? "send" : "reply", async () => {
      onPut(await replyToComment(sessionId, comment.id, text));
      setReply("");
      if (toClaude || mentionsClaude(text)) await onSend();
    });
  };

  return (
    <section
      role="dialog"
      aria-label="Comment thread"
      data-testid="comment-thread"
      data-comment={comment.id}
      data-state={state}
      ref={ref}
      style={{ ...style, width: CARD_WIDTH }}
      className={`${card} flex flex-col`}
    >
      {/* The header stays in view; the rest scrolls when the card is taller than the canvas. */}
      <div className="flex shrink-0 items-start gap-2">
        <p data-testid="comment-pinned-to" className="flex-1 text-[12px] text-[var(--cs-muted)]">
          {pinnedTo(comment)}
          {!comment.location && " · not on this build"}
        </p>
        <button
          type="button"
          aria-label="Close thread"
          onClick={onClose}
          className="grid size-5 place-items-center rounded text-[var(--cs-muted)] hover:bg-[var(--cs-hover)] hover:text-[var(--cs-text)]"
        >
          <X className="size-3.5" />
        </button>
      </div>

      <div data-testid="comment-thread-body" className="cs-scroll -mr-1 min-h-0 flex-1 overflow-y-auto pr-1">
      <ol aria-label="Thread" className="mt-1.5 flex flex-col">
        <ThreadEntry author={comment.author} at={comment.created_at} text={comment.text} />
        {comment.replies.map((r) => (
          <ThreadEntry key={r.id} author={r.author} at={r.created_at} text={r.text} claude={r.role === "assistant"} />
        ))}
        {working && (
          <li data-testid="comment-working" className="border-t border-[var(--cs-border)] pt-2 text-[12px] text-[var(--cs-muted)] first:border-t-0">
            {state === "waiting"
              ? "Claude asked you a question here. Answer it and Claude carries on, or send something else."
              : state === "queued"
                ? `Queued behind “${conversation?.queued_behind_title ?? "another request"}”: runs take turns on the design.`
                : "Claude is working on this…"}
          </li>
        )}
      </ol>

      {question && answered !== question.questionId && (
        <div data-testid="comment-question" className="mt-2 rounded-lg bg-[var(--cs-surface-2)] p-2">
          <p className="font-semibold leading-snug">{question.text}</p>
          <div className="mt-1.5 flex flex-col gap-1">
            {question.options.map((o) => (
              <button
                key={o.id}
                type="button"
                disabled={!onAnswer}
                onClick={() => {
                  setAnswered(question.questionId);
                  void onAnswer?.(question.questionId, o.id);
                }}
                className={`${btn} h-auto justify-start py-1 text-left`}
              >
                <span>
                  {o.label}
                  {o.description && <span className="block text-[11px] text-[var(--cs-muted)]">{o.description}</span>}
                </span>
              </button>
            ))}
          </div>
        </div>
      )}

      {changes.length > 0 && (
        <ul aria-label="Changes for this comment" className="mt-2 flex flex-col gap-1">
          {changes.map((c) => (
            <li
              key={c.id}
              data-testid="comment-change"
              data-change={c.id}
              data-status={c.status}
              className="flex items-center gap-2 rounded-lg bg-[var(--cs-surface-2)] px-2 py-1.5"
            >
              <div className="min-w-0 flex-1">
                <p className="text-[11px] text-[var(--cs-muted)]">Tracked change · answering this comment</p>
                <p className="truncate leading-snug" title={c.reason}>
                  {c.title}
                </p>
                <p className="text-[11px] text-[var(--cs-muted)]">{c.status === "proposed" ? "Proposed" : c.status === "accepted" ? "Applied" : "Rejected"}</p>
              </div>
              {c.status !== "accepted" && (
                <button
                  type="button"
                  aria-label={`Accept: ${c.title}`}
                  title={reviewWhy ?? "Accept"}
                  disabled={!!reviewWhy}
                  onClick={() => review({ accept: [c.id], reject: [] }, c.id)}
                  className="grid size-6 place-items-center rounded-md text-[var(--cs-muted)] enabled:hover:bg-[var(--cs-hover)] enabled:hover:text-[var(--cs-text)] disabled:opacity-50"
                >
                  <Check className="size-3.5" />
                </button>
              )}
              {c.status === "proposed" && (
                <button
                  type="button"
                  aria-label={`Reject: ${c.title}`}
                  title={reviewWhy ?? "Reject"}
                  disabled={!!reviewWhy}
                  onClick={() => review({ accept: [], reject: [c.id] }, c.id)}
                  className="grid size-6 place-items-center rounded-md text-[var(--cs-muted)] enabled:hover:bg-[var(--cs-hover)] enabled:hover:text-[var(--cs-text)] disabled:opacity-50"
                >
                  <X className="size-3.5" />
                </button>
              )}
            </li>
          ))}
        </ul>
      )}

      {error && (
        <p role="alert" className="mt-1 text-[12px] text-[var(--cs-accent)]">
          {error}
        </p>
      )}

      {!comment.resolved && (
        <textarea
          aria-label="Reply"
          rows={1}
          value={reply}
          placeholder="Reply…"
          onChange={(e) => setReply(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              postReply(false);
            }
          }}
          className="mt-2 w-full resize-none rounded-md bg-[var(--cs-surface-2)] px-2 py-1.5 outline-none focus:ring-1 focus:ring-[var(--cs-accent)] placeholder:text-[var(--cs-faint)]"
        />
      )}

      <div className="mt-2 flex flex-wrap items-center gap-1">
        {undo && (
          <button
            type="button"
            disabled={!!reviewWhy}
            title={reviewWhy ?? "Revert the change made for this comment"}
            onClick={() => review(undo, "undo")}
            className={`${btn} border border-[var(--cs-border)]`}
          >
            <Undo2 className="size-3.5" />
            Undo
          </button>
        )}
        {/* Claude's open question here doesn't hold the thread: whatever is sent goes to Claude. */}
        {!comment.resolved && (!working || state === "waiting") && (
          <button
            type="button"
            disabled={!!sendWhy || !!pending}
            title={sendWhy ?? sendNote ?? "Claude answers in this thread"}
            onClick={() => postReply(true)}
            className={`${btn} border border-[var(--cs-border)]`}
          >
            <Send className="size-3.5" />
            {comment.request ? "Send again" : "Send to Claude"}
          </button>
        )}
        <button
          type="button"
          disabled={!!pending || working}
          title={state === "waiting" ? "Answer Claude's question first" : working ? "Claude is still working on it" : comment.resolved ? "Show the pin again" : "Close the thread and hide the pin"}
          onClick={() =>
            act("resolve", async () => {
              onPut(await resolveComment(sessionId, comment.id, !comment.resolved));
              if (!comment.resolved) onClose(); // Resolve closes the thread; it stays in the comments list
            })
          }
          className={`${primary} ml-auto`}
        >
          {comment.resolved ? "Reopen" : "Resolve"}
        </button>
      </div>
      </div>
    </section>
  );
}

function ThreadEntry({ author, at, text, claude }: { author: string; at: string; text: string; claude?: boolean }) {
  return (
    <li data-testid={claude ? "comment-reply-claude" : "comment-entry"} className="border-t border-[var(--cs-border)] py-2 first:border-t-0 first:pt-0">
      <p>
        <b className="font-semibold">{author}</b> <span className="text-[12px] text-[var(--cs-muted)]">{timeAgo(at)}</span>
      </p>
      <p className="whitespace-pre-wrap leading-snug">{text}</p>
    </li>
  );
}
