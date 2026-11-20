"use client";

import { useEffect, useState } from "react";
import type { ChangeReview, ChangeSet } from "./changes";
import type { CheckUnit, LengthUnit } from "./units";
import type { Board } from "./inspect";
import { sessionRoute } from "@/lib/share";
import { subscribe } from "@/lib/session-api";
// `fetch` here says a dropped connection plainly, and `failure` a failed response (QA fix brief 8).
import { failure, reach as fetch, shownError } from "@/lib/errors";

// Session API types the canvas uses (contracts/session-api.openapi.json). The browser calls
// /api, which Next.js proxies to the backend or, in development and tests, the mock API.

export type BoardRef = { name: string; revision: string };
/** `not_applicable`: standards that don't apply to the design (not failures); absent in older data. */
export type CheckSummary = { pass: number; fail: number; untested: number; not_applicable?: number };

export type Build = {
  id: string;
  created_at: string;
  /** The board the build was checked against; null for a design without a board. */
  board: BoardRef | null;
  status: "ready" | "failed";
  checks: CheckSummary;
  maps: string[];
  mesh_url: string;
  checks_url: string;
  source_url: string;
  /** The board revision of the build, in the board JSON format (`backend/app/boards/SCHEMA.md`); 404 when `board` is null. */
  board_url: string;
  step_url: string | null;
  /** The earlier build whose design this build restores (undo in the history). */
  restored_from?: string | null;
  /** Why a failed build couldn't be built, in plain words; null for a ready build. */
  failure?: string | null;
};

/** Where one feature is (`FeatureSummary`): centroid, size and volume in mm, long-axis direction in degrees. */
export type FeatureSummary = {
  tag: string;
  part: string;
  centroid: [number, number, number] | null;
  size: [number, number, number] | null;
  volume: number;
  rotation: number | null;
};
export type ParameterChange = {
  name: string;
  change: "added" | "removed" | "changed";
  old: unknown;
  new: unknown;
};
/** One feature added, removed, moved, rotated or resized; `distance` in mm, `rotation` in degrees. */
export type FeatureChange = {
  tag: string;
  part: string;
  change: string;
  distance: number | null;
  offset: [number, number, number] | null;
  rotation: number | null;
  old: FeatureSummary | null;
  new: FeatureSummary | null;
};
/** What changed from build `old` to build `new` (`BuildDiff`). */
export type BuildDiff = { old: string; new: string; parameters: ParameterChange[]; features: FeatureChange[] };

export type Session = {
  id: string;
  title: string;
  created_at: string;
  board?: BoardRef | null;
  status: "idle" | "running" | "waiting_for_answer";
  latest_build_id?: string | null;
  /** The board revision the session can load next; null once it is loaded. */
  next_revision?: BoardRef | null;
  /** A demo's hardcoded original (demos/sessions/), before this browser opens its own copy. */
  sample?: boolean;
  /** The demo this session is: its original or this browser's own copy, a session like any other. */
  demo?: string | null;
  /** The standards set the session is designed and checked against; null: the built-in one. */
  standards?: string | null;
  /** Served from a share link: `view` a read-only snapshot, `copy` a design to copy. */
  shared?: "view" | "copy" | null;
};

export type ValueUnit = CheckUnit;
export type CheckValue = { value: number | string; unit?: ValueUnit | null };
export type Required = CheckValue & { kind: "min" | "max" | "tolerance" | "spec" };
/** Where on the model a result is: a surface point in board coordinates (mm) and its faces and edges. */
export type Location = {
  point: [number, number, number];
  part?: string | null;
  faces?: string[];
  edges?: string[];
};

export type CheckResult = {
  id: string;
  check: string;
  title: string;
  standard?: string | null;
  status: "pass" | "fail" | "untested" | "not_applicable";
  /** Why the check doesn't apply to this design, for `not_applicable` results. */
  reason?: string | null;
  /** Reference designator, for checks that concern one board component. */
  component?: string | null;
  opening?: string | null;
  /** The two assembly parts the result is between, e.g. `["lid", "heat_set_insert_1"]`. */
  parts?: [string, string] | null;
  measured?: CheckValue | null;
  required?: Required | null;
  summary: string;
  covers: string;
  location?: Location | null;
};
export type CheckReport = { build_id: string; board: BoardRef | null; summary: CheckSummary; results: CheckResult[] };

/** A design parameter (`Parameter` in the session API contract). */
export type Parameter = {
  name: string;
  value: number | string;
  unit?: "mm" | "deg" | null;
  locked: boolean;
  min?: number | null;
  max?: number | null;
  description?: string;
  /** The feature the parameter drives. */
  feature?: string | null;
};

/** The CAD code that produced a build, as run by the geometry backend. */
/** Where in the code one feature was created: first and last line, 1-based and inclusive. */
export type FeatureSource = {
  tag: string;
  part: string;
  lines: [number, number];
  /** Made by hand (a sketch on a face): Claude keeps it as it is. */
  locked?: boolean;
};
export type BuildSource = {
  build_id: string;
  backend: string;
  language: "python";
  filename: string;
  code: string;
  features: FeatureSource[];
};

/** Every format the exporters write (`ExportFormat` in the session API contract). */
export const EXPORT_FORMATS = ["step", "stl", "3mf", "glb", "obj", "dxf", "pdf", "source", "build_note"] as const;
export type ExportFormat = (typeof EXPORT_FORMATS)[number];
export type ExportFile = { format: ExportFormat; name: string; media_type: string; url: string };
export type Export = { id: string; build_id: string; created_at: string; files: ExportFile[] };

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) throw await failure(res);
  return (await res.json()) as T;
}

/** A session's API path; a share link's snapshot is served at `/api/shares/<token>` (`@/lib/share`). */
export const sessionPath = (id: string) => `/api${sessionRoute(id)}`;

export function getSession(id: string): Promise<Session> {
  return fetch(sessionPath(id)).then((r) => json<Session>(r));
}

export function listBuilds(id: string): Promise<Build[]> {
  return fetch(`${sessionPath(id)}/builds`)
    .then((r) => json<Build[]>(r))
    .then((builds) => builds.map((b) => resolveBuild(id, b)));
}

export function getChecks(build: Build): Promise<CheckReport> {
  return fetch(build.checks_url).then((r) => json<CheckReport>(r));
}

/** The board revision a build is for (board JSON, `backend/app/boards/SCHEMA.md`), at the build's `board_url`. */
export function getBoard(build: Pick<Build, "board_url">): Promise<Board> {
  return fetch(build.board_url).then((r) => json<Board>(r));
}

export function getSource(build: Build): Promise<BuildSource> {
  return fetch(build.source_url).then((r) => json<BuildSource>(r));
}

/** One change to the design in the session's timeline (`HistoryStep`), whoever made it. */
export type HistoryStep = {
  id: string;
  actor: "user" | "claude";
  kind: "parameter" | "edit" | "sketch" | "code" | "run" | "review" | "risks" | "restore" | "import";
  /** What changed: "push/pull 1 mm". */
  label: string;
  /** Who and what: "You: push/pull 1 mm". */
  text: string;
  created_at: string;
  build_id: string | null;
  message_id: string | null;
};
/** The design timeline (`DesignHistory`): `position` steps are applied; Undo and Redo move over `undo` and `redo`. */
export type DesignHistory = { steps: HistoryStep[]; position: number; undo: HistoryStep | null; redo: HistoryStep | null };
/**
 * An undo or redo (`HistoryMove`): the step moved over, the rebuild, the timeline after it, and
 * the change set of the state it moved to (null: none, where `GET /changes` is a 404).
 */
export type HistoryMove = {
  direction: "undo" | "redo";
  step: HistoryStep;
  build: Build;
  history: DesignHistory;
  change_set: ChangeSet | null;
};

export function getHistory(sessionId: string): Promise<DesignHistory> {
  return fetch(`${sessionPath(sessionId)}/history`).then((r) => json<DesignHistory>(r));
}

/** Undo or redo one step of the design timeline; the rebuild also arrives as a `build_ready`. */
export function moveHistory(sessionId: string, direction: "undo" | "redo"): Promise<HistoryMove> {
  return fetch(`${sessionPath(sessionId)}/history/${direction}`, { method: "POST" })
    .then((r) => json<HistoryMove>(r))
    .then((m) => ({ ...m, build: resolveBuild(sessionId, m.build) }));
}

export function getParameters(sessionId: string): Promise<Parameter[]> {
  return fetch(`${sessionPath(sessionId)}/parameters`).then((r) => json<Parameter[]>(r));
}

/** A build's CAD source, reloaded when the build changes; never the previous build's. */
export function useBuildSource(build: Build | undefined): { source: BuildSource | null; error: string | null } {
  const [loaded, setLoaded] = useState<{ url: string; source?: BuildSource; error?: string } | null>(null);
  useEffect(() => {
    if (!build) return;
    let live = true;
    const url = build.source_url;
    getSource(build)
      .then((source) => live && setLoaded({ url, source }))
      .catch((e: Error) => live && setLoaded({ url, error: shownError(e) }));
    return () => {
      live = false;
    };
  }, [build]);
  const current = build && loaded?.url === build.source_url ? loaded : null;
  return { source: current?.source ?? null, error: current?.error ?? null };
}

/** The session's design parameters, reloaded for every build (a rebuild can change them). */
export function useParameters(sessionId: string, buildId: string | undefined): Parameter[] {
  const [loaded, setLoaded] = useState<{ key: string; params: Parameter[] } | null>(null);
  const key = `${sessionId}/${buildId ?? ""}`;
  useEffect(() => {
    let live = true;
    getParameters(sessionId)
      .then((params) => live && setLoaded({ key, params }))
      // Without parameters the tree still lists every feature.
      .catch(() => live && setLoaded({ key, params: [] }));
    return () => {
      live = false;
    };
  }, [sessionId, key]);
  // Until the new build's parameters arrive, keep showing the last ones.
  return loaded?.params ?? [];
}

/** Exports a build. `units` is the build note's length unit; the geometry files stay in mm. */
export function createExport(
  sessionId: string,
  buildId: string,
  formats: readonly ExportFormat[],
  units: LengthUnit = "mm",
): Promise<Export> {
  return fetch(`${sessionPath(sessionId)}/exports`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ build_id: buildId, formats, units }),
  }).then((r) => json<Export>(r));
}

/** One face a find-in-model query matched, with the value it compared (if it compared one). */
export type FindMatch = { face: string; part: string; feature: string; value?: number | null; unit?: "mm" | "deg" | null };
/** What a find-in-model query was read as, and the faces it matched (`FindResult` in the contract). */
export type FindResult = {
  query: string;
  status: "found" | "none" | "unclear";
  understood: string;
  question?: string | null;
  matches: FindMatch[];
};

/** Why edited code was refused (`CodeError`): it doesn't parse, or it doesn't build. */
export class CodeEditError extends Error {
  constructor(
    message: string,
    readonly line: number | null = null,
    readonly column: number | null = null,
  ) {
    super(message);
  }
}

/**
 * A hand edit to the code of `buildId`: the API rebuilds and rechecks with `code` and returns the
 * rebuild (which also arrives as a `build_ready`). Refused code throws a `CodeEditError`.
 */
export async function updateSource(sessionId: string, buildId: string, code: string): Promise<Build> {
  const res = await fetch(`${sessionPath(sessionId)}/source`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ build_id: buildId, code }),
  });
  if (res.status === 400) {
    const body = (await res.json().catch(() => null)) as { detail?: string; line?: number; column?: number } | null;
    throw new CodeEditError(body?.detail ?? "The code was refused.", body?.line ?? null, body?.column ?? null);
  }
  return resolveBuild(sessionId, await json<Build>(res));
}

/** Resolves a build's URLs: relative ones are relative to the session (`/api/sessions/{id}/`). */
export function resolveBuild(sessionId: string, build: Build): Build {
  const abs = (u: string) => (u.startsWith("/") || /^https?:/.test(u) ? u : `${sessionPath(sessionId)}/${u}`);
  return {
    ...build,
    mesh_url: abs(build.mesh_url),
    checks_url: abs(build.checks_url),
    source_url: abs(build.source_url),
    board_url: abs(build.board_url),
    step_url: build.step_url ? abs(build.step_url) : null,
  };
}

/** Adds a build to a list, replacing one with the same ID, oldest first. */
export function withBuild(builds: Build[], build: Build): Build[] {
  return [...builds.filter((b) => b.id !== build.id), build];
}

/** Accepts and rejects changes; the rebuild arrives as a `build_ready`. Returns the updated set. */
export function reviewChanges(sessionId: string, review: ChangeReview): Promise<ChangeSet> {
  return fetch(`${sessionPath(sessionId)}/changes`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(review),
  }).then((r) => json<ChangeSet>(r));
}

/**
 * The session's change set, from its `changes_ready` events (the stream starts from the
 * beginning, so the latest set arrives on connect). A restore retires the set; an undo or redo
 * carries the set of the state it moved to (`HistoryMove.change_set`).
 */
export function useChangeSet(sessionId: string): [ChangeSet | null, (set: ChangeSet | null) => void] {
  const [set, setSet] = useState<ChangeSet | null>(null);
  useEffect(() => {
    let live = true;
    // The session's one shared stream (`subscribe`).
    const close = subscribe(sessionId, (event) => {
      if (!live) return;
      // The canvas's own types for the event's change set and build (`./changes`, `Build` here).
      if (event.type === "changes_ready") setSet((event as unknown as { change_set: ChangeSet }).change_set);
      else if (event.type === "build_ready" && (event.build as unknown as Build).restored_from) setSet(null);
    });
    return () => {
      live = false;
      close();
    };
  }, [sessionId]);
  return [set, setSet];
}

export type SessionBuilds = { session: Session | null; builds: Build[]; error: string | null };

/**
 * The session and its builds: loaded once, then kept up to date from the session's
 * `build_ready` events, so a new build shows up on the canvas as soon as it is ready.
 */
export function useSessionBuilds(sessionId: string | undefined): SessionBuilds {
  const [state, setState] = useState<SessionBuilds>({ session: null, builds: [], error: null });

  useEffect(() => {
    if (!sessionId) return;
    let live = true;
    Promise.all([getSession(sessionId), listBuilds(sessionId)])
      .then(([session, builds]) => {
        if (live) setState((s) => ({ session, builds: s.builds.reduce(withBuild, builds), error: null }));
      })
      .catch((e: Error) => live && setState((s) => ({ ...s, error: shownError(e) })));

    // The first request names a new session, before its reply starts: read the session again
    // then (once: later messages don't rename it).
    let renamed = false;
    const close = subscribe(sessionId, (event) => {
      if (!live) return;
      if (event.type === "message_start") {
        if (renamed) return;
        renamed = true;
        getSession(sessionId)
          .then((session) => live && setState((s) => ({ ...s, session })))
          .catch(() => undefined);
      } else if (event.type === "title") {
        // A rename (Claude naming the part, or the user): the header shows it at once.
        const { title, title_locked } = event;
        setState((s) => (s.session ? { ...s, session: { ...s.session, title, title_locked: !!title_locked } } : s));
      } else if (event.type === "build_ready") {
        const build = event.build as unknown as Build;
        setState((s) => ({ ...s, builds: withBuild(s.builds, resolveBuild(sessionId, build)) }));
      }
    });
    return () => {
      live = false;
      close();
    };
  }, [sessionId]);

  return state;
}
