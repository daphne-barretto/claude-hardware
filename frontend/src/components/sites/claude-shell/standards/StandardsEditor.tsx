"use client";

import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { ChevronDown, ChevronRight, Copy, Lock, Plus, Trash2, X } from "lucide-react";
import { listStandards, type StandardsSet } from "@/lib/session-api";
import {
  createStandardsSet,
  deleteStandardsSet,
  listKinds,
  StandardsRejected,
  updateStandardsSet,
  type CheckKind,
  type StandardsProblem,
} from "@/lib/standards-api";
import {
  APPLIES_TO_LABELS,
  availableKinds,
  changed,
  copyName,
  newDraft,
  newSetName,
  plainLabel,
  problemsByRule,
  toDraft,
  toInput,
  valueProblem,
  valueText,
  withValue,
  type AppliesTo,
  type RuleDraft,
} from "./rules";
import { shownError } from "@/lib/errors";

export const MANAGE_STANDARDS = "Manage constraint sets…";
export const EDITOR_LABEL = "Manage constraint sets";
export const NEW_KIND_EXPLAINED =
  "Every rule needs a check that measures the model, so a new kind of rule needs new code in the app: a check, " +
  "then an entry in this catalogue. Ask the team that maintains Claude Hardware for the check you need; once it " +
  "exists it appears here. Until then the editor doesn't offer it, because nothing would enforce it.";

type Props = {
  /** The set to show first (the chosen one), if any. */
  initial: string | null;
  /** The set the chat uses (chosen on New, or the session's). */
  active: string | null;
  /** Use a set for this chat (choose it on New, or switch the session to it). */
  onUse: (id: string) => void;
  /** A set was saved: if the chat uses it, the session rechecks against it. */
  onSaved?: (set: StandardsSet) => void;
  onClose: () => void;
};

const field =
  "h-8 rounded-md border border-[var(--cs-border)] bg-[var(--cs-surface-2)] px-2 text-[13px] outline-none focus:border-[var(--cs-link)] disabled:cursor-not-allowed disabled:opacity-50";
const button =
  "flex h-8 items-center gap-1.5 rounded-lg px-2.5 text-[13px] enabled:hover:bg-[var(--cs-hover)] disabled:cursor-not-allowed disabled:opacity-50";

/**
 * The standards editor (docs/constraints.md): the sets on the left (the built-in team set,
 * read-only, and the browser's own), the chosen set's rules on the right in plain language.
 * The user duplicates or starts a set, edits values, turns rules on and off, adds rules from the
 * catalogue of checks the app can enforce, and saves; the API validates everything.
 */
export function StandardsEditor({ initial, active, onUse, onSaved, onClose }: Props) {
  const [sets, setSets] = useState<StandardsSet[]>([]);
  const [kinds, setKinds] = useState<CheckKind[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(initial);
  const [name, setName] = useState("");
  const [drafts, setDrafts] = useState<RuleDraft[]>([]);
  const [open, setOpen] = useState<Set<number>>(new Set());
  const [problems, setProblems] = useState<StandardsProblem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [explainNew, setExplainNew] = useState(false);
  const [saving, setSaving] = useState(false);

  const selected = sets.find((s) => s.id === selectedId) ?? sets[0] ?? null;
  const readOnly = !!selected?.built_in;
  const byCheck = useMemo(() => new Map(kinds.map((k) => [k.check, k])), [kinds]);
  const dirty = !!selected && !readOnly && changed(selected, name, drafts);
  const byRule = problemsByRule(problems);

  const show = (set: StandardsSet | null) => {
    setSelectedId(set?.id ?? null);
    setName(set?.name ?? "");
    setDrafts(set ? set.standards.map(toDraft) : []);
    setOpen(new Set());
    setProblems([]);
    setError(null);
    setAdding(false);
  };

  useEffect(() => {
    let live = true;
    Promise.all([listStandards(), listKinds()])
      .then(([s, k]) => {
        if (!live) return;
        setSets(s);
        setKinds(k);
        show(s.find((x) => x.id === initial) ?? s[0] ?? null);
      })
      .catch((e: Error) => live && setError(`Could not load the constraint sets: ${shownError(e)}`));
    return () => {
      live = false;
    };
    // Loaded once, when the editor opens.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const refresh = async (focus: string | null) => {
    const s = await listStandards();
    setSets(s);
    show(s.find((x) => x.id === focus) ?? s[0] ?? null);
  };

  const run = async (work: () => Promise<void>) => {
    setError(null);
    try {
      await work();
    } catch (e) {
      if (e instanceof StandardsRejected) setProblems(e.problems);
      setError(shownError(e));
    }
  };

  const duplicate = () =>
    selected &&
    run(async () => {
      const copy = await createStandardsSet({ name: copyName(selected.name, sets), duplicate_of: selected.id });
      await refresh(copy.id);
    });
  const createNew = () =>
    run(async () => {
      const created = await createStandardsSet({ name: newSetName(sets) });
      await refresh(created.id);
    });
  const remove = () =>
    selected &&
    run(async () => {
      await deleteStandardsSet(selected.id);
      await refresh(null);
    });
  const save = () =>
    selected &&
    run(async () => {
      setSaving(true);
      try {
        const saved = await updateStandardsSet(selected.id, {
          name,
          standards: drafts.map((d) => toInput(d, byCheck.get(d.check))),
        });
        await refresh(saved.id);
        onSaved?.(saved);
      } finally {
        setSaving(false);
      }
    });

  const edit = (index: number, change: Partial<RuleDraft>) => {
    setDrafts((ds) => ds.map((d, i) => (i === index ? { ...d, ...change } : d)));
    setProblems((ps) => ps.filter((p) => p.rule !== index));
  };
  const toggleOpen = (index: number) =>
    setOpen((o) => {
      const next = new Set(o);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  const addRule = (kind: CheckKind) => {
    setDrafts((ds) => [...ds, newDraft(kind)]);
    setAdding(false);
    setProblems([]);
  };
  const removeRule = (index: number) => {
    setDrafts((ds) => ds.filter((_, i) => i !== index));
    setOpen(new Set());
    setProblems([]);
  };

  const addable = availableKinds(kinds, drafts);
  const setProblemsList = byRule.get(-1) ?? [];
  const busyTitle = dirty ? "Save or discard your changes first" : undefined;

  // On the body, so the page around the picker (centred text, stacking) doesn't reach it.
  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4 text-left"
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={EDITOR_LABEL}
        className="flex h-[min(680px,90vh)] w-full max-w-[920px] overflow-hidden rounded-[14px] border border-[var(--cs-border)] bg-[var(--cs-surface-2)] text-[14px] shadow-2xl max-md:flex-col"
      >
        <nav aria-label="Constraint sets" className="flex w-[240px] shrink-0 flex-col border-r border-[var(--cs-border)] bg-[var(--cs-sidebar)] p-2 max-md:h-[170px] max-md:w-full max-md:border-b max-md:border-r-0">
          <p className="px-2 pb-1.5 pt-1 text-[12px] font-[520] text-[var(--cs-muted)]">Constraint sets</p>
          <ul className="cs-scroll min-h-0 flex-1 overflow-y-auto">
            {sets.map((s) => (
              <li key={s.id}>
                <button
                  type="button"
                  aria-label={s.built_in ? `${s.name}, built-in` : s.name}
                  aria-current={s.id === selected?.id ? "true" : undefined}
                  disabled={dirty && s.id !== selected?.id}
                  title={dirty && s.id !== selected?.id ? busyTitle : undefined}
                  onClick={() => show(s)}
                  className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left enabled:hover:bg-[var(--cs-hover)] disabled:cursor-not-allowed disabled:opacity-50 aria-[current=true]:bg-[var(--cs-active)]"
                >
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[13px]">{s.name}</span>
                    <span className="block text-[12px] text-[var(--cs-muted)]">
                      {s.built_in ? "Built-in · " : ""}
                      {s.standards.length} {s.standards.length === 1 ? "rule" : "rules"}
                      {s.id === active ? " · in use" : ""}
                    </span>
                  </span>
                  {s.built_in && <Lock aria-hidden className="size-3.5 shrink-0 text-[var(--cs-muted)]" />}
                </button>
              </li>
            ))}
          </ul>
          <button type="button" onClick={createNew} disabled={dirty} title={busyTitle} className={`${button} mt-1`}>
            <Plus className="size-3.5" /> New set
          </button>
        </nav>

        <section aria-label="Rules" className="flex min-w-0 flex-1 flex-col">
          <header className="flex items-center gap-2 border-b border-[var(--cs-border)] px-4 py-3">
            {selected && !readOnly ? (
              <input
                aria-label="Set name"
                value={name}
                onChange={(e) => {
                  setName(e.target.value);
                  setProblems((ps) => ps.filter((p) => p.rule !== null && p.rule !== undefined));
                }}
                className={`${field} min-w-0 flex-1 text-[15px]`}
              />
            ) : (
              <h2 className="min-w-0 flex-1 truncate text-[15px] font-[520]">{selected?.name ?? "Constraint set"}</h2>
            )}
            {selected && (
              <button type="button" onClick={duplicate} disabled={dirty} title={busyTitle} className={button}>
                <Copy className="size-3.5" /> Duplicate
              </button>
            )}
            {selected && !readOnly && (
              <button type="button" onClick={remove} className={button}>
                <Trash2 className="size-3.5" /> Delete set
              </button>
            )}
            <button
              type="button"
              aria-label="Close"
              onClick={onClose}
              className="grid size-8 place-items-center rounded-lg text-[var(--cs-muted)] hover:bg-[var(--cs-hover)] hover:text-[var(--cs-text)]"
            >
              <X className="size-4" />
            </button>
          </header>

          {readOnly && (
            <p className="border-b border-[var(--cs-border)] px-4 py-2 text-[13px] text-[var(--cs-muted)]">
              The built-in constraint set is read-only. Duplicate the set to change values, turn rules off or add rules.
            </p>
          )}
          {(error || setProblemsList.length > 0) && (
            <div role="alert" className="border-b border-[var(--cs-border)] px-4 py-2 text-[13px] text-[var(--cs-accent)]">
              {setProblemsList.length ? setProblemsList.map((p, i) => <p key={i}>{p.message}</p>) : <p>{error}</p>}
            </div>
          )}

          <div className="cs-scroll min-h-0 flex-1 overflow-y-auto px-4 py-2">
            {selected && drafts.length === 0 && (
              <p className="py-6 text-center text-[var(--cs-muted)]">No rules yet. Add one from the checks the app can enforce.</p>
            )}
            <ul aria-label="Rules in this set" className="flex flex-col">
              {drafts.map((d, i) => (
                <RuleRow
                  key={`${d.check}-${i}`}
                  draft={d}
                  kind={byCheck.get(d.check)}
                  readOnly={readOnly}
                  open={open.has(i)}
                  problems={byRule.get(i) ?? []}
                  onToggleOpen={() => toggleOpen(i)}
                  onChange={(c) => edit(i, c)}
                  onRemove={() => removeRule(i)}
                />
              ))}
            </ul>

            {selected && !readOnly && (
              <div className="mt-2">
                <button type="button" aria-expanded={adding} onClick={() => setAdding((a) => !a)} className={button}>
                  <Plus className="size-3.5" /> Add rule
                </button>
                {adding && (
                  <section aria-label="Add a rule" className="mt-1 rounded-xl border border-[var(--cs-border)] p-2">
                    {addable.length === 0 ? (
                      <p className="px-2 py-1.5 text-[13px] text-[var(--cs-muted)]">This set has a rule of every kind the app can check.</p>
                    ) : (
                      <ul className="flex flex-col">
                        {addable.map((k) => (
                          <li key={k.check}>
                            <button
                              type="button"
                              aria-label={`Add ${k.title}`}
                              onClick={() => addRule(k)}
                              className="w-full rounded-lg px-2 py-1.5 text-left hover:bg-[var(--cs-hover)]"
                            >
                              <span className="block text-[13px]">{k.title}</span>
                              <span className="block text-[12px] text-[var(--cs-muted)]">
                                {k.description}
                                {k.unit && k.min !== null && k.min !== undefined && k.max !== null && k.max !== undefined
                                  ? ` Accepts ${valueText(k.min, k.unit)} to ${valueText(k.max, k.unit)}.`
                                  : ""}
                              </span>
                            </button>
                          </li>
                        ))}
                      </ul>
                    )}
                    <button
                      type="button"
                      aria-expanded={explainNew}
                      onClick={() => setExplainNew((v) => !v)}
                      className="mt-1 px-2 py-1 text-[12px] text-[var(--cs-link)] hover:underline"
                    >
                      Request a new kind of check
                    </button>
                    {explainNew && <p className="px-2 pb-1 text-[12px] text-[var(--cs-muted)]">{NEW_KIND_EXPLAINED}</p>}
                  </section>
                )}
              </div>
            )}
          </div>

          <footer className="flex items-center gap-2 border-t border-[var(--cs-border)] px-4 py-3">
            {selected && selected.id === active ? (
              <span className="text-[12px] text-[var(--cs-muted)]">This chat uses this set.</span>
            ) : (
              selected && (
                <button
                  type="button"
                  onClick={() => onUse(selected.id)}
                  disabled={dirty}
                  title={busyTitle}
                  className={`${button} border border-[var(--cs-border)]`}
                >
                  Use in this chat
                </button>
              )
            )}
            <span className="flex-1" />
            {dirty && (
              <button type="button" onClick={() => selected && show(selected)} className={button}>
                Discard changes
              </button>
            )}
            {selected && !readOnly && (
              <button
                type="button"
                onClick={save}
                disabled={!dirty || saving}
                title={!dirty ? "No changes to save" : saving ? "Saving" : undefined}
                className="flex h-8 items-center rounded-lg bg-[var(--cs-text)] px-3 text-[13px] font-[520] text-[var(--cs-bg)] disabled:cursor-not-allowed disabled:opacity-40"
              >
                Save changes
              </button>
            )}
          </footer>
        </section>
      </div>
    </div>,
    document.body,
  );
}

function RuleRow({
  draft,
  kind,
  readOnly,
  open,
  problems,
  onToggleOpen,
  onChange,
  onRemove,
}: {
  draft: RuleDraft;
  kind: CheckKind | undefined;
  readOnly: boolean;
  open: boolean;
  problems: StandardsProblem[];
  onToggleOpen: () => void;
  onChange: (change: Partial<RuleDraft>) => void;
  onRemove: () => void;
}) {
  const title = draft.title || kind?.title || draft.check;
  const early = readOnly ? null : valueProblem(draft, kind);
  const shown = problems.length ? problems.map((p) => p.message) : early ? [early] : [];
  return (
    <li
      data-check={draft.check}
      data-enabled={draft.enabled}
      className="border-b border-[var(--cs-border)] py-2.5 last:border-b-0"
    >
      <div className="flex items-center gap-2.5">
        {!readOnly && (
          <button
            type="button"
            role="switch"
            aria-checked={draft.enabled}
            aria-label={`${title} on`}
            onClick={() => onChange({ enabled: !draft.enabled })}
            className={`relative h-5 w-9 shrink-0 rounded-full transition-colors ${draft.enabled ? "bg-[var(--cs-link)]" : "bg-[var(--cs-active)]"}`}
          >
            <span className={`absolute top-0.5 size-4 rounded-full bg-white transition-all ${draft.enabled ? "left-[18px]" : "left-0.5"}`} />
          </button>
        )}
        <div className={`min-w-0 flex-1 ${draft.enabled ? "" : "opacity-50"}`}>
          <p className="truncate text-[13px] font-[520]">{title}</p>
          <p className="truncate text-[13px] text-[var(--cs-muted)]">
            <span data-testid="rule-label">
              {plainLabel(draft, kind)}
              {draft.enabled ? "" : " · off, not checked"}
            </span>
            {` · ${APPLIES_TO_LABELS[draft.applies_to]}`}
          </p>
        </div>
        {!readOnly && (
          <>
            <label className="flex items-center gap-1 text-[12px] text-[var(--cs-muted)]">
              <input
                aria-label={`${title} value`}
                inputMode={kind?.kind === "spec" ? "text" : "decimal"}
                value={draft.value}
                onChange={(e) => onChange(withValue(draft, e.target.value, kind))}
                className={`${field} ${kind?.kind === "spec" ? "w-[150px]" : "w-[72px]"}`}
              />
              {kind?.unit === "deg" ? "°" : kind?.unit ?? ""}
            </label>
          </>
        )}
        <button
          type="button"
          aria-label={`Details of ${title}`}
          aria-expanded={open}
          onClick={onToggleOpen}
          className="grid size-7 shrink-0 place-items-center rounded-md text-[var(--cs-muted)] hover:bg-[var(--cs-hover)]"
        >
          {open ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
        </button>
        {!readOnly && (
          <button
            type="button"
            aria-label={`Remove ${title}`}
            onClick={onRemove}
            className="grid size-7 shrink-0 place-items-center rounded-md text-[var(--cs-muted)] hover:bg-[var(--cs-hover)] hover:text-[var(--cs-text)]"
          >
            <Trash2 className="size-3.5" />
          </button>
        )}
      </div>
      {shown.length > 0 && (
        <div role="alert" className="ml-11 mt-1 text-[12px] text-[var(--cs-accent)]">
          {shown.map((m, i) => (
            <p key={i}>{m}</p>
          ))}
        </div>
      )}
      {open && (
        <div className={`mt-2 grid gap-2 text-[12px] ${readOnly ? "" : "ml-11"}`}>
          <p className="text-[var(--cs-muted)]">
            Checked by {kind ? kind.check.replaceAll("_", " ") : draft.check}: {kind?.description}
          </p>
          {readOnly ? (
            <>
              <p>{draft.description}</p>
              {draft.aliases && <p className="text-[var(--cs-muted)]">Words that name it: {draft.aliases}</p>}
              {Object.entries(draft.dimensions).map(([k, v]) => (
                <p key={k} className="text-[var(--cs-muted)]">
                  {kind?.dimensions.find((x) => x.name === k)?.label ?? k}: {valueText(v, "mm")}
                </p>
              ))}
            </>
          ) : (
            <>
              <label className="grid gap-1">
                Title
                <input aria-label={`${title} title`} value={draft.title} onChange={(e) => onChange({ title: e.target.value })} className={field} />
              </label>
              <label className="grid gap-1">
                Description
                <textarea
                  aria-label={`${title} description`}
                  value={draft.description}
                  onChange={(e) => onChange({ description: e.target.value })}
                  rows={2}
                  className={`${field} h-auto py-1.5`}
                />
              </label>
              <label className="grid gap-1">
                Words that name it in a message (comma separated)
                <input aria-label={`${title} words`} value={draft.aliases} onChange={(e) => onChange({ aliases: e.target.value })} className={field} />
              </label>
              {kind?.dimensions.map((dim) => (
                <label key={dim.name} className="grid gap-1">
                  {dim.label} ({dim.unit})
                  <input
                    aria-label={`${title} ${dim.label.toLowerCase()}`}
                    inputMode="decimal"
                    value={draft.dimensions[dim.name] ?? ""}
                    onChange={(e) => onChange({ dimensions: { ...draft.dimensions, [dim.name]: e.target.value } })}
                    className={`${field} w-[120px]`}
                  />
                </label>
              ))}
              {kind?.material && (
                <label className="grid gap-1">
                  Material of the part
                  <input aria-label={`${title} material`} value={draft.material} onChange={(e) => onChange({ material: e.target.value })} className={field} />
                </label>
              )}
              <div role="radiogroup" aria-label={`${title} applies to`} className="grid gap-1">
                Applies to
                <div className="flex flex-wrap gap-1">
                  {(Object.keys(APPLIES_TO_LABELS) as AppliesTo[]).map((a) => (
                    <button
                      key={a}
                      type="button"
                      role="radio"
                      aria-checked={draft.applies_to === a}
                      onClick={() => onChange({ applies_to: a })}
                      className="h-7 rounded-md border border-[var(--cs-border)] px-2 hover:bg-[var(--cs-hover)] aria-checked:border-[var(--cs-link)] aria-checked:bg-[var(--cs-active)]"
                    >
                      {APPLIES_TO_LABELS[a]}
                    </button>
                  ))}
                </div>
              </div>
            </>
          )}
        </div>
      )}
    </li>
  );
}
