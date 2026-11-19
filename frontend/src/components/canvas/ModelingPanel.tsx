"use client";

import { useEffect, useState } from "react";
import { EyeOff, Lock } from "lucide-react";
import type { Build } from "./api";
import { RebuildResult, useRebuild } from "./ParametersPanel";
import {
  AXES,
  createEdit,
  defaultEditDraft,
  deleteEdit,
  draftOf,
  editFields,
  editSummary,
  editTarget,
  fieldLabel,
  fixText,
  isLength,
  listEdits,
  parseEdit,
  PATTERN_KINDS,
  pickedEdits,
  toolOf,
  updateEdit,
  type Axis,
  type Edit,
  type EditDraft,
  type EditField,
  type EditKind,
  type PatternKind,
} from "./modeling";
import { failingChecks, formatParameter, parseParameter } from "./parameters";
import type { PartIndex, Picked } from "./selection";
import type { LengthUnit } from "./units";
import { featureOf } from "./selection";
import type { Parameters } from "./useParameterEdits";
import { shownError } from "@/lib/errors";

const radio = (on: boolean) =>
  `h-6 flex-1 rounded-md ${on ? "bg-[var(--cs-surface)] text-[var(--cs-text)]" : "text-[var(--cs-muted)] hover:text-[var(--cs-text)]"}`;
const small =
  "h-6 rounded-md bg-[var(--cs-surface-2)] px-2 text-[12px] hover:bg-[var(--cs-hover)] disabled:cursor-not-allowed disabled:opacity-50";

/**
 * The modeling tools' panel: the chosen tool's form for what is picked on the model, and
 * the hand edits in the design. Adding an edit, changing its values, suppressing or deleting it
 * is a hand edit: the edit is a locked feature in the code that Claude keeps, the design
 * rebuilds and rechecks, and the panel shows the rebuild's checks. When the rebuild fails a
 * check, Claude can be asked for a fix that keeps the edit. A picked feature made by the code
 * shows its parameters here, which are edited like the parameters panel's.
 */
export function ModelingPanel({
  sessionId,
  build,
  builds,
  picks,
  index,
  unit,
  tool,
  parameters,
  busy = false,
  fixBlocked,
  onEdited,
  onFixRequest,
}: {
  sessionId: string;
  build: Build | undefined;
  builds: Build[];
  picks: Picked[];
  index: PartIndex[];
  unit: LengthUnit;
  tool: EditKind;
  parameters: Parameters;
  /** Claude is working: the design can't change by hand until the run ends. */
  busy?: boolean;
  /** Why a fix request can't be sent now (a question is open). */
  fixBlocked?: string;
  /** An edit was saved: the canvas follows the rebuild. */
  onEdited?: () => void;
  onFixRequest?: (text: string) => void;
}) {
  const [editing, setEditing] = useState<Edit | null>(null);
  const kind = editing?.kind ?? tool;
  const [drafts, setDrafts] = useState<Record<string, EditDraft>>({});
  const draftKey = editing ? editing.tag : kind;
  const values: EditDraft = drafts[draftKey] ?? (editing ? draftOf(editing, unit) : defaultEditDraft(kind, unit));
  const [insert, setInsert] = useState(false);
  const [axis, setAxis] = useState<Axis>("x");
  const [pattern, setPattern] = useState<PatternKind>("linear");
  const options = editing
    ? { insert: !!editing.insert, axis: editing.axis ?? "x", pattern: editing.pattern ?? "linear" }
    : { insert, axis, pattern };
  const [errors, setErrors] = useState<Partial<Record<EditField, string>>>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [edit, setEdit] = useState<{ name: string; before: string[] } | null>(null);
  const [asked, setAsked] = useState<string | null>(null);
  const rebuild = useRebuild(builds, edit);

  // The hand edits in the design, reloaded after every build (an undo can take some out).
  const [edits, setEdits] = useState<Edit[]>([]);
  useEffect(() => {
    let live = true;
    listEdits(sessionId)
      .then((e) => live && setEdits(e))
      .catch(() => live && setEdits([]));
    return () => {
      live = false;
    };
  }, [sessionId, builds.length]);

  const target = editTarget(kind, picks, index);
  const picked = pickedEdits(edits, picks, index);
  // A picked feature the code made: its parameters are edited here too.
  const codeFeatures = [...new Set(picks.map((p) => featureOf(index, p)).filter((t): t is string => !!t))].filter(
    (t) => !edits.some((e) => e.tag === t || e.insert_tag === t),
  );
  const featureParams = (parameters.params ?? []).filter((p) => p.feature && codeFeatures.includes(p.feature));

  const why = editing
    ? busy
      ? "Claude is working: edit when it is done"
      : saving
        ? "Saving"
        : undefined
    : "reason" in target
      ? target.reason
      : !build
        ? "No build to edit"
        : busy
          ? "Claude is working: edit when it is done"
          : saving
            ? "Saving"
            : undefined;

  const run = (name: string, request: () => Promise<unknown>, done?: (result: unknown) => void) => {
    setError(null);
    setNotice(null);
    setAsked(null);
    setSaving(true);
    const before = builds.map((b) => b.id);
    request()
      .then((result) => {
        setEdit({ name, before });
        done?.(result);
        onEdited?.();
      })
      .catch((e: Error) => setError(shownError(e)))
      .finally(() => setSaving(false));
  };

  const submit = () => {
    if (why) return;
    const parsed = parseEdit(kind, values, options, unit);
    if ("errors" in parsed) return setErrors(parsed.errors);
    setErrors({});
    if (editing) {
      const tag = editing.tag;
      run(tag, () => updateEdit(sessionId, tag, parsed.values), () => {
        setEditing(null);
        setNotice(`${tag} changed. Still locked: Claude keeps it.`);
      });
      return;
    }
    if (!build || "reason" in target) return;
    const body = { build_id: build.id, kind, ...target.target, ...parsed.values };
    run(toolOf(kind).label, () => createEdit(sessionId, body), (made) => {
      const e = made as Edit;
      setNotice(`${e.tag} is locked: Claude keeps it.`);
      // The edit is named by its tag from now on (the fix request names it).
      setEdit((last) => last && { ...last, name: e.tag });
    });
  };

  const toggle = (e: Edit) =>
    run(e.tag, () => updateEdit(sessionId, e.tag, { suppressed: !e.suppressed }), () =>
      setNotice(e.suppressed ? `${e.tag} is back in the build.` : `${e.tag} is suppressed: kept in the design, left out of the build.`),
    );
  const remove = (e: Edit) =>
    run(e.tag, () => deleteEdit(sessionId, e.tag), (result) => {
      const removed = (result as { removed: string[] }).removed;
      const also = removed.filter((t) => t !== e.tag);
      setNotice(`Deleted ${e.tag}${also.length ? `, and ${also.join(", ")}, which copied it` : ""}.`);
      if (editing?.tag === e.tag) setEditing(null);
    });

  const failing = rebuild?.state === "done" ? failingChecks(rebuild.report) : [];
  const fixWhy = busy ? "Claude is working" : fixBlocked;
  const fields = editFields(kind, options);
  const label = toolOf(kind).label;

  return (
    <section
      aria-label="Modeling"
      data-tool={kind}
      className="cs-scroll flex max-h-full w-72 max-w-full flex-col overflow-y-auto rounded-xl border border-[var(--cs-border)] bg-[#262626]/95 p-3 text-[12px] shadow-xl"
    >
      <p className="text-[13px]">{editing ? `Edit ${editing.tag}` : toolOf(kind).hint}</p>
      <p className="mt-0.5 text-[var(--cs-muted)]">
        {editing
          ? "Change its values. It stays locked: Claude keeps it."
          : "It changes the design's code and becomes a locked feature Claude keeps."}
      </p>

      {!editing && (
        <p data-testid="edit-target" data-ready={!("reason" in target)} className="mt-2 rounded-md bg-[var(--cs-surface-2)] px-2 py-1.5">
          {"reason" in target ? (
            <span className="text-[var(--cs-muted)]">{target.reason}.</span>
          ) : (
            <>
              On <span className="text-[var(--cs-text)]">{target.label}</span>
            </>
          )}
        </p>
      )}

      {(kind === "hole" || kind === "boss") && (
        <label className="mt-2 flex items-center gap-1.5 text-[var(--cs-muted)]">
          <input
            type="checkbox"
            aria-label="Heat-set insert"
            checked={options.insert}
            disabled={!!editing}
            onChange={(e) => setInsert(e.target.checked)}
          />
          {kind === "hole" ? "Sized for the team's heat-set insert" : "Insert pocket in the top (from the constraint set)"}
        </label>
      )}
      {kind === "pattern" && (
        <div role="radiogroup" aria-label="Pattern" className="mt-2 flex rounded-lg bg-[var(--cs-surface-2)] p-0.5">
          {PATTERN_KINDS.map((p) => (
            <button
              key={p}
              type="button"
              role="radio"
              aria-checked={options.pattern === p}
              disabled={!!editing}
              onClick={() => setPattern(p)}
              className={radio(options.pattern === p)}
            >
              {p === "linear" ? "Linear" : "Circular"}
            </button>
          ))}
        </div>
      )}
      {(kind === "mirror" || (kind === "pattern" && options.pattern === "linear")) && (
        <div role="radiogroup" aria-label={kind === "mirror" ? "Mirror across" : "Direction"} className="mt-1.5 flex items-center gap-1.5">
          <span className="text-[var(--cs-muted)]">{kind === "mirror" ? "Across" : "Along"}</span>
          <div className="flex flex-1 rounded-lg bg-[var(--cs-surface-2)] p-0.5">
            {AXES.map((a) => (
              <button
                key={a}
                type="button"
                role="radio"
                aria-checked={options.axis === a}
                disabled={!!editing}
                onClick={() => setAxis(a)}
                className={radio(options.axis === a)}
              >
                {a}
              </button>
            ))}
          </div>
        </div>
      )}

      <form
        className="mt-2 flex flex-col gap-1.5"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        {fields.map((field) => {
          const id = `edit-${field}`;
          const text = fieldLabel(field, kind);
          const fieldError = errors[field];
          return (
            <div key={field}>
              <div className="flex items-center gap-1.5">
                <label htmlFor={id} className="min-w-0 flex-1 text-[var(--cs-muted)]">
                  {text}
                </label>
                <input
                  id={id}
                  aria-label={text}
                  aria-invalid={fieldError ? true : undefined}
                  inputMode="decimal"
                  value={values[field] ?? ""}
                  onChange={(e) => setDrafts((d) => ({ ...d, [draftKey]: { ...values, [field]: e.target.value } }))}
                  className={`h-6 w-20 shrink-0 rounded-md bg-[var(--cs-surface-2)] px-1.5 text-right text-[var(--cs-text)] outline-none focus:ring-1 focus:ring-[var(--cs-accent)] ${fieldError ? "ring-1 ring-[var(--cs-accent)]" : ""}`}
                />
                <span className="w-6 shrink-0 text-[var(--cs-muted)]">{isLength(field) ? unit : field === "count" ? "" : "°"}</span>
              </div>
              {fieldError && <p className="mt-0.5 text-right text-[var(--cs-accent)]">{fieldError}</p>}
            </div>
          );
        })}
        {(kind === "hole" || kind === "boss" || kind === "rib") && (
          <p className="text-[11px] text-[var(--cs-muted)]">Position is from the face&apos;s centre; y points up on a wall.</p>
        )}
        <div className="mt-1 flex gap-1.5">
          <button
            type="submit"
            disabled={!!why}
            title={why}
            className="h-8 flex-1 rounded-lg bg-[var(--cs-text)] px-3 text-[13px] font-medium text-[#1f1f1f] disabled:cursor-not-allowed disabled:opacity-50"
          >
            {saving ? "Saving…" : editing ? `Save ${editing.tag}` : `Add ${label.toLowerCase()}`}
          </button>
          {editing && (
            <button type="button" onClick={() => setEditing(null)} className={`${small} h-8`}>
              Cancel
            </button>
          )}
        </div>
      </form>

      {error && (
        <p role="alert" className="mt-2 text-[var(--cs-accent)]">
          {error}
        </p>
      )}
      {notice && (
        <p role="status" data-testid="edit-notice" className="mt-2 flex items-center gap-1.5 text-[var(--cs-muted)]">
          <Lock className="size-3.5 shrink-0 text-[var(--cs-text)]" aria-hidden="true" />
          <span>{notice}</span>
        </p>
      )}
      {rebuild && <RebuildResult rebuild={rebuild} unit={unit} />}
      {failing.length > 0 && onFixRequest && edit && (
        <button
          type="button"
          data-testid="ask-fix"
          disabled={!!fixWhy || asked === rebuild?.name}
          title={fixWhy ?? "Claude proposes a fix in chat that keeps your edit"}
          onClick={() => {
            onFixRequest(fixText(edit.name, failing));
            setAsked(edit.name);
          }}
          className="mt-2 h-7 rounded-lg border border-[var(--cs-border)] px-2 text-[12px] hover:bg-[var(--cs-hover)] disabled:cursor-not-allowed disabled:opacity-50"
        >
          {asked === edit.name ? "Asked Claude for a fix" : "Ask Claude for a fix that keeps my edit"}
        </button>
      )}

      {featureParams.length > 0 && (
        <section aria-label="Feature parameters" className="mt-3 border-t border-[var(--cs-border)] pt-2">
          <p className="text-[var(--cs-muted)]">{codeFeatures.join(", ")}: made by the code</p>
          <ul className="mt-1 flex flex-col gap-1">
            {featureParams.map((p) => (
              <li key={p.name} className="flex items-center gap-1.5">
                <span className="min-w-0 flex-1 truncate">{p.description || p.name}</span>
                <input
                  aria-label={`Feature parameter ${p.name}`}
                  defaultValue={formatParameter(p, unit)}
                  key={`${p.name}/${p.value}`}
                  disabled={busy || !!parameters.saving}
                  onKeyDown={(e) => {
                    if (e.key !== "Enter") return;
                    const parsed = parseParameter(e.currentTarget.value, p, unit);
                    if ("error" in parsed) return parameters.setError(p.name, parsed.error);
                    parameters.edit(p, parsed.value).then((ok) => ok && onEdited?.());
                  }}
                  className="h-6 w-20 shrink-0 rounded-md bg-[var(--cs-surface-2)] px-1.5 text-right outline-none focus:ring-1 focus:ring-[var(--cs-accent)]"
                />
                {p.locked && <Lock className="size-3 shrink-0" aria-label={`${p.name} is locked`} />}
              </li>
            ))}
          </ul>
        </section>
      )}

      <section aria-label="Hand edits" className="mt-3 border-t border-[var(--cs-border)] pt-2">
        <p className="text-[var(--cs-muted)]">Hand edits · {edits.length}</p>
        {edits.length === 0 ? (
          <p className="mt-1 text-[var(--cs-faint)]">None yet. Each one is a locked feature Claude keeps.</p>
        ) : (
          <ol className="mt-1 flex flex-col gap-1">
            {edits.map((e) => {
              const on = picked.some((p) => p.tag === e.tag) || editing?.tag === e.tag;
              return (
                <li
                  key={e.tag}
                  data-edit={e.tag}
                  data-suppressed={e.suppressed}
                  data-picked={on}
                  className={`rounded-md px-1.5 py-1 ${on ? "bg-[var(--cs-accent)]/15" : ""}`}
                >
                  <p className="flex items-center gap-1">
                    <span className={`min-w-0 flex-1 truncate ${e.suppressed ? "text-[var(--cs-faint)] line-through" : ""}`}>{e.tag}</span>
                    {e.suppressed && <EyeOff className="size-3 shrink-0 text-[var(--cs-muted)]" aria-label="suppressed" />}
                    <Lock className="size-3 shrink-0" aria-label={`${e.tag} is locked`} />
                  </p>
                  <p className="truncate text-[11px] text-[var(--cs-muted)]">{editSummary(e, unit)}</p>
                  <div className="mt-1 flex gap-1">
                    <button type="button" className={small} disabled={busy || saving} onClick={() => setEditing(e)} aria-label={`Edit ${e.tag}`}>
                      Edit
                    </button>
                    <button type="button" className={small} disabled={busy || saving} onClick={() => toggle(e)} aria-label={`${e.suppressed ? "Unsuppress" : "Suppress"} ${e.tag}`}>
                      {e.suppressed ? "Unsuppress" : "Suppress"}
                    </button>
                    <button type="button" className={small} disabled={busy || saving} onClick={() => remove(e)} aria-label={`Delete ${e.tag}`}>
                      Delete
                    </button>
                  </div>
                </li>
              );
            })}
          </ol>
        )}
      </section>
    </section>
  );
}
