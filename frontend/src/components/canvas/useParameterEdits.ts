"use client";

import { useEffect, useState } from "react";
import { getChecks, type Build, type CheckReport } from "./api";
import { listParameters, rebuiltBuild, updateParameter, withParameter, type Parameter } from "./parameters";
import { shownError } from "@/lib/errors";

export type Rebuild =
  | { state: "building"; name: string }
  | { state: "checking"; name: string; build: Build }
  | { state: "done"; name: string; build: Build; report: CheckReport }
  | { state: "error"; name: string; message: string };

export const parameterLabel = (p: Parameter) => p.description || p.name;

/** A copy of `record` with `key` set to `value`, or removed when `value` is null. */
export function withKey(record: Record<string, string>, key: string, value: string | null): Record<string, string> {
  const next = { ...record };
  if (value === null) delete next[key];
  else next[key] = value;
  return next;
}

export type Parameters = {
  params: Parameter[] | null;
  loadError: string | null;
  /** The parameter being saved, if any. */
  saving: string | null;
  /** The last save error per parameter. */
  errors: Record<string, string>;
  setError: (name: string, message: string | null) => void;
  notice: string | null;
  /** The rebuild after the last hand edit, and its checks. */
  rebuild: Rebuild | null;
  /** A hand edit: locks the parameter and rebuilds. Resolves true once saved. */
  edit: (param: Parameter, value: number | string) => Promise<boolean>;
  /** Hands the parameter back to Claude. */
  unlock: (param: Parameter) => Promise<void>;
};

/**
 * The session's design parameters and hand edits to them, shared by everything that edits them
 * (the parameters panel, the dimensions on the model, dragging a face): a hand edit locks the
 * parameter, the API rebuilds, and the rebuild's checks are loaded.
 */
export function useParameterEdits(sessionId: string, builds: Build[], onEdited?: () => void): Parameters {
  const [params, setParams] = useState<Parameter[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState<string | null>(null);
  // The last hand edit: the builds there were before it, so the rebuild is the next new one.
  const [last, setLast] = useState<{ name: string; before: string[] } | null>(null);
  const [checked, setChecked] = useState<{ buildId: string; report: CheckReport } | { buildId: string; error: string } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    listParameters(sessionId)
      .then((p) => live && setParams(p))
      .catch((e: Error) => live && setLoadError(shownError(e)));
    return () => {
      live = false;
    };
  }, [sessionId]);

  // The rebuild arrives as a new build (a `build_ready` event); then its checks are loaded.
  const rebuilt = last ? rebuiltBuild(builds, last.before) : null;
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
  const rebuild: Rebuild | null = !last
    ? null
    : !rebuilt
      ? { state: "building", name: last.name }
      : !result
        ? { state: "checking", name: last.name, build: rebuilt }
        : "report" in result
          ? { state: "done", name: last.name, build: rebuilt, report: result.report }
          : { state: "error", name: last.name, message: result.error };

  const setError = (name: string, message: string | null) => setErrors((e) => withKey(e, name, message));

  const edit = (param: Parameter, value: number | string): Promise<boolean> => {
    if (saving) return Promise.resolve(false);
    const before = builds.map((b) => b.id);
    setSaving(param.name);
    setNotice(null);
    setError(param.name, null);
    return updateParameter(sessionId, param.name, { value })
      .then((p) => {
        setParams((ps) => ps && withParameter(ps, p));
        setLast({ name: parameterLabel(p), before });
        onEdited?.();
        return true;
      })
      .catch((e: Error) => {
        setError(param.name, shownError(e));
        return false;
      })
      .finally(() => setSaving(null));
  };

  const unlock = (param: Parameter): Promise<void> => {
    setSaving(param.name);
    return updateParameter(sessionId, param.name, { locked: false })
      .then((p) => {
        setParams((ps) => ps && withParameter(ps, p));
        setNotice(`${parameterLabel(p)} is Claude's again: it can change it to meet the constraints.`);
      })
      .catch((e: Error) => setError(param.name, shownError(e)))
      .finally(() => setSaving(null));
  };

  return { params, loadError, saving, errors, setError, notice, rebuild, edit, unlock };
}
