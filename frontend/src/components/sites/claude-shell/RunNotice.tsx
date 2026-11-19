"use client";

// What the chat shows when a limit or failure is hit (backend app.agent.limits): a notice while
// the reply goes on (retrying with a countdown, waiting in line, a warning), the stopped reply's
// message with Retry, and a banner while the server can't be reached. The words come from the
// server; these views add the live countdown and the controls.

import { createContext, useContext, useEffect, useState } from "react";
import { CircleAlert, Clock, Loader2, RotateCw, TriangleAlert, WifiOff } from "lucide-react";
import type { StreamStatus } from "@/lib/session-api";
import type { Block } from "./chat";
import { NoticeCard } from "./NoticeCard";

type Controls = {
  /** Retry the stopped reply; it picks up from the design and builds so far. */
  onRetry?: () => void;
  /** Stop the reply that is running, waiting or retrying. */
  onStop?: () => void;
  /** The stopped reply Retry applies to: only the latest one offers it. */
  latestStop?: Block;
  /** A Retry or Stop request is on its way. */
  acting?: boolean;
};

const RunControls = createContext<Controls>({});
export const RunControlsProvider = RunControls.Provider;

const button =
  "inline-flex items-center gap-1 rounded-md border border-[var(--cs-border)] px-2 py-0.5 text-[12px] text-[var(--cs-text)] hover:bg-[var(--cs-surface-2)] disabled:opacity-50";

/** Seconds left until `at + seconds`, ticking each second. */
function useCountdown(at: number, seconds: number | null | undefined) {
  const left = () => Math.max(0, Math.ceil((at + (seconds ?? 0) * 1000 - Date.now()) / 1000));
  const [now, setNow] = useState(left);
  useEffect(() => {
    if (!seconds) return;
    const timer = setInterval(() => setNow(left()), 1000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [at, seconds]);
  return now;
}

/** Any notice in a reply: retrying, waiting or a warning as the run goes on, or a refusal or safety card. */
export function NoticeView({ block, live }: { block: Extract<Block, { kind: "notice" }>; live: boolean }) {
  const { notice } = block;
  const { onStop, acting } = useContext(RunControls);
  const left = useCountdown(block.at, notice.kind === "retrying" ? notice.retry_in_s : null);
  // One notice family: a refusal or a safety notice is a card of its own.
  if (notice.kind === "refused" || notice.kind === "safety") return <NoticeCard notice={notice} />;
  if (notice.kind === "warning") {
    return (
      <p data-testid="run-notice" data-kind="warning" role="status" className="flex items-start gap-1.5 text-[13px] text-amber-500">
        <TriangleAlert aria-hidden className="mt-1 size-3.5 shrink-0" />
        {notice.text}
      </p>
    );
  }
  const Icon = notice.kind === "retrying" ? RotateCw : Clock;
  return (
    <div
      data-testid="run-notice"
      data-kind={notice.kind}
      role="status"
      className="flex flex-col items-start gap-1.5 rounded-lg border border-[var(--cs-border)] px-3 py-2 text-[13px] text-[var(--cs-muted)]"
    >
      <p className="flex items-start gap-1.5">
        <Icon aria-hidden className="mt-1 size-3.5 shrink-0" />
        {notice.text}
      </p>
      {live && (
        <div className="flex items-center gap-2">
          {notice.kind === "retrying" && (
            <span data-testid="retry-countdown" className="flex items-center gap-1 text-[12px]">
              <Loader2 aria-hidden className="size-3 animate-spin" />
              {left > 0 ? `Retrying in ${left} s` : "Retrying…"}
            </span>
          )}
          {onStop && (
            <button type="button" className={button} onClick={onStop} disabled={acting}>
              Stop
            </button>
          )}
        </div>
      )}
    </div>
  );
}

/** The message of a reply that stopped, with Retry when it can pick up where it left off. */
export function StoppedView({ block }: { block: Extract<Block, { kind: "error" }> }) {
  const { onRetry, latestStop, acting } = useContext(RunControls);
  const offer = block.retryable && onRetry && latestStop === block;
  return (
    <div role="alert" data-testid="run-stopped" data-reason={block.reason ?? undefined} className="flex flex-col items-start gap-1.5 text-[13px] text-red-400">
      <p className="flex items-start gap-1.5">
        <CircleAlert aria-hidden className="mt-1 size-3.5 shrink-0" />
        {block.text}
      </p>
      {offer && (
        <button type="button" className={button} onClick={onRetry} disabled={acting}>
          <RotateCw aria-hidden className="size-3" />
          Retry
        </button>
      )}
    </div>
  );
}

/** The event stream dropped: a quiet line while it reconnects, a banner while the server can't be reached. */
export function ConnectionBanner({ status, onRetryNow }: { status: StreamStatus | null; onRetryNow: () => void }) {
  if (status === "reconnecting") {
    return (
      <p data-testid="connection-status" role="status" className="flex items-center justify-center gap-1.5 py-1 text-[12px] text-[var(--cs-muted)]">
        <Loader2 aria-hidden className="size-3 animate-spin" />
        Reconnecting…
      </p>
    );
  }
  if (status !== "offline") return null;
  return (
    <div
      data-testid="connection-status"
      role="alert"
      className="mx-auto my-2 flex max-w-[720px] items-center gap-2 rounded-lg border border-[var(--cs-border)] bg-[var(--cs-surface-2)] px-3 py-2 text-[13px]"
    >
      <WifiOff aria-hidden className="size-4 shrink-0 text-[var(--cs-muted)]" />
      <span className="flex-1">Can&apos;t reach the server right now. Your work is saved; this chat reconnects by itself when it&apos;s back.</span>
      <button type="button" className={button} onClick={onRetryNow}>
        Try now
      </button>
    </div>
  );
}
