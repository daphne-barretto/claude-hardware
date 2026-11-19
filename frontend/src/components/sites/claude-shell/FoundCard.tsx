"use client";

import { Search } from "lucide-react";
import { findCount, groupMatches } from "@/components/canvas/find";
import type { Block } from "./chat";

type Props = {
  block: Extract<Block, { kind: "found" }>;
  /** Highlight the matches on the canvas; without it (no canvas) the card has no controls. */
  onShow?: () => void;
  /** Make the matches the selection the next message carries. */
  onSelect?: () => void;
};

/**
 * Faces Claude found in chat: what the query was read as, the matches by feature (the
 * canvas highlights them), and a control to send them back as the selection. A query Claude
 * didn't fully understand shows what was understood and the question.
 */
export function FoundCard({ block, onShow, onSelect }: Props) {
  const { result } = block;
  const count = findCount(result);
  const groups = groupMatches(result.matches);
  const found = result.matches.length > 0;
  return (
    <section
      aria-label="Found in model"
      data-testid="found-in-model"
      data-status={result.status}
      className="rounded-lg border border-[var(--cs-border)] text-[13px]"
    >
      <p className="flex items-center gap-2 border-b border-[var(--cs-border)] px-3 py-2">
        <Search aria-hidden className="size-3.5 shrink-0 text-[var(--cs-muted)]" />
        <span className="font-[500]">Found in model</span>
        {count && (
          <span data-testid="found-count" className="ml-auto text-[var(--cs-muted)]">
            {count}
          </span>
        )}
      </p>
      <div className="flex flex-col gap-1.5 px-3 py-2">
        <p data-testid="found-understood">
          <span className="text-[var(--cs-muted)]">Understood: </span>
          {result.understood}
        </p>
        {result.status === "unclear" && result.question && (
          <p data-testid="found-question" className="text-[var(--cs-accent)]">
            {result.question}
          </p>
        )}
        {found && (
          <ul aria-label="Matches" className="flex max-h-40 flex-col gap-0.5 overflow-y-auto">
            {groups.map((g) => (
              <li key={g.feature} data-feature={g.feature} className="flex justify-between gap-2 text-[var(--cs-muted)]">
                <span className="truncate text-[var(--cs-text)]">{g.feature}</span>
                <span className="shrink-0">
                  {g.faces.length} {g.faces.length === 1 ? "face" : "faces"} · {g.value ?? g.part}
                </span>
              </li>
            ))}
          </ul>
        )}
        {found && (onShow || onSelect) && (
          <div className="flex items-center gap-1.5">
            {onShow && (
              <button
                type="button"
                onClick={onShow}
                title="Highlight these faces on the canvas"
                className="h-7 rounded-md border border-[var(--cs-border)] px-2.5 hover:bg-[var(--cs-hover)]"
              >
                Show on model
              </button>
            )}
            {onSelect && (
              <button
                type="button"
                onClick={onSelect}
                title="Select these faces: the next message carries them"
                className="h-7 rounded-md border border-[var(--cs-border)] px-2.5 hover:bg-[var(--cs-hover)]"
              >
                Use as selection
              </button>
            )}
          </div>
        )}
      </div>
    </section>
  );
}
