// The standards editor's routes (docs/constraints.md, contracts/session-api.openapi.json):
// the rule catalogue, the browser's own standards sets, and a session's set.
import { ApiError, type Session, type StandardRule, type StandardsSet } from "./session-api";
import { responseMessage } from "./errors";

/** A named size a rule of a kind must give (e.g. an insert's pocket diameter). */
export type KindDimension = { name: string; label: string; unit: "mm" | "deg"; min: number; max: number; description: string };

/** One kind of rule the app can enforce (GET /api/standards/kinds): rules are made of these only. */
export type CheckKind = {
  check: string;
  standard_id: string;
  title: string;
  kind: "min" | "max" | "tolerance" | "spec";
  unit?: "mm" | "deg" | null;
  min?: number | null;
  max?: number | null;
  /** The rule in plain language, with `{value}` for its value. */
  phrase: string;
  description: string;
  applies_to: "any" | "board" | "object";
  value_label: string;
  dimensions: KindDimension[];
  material: boolean;
  /** The built-in set's rule of this kind, to start from. */
  suggested?: StandardRule | null;
};

/** A rule as the editor sends it: its ID, comparison and unit follow from its check. */
export type StandardInput = {
  check: string;
  title?: string;
  description?: string;
  value?: number | string | null;
  applies_to?: string;
  enabled: boolean;
  aliases: string[];
  dimensions?: Record<string, number | string | null> | null;
  material?: string | null;
};

/** One thing wrong with a set: `rule` is the rule's index in the request. */
export type StandardsProblem = { rule?: number | null; field?: string | null; message: string };

/** The API refused a set (422): every problem, to show next to its rule. */
export class StandardsRejected extends ApiError {
  constructor(
    message: string,
    readonly problems: StandardsProblem[],
  ) {
    super(422, message);
  }
}

async function send<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`/api${path}`, { ...init, headers: { "Content-Type": "application/json", ...init.headers } });
  if (res.status === 204) return undefined as T;
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    // Not JSON: the status says it.
  }
  if (!res.ok) {
    const b = body as { detail?: unknown; problems?: StandardsProblem[] } | null;
    const detail = responseMessage(res.status, b?.detail);
    if (res.status === 422 && Array.isArray(b?.problems)) throw new StandardsRejected(detail, b.problems);
    throw new ApiError(res.status, detail);
  }
  return body as T;
}

export function listKinds(): Promise<CheckKind[]> {
  return send<CheckKind[]>("/standards/kinds");
}

export function createStandardsSet(body: { name: string; duplicate_of?: string | null }): Promise<StandardsSet> {
  return send<StandardsSet>("/standards", { method: "POST", body: JSON.stringify(body) });
}

export function updateStandardsSet(id: string, body: { name?: string; standards?: StandardInput[] }): Promise<StandardsSet> {
  return send<StandardsSet>(`/standards/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(body) });
}

export function deleteStandardsSet(id: string): Promise<void> {
  return send<void>(`/standards/${encodeURIComponent(id)}`, { method: "DELETE" });
}

/** Design and check the session against this set from now on: the design is rechecked and the chat says what changed. */
export function setSessionStandards(sessionId: string, standards: string): Promise<Session> {
  return send<Session>(`/sessions/${encodeURIComponent(sessionId)}/standards`, {
    method: "PUT",
    body: JSON.stringify({ standards }),
  });
}
