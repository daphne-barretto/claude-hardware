import { Lock } from "lucide-react";
import type { ConflictConstraint, ConstraintConflict, Location } from "@/lib/session-api";

const RELATION: Record<ConflictConstraint["relation"], string> = { "=": "=", ">=": "≥", "<=": "≤" };

/** Who set each kind of constraint. */
export const CONSTRAINT_SOURCE: Record<ConflictConstraint["kind"], string> = {
  locked: "locked by you",
  standard: "constraint",
  board: "board",
};

const num = (v: number) => String(Number(v.toFixed(2)));
const withUnit = (v: number, unit: string) => `${num(v)} ${unit}`;

/** A constraint as it binds: "Minimum wall thickness ≥ 2 mm", "U1 (radio module) height 2 mm". */
export function constraintText(c: ConflictConstraint) {
  if (c.kind === "board") return `${c.label} height ${withUnit(c.value, c.unit)}`;
  return `${c.label} ${RELATION[c.relation]} ${withUnit(c.value, c.unit)}`;
}

/** The best achievable value of a constraint, when only it gives way. */
export function bestText(c: ConflictConstraint) {
  return c.best == null ? "can't change" : `best ${withUnit(c.best, c.unit)}`;
}

/** Where on the model: the part, the face and the point (board coordinates, mm). */
export function locationText(l: Location) {
  const where = [l.part, l.faces?.[0]].filter(Boolean).join(", ");
  return `${where ? `${where} at ` : ""}${l.point.map(num).join(", ")} mm`;
}

/**
 * The can't-satisfy state: the constraints that clash, who set each, where on the model it
 * acts and the best value it could take if it alone gave way, and how far apart they are.
 */
export function ConflictCard({ conflict, compact = false }: { conflict: ConstraintConflict; compact?: boolean }) {
  return (
    <div
      role="region"
      aria-label="Conflict"
      data-testid="conflict-card"
      className={`rounded-xl border border-[var(--cs-accent)]/40 bg-[var(--cs-surface-2)] text-[13px] ${compact ? "p-2.5" : "p-3"}`}
    >
      <p className="text-[var(--cs-accent)]">Can&apos;t meet all {conflict.constraints.length} at once</p>
      <ul aria-label="Clashing constraints" className="mt-1.5 flex flex-col divide-y divide-[var(--cs-border)]">
        {conflict.constraints.map((c) => (
          <li
            key={`${c.kind}:${c.id}`}
            data-testid="conflict-constraint"
            data-kind={c.kind}
            data-id={c.id}
            className="flex flex-col gap-0.5 py-1.5"
          >
            <span className="flex items-center gap-1.5">
              <span data-testid="constraint-text" className="font-[500] text-[var(--cs-text)]">
                {constraintText(c)}
              </span>
              {c.kind === "locked" && <Lock aria-label="locked" className="size-3 text-[var(--cs-link)]" />}
              <span className="ml-auto shrink-0 text-[var(--cs-muted)]">{CONSTRAINT_SOURCE[c.kind]}</span>
            </span>
            <span className="text-[12px] text-[var(--cs-muted)]">
              <span data-testid="constraint-best">{bestText(c)}</span>
              {!compact &&
                c.locations.map((l, i) => (
                  <span key={i} data-testid="constraint-location">
                    {" · "}
                    {locationText(l)}
                  </span>
                ))}
            </span>
          </li>
        ))}
      </ul>
      <p data-testid="conflict-over-by" className="mt-1.5 text-[var(--cs-muted)]">
        {withUnit(conflict.over_by, "mm")} apart. Each best value holds only if that one alone gives way.
      </p>
    </div>
  );
}
