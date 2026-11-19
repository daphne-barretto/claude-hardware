// Share links (backend/app/share_routes.py, docs/architecture.md, "Queue, history, and shares").
//
// A session's owner shares it at an unguessable `/share/<token>`: `view` is a read-only snapshot
// of the chat and the model, `copy` the latest design alone, which anyone with the link can copy
// into a session of their own. The page at `/share/<token>` shows a snapshot as the session
// `share-<token>`: its reads go to `/api/shares/<token>/...` (`sessionRoute`), so the chat and
// the canvas need nothing else to show it.

import { responseMessage } from "./errors";

export type ShareKind = "view" | "copy";

export type ShareLink = {
  token: string;
  kind: ShareKind;
  /** The page to share: `/share/<token>`. */
  url: string;
  active: boolean;
  attachments: boolean;
  created_at: string;
  updated_at: string;
  /** False once the session has new messages or builds, which aren't shared until updated. */
  up_to_date: boolean;
};

const PREFIX = "share-";

/** The session ID a share link's snapshot has on the page. */
export const shareSessionId = (token: string) => PREFIX + token;

/** The token of a share link's session ID, or null for a session of one's own. */
export const shareToken = (sessionId: string): string | null =>
  sessionId.startsWith(PREFIX) ? sessionId.slice(PREFIX.length) : null;

/** A session's path on the session API, without `/api`: a share link's snapshot is under `/shares`. */
export function sessionRoute(sessionId: string): string {
  const token = shareToken(sessionId);
  return token ? `/shares/${encodeURIComponent(token)}` : `/sessions/${encodeURIComponent(sessionId)}`;
}

/** What each kind of link shares, as the menu says it. */
export const SHARE_COPY: Record<ShareKind, { title: string; says: string }> = {
  view: {
    title: "Share chat",
    says: "Anyone with the link can see the chat, the model and its files. Messages you send later aren't shared until you update the link.",
  },
  copy: {
    title: "Share a copy",
    says: "Anyone with the link can make their own editable copy of this design. Your chat isn't included.",
  },
};

/** What a shared view says in place of the composer. */
export const SHARED_READ_ONLY = "Shared by link · read-only";
/** Why the canvas's fix requests are off in a shared view. */
export const SHARED_BLOCKED = "Shared links are read-only";
/** A link whose owner stopped sharing. */
export const TURNED_OFF = "This link was turned off";
export const NO_SUCH_LINK = "This link doesn't exist";

/** The page's full link, for the clipboard. */
export const shareHref = (link: Pick<ShareLink, "url">) =>
  (typeof window === "undefined" ? "" : window.location.origin) + link.url;

/** An error from the share routes, with the API's `detail`. */
export class ShareError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body) headers.set("Content-Type", "application/json");
  const res = await fetch(`/api${path}`, { ...init, headers });
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as { detail?: string } | null;
    throw new ShareError(responseMessage(res.status, body?.detail), res.status);
  }
  return (await res.json()) as T;
}

const shares = (sessionId: string) => `/sessions/${encodeURIComponent(sessionId)}/shares`;

/** The session's live links (owner only). */
export function listShareLinks(sessionId: string): Promise<ShareLink[]> {
  return call<ShareLink[]>(shares(sessionId));
}

export function createShareLink(sessionId: string, kind: ShareKind, attachments = false): Promise<ShareLink> {
  return call<ShareLink>(shares(sessionId), { method: "POST", body: JSON.stringify({ kind, attachments }) });
}

/** Takes the snapshot again: same link, the session as it is now. */
export function updateShareLink(sessionId: string, kind: ShareKind, attachments = false): Promise<ShareLink> {
  return call<ShareLink>(`${shares(sessionId)}/update`, { method: "POST", body: JSON.stringify({ kind, attachments }) });
}

/** Stops sharing: the link is turned off for good. */
export function revokeShareLink(sessionId: string, kind: ShareKind): Promise<ShareLink> {
  return call<ShareLink>(`${shares(sessionId)}/revoke`, { method: "POST", body: JSON.stringify({ kind }) });
}

/** What a link's page is: a live snapshot of a kind, a turned-off link, or none. */
export type LinkState = { status: "loading" } | { status: "live"; kind: ShareKind; title: string } | { status: "off" } | { status: "missing" };

export async function linkState(token: string): Promise<LinkState> {
  const res = await fetch(`/api/shares/${encodeURIComponent(token)}`);
  if (res.status === 410) return { status: "off" };
  if (!res.ok) return { status: "missing" };
  const session = (await res.json()) as { shared?: ShareKind | null; title: string };
  return { status: "live", kind: session.shared === "copy" ? "copy" : "view", title: session.title };
}
