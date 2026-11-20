"use client";

import { CircleHelp, Clock, Loader2, MessageSquare } from "lucide-react";
import { OPEN_THREAD, type Conversation, type ThreadReportEvent } from "@/lib/session-api";

/** Opens a comment's thread on the canvas (the comments layer listens). */
export function openThread(commentId: string) {
  window.dispatchEvent(new CustomEvent(OPEN_THREAD, { detail: commentId }));
}

/** Where a conversation stands, in words. */
export function conversationStatus(c: Conversation): string {
  switch (c.status) {
    case "working":
      return "Claude is working";
    case "queued":
      return c.queued_behind_title ? `Queued behind “${c.queued_behind_title}”` : "Queued";
    case "waiting_for_answer":
      return "Waiting for your answer";
    default:
      return "Idle";
  }
}

const linkBtn =
  "flex h-6 shrink-0 items-center gap-1 rounded-md px-1.5 text-[12px] text-[var(--cs-muted)] hover:bg-[var(--cs-hover)] hover:text-[var(--cs-text)]";

/**
 * The comment threads Claude is busy with, above the composer: each thread is its own
 * conversation, and runs take turns on the one design, so each says whether Claude is working
 * on it, it is queued behind another, or Claude asked something in it. A thread's question can
 * be answered here as well as in the thread. Nothing here blocks the composer.
 */
export function ThreadsPanel({
  conversations,
  busy,
  onAnswer,
  readOnly,
}: {
  conversations: Conversation[];
  busy: boolean;
  onAnswer?: (questionId: string, optionId: string) => void;
  readOnly?: boolean;
}) {
  const threads = conversations.filter((c) => c.kind === "thread" && c.status !== "idle");
  const main = conversations.find((c) => c.kind === "main");
  if (!threads.length && main?.status !== "queued") return null;
  return (
    <section aria-label="Comment threads" data-testid="threads-panel" className="mb-2 flex flex-col gap-1.5">
      {main?.status === "queued" && (
        <p data-testid="main-queued" className="flex items-center gap-1.5 text-[12px] text-[var(--cs-muted)]">
          <Clock aria-hidden className="size-3.5" />
          {main.queued_behind_title
            ? `Your message is queued behind “${main.queued_behind_title}”: runs take turns on the design.`
            : "Your message is queued: runs take turns on the design."}
        </p>
      )}
      {threads.map((c) => (
        <div
          key={c.id}
          data-testid="thread-status"
          data-thread={c.id}
          data-status={c.status}
          className="rounded-lg border border-[var(--cs-border)] px-2.5 py-1.5 text-[13px]"
        >
          <div className="flex items-center gap-1.5">
            {c.status === "working" ? (
              <Loader2 aria-hidden className="size-3.5 shrink-0 animate-spin text-[var(--cs-muted)]" />
            ) : c.status === "waiting_for_answer" ? (
              <CircleHelp aria-hidden className="size-3.5 shrink-0 text-[#d9b25f]" />
            ) : (
              <Clock aria-hidden className="size-3.5 shrink-0 text-[var(--cs-muted)]" />
            )}
            <span className="min-w-0 flex-1 truncate">
              {c.status === "waiting_for_answer" ? (
                <>Claude has a question in “{c.title}”</>
              ) : (
                <>
                  “{c.title}” <span className="text-[var(--cs-muted)]">· {conversationStatus(c)}</span>
                </>
              )}
            </span>
            <button type="button" className={linkBtn} onClick={() => openThread(c.id)} title="Open the thread on the canvas">
              <MessageSquare aria-hidden className="size-3.5" />
              Open thread
            </button>
          </div>
          {c.status === "waiting_for_answer" && c.question && (
            <div className="mt-1.5 flex flex-col gap-1">
              <p className="leading-snug">{c.question.text}</p>
              <div role="group" aria-label={`Answer: ${c.question.text}`} className="flex flex-wrap gap-1">
                {c.question.options.map((o) => (
                  <button
                    key={o.id}
                    type="button"
                    disabled={readOnly || !onAnswer}
                    title={o.description ?? (busy ? "Claude is working: your answer waits its turn" : undefined)}
                    onClick={() => onAnswer?.(c.question!.question_id, o.id)}
                    className="rounded-md border border-[var(--cs-border)] px-2 py-0.5 text-[12px] enabled:hover:bg-[var(--cs-hover)] disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {o.label}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      ))}
    </section>
  );
}

/** A thread's run reporting back in the main chat: what changed, the checks and the build. */
export function ThreadReport({ report }: { report: ThreadReportEvent }) {
  const checks = report.checks;
  return (
    <div
      role="group"
      aria-label={`From the thread “${report.thread_title}”`}
      data-testid="thread-report"
      data-thread={report.comment_id}
      data-outcome={report.outcome}
      className="rounded-lg border border-[var(--cs-border)] px-3 py-2 text-[13px]"
    >
      <div className="flex items-center gap-1.5">
        <MessageSquare aria-hidden className="size-3.5 shrink-0 text-[var(--cs-muted)]" />
        <p className="min-w-0 flex-1 truncate text-[var(--cs-muted)]">
          In the thread <span className="text-[var(--cs-text)]">“{report.thread_title}”</span>
          {report.outcome === "changed" ? " · changed the design" : report.outcome === "failed" ? " · couldn't finish" : " · replied"}
        </p>
        <button type="button" className={linkBtn} onClick={() => openThread(report.comment_id)} title="Open the thread on the canvas">
          Open thread
        </button>
      </div>
      <p className="mt-1 leading-snug">{report.summary}</p>
      {!!report.changes?.length && (
        <ul className="mt-1 list-disc pl-5 text-[var(--cs-muted)]">
          {report.changes.map((c) => (
            <li key={c}>{c}</li>
          ))}
        </ul>
      )}
      {report.build_number != null && (
        <p data-testid="thread-report-build" className="mt-1 text-[12px] text-[var(--cs-muted)]">
          Build {report.build_number}
          {checks && ` · ${checks.pass} passed, ${checks.fail} failing${checks.untested ? `, ${checks.untested} untested` : ""}`}
        </p>
      )}
    </div>
  );
}
