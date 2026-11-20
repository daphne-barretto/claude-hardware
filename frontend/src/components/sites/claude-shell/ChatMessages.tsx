"use client";

import { Markdown } from "./Markdown";
import { createContext, useContext, useId, useState } from "react";
import { Box, Check, CircleAlert, GitPullRequestArrow, ListChecks, Loader2, Plus, TriangleAlert } from "lucide-react";
import { createSession, type Constraint, type QuestionOption, type RiskFlag } from "@/lib/session-api";
import type { Build } from "@/components/canvas/api";
import { buildTitle, summaryLine } from "@/components/canvas/history";
import { CheckCounts } from "@/components/canvas/CheckCounts";
import type { CheckCountStatus } from "@/components/canvas/openChecks";
import { isPassing, riskSubject, type Block, type ChatMessage } from "./chat";
import { ImportNote } from "./ImportNote";
import { RiskReviewNote } from "./RiskReviewNote";
import type { RiskReviewError } from "./riskReview";
import { FileChip, SelectionChip } from "./Composer";
import { ConflictCard } from "./ConflictCard";
import { FoundCard } from "./FoundCard";
import { StopButton } from "./QueuedMessages";
import { ThreadReport } from "./ThreadsPanel";
import { NoticeView, StoppedView } from "./RunNotice";
import { shownError } from "@/lib/errors";

type Props = {
  messages: ChatMessage[];
  /** Claude is replying: questions cannot be answered until the reply ends. */
  busy: boolean;
  /** A message was sent and Claude's reply hasn't started yet. */
  waiting?: boolean;
  onAnswer: (questionId: string, option: QuestionOption) => void;
  /** Accept or ignore a flagged risk's fix; without it the flags show no controls. */
  onReviewRisk?: (flagId: string, decision: "accept" | "reject") => void;
  /** A risk's fix is rebuilding: the other fixes wait. */
  reviewingRisk?: boolean;
  /** Why the last Accept or Ignore didn't go through, shown on its flag. */
  riskError?: RiskReviewError | null;
  /** Show a flagged risk's location on the canvas; without it (no canvas) the line is plain text. */
  onShowRisk?: (flag: RiskFlag) => void;
  /** A build card was clicked: show that build on the canvas. */
  onShowBuild?: (buildId: string) => void;
  /**
   * A check count was clicked ("3 untested"): open the canvas's Constraints panel on that group,
   * for that build. Without it (no canvas), the counts are plain text.
   */
  onOpenChecks?: (buildId: string, status: CheckCountStatus) => void;
  /** Highlight (or, with `select`, select) faces Claude found; without it (no canvas) they are only listed. */
  onFound?: (block: FoundBlock, select: boolean) => void;
  wide?: boolean;
  /** Opens a session just created to replace one that is gone. */
  onOpenSession?: (id: string) => void;
  /** Stop the running reply (a Stop control under it); the message queue goes on. */
  onStop?: () => void;
  stopping?: boolean;
  /** A read-only session (a shared link): why nothing can be answered or reviewed. */
  readOnly?: string;
  /** The session's builds, oldest first: a build card names its build by its place ("Build 3"). */
  builds?: readonly Build[];
  /**
   * The design's constraints: one taken from a user message's words gets a line under it (the
   * Constraints panel in the canvas shows them all).
   */
  constraints?: readonly Constraint[];
};

/** Why the chat is read-only (a shared link), if it is: questions show no options. */
const ReadOnly = createContext<string | undefined>(undefined);
/** The session's builds, for the build cards' names. */
const Builds = createContext<readonly Build[]>([]);
/** Opens the Constraints panel from a check count, if there is a canvas. */
const OpenChecks = createContext<Props["onOpenChecks"]>(undefined);

type FoundBlock = Extract<Block, { kind: "found" }>;
type RiskProps = Pick<Props, "busy" | "onReviewRisk" | "reviewingRisk" | "riskError" | "onShowRisk" | "onFound">;

/** The chat transcript: user messages, and Claude's streamed replies with progress, builds and questions. */
export function ChatMessages({
  messages,
  busy,
  waiting,
  onAnswer,
  onReviewRisk,
  reviewingRisk,
  riskError,
  onShowRisk,
  onShowBuild,
  onOpenChecks,
  onFound,
  wide,
  onOpenSession,
  onStop,
  stopping,
  readOnly,
  builds = NO_BUILDS,
  constraints = NO_CONSTRAINTS,
}: Props) {
  // Read-only: no risk fixes to accept or ignore (the server would refuse them).
  const risk = { busy, onReviewRisk: readOnly ? undefined : onReviewRisk, reviewingRisk, riskError, onShowRisk, onFound };
  return (
    <ReadOnly.Provider value={readOnly}>
    <Builds.Provider value={builds}>
    <OpenChecks.Provider value={onOpenChecks}>
    <ol className={`mx-auto flex flex-col gap-4 py-6 ${wide ? "max-w-[720px]" : ""}`}>
      {/* A run for comments sent to Claude replies in the comment threads, not here. */}
      {messages.filter((m) => !m.thread).map((m) =>
        m.role === "user" ? (
          <li key={m.id} data-testid="user-message" className="ml-auto max-w-[90%] rounded-xl bg-[var(--cs-surface-2)] px-3.5 py-3 leading-6">
            {(m.files || m.selection) && (
              <div className="mb-2 flex flex-wrap gap-1.5">
                {m.selection && <SelectionChip selection={m.selection} label={m.selectionLabel} />}
                {m.files?.map((f) => (
                  <FileChip key={f} name={f} />
                ))}
              </div>
            )}
            <span className="whitespace-pre-wrap">{m.text}</span>
            <ConstraintNote constraints={constraints.filter((c) => c.source === "user" && c.message_id === m.id)} />
          </li>
        ) : (
          <li
            key={m.id}
            aria-busy={m.streaming || undefined}
            data-testid="assistant-message"
            className="flex flex-col gap-2 leading-6 text-[var(--cs-text)]"
          >
            {m.blocks ? (
              m.blocks.map((b, i) => (
                <BlockView
                  key={i}
                  block={b}
                  // Only the latest progress line of a running reply is still in progress.
                  active={!!m.streaming && b.kind === "progress" && !m.blocks!.slice(i + 1).some((x) => x.kind === "progress")}
                  busy={busy}
                  onAnswer={onAnswer}
                  risk={risk}
                  onShowBuild={onShowBuild}
                  onOpenSession={onOpenSession}
                />
              ))
            ) : (
              <Markdown text={m.text} />
            )}
            {m.streaming && m.blocks?.length === 0 && (
              <span className="text-[13px] text-[var(--cs-muted)]">Thinking…</span>
            )}
            {/* One Stop per reply: a retrying or waiting notice carries its own (./RunNotice). */}
            {m.streaming && onStop && !m.blocks?.some(isPassing) && <StopButton onStop={onStop} stopping={stopping} />}
          </li>
        ),
      )}
      {waiting && (
        <li data-testid="assistant-waiting" aria-busy className="flex items-center gap-2 leading-6 text-[var(--cs-muted)]">
          <Loader2 aria-hidden className="size-4 animate-spin" />
          <span role="status" className="text-[13px]">
            Thinking…
          </span>
        </li>
      )}
    </ol>
    </OpenChecks.Provider>
    </Builds.Provider>
    </ReadOnly.Provider>
  );
}

function BlockView({
  block,
  active,
  busy,
  onAnswer,
  risk,
  onShowBuild,
  onOpenSession,
}: {
  block: Block;
  active: boolean;
  busy: boolean;
  onAnswer: Props["onAnswer"];
  risk: RiskProps;
  onShowBuild: Props["onShowBuild"];
  onOpenSession?: Props["onOpenSession"];
}) {
  switch (block.kind) {
    case "text":
      return <Markdown text={block.text} />;
    case "progress":
      return (
        <p data-testid="progress-line" className="flex items-center gap-1.5 text-[13px] text-[var(--cs-muted)]">
          {active ? (
            <Loader2 aria-hidden className="size-3.5 shrink-0 animate-spin" />
          ) : (
            <Check aria-hidden className="size-3.5 shrink-0" />
          )}
          {block.text}
        </p>
      );
    case "build":
      return (
        <>
          {block.build.origin && <ImportNote origin={block.build.origin} />}
          <BuildCard build={block.build} onShowBuild={onShowBuild} />
        </>
      );
    case "changes":
      return <ChangesView block={block} />;
    case "plan":
      return (
        <div
          role="group"
          aria-label="Build plan"
          data-testid="build-plan"
          className="rounded-lg border border-[var(--cs-border)] px-3 py-2 text-[13px]"
        >
          <p className="mb-1 flex items-center gap-1.5 font-[500]">
            <ListChecks aria-hidden className="size-3.5 shrink-0 text-[var(--cs-muted)]" />
            Build plan
          </p>
          <ol className="list-decimal pl-5 text-[var(--cs-text)]">
            {block.steps.map((step, i) => (
              <li key={i}>{step}</li>
            ))}
          </ol>
        </div>
      );
    case "risks":
      return <RisksView block={block} {...risk} />;
    case "question":
      return <QuestionView block={block} busy={busy} onAnswer={onAnswer} />;
    case "found": {
      const { onFound } = risk;
      return (
        <FoundCard
          block={block}
          onShow={onFound && (() => onFound(block, false))}
          onSelect={onFound && (() => onFound(block, true))}
        />
      );
    }
    case "error":
      return <StoppedView block={block} />;
    case "notice":
      return <NoticeView block={block} live={busy} />;
    case "session_gone":
      return <SessionGone onOpenSession={onOpenSession} />;
    case "thread_report":
      return <ThreadReport report={block.report} />;
  }
}

/**
 * The chat's session is no longer on the server (a deploy wiped it, docs/architecture.md): say so
 * plainly and offer a new chat, which creates a session and opens it.
 */
function SessionGone({ onOpenSession }: { onOpenSession?: Props["onOpenSession"] }) {
  const [starting, setStarting] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  const start = async () => {
    setStarting(true);
    setFailed(null);
    try {
      const session = await createSession();
      onOpenSession?.(session.id);
    } catch (e) {
      setFailed(`Could not start a new chat: ${shownError(e)}`);
      setStarting(false);
    }
  };
  return (
    <div role="alert" className="flex flex-col items-start gap-2 rounded-lg border border-[var(--cs-border)] px-3 py-2.5 text-[13px]">
      <p className="flex items-start gap-1.5">
        <CircleAlert aria-hidden className="mt-1 size-3.5 shrink-0 text-[var(--cs-muted)]" />
        This chat is no longer on the server, so Claude can&apos;t go on with it. Start a new chat to keep working.
      </p>
      <button
        type="button"
        onClick={start}
        disabled={starting}
        title={starting ? "Starting a new chat…" : undefined}
        className="flex h-8 items-center gap-1.5 rounded-lg border border-[var(--cs-border)] px-2.5 enabled:hover:bg-[var(--cs-hover)] disabled:cursor-not-allowed disabled:opacity-50"
      >
        <Plus aria-hidden className="size-3.5 text-[var(--cs-muted)]" />
        Start a new chat
      </button>
      {failed && <p className="text-red-400">{failed}</p>}
    </div>
  );
}

const NO_BUILDS: readonly Build[] = [];
const NO_CONSTRAINTS: readonly Constraint[] = [];

/** One line under a user message naming the constraints its words set (the Constraints panel has them). */
function ConstraintNote({ constraints }: { constraints: readonly Constraint[] }) {
  if (!constraints.length) return null;
  const names = constraints.map((c) => c.label);
  const list = names.length < 3 ? names.join(" and ") : `${names.slice(0, -1).join(", ")}, and ${names.at(-1)}`;
  return (
    <p data-testid="constraint-note" className="mt-1.5 flex items-center gap-1.5 text-[12px] leading-5 text-[var(--cs-muted)]">
      <ListChecks aria-hidden className="size-3 shrink-0" />
      <span>
        {constraints.length === 1 ? "Constraint" : "Constraints"} from your words: {list}
      </span>
    </p>
  );
}

/** A build in the chat, named by its place in the session ("Build 3 · <board> 2.0"), never its ID. */
function BuildCard({ build, onShowBuild }: { build: Extract<Block, { kind: "build" }>["build"]; onShowBuild: Props["onShowBuild"] }) {
  const builds = useContext(Builds);
  const name = buildTitle(build, builds);
  const checks = summaryLine(build.checks);
  const openChecks = useContext(OpenChecks);
  // The card shows its build; each count opens the Constraints panel on its group.
  return (
    <div className="flex w-full items-center gap-2 rounded-lg border border-[var(--cs-border)] text-[13px]">
      <button
        type="button"
        data-testid="build-ready"
        data-build={build.id}
        aria-label={`${name} · ${checks}`}
        title="Show this build on the canvas"
        onClick={() => onShowBuild?.(build.id)}
        className="flex min-w-0 flex-1 items-center gap-2 self-stretch rounded-l-lg py-2 pl-3 text-left hover:bg-[var(--cs-hover)]"
      >
        <Box aria-hidden className="size-3.5 shrink-0 text-[var(--cs-muted)]" />
        <span>{name}</span>
      </button>
      <CheckCounts
        summary={build.checks}
        onOpen={openChecks && ((status) => openChecks(build.id, status))}
        className="py-2 pr-3 text-right text-[var(--cs-muted)]"
      />
    </div>
  );
}

/** A run's proposal: each change with the checks that prove it (reviewed on the canvas). */
function ChangesView({ block }: { block: Extract<Block, { kind: "changes" }> }) {
  const { changes, checks, build } = block.changeSet;
  const openChecks = useContext(OpenChecks);
  const label = `${changes.length} proposed ${changes.length === 1 ? "change" : "changes"}`;
  return (
    <section aria-label="Proposed changes" data-testid="proposed-changes" className="rounded-lg border border-[var(--cs-border)] text-[13px]">
      <p className="flex items-center gap-2 border-b border-[var(--cs-border)] px-3 py-2">
        <GitPullRequestArrow aria-hidden className="size-3.5 shrink-0 text-[var(--cs-muted)]" />
        <span className="font-[500]">{label}</span>
        <CheckCounts
          summary={checks}
          onOpen={openChecks && ((status) => openChecks(build, status))}
          className="ml-auto text-[var(--cs-muted)]"
        />
      </p>
      <ol aria-label="Proposed changes" className="flex flex-col">
        {changes.map((c) => {
          const proven = c.checks.filter((k) => k.alone === "pass").length;
          return (
            <li key={c.id} data-change-id={c.id} className="flex flex-col gap-0.5 px-3 py-2 [&+li]:border-t [&+li]:border-[var(--cs-border)]">
              <span>{c.title}</span>
              <span className="text-[var(--cs-muted)]">
                {c.status} ·{" "}
                {c.checks.length === 0
                  ? "no check depends on it alone"
                  : `${proven} of ${c.checks.length} ${c.checks.length === 1 ? "check passes" : "checks pass"} with it`}
              </span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

const RISK_STATUS: Record<RiskFlag["status"], string> = {
  proposed: "Fix proposed",
  accepted: "Fix accepted",
  rejected: "Fix ignored",
};

/**
 * The risks Claude flagged, apart from the change set: each line names the risk and
 * where it is (selecting it shows the spot on the canvas), with Accept and Ignore for its fix.
 * Accepting or ignoring rebuilds; the controls wait while Claude replies or a fix rebuilds, and
 * the decision already made is pressed and says so.
 */
function RisksView({ block, busy, onReviewRisk, reviewingRisk, riskError, onShowRisk }: { block: Extract<Block, { kind: "risks" }> } & RiskProps) {
  const noteId = useId();
  const { flags } = block.riskFlags;
  const waiting = busy ? "Claude is still replying." : reviewingRisk ? "A fix is rebuilding." : null;
  return (
    <section aria-label="Flagged risks" data-testid="risk-flags" className="rounded-lg border border-[var(--cs-border)] text-[13px]">
      <p className="flex items-center gap-2 border-b border-[var(--cs-border)] px-3 py-2">
        <TriangleAlert aria-hidden className="size-3.5 shrink-0 text-amber-400" />
        <span className="font-[500]">
          {flags.length} flagged {flags.length === 1 ? "risk" : "risks"}
        </span>
        <span className="ml-auto text-[var(--cs-muted)]">Each fix is separate from the changes</span>
      </p>
      <ol aria-label="Flagged risks" className="flex flex-col">
        {flags.map((f) => {
          const subject = riskSubject(f);
          const decision = (kind: "accept" | "reject") => {
            const done = f.status === (kind === "accept" ? "accepted" : "rejected");
            const reason = done ? (kind === "accept" ? "You accepted this fix." : "You ignored this fix.") : waiting;
            return (
              <button
                type="button"
                aria-label={`${kind === "accept" ? "Accept" : "Ignore"} the fix for ${subject}`}
                aria-pressed={done}
                disabled={!!reason}
                title={reason ?? undefined}
                aria-describedby={reason && !done ? noteId : undefined}
                onClick={() => onReviewRisk?.(f.id, kind)}
                className={`h-7 rounded-md border px-2.5 disabled:cursor-not-allowed ${
                  done
                    ? "border-[var(--cs-accent)] bg-[var(--cs-accent)]/10"
                    : "border-[var(--cs-border)] enabled:hover:bg-[var(--cs-hover)] disabled:opacity-50"
                }`}
              >
                {kind === "accept" ? "Accept" : "Ignore"}
              </button>
            );
          };
          return (
            <li key={f.id} data-risk-id={f.id} className="flex flex-col gap-1.5 px-3 py-2 [&+li]:border-t [&+li]:border-[var(--cs-border)]">
              {onShowRisk ? (
                <button
                  type="button"
                  onClick={() => onShowRisk(f)}
                  title="Show it on the canvas"
                  className="-mx-1 rounded-md px-1 text-left hover:bg-[var(--cs-hover)]"
                >
                  {f.message}
                </button>
              ) : (
                <span>{f.message}</span>
              )}
              <div className="flex items-center gap-1.5">
                <span className="min-w-0 flex-1 text-[var(--cs-muted)]">
                  {f.fix.title} · {RISK_STATUS[f.status]}
                  {f.fix.resolves ? "" : " · makes it smaller"}
                </span>
                {onReviewRisk && (
                  <>
                    {decision("accept")}
                    {decision("reject")}
                  </>
                )}
              </div>
              {onReviewRisk && riskError?.flagId === f.id && <RiskReviewNote text={riskError.text} />}
            </li>
          );
        })}
      </ol>
      {onReviewRisk && waiting && (
        <p id={noteId} className="border-t border-[var(--cs-border)] px-3 py-2 text-[var(--cs-muted)]">
          {waiting}
        </p>
      )}
    </section>
  );
}

/**
 * Claude's question with its options, and the conflict behind it when the constraints can't
 * all be met.
 */
function QuestionView({
  block,
  busy,
  onAnswer,
}: {
  block: Extract<Block, { kind: "question" }>;
  busy: boolean;
  onAnswer: Props["onAnswer"];
}) {
  const readOnly = useContext(ReadOnly);
  const answered = block.options.find((o) => o.id === block.answer);
  return (
    <div role="group" aria-label={block.text} className="flex flex-col gap-2">
      <p className="font-[500]">{block.text}</p>
      {block.conflict && <ConflictCard conflict={block.conflict} />}
      {readOnly ? (
        // A shared link can't be answered: no options, only what was chosen and why.
        <p data-testid="question-read-only" className="text-[13px] text-[var(--cs-muted)]">
          {answered ? `Answered: ${answered.label}. ` : ""}
          {readOnly}.
        </p>
      ) : (
        <QuestionOptions question={block} busy={busy} onAnswer={onAnswer} />
      )}
    </div>
  );
}

/**
 * A question's option buttons. They are disabled while Claude is still replying, once
 * answered, and once Claude went on past it (a reopened session's resumed run); each then says why, in a tooltip and in a note linked by aria-describedby. The
 * chosen option keeps its check mark but shows a not-allowed cursor.
 */
export function QuestionOptions({
  question,
  busy,
  onAnswer,
}: {
  question: Pick<Extract<Block, { kind: "question" }>, "questionId" | "options" | "answer" | "passed">;
  busy: boolean;
  onAnswer: Props["onAnswer"];
}) {
  const noteId = useId();
  const reason = question.answer
    ? "You answered this question."
    : question.passed
      ? "Claude already went on past this question."
      : busy
        ? "Claude is still replying."
        : null;
  return (
    <>
      <div className="flex flex-col gap-1.5">
        {question.options.map((o) => {
          const chosen = question.answer === o.id;
          return (
            <button
              key={o.id}
              type="button"
              aria-pressed={chosen}
              disabled={!!reason}
              title={reason ?? undefined}
              aria-describedby={reason ? noteId : undefined}
              onClick={() => onAnswer(question.questionId, o)}
              className={`flex items-start gap-2 rounded-lg border px-3 py-2 text-left disabled:cursor-not-allowed ${
                chosen
                  ? "border-[var(--cs-accent)] bg-[var(--cs-accent)]/10"
                  : "border-[var(--cs-border)] enabled:hover:bg-[var(--cs-hover)] disabled:opacity-50"
              }`}
            >
              <span className="min-w-0 flex-1">
                <span className="block text-[14px]">{o.label}</span>
                {o.description && <span className="block text-[13px] text-[var(--cs-muted)]">{o.description}</span>}
              </span>
              {chosen && (
                <Check
                  aria-hidden
                  data-testid="chosen-mark"
                  className="mt-1 size-3.5 shrink-0 text-[var(--cs-accent)]"
                />
              )}
            </button>
          );
        })}
      </div>
      {reason && (
        <p id={noteId} className="text-[13px] text-[var(--cs-muted)]">
          {reason}
        </p>
      )}
    </>
  );
}
