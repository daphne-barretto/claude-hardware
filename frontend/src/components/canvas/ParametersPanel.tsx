"use client";

import { useEffect, useState } from "react";
import { Lock } from "lucide-react";
import { getChecks, type Build, type CheckReport } from "./api";
import { CheckRow } from "./CheckRow";
import { displayUnit, failingChecks, formatParameter, parseParameter, rebuiltBuild, type Parameter } from "./parameters";
import { CheckCounts } from "./CheckCounts";
import { useOpenChecks } from "./openChecks";
import type { LengthUnit } from "./units";
import { parameterLabel as label, withKey, type Parameters, type Rebuild } from "./useParameterEdits";
import { shownError } from "@/lib/errors";

export type { Rebuild };

/**
 * The rebuild after a hand edit: it arrives as a new build (a `build_ready` event) that was not
 * in `edit.before`; then its checks are loaded.
 */
export function useRebuild(builds: Build[], edit: { name: string; before: string[] } | null): Rebuild | null {
  const [checked, setChecked] = useState<{ buildId: string; report: CheckReport } | { buildId: string; error: string } | null>(null);
  const rebuilt = edit ? rebuiltBuild(builds, edit.before) : null;
  useEffect(() => {
    if (!rebuilt) return;
    let live = true;
    getChecks(rebuilt)
      .then((report) => live && setChecked({ buildId: rebuilt.id, report }))
      .catch((e: Error) => live && setChecked({ buildId: rebuilt.id, error: shownError(e) }));
    return () => {
      live = false;
    };
  }, [rebuilt]);
  const result = rebuilt && checked?.buildId === rebuilt.id ? checked : null;
  return !edit
    ? null
    : !rebuilt
      ? { state: "building", name: edit.name }
      : !result
        ? { state: "checking", name: edit.name, build: rebuilt }
        : "report" in result
          ? { state: "done", name: edit.name, build: rebuilt, report: result.report }
          : { state: "error", name: edit.name, message: result.error };

}

/**
 * The design parameters. Typing a value is a hand edit: it locks the parameter (shown with a
 * lock), the API rebuilds, and the panel shows the rebuild's failing checks. Unlocking hands
 * the parameter back to Claude. The parameters and edits are shared with the dimensions on the
 * model (useParameterEdits), so an edit made there shows here too. `readOnly` (a shared
 * link): why nothing can be edited; the values show, but can't be typed into or unlocked.
 */
export function ParametersPanel({ parameters, unit, readOnly }: { parameters: Parameters; unit: LengthUnit; readOnly?: string }) {
  const { params, loadError, saving, errors, setError, notice, rebuild } = parameters;
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const setDraft = (name: string, text: string | null) => setDrafts((d) => withKey(d, name, text));

  const commit = (param: Parameter) => {
    const text = drafts[param.name];
    if (text === undefined || saving || readOnly) return;
    const parsed = parseParameter(text, param, unit);
    if ("error" in parsed) return setError(param.name, parsed.error);
    setError(param.name, null);
    if (text.trim() === formatParameter(param, unit)) return setDraft(param.name, null);
    parameters.edit(param, parsed.value).then((ok) => ok && setDraft(param.name, null));
  };

  const unlock = (param: Parameter) => parameters.unlock(param);

  return (
    <section
      aria-label="Parameters"
      className="cs-scroll @container flex max-h-full w-80 max-w-full flex-col overflow-y-auto rounded-xl border border-[var(--cs-border)] bg-[#262626]/95 p-3 text-[12px] shadow-xl"
    >
      <p className="text-[13px]">Parameters</p>
      {readOnly ? (
        <p data-testid="parameters-read-only" role="note" className="mt-0.5 text-[var(--cs-muted)]">
          {readOnly}. Continue from it to edit.
        </p>
      ) : (
        <p className="mt-0.5 text-[var(--cs-muted)]">Type a value to lock it. Claude designs around locked values.</p>
      )}
      {loadError && <p className="mt-2 text-[var(--cs-accent)]">The parameters could not be loaded: {loadError}</p>}
      {!params && !loadError && <p className="mt-2 text-[var(--cs-muted)]">Loading the parameters…</p>}
      {params && (
        <ul className="mt-2 flex flex-col gap-1.5">
          {params.map((p) => {
            const id = `param-${p.name}`;
            const u = displayUnit(p, unit);
            const error = errors[p.name];
            return (
              <li key={p.name} data-parameter={p.name} data-locked={p.locked ? "true" : "false"}>
                {/* In a narrow panel (the canvas layout's tool column on a laptop) the name goes above its field. */}
                <div className="flex flex-wrap items-center justify-end gap-x-1.5 gap-y-0.5">
                  <label htmlFor={id} className="min-w-0 basis-full truncate leading-tight text-[var(--cs-muted)] @[16rem]:basis-0 @[16rem]:flex-1" title={p.name}>
                    {label(p)}
                  </label>
                  <input
                    id={id}
                    aria-label={label(p)}
                    aria-invalid={error ? true : undefined}
                    aria-describedby={error ? `${id}-error` : undefined}
                    inputMode={typeof p.value === "number" ? "decimal" : "text"}
                    value={drafts[p.name] ?? formatParameter(p, unit)}
                    readOnly={!!readOnly || saving === p.name}
                    title={readOnly ? `${readOnly}. Continue from it to edit.` : undefined}
                    onChange={(e) => setDraft(p.name, e.target.value)}
                    onBlur={() => commit(p)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") commit(p);
                      if (e.key === "Escape") {
                        setDraft(p.name, null);
                        setError(p.name, null);
                      }
                    }}
                    className={`h-6 ${typeof p.value === "number" ? "w-16" : "w-24"} shrink-0 rounded-md bg-[var(--cs-surface-2)] px-1.5 text-right text-[var(--cs-text)] outline-none focus:ring-1 focus:ring-[var(--cs-accent)] ${readOnly ? "cursor-default text-[var(--cs-muted)] focus:ring-0" : ""} ${error ? "ring-1 ring-[var(--cs-accent)]" : ""}`}
                  />
                  <span className="w-6 shrink-0 text-[var(--cs-muted)]">{u}</span>
                  {p.locked && readOnly ? (
                    <span className="grid size-6 shrink-0 place-items-center text-[var(--cs-muted)]" title="Locked by a hand edit">
                      <Lock className="size-3.5" aria-label={`${label(p)} is locked`} />
                    </span>
                  ) : p.locked ? (
                    <button
                      type="button"
                      aria-label={`Unlock ${label(p)}`}
                      title="Locked by your edit. Unlock to hand it back to Claude."
                      disabled={saving === p.name}
                      onClick={() => unlock(p)}
                      className="grid size-6 shrink-0 place-items-center rounded-md text-[var(--cs-text)] hover:bg-[var(--cs-hover)] disabled:cursor-not-allowed disabled:opacity-60"
                    >
                      <Lock className="size-3.5" aria-hidden="true" />
                    </button>
                  ) : (
                    <span className="size-6 shrink-0" />
                  )}
                </div>
                {error && (
                  <p id={`${id}-error`} className="mt-0.5 text-right text-[var(--cs-accent)]">
                    {error}
                  </p>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {notice && (
        <p role="status" className="mt-2 text-[var(--cs-muted)]">
          {notice}
        </p>
      )}
      {rebuild && <RebuildResult rebuild={rebuild} unit={unit} />}
    </section>
  );
}

export function RebuildResult({ rebuild, unit }: { rebuild: Rebuild; unit: LengthUnit }) {
  const failing = rebuild.state === "done" ? failingChecks(rebuild.report) : [];
  const openChecks = useOpenChecks();
  return (
    <section
      aria-label="Rebuild"
      data-testid="rebuild"
      data-state={rebuild.state}
      data-build={"build" in rebuild ? rebuild.build.id : undefined}
      className="mt-3 border-t border-[var(--cs-border)] pt-2"
      aria-live="polite"
    >
      {rebuild.state === "building" && <p className="text-[var(--cs-muted)]">Rebuilding with your {rebuild.name.toLowerCase()}…</p>}
      {rebuild.state === "checking" && <p className="text-[var(--cs-muted)]">Rebuilt. Running checks…</p>}
      {rebuild.state === "error" && <p className="text-[var(--cs-accent)]">The checks could not be loaded: {rebuild.message}</p>}
      {rebuild.state === "done" && (
        <>
          <p data-testid="rebuild-summary">
            Rebuilt and checked: <CheckCounts summary={rebuild.report.summary} onOpen={openChecks} />
          </p>
          {failing.length > 0 ? (
            <ul aria-label="Failing checks" className="mt-1 flex flex-col gap-1">
              {failing.map((r) => (
                <CheckRow key={r.id} result={r} unit={unit} tone="text-[var(--cs-accent)]" />
              ))}
            </ul>
          ) : (
            <p className="mt-1 text-[var(--cs-muted)]">Every check passes.</p>
          )}
        </>
      )}
    </section>
  );
}
