"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { PinFilter } from "./pinVisibility";

/**
 * Opening the canvas's Constraints panel from a check count ("3 untested") elsewhere: a build
 * card in the chat, the proposed changes, a rebuild's summary. The count's group picks the
 * panel's check pins filter (Failed for "fail", All otherwise) and the rows the panel scrolls to.
 */

/** A group of results a check summary counts. */
export type CheckCountStatus = "pass" | "fail" | "untested" | "not_applicable";

/** A request to open the panel on a group; `n` counts the requests, so the same one opens it again. */
export type OpenChecksRequest = { status: CheckCountStatus; n: number };

const WORDS: Record<CheckCountStatus, string> = {
  pass: "pass",
  fail: "fail",
  untested: "untested",
  not_applicable: "not applicable",
};

export type CountSummary = { pass: number; fail: number; untested: number; not_applicable?: number };

/** A summary's counts in the order a summary line reads them; "not applicable" only when some are. */
export function countItems(summary: CountSummary): { status: CheckCountStatus; count: number; text: string }[] {
  const statuses: CheckCountStatus[] = ["pass", "fail", "untested"];
  if (summary.not_applicable) statuses.push("not_applicable");
  return statuses.map((status) => {
    const count = summary[status] ?? 0;
    return { status, count, text: `${count} ${WORDS[status]}` };
  });
}

/** The accessible name of a count that opens the panel: "Open constraints: 3 untested". */
export function openChecksLabel(status: CheckCountStatus, count: number): string {
  return `Open constraints: ${count} ${WORDS[status]}`;
}

/** The panel's check pins filter for a count: failed checks for "fail", every pin otherwise. */
export function pinFilterFor(status: CheckCountStatus): PinFilter {
  return status === "fail" ? "failed" : "all";
}

type OpenChecksValue = {
  request: OpenChecksRequest | null;
  open: (status: CheckCountStatus) => void;
  /** The canvas applied request `n`: it isn't applied again when the canvas mounts anew. */
  done: (n: number) => void;
};

export const OpenChecksContext = createContext<OpenChecksValue | null>(null);

/** For the workspace: the context value the canvas and the counts share. */
export function useOpenChecksValue(): OpenChecksValue {
  const [state, setState] = useState<{ request: OpenChecksRequest | null; n: number }>({ request: null, n: 0 });
  const open = useCallback(
    (status: CheckCountStatus) => setState((s) => ({ request: { status, n: s.n + 1 }, n: s.n + 1 })),
    [],
  );
  const done = useCallback((n: number) => setState((s) => (s.request?.n === n ? { ...s, request: null } : s)), []);
  return useMemo(() => ({ request: state.request, open, done }), [state.request, open, done]);
}

/** For a count inside the canvas: opens the panel on its group; null outside a session's workspace. */
export function useOpenChecks(): ((status: CheckCountStatus) => void) | null {
  return useContext(OpenChecksContext)?.open ?? null;
}

/**
 * For the canvas: applies each request once, when it can show the panel (`ready`: it has a build).
 * `apply` may be a new function each render.
 */
export function useOpenChecksRequest(ready: boolean, apply: (request: OpenChecksRequest) => void) {
  const ctx = useContext(OpenChecksContext);
  const request = ctx?.request ?? null;
  const done = ctx?.done;
  useEffect(() => {
    if (!request || !ready) return;
    apply(request);
    done?.(request.n);
    // Once per request: `apply` is a new function each render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [request, ready, done]);
}
