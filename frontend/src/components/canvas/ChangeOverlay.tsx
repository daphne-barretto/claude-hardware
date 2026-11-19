"use client";

import { useRef, useState } from "react";
import { Check, X } from "lucide-react";
import { reportAction } from "@/lib/session-api";
import { reviewChanges, type CheckReport } from "./api";
import { changeAnchor, changeGhost, changesSummary, proofText, reviewFor, type Change, type ChangeSet, type Point3 } from "./changes";
import { spreadPins, type ScreenPoint } from "./checks";
import { useFitInside } from "./fitPanel";
import type { LengthUnit } from "./units";
import { shownError } from "@/lib/errors";

/** How far above its point a change pin sits, so the check pin on the same spot stays clickable. */
const PIN_LIFT = 26;

const STATUS_LABEL: Record<Change["status"], string> = { proposed: "Proposed", accepted: "Accepted", rejected: "Rejected" };

/**
 * Tracked changes on the model: a ghost of each proposed change (a dashed outline where the
 * element goes, and for a move where it was, with a dimension callout) and a numbered pin per
 * change. A pin selects its change (`onSelect`).
 */
export function ChangePins({
  changeSet,
  report,
  unit,
  selectedId,
  onSelect,
  toScreen,
}: {
  changeSet: ChangeSet;
  /** The shown build's checks, which place each change on the model; null hides the pins. */
  report: CheckReport | null;
  unit: LengthUnit;
  selectedId: string | null;
  onSelect: (change: Change, at: Point3) => void;
  /** Projects a board point to viewport pixels; changes identity whenever the camera moves. */
  toScreen: (point: Point3) => ScreenPoint | null;
}) {
  const changes = changeSet.changes;
  const anchors = new Map(changes.map((c) => [c.id, report ? changeAnchor(c, report) : null]));
  const pins = spreadPins(
    changes.flatMap((c) => {
      const p = anchors.get(c.id);
      const at = p && toScreen(p);
      return at ? [{ id: c.id, at: { x: at.x, y: at.y - PIN_LIFT } }] : [];
    }),
  );

  return (
    <>
      <svg data-testid="change-ghosts" className="pointer-events-none absolute inset-0 size-full overflow-visible">
        {changes.map((c) => {
          const p = anchors.get(c.id);
          if (!p || c.status !== "proposed") return null;
          const ghost = changeGhost(c, p, unit);
          const at = toScreen(ghost.at);
          if (!at) return null;
          const from = ghost.from && toScreen(ghost.from);
          const color = ghost.kind === "removed" ? "#e06c6c" : "#d97757";
          const box = (q: ScreenPoint, faint = false) => (
            <rect
              x={q.x - 8}
              y={q.y - 8}
              width={16}
              height={16}
              rx={3}
              fill={faint || ghost.kind === "removed" ? "none" : color}
              fillOpacity={0.25}
              stroke={color}
              strokeOpacity={faint ? 0.6 : 1}
              strokeDasharray="3 3"
            />
          );
          return (
            <g key={c.id} data-testid="change-ghost" data-change={c.id} data-kind={ghost.kind}>
              {from && (
                <>
                  {box(from, true)}
                  <line x1={from.x} y1={from.y} x2={at.x} y2={at.y} stroke={color} strokeWidth={1.5} />
                </>
              )}
              {box(at)}
              {ghost.kind === "removed" && (
                <path d={`M${at.x - 5} ${at.y - 5}L${at.x + 5} ${at.y + 5}M${at.x + 5} ${at.y - 5}L${at.x - 5} ${at.y + 5}`} stroke={color} />
              )}
              {ghost.callout && from && (
                <text
                  data-testid="change-callout"
                  x={(from.x + at.x) / 2}
                  y={(from.y + at.y) / 2 + 26}
                  textAnchor="middle"
                  fill="#f0c4b0"
                  fontSize={11}
                  paintOrder="stroke"
                  stroke="#1f1f1f"
                  strokeWidth={3}
                >
                  {ghost.callout}
                </text>
              )}
            </g>
          );
        })}
      </svg>

      <div data-testid="change-pins" className="pointer-events-none absolute inset-0 overflow-hidden">
        {changes.map((c, i) => {
          const at = pins.get(c.id);
          const p = anchors.get(c.id);
          if (!at || !p) return null;
          return (
            <button
              key={c.id}
              type="button"
              data-testid="change-pin"
              data-change={c.id}
              data-status={c.status}
              aria-label={`Change ${i + 1}: ${c.title}`}
              aria-pressed={selectedId === c.id}
              title={c.title}
              onClick={() => onSelect(c, p)}
              // Under the check pins where they meet, so both stay clickable.
              style={{ left: at.x, top: at.y, zIndex: 0 }}
              className={`pointer-events-auto absolute grid size-5 -translate-x-1/2 -translate-y-1/2 place-items-center rounded-full border-2 border-[#1f1f1f] text-[11px] font-bold shadow aria-pressed:ring-2 aria-pressed:ring-white ${
                c.status === "accepted"
                  ? "bg-[#5fae7a] text-[#1f1f1f]"
                  : c.status === "rejected"
                    ? "bg-[#5a5a5a] text-[var(--cs-muted)]"
                    : "bg-[#f0a27a] text-[#1f1f1f]"
              }`}
            >
              {i + 1}
            </button>
          );
        })}
      </div>
    </>
  );
}

/**
 * The change panel: the proposal's summary and checks, and each change with its reason, the
 * checks that prove it, its status, and reject and accept. A review calls the session API; the
 * rebuild arrives as a new build, which the canvas then shows.
 */
export function ChangePanel({
  sessionId,
  changeSet,
  busy,
  selectedId,
  onReviewed,
  className,
}: {
  sessionId: string;
  changeSet: ChangeSet;
  /** Claude is running: reviews wait. */
  busy: boolean;
  selectedId: string | null;
  onReviewed: (set: ChangeSet) => void;
  className: string;
}) {
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Opened from the Changes button: on a phone it would run past the screen's right edge.
  const ref = useRef<HTMLElement>(null);
  useFitInside(ref);
  const review = (change: Change, decision: "accept" | "reject") => {
    setPending(change.id);
    setError(null);
    reportAction(sessionId, { kind: "change_decision", change_id: change.id, decision });
    reviewChanges(sessionId, reviewFor(change, decision))
      .then(onReviewed)
      .catch((e: Error) => setError(shownError(e)))
      .finally(() => setPending(null));
  };
  const why = busy ? "Claude is working: review when the run has finished" : pending ? "Applying the review" : undefined;

  return (
    <section ref={ref} aria-label="Proposed changes" data-testid="change-panel" data-build={changeSet.build} className={className}>
      <p className="text-[12px] text-[var(--cs-muted)]">Proposed by Claude</p>
      <h2 data-testid="change-summary" className="font-semibold">
        {changesSummary(changeSet)}
      </h2>
      <p data-testid="change-checks" className="text-[12px] text-[var(--cs-muted)]">
        Checks: {changeSet.checks.pass} pass · {changeSet.checks.fail} fail · {changeSet.checks.untested} untested
      </p>
      <ol aria-label="Changes to review" className="cs-scroll mt-2 flex max-h-[min(50vh,24rem)] flex-col overflow-y-auto">
        {changeSet.changes.map((c, i) => {
          const proof = proofText(c);
          return (
            <li
              key={c.id}
              data-change={c.id}
              data-status={c.status}
              data-selected={selectedId === c.id}
              className={`flex gap-2 border-t border-[var(--cs-border)] px-1 py-2 first:border-t-0 ${selectedId === c.id ? "rounded-md bg-[var(--cs-hover)]" : ""}`}
            >
              <span className="mt-0.5 grid size-4 shrink-0 place-items-center rounded-full bg-[var(--cs-surface)] text-[10px]">{i + 1}</span>
              <div className="min-w-0 flex-1">
                <p className="leading-snug">{c.title}</p>
                <p title={c.reason} className="line-clamp-2 text-[12px] text-[var(--cs-muted)]">
                  {c.reason}
                </p>
                {proof && <p className="mt-0.5 truncate text-[11px] text-[var(--cs-muted)]">{proof}</p>}
                <p data-testid="change-status" className="mt-0.5 text-[11px] text-[var(--cs-muted)]">
                  {pending === c.id ? "Rebuilding…" : STATUS_LABEL[c.status]}
                </p>
              </div>
              <div className="flex shrink-0 items-start gap-0.5">
                <ReviewButton label={`Reject change ${i + 1}`} done={c.status === "rejected" && "Already rejected"} why={why} onClick={() => review(c, "reject")}>
                  <X className="size-3.5" />
                </ReviewButton>
                <ReviewButton label={`Accept change ${i + 1}`} done={c.status === "accepted" && "Already accepted"} why={why} onClick={() => review(c, "accept")}>
                  <Check className="size-3.5" />
                </ReviewButton>
              </div>
            </li>
          );
        })}
      </ol>
      {error && (
        <p role="alert" className="mt-1 text-[12px] text-[var(--cs-accent)]">
          {error}
        </p>
      )}
    </section>
  );
}

function ReviewButton({
  label,
  done,
  why,
  onClick,
  children,
}: {
  label: string;
  /** Why the button is off because the change already has this status, or false. */
  done: string | false;
  why: string | undefined;
  onClick: () => void;
  children: React.ReactNode;
}) {
  const reason = done || why;
  return (
    <button
      type="button"
      aria-label={label}
      title={reason || label}
      disabled={!!reason}
      onClick={onClick}
      className={`grid size-6 place-items-center rounded-md hover:bg-[var(--cs-hover)] disabled:cursor-not-allowed disabled:opacity-50 ${
        done ? "text-[var(--cs-text)]" : "text-[var(--cs-muted)] hover:text-[var(--cs-text)]"
      }`}
    >
      {children}
    </button>
  );
}
