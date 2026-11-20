"use client";

import { Fragment } from "react";
import { countItems, openChecksLabel, type CheckCountStatus, type CountSummary } from "./openChecks";

/**
 * A check summary as its counts, "74 pass · 0 fail · 3 untested · 1 not applicable". With
 * `onOpen`, each count is a button that opens the Constraints panel on its group; without it
 * (no canvas to open), plain text. The text reads the same either way.
 */
export function CheckCounts({
  summary,
  onOpen,
  className = "",
}: {
  summary: CountSummary;
  onOpen?: ((status: CheckCountStatus) => void) | null;
  className?: string;
}) {
  return (
    <span data-testid="check-counts" className={className}>
      {countItems(summary).map(({ status, count, text }, i) => (
        <Fragment key={status}>
          {i > 0 && " · "}
          {onOpen ? (
            <button
              type="button"
              data-status={status}
              aria-label={openChecksLabel(status, count)}
              title="Open in the Constraints panel"
              onClick={(e) => {
                // A count inside a clickable card opens the panel, not the card.
                e.stopPropagation();
                onOpen(status);
              }}
              className="-mx-0.5 cursor-pointer rounded px-0.5 underline-offset-2 hover:bg-[var(--cs-hover)] hover:text-[var(--cs-text)] hover:underline focus-visible:outline focus-visible:outline-1 focus-visible:outline-[var(--cs-muted)]"
            >
              {text}
            </button>
          ) : (
            text
          )}
        </Fragment>
      ))}
    </span>
  );
}
