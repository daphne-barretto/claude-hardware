"use client";

import { useEffect } from "react";
import { AlertTriangle, Redo2, Undo2, Wand2, X } from "lucide-react";
import type { Build, HistoryMove } from "./api";
import { failureText, moveText } from "./designHistory";

const action =
  "flex h-7 items-center gap-1.5 rounded-md px-2 text-[12px] hover:bg-[var(--cs-hover)] disabled:cursor-not-allowed disabled:opacity-50";

/** How long the undo toast stays. */
export const TOAST_MS = 10_000;

/**
 * The newest build failed (the canvas keeps showing the last good model): what couldn't be
 * built and why, in plain words, with Undo and Ask Claude to fix.
 */
export function FailedBuildBanner({
  build,
  onUndo,
  undoWhy,
  onFix,
  fixWhy,
}: {
  build: Build;
  onUndo: () => void;
  /** Why undo can't be used now. */
  undoWhy?: string;
  onFix?: () => void;
  fixWhy?: string;
}) {
  return (
    <div
      role="alert"
      aria-label="Build failed"
      data-testid="failed-build"
      className="pointer-events-auto flex max-w-[min(560px,calc(100%-2rem))] flex-wrap items-center gap-2 rounded-xl border border-[#5c2b29] bg-[#2a1716] px-3 py-2 text-[13px] text-[#f2b8b5] shadow-lg"
    >
      <AlertTriangle aria-hidden className="size-4 shrink-0" />
      <p className="min-w-0 flex-1">
        {failureText(build)} <span className="text-[var(--cs-muted)]">Showing the last model that built.</span>
      </p>
      <div className="flex shrink-0 gap-1 text-[var(--cs-text)]">
        <button type="button" onClick={onUndo} disabled={!!undoWhy} title={undoWhy ?? "Undo the change"} className={action}>
          <Undo2 aria-hidden className="size-3.5" />
          Undo
        </button>
        {onFix && (
          <button type="button" onClick={onFix} disabled={!!fixWhy} title={fixWhy ?? "Ask Claude to fix the design"} className={action}>
            <Wand2 aria-hidden className="size-3.5" />
            Ask Claude to fix
          </button>
        )}
      </div>
    </div>
  );
}

/**
 * After an undo or redo: "Undid: You: push/pull 1 mm" with Redo (or Undo after a redo), or why it
 * couldn't be done. It goes away by itself.
 */
export function HistoryToast({
  move,
  error,
  onAgain,
  againWhy,
  onDismiss,
}: {
  move: HistoryMove | null;
  error: string | null;
  /** Redo after an undo, undo after a redo. */
  onAgain: () => void;
  againWhy?: string;
  onDismiss: () => void;
}) {
  const key = move ? `${move.direction}:${move.step.id}:${move.build.id}` : error;
  useEffect(() => {
    if (!key) return;
    const timer = setTimeout(onDismiss, TOAST_MS);
    return () => clearTimeout(timer);
  }, [key, onDismiss]);
  if (!move && !error) return null;
  const again = move?.direction === "undo" ? "Redo" : "Undo";
  return (
    <div
      role="status"
      aria-label="Design history"
      data-testid="history-toast"
      className="pointer-events-auto flex max-w-[min(480px,calc(100%-2rem))] items-center gap-2 rounded-xl border border-[var(--cs-border)] bg-[#262626] px-3 py-2 text-[13px] shadow-xl"
    >
      <p className="min-w-0 flex-1 truncate">{move ? moveText(move) : error}</p>
      {move && (
        <button type="button" onClick={onAgain} disabled={!!againWhy} title={againWhy} className={action}>
          {again === "Redo" ? <Redo2 aria-hidden className="size-3.5" /> : <Undo2 aria-hidden className="size-3.5" />}
          {again}
        </button>
      )}
      <button type="button" aria-label="Dismiss" onClick={onDismiss} className="grid size-6 place-items-center rounded-md text-[var(--cs-muted)] hover:bg-[var(--cs-hover)]">
        <X aria-hidden className="size-3.5" />
      </button>
    </div>
  );
}
