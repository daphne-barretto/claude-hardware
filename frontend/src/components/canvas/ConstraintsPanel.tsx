"use client";

import { useId, useRef, useState, type ReactNode } from "react";
import { BookOpen, Check, ChevronRight, ListChecks, Circle, CircleDashed, CircleMinus, MessageSquareQuote, Pencil, Undo2, X } from "lucide-react";
import type { Constraint } from "@/lib/session-api";
import type { CheckReport, CheckResult } from "./api";
import { evidence, type LocatedResult } from "./checks";
import {
  constraintGroups,
  groupName,
  placeLabel,
  requiredValue,
  STATUS_TEXT,
  type ConstraintGroup,
  type GroupStatus,
} from "./constraintGroups";
import { useConstraints } from "./constraintsContext";
import { summaryLine } from "./history";
import { PIN_FILTER_LABELS, PIN_FILTERS, type PinFilter } from "./pinVisibility";
import { lengthsInText, type LengthUnit } from "./units";

/** Where a constraint came from, in full: the rule in the session's constraint set, the user's words, or an edit. */
export function sourceText(c: Constraint, setName?: string | null) {
  const set = setName || "the constraint set";
  if (c.source === "user") return `From your words: “${c.quote ?? ""}”`;
  if (c.source === "edit") return `Edited by you. Set value: ${valueText(c, c.standard_value)} (${set})`;
  return `Constraint ${c.id} in ${set}`;
}

function valueText(c: Constraint, value: number | string) {
  if (c.kind === "spec") return String(value);
  return `${value}${c.unit === "deg" ? "°" : ` ${c.unit}`}`;
}

export function StatusIcon({ status, className = "size-3" }: { status: GroupStatus | CheckResult["status"]; className?: string }) {
  switch (status) {
    case "fail":
      return <X aria-hidden className={`${className} shrink-0 text-[var(--cs-accent)]`} />;
    case "pass":
      return <Check aria-hidden className={`${className} shrink-0 text-[#5fae7a]`} />;
    case "untested":
      return <CircleDashed aria-hidden className={`${className} shrink-0 text-[var(--cs-muted)]`} />;
    case "not_applicable":
      return <CircleMinus aria-hidden className={`${className} shrink-0 text-[var(--cs-muted)]`} />;
    default:
      return <Circle aria-hidden className={`${className} shrink-0 text-[var(--cs-muted)]`} />;
  }
}

/**
 * The design's constraints and how the build meets them, in one panel (the canvas toolbar's
 * Constraints button; before a first build, on the empty canvas): the summary of the checks and
 * the board revision they ran against, the check pins filter, and one row per constraint (and per
 * other check with results), failing first, with its status, required value and how many places
 * the check tested. A row expands to edit the value as the constraint set allows, to say where
 * the value comes from, and to list each place it is checked with its measured value; a place
 * selects and locates that result on the model (`onSelect`), as its pin does.
 *
 * With no report (nothing built yet, or the checks still running) the constraints show as
 * "not checked yet".
 */
export function ConstraintsPanel({
  report,
  unit,
  against,
  error = null,
  run,
  onRun,
  selectedId = null,
  onSelect,
  pinFilter,
  onPinFilter,
  className = "",
  header,
}: {
  report: CheckReport | null;
  unit: LengthUnit;
  /** The board revision the checks ran against ("Against <board> <revision>"), if any. */
  against?: string | null;
  error?: string | null;
  /** How many times the checks have been run again; changes on each run. */
  run?: number;
  /** Runs the checks again; without it, there's no build to check. */
  onRun?: () => void;
  selectedId?: string | null;
  /** Selects and locates a place on the model, as a pin does. */
  onSelect?: (result: LocatedResult) => void;
  /** Which check pins show on the model, and a change of it; without them, no filter. */
  pinFilter?: PinFilter;
  onPinFilter?: (filter: PinFilter) => void;
  className?: string;
  /** Extra controls in the header (a Close button). */
  header?: ReactNode;
}) {
  const { constraints, onChange, disabledReason, setName } = useConstraints();
  const groups = constraintGroups(constraints, report, unit);
  const [openKey, setOpenKey] = useState<string | null>(null);
  const [showNotApplicable, setShowNotApplicable] = useState(false);
  const rows = useRef(new Map<string, HTMLButtonElement>());
  const notApplicable = groups.filter((g) => g.status === "not_applicable");
  const shown = showNotApplicable ? groups : groups.filter((g) => g.status !== "not_applicable");
  const selectedKey = selectedId ? groups.find((g) => g.results.some((r) => r.id === selectedId))?.key : undefined;
  const building = !!onRun;

  const collapse = (key: string) => {
    setOpenKey(null);
    rows.current.get(key)?.focus();
  };

  return (
    <section
      aria-label="Constraints"
      data-testid="constraints-panel"
      data-run={run}
      data-units={unit}
      data-pin-cover=""
      className={`cs-scroll flex max-h-[calc(100dvh-10rem)] w-[22rem] max-w-[calc(100cqw-1.5rem)] flex-col overflow-y-auto rounded-xl border border-[var(--cs-border)] bg-[#262626] p-3 text-[13px] shadow-xl ${className}`}
    >
      <div className="flex items-center gap-2">
        <h2 className="flex-1 font-semibold">Constraints</h2>
        {pinFilter && onPinFilter && (
          <div
            role="radiogroup"
            aria-label="Check pins"
            title="Which checks show a pin on the model"
            className="flex h-7 shrink-0 items-center rounded-md bg-[var(--cs-surface-2)] px-0.5 text-[12px]"
          >
            {PIN_FILTERS.map((f) => (
              <button
                key={f}
                type="button"
                role="radio"
                aria-checked={pinFilter === f}
                onClick={() => onPinFilter(f)}
                className={`h-6 whitespace-nowrap rounded px-1.5 ${pinFilter === f ? "bg-[var(--cs-surface)] text-[var(--cs-text)]" : "text-[var(--cs-muted)] hover:text-[var(--cs-text)]"}`}
              >
                {PIN_FILTER_LABELS[f]}
              </button>
            ))}
          </div>
        )}
        {header}
      </div>
      <div className="mt-1">
        {error ? (
          // Checks run on every build; loading them is the only thing that can fail and be retried.
          <p className="flex items-center gap-2 text-[var(--cs-accent)]">
            <span className="min-w-0 flex-1">{error}</span>
            {onRun && (
              <button
                type="button"
                onClick={onRun}
                className="h-7 shrink-0 rounded-md px-2 text-[var(--cs-muted)] hover:bg-[var(--cs-hover)] hover:text-[var(--cs-text)]"
              >
                Retry
              </button>
            )}
          </p>
        ) : report ? (
          <>
            <p data-testid="checks-summary">{summaryLine(report.summary)}</p>
            <p className="text-[var(--cs-muted)]">{against ?? "No board"}</p>
          </>
        ) : (
          <p data-testid="checks-summary" className="text-[var(--cs-muted)]">
            {building ? "Running checks…" : "Not checked yet: the checks run on the first build."}
          </p>
        )}
      </div>
      {groups.length > 0 && (
        <ul aria-label="Constraints" className="mt-2 flex flex-col gap-0.5 border-t border-[var(--cs-border)] pt-2">
          {shown.map((g) => {
            const open = g.key === openKey;
            return (
              <li key={g.key} className={open ? "rounded-lg bg-[var(--cs-surface-2)]" : ""}>
                <button
                  ref={(el) => {
                    if (el) rows.current.set(g.key, el);
                    else rows.current.delete(g.key);
                  }}
                  type="button"
                  data-testid="constraint-item"
                  data-constraint={g.key}
                  data-status={g.status}
                  data-selected={g.key === selectedKey ? "true" : undefined}
                  aria-label={groupName(g)}
                  aria-expanded={open}
                  onClick={() => setOpenKey(open ? null : g.key)}
                  className="flex min-h-8 w-full items-center gap-2 rounded-lg px-2 py-1 text-left hover:bg-[var(--cs-hover)] data-[selected]:bg-[var(--cs-active)]"
                >
                  <StatusIcon status={g.status} className="size-3.5" />
                  <span className="min-w-0 flex-1">{g.label}</span>
                  {g.places > 1 && <span className="shrink-0 text-[12px] text-[var(--cs-muted)]">{g.places} places</span>}
                  <ChevronRight aria-hidden className={`size-3.5 shrink-0 text-[var(--cs-muted)] transition-transform ${open ? "rotate-90" : ""}`} />
                </button>
                {open && (
                  <ConstraintDetail
                    group={g}
                    unit={unit}
                    selectedId={selectedId}
                    onSelect={onSelect}
                    onChange={onChange}
                    disabledReason={disabledReason}
                    setName={setName}
                    onClose={() => collapse(g.key)}
                  />
                )}
              </li>
            );
          })}
        </ul>
      )}
      <div className="mt-2 flex items-center gap-1">
        {notApplicable.length > 0 && (
          <button
            type="button"
            aria-expanded={showNotApplicable}
            onClick={() => setShowNotApplicable((v) => !v)}
            className="flex h-7 items-center gap-1.5 rounded-md px-2 text-[var(--cs-muted)] hover:bg-[var(--cs-hover)] hover:text-[var(--cs-text)]"
          >
            <CircleMinus aria-hidden className="size-3" />
            {showNotApplicable ? "Hide not applicable" : `${notApplicable.length} not applicable`}
          </button>
        )}
      </div>
    </section>
  );
}

/** A row's expanded part: the required value and its source, and the places it is checked. */
function ConstraintDetail({
  group: g,
  unit,
  selectedId,
  onSelect,
  onChange,
  disabledReason,
  setName,
  onClose,
}: {
  group: ConstraintGroup;
  unit: LengthUnit;
  selectedId: string | null;
  onSelect?: (result: LocatedResult) => void;
  onChange: (id: string, value?: number | string) => void;
  disabledReason: string | null;
  setName: string | null;
  onClose: () => void;
}) {
  const c = g.constraint;
  const required = g.results.find((r) => r.required)?.required;
  return (
    <div
      role="region"
      aria-label={`Constraint: ${g.label}`}
      data-testid="constraint-detail"
      data-constraint={g.key}
      onKeyDown={(e) => {
        if (e.key !== "Escape") return;
        // Collapses the row, not the whole panel.
        e.preventDefault();
        e.stopPropagation();
        onClose();
      }}
      className="flex flex-col gap-2 px-2 pb-2 pt-1"
    >
      <p className="text-[12px] text-[var(--cs-muted)]">{STATUS_TEXT[g.status][0].toUpperCase() + STATUS_TEXT[g.status].slice(1)}</p>
      {c ? (
        <RequiredValue constraint={c} onChange={onChange} disabledReason={disabledReason} setName={setName} />
      ) : (
        <div className="flex flex-col gap-1">
          {required && (
            <p>
              <span className="text-[var(--cs-muted)]">Required: </span>
              <span data-testid="constraint-value">{requiredValue(required, unit)}</span>
            </p>
          )}
          <p data-testid="constraint-source" className="text-[12px] text-[var(--cs-muted)]">
            {g.results.some((r) => r.standard)
              ? "Not one of this design's constraints, so it has no value to edit here."
              : "A check the app runs itself, with no value in the constraint set to edit."}
          </p>
        </div>
      )}
      {g.results.length > 0 ? (
        <ul aria-label="Places checked" className="flex flex-col gap-0.5 border-t border-[var(--cs-border)] pt-1.5">
          {g.results.map((r) => (
            <Place key={r.id} result={r} unit={unit} selected={r.id === selectedId} onSelect={onSelect} />
          ))}
        </ul>
      ) : (
        <p className="border-t border-[var(--cs-border)] pt-1.5 text-[12px] text-[var(--cs-muted)]">
          Not checked yet: the checks run on the next build.
        </p>
      )}
    </div>
  );
}

/** The required value, its editor, its source, and the way back to the set's value. */
function RequiredValue({
  constraint: c,
  onChange,
  disabledReason,
  setName,
}: {
  constraint: Constraint;
  onChange: (id: string, value?: number | string) => void;
  disabledReason: string | null;
  setName: string | null;
}) {
  const [editing, setEditing] = useState(false);
  const noteId = useId();
  const sourceId = useId();
  const disabled = !!disabledReason;
  const Icon = c.source === "standard" ? BookOpen : c.source === "user" ? MessageSquareQuote : Pencil;
  return (
    <div className="flex flex-col gap-1.5">
      {editing && !disabled ? (
        <ValueEditor
          constraint={c}
          onCancel={() => setEditing(false)}
          onSave={(value) => {
            setEditing(false);
            onChange(c.id, value);
          }}
        />
      ) : (
        <div className="flex items-center gap-2">
          <p className="min-w-0 flex-1">
            <span className="text-[var(--cs-muted)]">Required: </span>
            <span data-testid="constraint-value">{c.label}</span>
          </p>
          <button
            type="button"
            aria-label={`Edit ${c.title}`}
            aria-describedby={disabled ? `${sourceId} ${noteId}` : sourceId}
            title={disabled ? disabledReason! : "Edit the value: the design is rebuilt and checked again"}
            disabled={disabled}
            onClick={() => setEditing(true)}
            className="flex h-6 shrink-0 items-center gap-1 rounded-md bg-[var(--cs-surface)] px-2 text-[12px] enabled:hover:bg-[#3a3a3a] disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Pencil aria-hidden className="size-3" />
            Edit
          </button>
        </div>
      )}
      <p id={sourceId} data-testid="constraint-source" data-source={c.source} className="flex items-start gap-1.5 text-[12px] text-[var(--cs-muted)]">
        <Icon aria-hidden className="mt-0.5 size-3 shrink-0" />
        <span>{sourceText(c, setName)}</span>
      </p>
      {c.source !== "standard" && (
        <button
          type="button"
          aria-describedby={disabled ? noteId : undefined}
          title={disabled ? disabledReason! : `Drop your value: ${valueText(c, c.standard_value)} from the constraint set applies again`}
          disabled={disabled}
          onClick={() => onChange(c.id)}
          className="flex h-6 items-center gap-1.5 self-start rounded-md px-1.5 text-[12px] text-[var(--cs-muted)] enabled:hover:bg-[var(--cs-hover)] enabled:hover:text-[var(--cs-text)] disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Undo2 aria-hidden className="size-3" />
          Back to the set&apos;s value
        </button>
      )}
      {disabled && (
        <p id={noteId} className="text-[12px] text-[var(--cs-muted)]">
          {disabledReason}
        </p>
      )}
    </div>
  );
}

/** The value's editor, as the constraint chips had it: a number for a limit, text for a spec. */
function ValueEditor({
  constraint: c,
  onSave,
  onCancel,
}: {
  constraint: Constraint;
  onSave: (value: number | string) => void;
  onCancel: () => void;
}) {
  const [draft, setDraft] = useState(String(c.value));
  const save = () => {
    const text = draft.trim();
    if (!text) return;
    const n = Number(text);
    onSave(c.kind !== "spec" && Number.isFinite(n) ? n : text);
  };
  return (
    <form
      data-testid="constraint-editor"
      onSubmit={(e) => {
        e.preventDefault();
        save();
      }}
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          // Closes the editor, not the row.
          e.preventDefault();
          e.stopPropagation();
          onCancel();
        }
      }}
      className="flex items-center gap-1 rounded-md border border-[var(--cs-accent)] px-1.5 py-1 text-[12px]"
    >
      <span className="min-w-0 truncate text-[var(--cs-muted)]">{c.title}</span>
      <input
        aria-label={c.title}
        autoFocus
        value={draft}
        inputMode={c.kind === "spec" ? "text" : "decimal"}
        onChange={(e) => setDraft(e.target.value)}
        className={`min-w-0 rounded bg-[var(--cs-surface)] px-1 outline-none focus:ring-1 focus:ring-[var(--cs-accent)] ${c.kind === "spec" ? "w-36" : "w-14"}`}
      />
      {c.unit && <span className="text-[var(--cs-muted)]">{c.unit === "deg" ? "°" : c.unit}</span>}
      <button type="submit" aria-label={`Save ${c.title}`} className="grid size-5 shrink-0 place-items-center rounded hover:bg-[var(--cs-hover)]">
        <Check className="size-3" />
      </button>
      <button type="button" aria-label="Cancel" onClick={onCancel} className="grid size-5 shrink-0 place-items-center rounded hover:bg-[var(--cs-hover)]">
        <X className="size-3" />
      </button>
    </form>
  );
}

/** One place the constraint is checked: a located result selects and locates it on the model. */
function Place({
  result: r,
  unit,
  selected,
  onSelect,
}: {
  result: CheckResult;
  unit: LengthUnit;
  selected: boolean;
  onSelect?: (result: LocatedResult) => void;
}) {
  const values = evidence(r, unit);
  const located = (r.status === "pass" || r.status === "fail") && !!r.location;
  const head = (
    <span className="flex items-center gap-1.5">
      <span role="img" aria-label={r.status} className="grid place-items-center">
        <StatusIcon status={r.status} />
      </span>
      <span className="min-w-0 truncate">{placeLabel(r)}</span>
    </span>
  );
  const body = (
    <>
      {values && (
        <span data-testid="check-values" className="block pl-[18px] text-[12px] text-[var(--cs-muted)]">
          {values}
        </span>
      )}
      {(!located || r.status === "fail") && (
        <span className="block pl-[18px] text-[12px] text-[var(--cs-faint,var(--cs-muted))]">
          {lengthsInText(r.reason ?? r.summary, unit)}
        </span>
      )}
    </>
  );
  if (located && onSelect)
    return (
      <li>
        <button
          type="button"
          data-testid="constraint-place"
          data-check={r.id}
          data-status={r.status}
          aria-pressed={selected}
          title="Show it on the model"
          onClick={() => onSelect(r as LocatedResult)}
          className="flex w-full flex-col rounded-md px-1.5 py-1 text-left hover:bg-[var(--cs-hover)] aria-pressed:bg-[var(--cs-active)]"
        >
          {head}
          {body}
        </button>
      </li>
    );
  return (
    <li data-testid="constraint-place" data-check={r.id} data-status={r.status} className="flex flex-col px-1.5 py-1">
      {head}
      {body}
    </li>
  );
}

/**
 * The Constraints panel on the empty canvas, before a first build: the constraints the user's
 * words and the constraint set give, not checked yet, to see and edit before anything is built.
 * Its button (top right, where the canvas toolbar's Constraints button is once there's a build)
 * shows and hides it; it starts open.
 */
export function ConstraintsBeforeBuild() {
  const [open, setOpen] = useState(true);
  return (
    <div data-menu-host="" className="pointer-events-none absolute right-3 top-3 z-20 flex flex-col items-end">
      <button
        type="button"
        aria-label="Constraints"
        aria-expanded={open}
        title="Constraints"
        onClick={() => setOpen((o) => !o)}
        className="pointer-events-auto grid size-8 place-items-center rounded-lg bg-[var(--cs-surface-2)] text-[var(--cs-muted)] hover:text-[var(--cs-text)] aria-expanded:bg-[var(--cs-active)] aria-expanded:text-[var(--cs-text)]"
      >
        <ListChecks className="size-4" />
      </button>
      {open && <ConstraintsPanel report={null} unit="mm" className="pointer-events-auto mt-1.5" />}
    </div>
  );
}
