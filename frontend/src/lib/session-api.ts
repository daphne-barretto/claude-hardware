// Client for the session API (contracts/session-api.openapi.json, contracts/README.md).
// The types mirror the pydantic models in backend/app/contracts/session.py.
import type { FindResult } from "@/components/canvas/api";
import { sessionRoute } from "./share";
import { followStream, retryStreamsNow } from "./session-stream";
import type { StreamStatus } from "./stream-hub";
import { statusSentence } from "./errors";

export type Effort = "low" | "medium" | "high" | "xhigh" | "max";
export type Mode = "manual" | "auto";

export type BoardRef = { name: string; revision: string };

export type Session = {
  id: string;
  title: string;
  created_at: string;
  board?: BoardRef | null;
  status: "idle" | "running" | "waiting_for_answer";
  latest_build_id?: string | null;
  /** The board revision `POST .../revisions/next` loads; null once it is loaded. */
  next_revision?: BoardRef | null;
  /** The standards set chosen with the session's first message. */
  standards?: string | null;
  /** A demo's hardcoded original (demos/sessions/), listed until this browser opens the demo (`openDemo`). */
  sample?: boolean;
  /** The demo this session is: its original (`sample`) or this browser's own copy, which works like any session. */
  demo?: string | null;
  /** Served from a share link: `view` a read-only snapshot, `copy` a design to copy. */
  shared?: "view" | "copy" | null;
  /** The user renamed it: the name is theirs and Claude no longer renames it. */
  title_locked?: boolean;
};

/** One rule of a standards set, as GET /api/standards lists it (StandardSummary). */
export type StandardRule = {
  id: string;
  title: string;
  description: string;
  check: string;
  kind?: "min" | "max" | "tolerance" | "spec";
  value?: number | string;
  unit?: "mm" | "deg" | null;
  applies_to?: "any" | "board" | "object";
  /** False: turned off in the set, so not checked. */
  enabled?: boolean;
  aliases?: string[];
  dimensions?: Record<string, number> | null;
  material?: string | null;
  /** The rule in plain language, e.g. "Walls at least 2.0 mm". */
  label?: string;
};

/**
 * A set of team standards, as GET /api/standards lists it: the built-in set (standards.yaml,
 * read-only) or one this browser made (docs/constraints.md).
 */
export type StandardsSet = {
  id: string;
  name: string;
  built_in?: boolean;
  duplicated_from?: string | null;
  standards: StandardRule[];
};

/** What the user has selected on the canvas; sent with the next message as context. */
export type Selection = {
  /** The part everything selected is on; null across parts. */
  part?: string | null;
  /** Whole parts selected. */
  parts?: string[];
  faces: string[];
  edges: string[];
  /** Board components selected, by reference designator. */
  components?: string[];
  point?: [number, number, number] | null;
};

export type Answer = { question_id: string; option_id: string };

export type Attachment = { name: string; media_type: string; url: string };

export type MessageCreate = {
  text: string;
  model?: string | null;
  effort?: Effort | null;
  mode: Mode;
  selection?: Selection | null;
  answer?: Answer | null;
  attachments?: Attachment[];
  /** A standards set ID; given with the session's first message. */
  standards?: string | null;
  /** Comments sent to Claude: one run handles them and answers each in its thread. */
  comments?: string[];
};

/** A message sent while Claude was working: it waits in the session's queue (`QueuedMessage`). */
export type QueuedMessage = { id: string; created_at: string; message: MessageCreate };

/** The session's message queue: sent one at a time, in order, as runs end. */
export type MessageQueue = {
  items: QueuedMessage[];
  /** Claude's question is open: the next message waits until it is answered or sent anyway. */
  paused: boolean;
};

/** A reply in a comment's thread: the user's (a note) or Claude's (from the run it names). */
export type CommentReply = {
  id: string;
  role: "user" | "assistant";
  author: string;
  text: string;
  created_at: string;
  message_id?: string | null;
};

/**
 * A comment pinned to a face (`Comment` in the session API contract). `location` is where the
 * pin is on the build the list was asked for: the face re-resolved after rebuilds, or null when
 * that build no longer has it.
 */
export type Comment = {
  id: string;
  build_id: string;
  face_id: string;
  point: [number, number, number];
  text: string;
  author: string;
  created_at: string;
  part?: string | null;
  feature?: string | null;
  anchor?: [number, number, number] | null;
  location?: { point: [number, number, number]; part?: string | null; faces?: string[]; edges?: string[] } | null;
  replies: CommentReply[];
  /** Set once sent to Claude: the run handling it. */
  request?: { message_id: string; status: "running" | "answered" | "failed" } | null;
  resolved: boolean;
};

/** A stored message (`GET /api/sessions/{id}/messages`). Assistant text is rebuilt from the event stream. */
export type Message = {
  id: string;
  role: "user" | "assistant";
  text: string;
  created_at: string;
  selection?: Selection | null;
  answer?: Answer | null;
  attachments?: Attachment[];
};

export type Build = {
  id: string;
  created_at: string;
  /** The board the build was checked against; null for a design without a board. */
  board: BoardRef | null;
  status: "ready" | "failed";
  checks: { pass: number; fail: number; untested: number; not_applicable?: number };
  maps: string[];
  mesh_url: string;
  checks_url: string;
  source_url: string;
  /** The board revision of the build, in the board JSON format (`backend/app/boards/SCHEMA.md`); 404 when `board` is null. */
  board_url: string;
  step_url: string | null;
  /** The file a build no message made came from (an import, or an example's existing design). */
  origin?: BuildOrigin | null;
};

/** Where a build that no message made came from: `import` (a file the user imported) or `existing` (the existing design an example opens on). */
export type BuildOrigin = { kind: "import" | "existing"; file_name: string };

/** One check behind a proposed change: its result before, with this change alone, and with every accepted one. */
export type ChangeCheck = {
  id: string;
  check: string;
  effect: string;
  before: string;
  alone: string;
  after: string;
};
/** One proposed change of a change set (contracts/change-set.schema.json). */
export type ProposedChange = {
  id: string;
  title: string;
  reason: string;
  status: "proposed" | "accepted" | "rejected";
  checks: ChangeCheck[];
  /** The comment this change answers. */
  comment?: string | null;
};
export type ChangeSet = {
  mode: Mode;
  base_build: string;
  build: string;
  checks: { pass: number; fail: number; untested: number };
  changes: ProposedChange[];
};
/** One constraint of the design's spec, shown as a chip above the chat input. */
export type Constraint = {
  id: string;
  title: string;
  label: string;
  kind: "min" | "max" | "tolerance" | "spec";
  value: number | string;
  unit: "mm" | "deg" | null;
  check: string;
  /** standard: standards.yaml; user: the user's words (`quote`); edit: the chip was edited. */
  source: "standard" | "user" | "edit";
  quote: string | null;
  message_id: string | null;
  standard_value: number | string;
};

/** Where a flagged risk is on the model (contracts/risk-flags.schema.json, RiskLocation). */
export type RiskLocation = {
  part?: string | null;
  feature?: string | null;
  component?: string | null;
  faces: string[];
  point?: [number, number, number] | null;
};
/** A risk the checks don't block, with a fix to accept or ignore on its own (contracts/README.md, "Risk flags"). */
export type RiskFlag = {
  id: string;
  kind: "thick" | "thin" | "low_draft" | "near_limit";
  /** The one line said in chat: what the risk is and where. */
  message: string;
  location: RiskLocation;
  status: "proposed" | "accepted" | "rejected";
  fix: { title: string; resolves: boolean };
};
export type RiskFlags = { build: string; flags: RiskFlag[] };

export type QuestionOption = { id: string; label: string; description?: string | null };

/** Where on the model something is (contracts/README.md, "Shared conventions"). Board coordinates, mm. */
export type Location = {
  point: [number, number, number];
  part?: string | null;
  faces?: string[];
  edges?: string[];
};

/** One side of a conflict: a locked parameter, a team standard or a board part. */
export type ConflictConstraint = {
  kind: "locked" | "standard" | "board";
  id: string;
  label: string;
  relation: "=" | ">=" | "<=";
  value: number;
  unit: "mm" | "deg";
  /** Best achievable value when only this constraint gives way; null when none helps. */
  best: number | null;
  locations: Location[];
};

/** Locked values, standards and the board that can't all be met (conflict detection). */
export type ConstraintConflict = {
  id: string;
  message: string;
  over_by: number;
  constraints: ConflictConstraint[];
  location: Location;
};

/**
 * A notice the chat shows apart from Claude's text. `refused`: Claude won't design this,
 * and `text` says why. `safety`: Claude goes on with a regulated or safety-critical product;
 * `regulations` are kinds of rules that may apply, none of them checked.
 */

export type SessionEvent =
  | { type: "message_start"; message_id: string; role: "assistant" }
  | { type: "text"; message_id: string; delta: string }
  | { type: "progress"; message_id: string; text: string; round?: number | null; rounds?: number | null }
  | {
      type: "question";
      message_id: string;
      question_id: string;
      text: string;
      options: QuestionOption[];
      location?: Location | null;
      conflict?: ConstraintConflict | null;
    }
  | { type: "build_ready"; message_id?: string | null; build: Build }
  | { type: "changes_ready"; message_id?: string | null; change_set: ChangeSet }
  | { type: "constraints"; message_id?: string | null; constraints: Constraint[] }
  | { type: "plan"; message_id: string; steps: string[] }
  | { type: "risks_ready"; message_id?: string | null; risk_flags: RiskFlags }
  /** Faces Claude found from chat: the canvas highlights them; they can become the selection. */
  | { type: "found"; message_id: string; build_id: string; result: FindResult }
  | { type: "message_end"; message_id: string }
  /** The run stopped: `message` says plainly why and what to do; `retryable` offers Retry (`retryRun`). */
  | { type: "error"; message_id?: string | null; message: string; retryable?: boolean; reason?: LimitReason | null }
  | { type: "comment"; message_id?: string | null; comment: Comment }
  /** The message queue changed; `sent` names the queued message it sent, as user message `message_id`. */
  | { type: "queue"; queue: MessageQueue; sent?: string | null; message_id?: string | null }
  | NoticeEvent
  /** The session was renamed: by Claude after the part it designs, or by the user (no message_id). */
  | { type: "title"; message_id?: string | null; title: string; title_locked?: boolean }
  /** The user sent a message (from any tab): the message as stored, so every tab shows it. */
  | { type: "user_message"; message_id: string; message: Message };

/** Why a run stopped or waits (backend `app.agent.limits`), next to the plain text. */
export type LimitReason =
  | "rate_limited"
  | "overloaded"
  | "network"
  | "billing"
  | "not_configured"
  | "too_large"
  | "model_unavailable"
  | "run_queue"
  | "build_queue"
  | "build_memory"
  | "build_time"
  | "build_crash"
  | "preempted"
  | "stopped"
  | "interrupted"
  | "save_failed"
  | "unknown";

/**
 * A notice the chat shows apart from Claude's text: one event for every kind (backend
 * `NoticeEvent`). `retrying` a temporary API error in `retry_in_s` seconds (`attempt` of
 * `attempts`), `waiting` in line (`place`, 1: next) or for a build, or a `warning`, while the run
 * goes on; a later event of the reply replaces a `retrying` or `waiting` notice. `refused`: Claude
 * won't design this; `safety`: a regulated product, with the `regulations` that may apply (none
 * checked) and the `region` the user named.
 */
export type NoticeEvent = {
  type: "notice";
  message_id?: string | null;
  kind: "retrying" | "waiting" | "warning" | "refused" | "safety";
  text: string;
  reason?: LimitReason | null;
  retry_in_s?: number | null;
  attempt?: number | null;
  attempts?: number | null;
  place?: number | null;
  categories?: string[];
  regulations?: string[];
  region?: string | null;
};

/** A notice as the chat keeps it. */
export type Notice = Omit<NoticeEvent, "type" | "message_id">;

export { EVENT_TYPES } from "./event-types";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

/**
 * The session itself is not on the server (the API answers 404 "no session ..."): a deploy
 * wiped its data (docs/architecture.md). Other 404s (a build, a standards set) are not this.
 */
export function isSessionGone(e: unknown): boolean {
  return e instanceof ApiError && e.status === 404 && /^no session\b/.test(e.message);
}

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  // A form body sets its own multipart Content-Type.
  const headers = new Headers(init.headers);
  if (!(init.body instanceof FormData) && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const res = await fetch(`/api${path}`, { ...init, headers });
  if (!res.ok) {
    // The server's own words (the code reads some, like "no session ..."), else a plain
    // sentence for the status: never "Internal Server Error" (QA fix brief 8).
    let detail = statusSentence(res.status);
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      // Not JSON: keep the plain sentence.
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

/** Creates a session. With nothing it starts empty: no board and no builds. */
export function createSession(body: { title?: string; from_share?: string } = {}): Promise<Session> {
  return call<Session>("/sessions", { method: "POST", body: JSON.stringify(body) });
}

/**
 * This browser's own copy of a demo, made from the demo's original the first time it is
 * opened: a session like any other (backend/app/demos.py).
 */
export function openDemo(demoId: string): Promise<Session> {
  return call<Session>(`/demos/${encodeURIComponent(demoId)}/session`, { method: "POST" });
}

/** Resets a demo: this browser's copy is deleted, and opening the demo makes a fresh one. */
export async function resetDemo(demoId: string): Promise<void> {
  const res = await fetch(`/api/demos/${encodeURIComponent(demoId)}/session`, { method: "DELETE" });
  if (res.ok) return;
  const body = await res.json().catch(() => null);
  throw new ApiError(res.status, typeof body?.detail === "string" ? body.detail : statusSentence(res.status));
}

/** Renames a session (its owner only). The name is the user's from then on (`title_locked`). */
export function renameSession(sessionId: string, title: string): Promise<Session> {
  return call<Session>(`/sessions/${encodeURIComponent(sessionId)}`, { method: "PATCH", body: JSON.stringify({ title }) });
}

export function listStandards(): Promise<StandardsSet[]> {
  return call<StandardsSet[]>("/standards");
}

export function listSessions(): Promise<Session[]> {
  return call<Session[]>("/sessions");
}

/**
 * The order Recents and Search show sessions in: the user's sessions newest first, then the
 * demos (this browser's copy, or the original until it is opened) in the order the API lists them.
 */
export function recentsOrder(sessions: Session[]): Session[] {
  const own = sessions.filter((s) => !s.demo && !s.sample);
  const demos = sessions.filter((s) => s.demo || s.sample);
  own.sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at) || b.id.localeCompare(a.id));
  return [...own, ...demos];
}

export function listMessages(sessionId: string): Promise<Message[]> {
  return call<Message[]>(`${sessionRoute(sessionId)}/messages`);
}

export const sessionHref = (id: string) => `/session/${encodeURIComponent(id)}`;

/** A demo's page: it opens this browser's own copy of the demo (`openDemo`). */
export const demoHref = (demoId: string) => `/demo/${encodeURIComponent(demoId)}`;

/** Where a session list row goes: a demo not opened yet goes to its demo page, which makes this browser's copy. */
export const listedHref = (s: Session) => (s.sample ? demoHref(s.demo ?? s.id) : sessionHref(s.id));

/** Every message goes in Auto mode: Claude applies its changes as it goes. */
export function lockedMode(body: MessageCreate): MessageCreate {
  return body.mode === "manual" ? { ...body, mode: "auto" } : body;
}

export function postMessage(sessionId: string, body: MessageCreate): Promise<unknown> {
  return call(`${sessionRoute(sessionId)}/messages`, { method: "POST", body: JSON.stringify(lockedMode(body)) });
}

const queuePath = (sessionId: string, item?: string) =>
  `${sessionRoute(sessionId)}/queue${item ? `/${encodeURIComponent(item)}` : ""}`;

/** The messages waiting for Claude's run to end. */
export function getQueue(sessionId: string): Promise<MessageQueue> {
  return call<MessageQueue>(queuePath(sessionId));
}

/** Queues a message while Claude works: it is sent as a normal message when the run ends. */
export function queueMessage(sessionId: string, body: MessageCreate): Promise<QueuedMessage> {
  return call<QueuedMessage>(queuePath(sessionId), { method: "POST", body: JSON.stringify(lockedMode(body)) });
}

/** Edits a queued message's text, or moves it (`position`, 0: next). */
export function updateQueued(sessionId: string, item: string, body: { text?: string; position?: number }): Promise<QueuedMessage> {
  return call<QueuedMessage>(queuePath(sessionId, item), { method: "PATCH", body: JSON.stringify(body) });
}

/** Takes a message out of the queue before it is sent. */
export function removeQueued(sessionId: string, item: string): Promise<QueuedMessage> {
  return call<QueuedMessage>(queuePath(sessionId, item), { method: "DELETE" });
}

/** Sends a queued message now ("Send anyway"): an open question is left unanswered. */
export function sendQueued(sessionId: string, item: string): Promise<Message> {
  return call<Message>(`${queuePath(sessionId, item)}/send`, { method: "POST" });
}

/** The choices a revision load carries: those of a message. */
/** Accept or ignore (reject) risk fixes, each on its own: the design rebuilds with the accepted ones. */
export function reviewRisks(sessionId: string, body: { accept?: string[]; reject?: string[] }): Promise<RiskFlags> {
  return call<RiskFlags>(`${sessionRoute(sessionId)}/risks`, { method: "POST", body: JSON.stringify(body) });
}

/** Uploads a file to send with a message; `name` defaults to the file's. */
export function uploadAttachment(sessionId: string, file: Blob, name = file instanceof File ? file.name : "attachment"): Promise<Attachment> {
  const form = new FormData();
  form.append("file", file, name);
  return call<Attachment>(`${sessionRoute(sessionId)}/attachments`, { method: "POST", body: form });
}

const constraintPath = (sessionId: string, id: string) =>
  `${sessionRoute(sessionId)}/constraints/${encodeURIComponent(id)}`;

/** Edits a constraint chip; the design is rebuilt and rechecked on the event stream. */
export function updateConstraint(sessionId: string, id: string, value: number | string): Promise<Constraint> {
  return call<Constraint>(constraintPath(sessionId, id), { method: "PATCH", body: JSON.stringify({ value }) });
}

/** Removes a chip from the user's words: the team standard applies again, rebuilt and rechecked. */
export function removeConstraint(sessionId: string, id: string): Promise<Constraint> {
  return call<Constraint>(constraintPath(sessionId, id), { method: "DELETE" });
}

/** Retries the reply that stopped: a new run picks up from the design and builds so far. */
export function retryRun(sessionId: string, choices: { model?: string | null; effort?: Effort | null; mode: Mode }): Promise<Message> {
  return call<Message>(`/sessions/${encodeURIComponent(sessionId)}/retry`, { method: "POST", body: JSON.stringify(choices) });
}

/** Stops the reply that is running, waiting in line, retrying or waiting for an answer; its work so far is kept, Retry is offered and the queue goes on. */
export function stopRun(sessionId: string): Promise<Session> {
  return call<Session>(`/sessions/${encodeURIComponent(sessionId)}/stop`, { method: "POST" });
}

/** The model picker's models: the catalogue entries the server's API key can use. */
export type ModelCatalog = {
  default: string;
  models: { id: string; name: string; blurb: string }[];
  more_models: { id: string; name: string; blurb: string }[];
};

export function listModels(): Promise<ModelCatalog> {
  return call<ModelCatalog>("/models");
}

/** STEP files are imported as a build (`importFile`) rather than attached to a message. */
export function isStepFile(name: string): boolean {
  return /\.(step|stp)$/i.test(name);
}

/** A STEP file that could not be imported; the message says why (size, timeout, unreadable). */
export class ImportError extends Error {}

/**
 * Imports a STEP file into the session as a build: it streams as a `build_ready`, so the canvas
 * shows it, and Claude is told about it with the next message. Throws `ImportError`.
 */
export async function importFile(sessionId: string, file: Blob, name = file instanceof File ? file.name : "import.step"): Promise<Build> {
  const form = new FormData();
  form.append("file", file, name);
  try {
    return await call<Build>(`${sessionRoute(sessionId)}/imports`, { method: "POST", body: form });
  } catch (e) {
    // The server's messages name the file and say what to do; a network failure doesn't.
    const detail = e instanceof ApiError ? e.message : `the server couldn't be reached. Check your connection and try again.`;
    throw new ImportError(detail.includes(name) ? detail : `Could not import ${name}: ${detail}`);
  }
}

export { RECONNECT_DELAYS_MS, type StreamStatus } from "./stream-hub";

/**
 * Subscribes to a session's server-sent events. Returns a function that closes the stream.
 *
 * Everyone following a session shares one connection, in every tab of the browser
 * (`./session-stream`), and hears every event from the start. A dropped connection is reconnected
 * from the last event seen (`Last-Event-ID`, or `after` when the browser gave up and a new stream
 * is opened), so no event is lost or repeated. `onStatus` hears `reconnecting` while that
 * happens, `offline` while the server can't be reached (it keeps trying, with backoff; `window`
 * event `session-stream:retry` tries at once) and `connected` once events flow again. `onError`
 * hears only that the session itself is gone (a 404).
 */
export function subscribe(
  sessionId: string,
  onEvent: (event: SessionEvent) => void,
  onError?: () => void,
  onStatus?: (status: StreamStatus) => void,
): () => void {
  let closed = false;
  const stop = followStream(`/api${sessionRoute(sessionId)}`, (message) => {
    if (closed) return;
    if (message.kind === "event") onEvent(JSON.parse(message.data) as SessionEvent);
    else if (message.kind === "status") onStatus?.(message.status);
    else {
      closed = true;
      onError?.();
    }
  });
  const retryNow = () => retryStreamsNow();
  if (typeof window !== "undefined") window.addEventListener("session-stream:retry", retryNow);
  return () => {
    closed = true;
    stop();
    if (typeof window !== "undefined") window.removeEventListener("session-stream:retry", retryNow);
  };
}

/** A user action only the frontend sees, for the session's telemetry log (docs/architecture.md). */
export type ClientAction =
  | { kind: "view"; view: "map" | "section" | "compare" | "history" | "camera" | "feature_tree" | "units"; value?: string | null }
  | { kind: "choice"; setting: "model" | "effort" | "mode"; value: string }
  | { kind: "change_decision"; change_id: string; decision: "accept" | "reject" };

/** Records `action` in the session's telemetry log. Fire and forget: telemetry never gets in the way. */
export function reportAction(sessionId: string, action: ClientAction): void {
  void fetch(`/api${sessionRoute(sessionId)}/telemetry`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(action),
  }).catch(() => {});
}

/** The notice shown wherever a session is recorded. */
