"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Check, Copy, Link2, Lock } from "lucide-react";
import {
  createShareLink,
  listShareLinks,
  revokeShareLink,
  shareHref,
  SHARE_COPY,
  updateShareLink,
  type ShareKind,
  type ShareLink,
} from "@/lib/share";
import { shownError } from "@/lib/errors";

const KINDS: ShareKind[] = ["view", "copy"];
const BUSY = "Claude is still replying. Share once the reply has finished.";

const btn =
  "flex h-7 items-center gap-1.5 rounded-lg border border-[var(--cs-border)] px-2.5 text-[12px] enabled:hover:bg-[var(--cs-hover)] disabled:cursor-not-allowed disabled:opacity-50";
const primary =
  "flex h-7 items-center gap-1.5 rounded-lg bg-[var(--cs-text)] px-2.5 text-[12px] font-[520] text-[var(--cs-bg)] enabled:hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50";

/**
 * Share, on a session of one's own: a small menu that shares it like a chat on claude.ai. "Share
 * chat" makes a read-only snapshot of the session at an unguessable link; "Share a copy" shares
 * the latest design alone, for anyone with the link to copy into a session of their own. Each
 * link can be copied, updated to the session as it is now (same link) or turned off.
 */
export function ShareMenu({ sessionId, busy }: { sessionId: string; busy: boolean }) {
  const [open, setOpen] = useState(false);
  const [links, setLinks] = useState<Partial<Record<ShareKind, ShareLink>>>({});
  const [error, setError] = useState<string | null>(null);
  const ref = useRef<HTMLDivElement>(null);

  const load = useCallback(() => {
    listShareLinks(sessionId)
      .then((list) => setLinks(Object.fromEntries(list.map((l) => [l.kind, l]))))
      .catch((e: Error) => setError(shownError(e)));
  }, [sessionId]);

  useEffect(() => {
    if (!open) return;
    load();
    const onDown = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, load]);

  const live = KINDS.some((k) => links[k]?.active);

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="flex h-8 items-center gap-1.5 rounded-lg px-2.5 text-[14px] hover:bg-[var(--cs-hover)]"
      >
        {live ? <Link2 className="size-3.5" /> : <Lock className="size-3.5" />}
        Share
      </button>
      {open && (
        <div
          role="dialog"
          aria-label="Share"
          className="absolute right-0 top-[calc(100%+6px)] z-30 flex w-[340px] max-w-[calc(100vw-32px)] flex-col gap-3 rounded-xl border border-[var(--cs-border)] bg-[var(--cs-surface)] p-3 text-left text-[13px] shadow-lg"
        >
          {KINDS.map((kind) => (
            <ShareSection
              key={kind}
              kind={kind}
              sessionId={sessionId}
              link={links[kind]}
              busy={busy}
              onChange={(link) => setLinks((l) => ({ ...l, [kind]: link ?? undefined }))}
              onError={setError}
            />
          ))}
          {error && (
            <p role="alert" className="text-[12px] text-[var(--cs-accent)]">
              {error}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function ShareSection({
  kind,
  sessionId,
  link,
  busy,
  onChange,
  onError,
}: {
  kind: ShareKind;
  sessionId: string;
  link: ShareLink | undefined;
  busy: boolean;
  onChange: (link: ShareLink | null) => void;
  onError: (error: string | null) => void;
}) {
  const { title, says } = SHARE_COPY[kind];
  const [attachments, setAttachments] = useState(link?.attachments ?? false);
  const [working, setWorking] = useState(false);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const t = setTimeout(() => setCopied(false), 2000);
    return () => clearTimeout(t);
  }, [copied]);

  const run = async (action: () => Promise<ShareLink | null>) => {
    setWorking(true);
    onError(null);
    try {
      onChange(await action());
    } catch (e) {
      onError(shownError(e));
    } finally {
      setWorking(false);
    }
  };
  const copy = async (l: ShareLink) => {
    try {
      await navigator.clipboard.writeText(shareHref(l));
      setCopied(true);
    } catch {
      window.prompt("Copy this link", shareHref(l));
    }
  };
  const create = () =>
    run(async () => {
      const made = await createShareLink(sessionId, kind, attachments);
      await copy(made);
      return made;
    });
  const why = busy ? BUSY : undefined;
  const active = link?.active ? link : undefined;

  return (
    <section aria-label={title} data-attachments={attachments ? "on" : "off"} className="flex flex-col gap-2 rounded-lg border border-[var(--cs-border)] p-2.5">
      <h3 className="text-[13px] font-[520] text-[var(--cs-text)]">{title}</h3>
      <p className="text-[12px] leading-[17px] text-[var(--cs-muted)]">{says}</p>
      {kind === "view" && (
        <label className="flex items-center gap-2 text-[12px] text-[var(--cs-muted)]">
          <input type="checkbox" checked={attachments} onChange={(e) => setAttachments(e.target.checked)} />
          Include attached files (otherwise only their names)
        </label>
      )}
      {active ? (
        <>
          <p data-testid={`share-status-${kind}`} className="flex items-center gap-1.5 text-[12px] text-[var(--cs-text)]">
            <span aria-hidden className="size-1.5 rounded-full bg-emerald-500" />
            Link is live
            <span className="text-[var(--cs-muted)]">
              · {active.up_to_date ? "up to date" : kind === "view" ? "newer messages aren't shared yet" : "newer changes aren't shared yet"}
            </span>
          </p>
          <p
            data-testid={`share-url-${kind}`}
            title={shareHref(active)}
            className="truncate rounded-md border border-[var(--cs-border)] px-2 py-1 font-mono text-[11px] text-[var(--cs-muted)] select-all"
          >
            {shareHref(active)}
          </p>
          <div className="flex flex-wrap gap-1.5">
            <button type="button" onClick={() => void copy(active)} className={primary}>
              {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
              <span aria-live="polite">{copied ? "Link copied" : "Copy link"}</span>
            </button>
            <button
              type="button"
              disabled={working || busy}
              title={why ?? "Share the session as it is now, at the same link"}
              onClick={() => void run(() => updateShareLink(sessionId, kind, attachments))}
              className={btn}
            >
              Update link
            </button>
            <button
              type="button"
              disabled={working}
              title="Turn the link off: it stops working for everyone"
              onClick={() => {
                setCopied(false);
                void run(async () => (await revokeShareLink(sessionId, kind), null));
              }}
              className={btn}
            >
              Stop sharing
            </button>
          </div>
        </>
      ) : (
        <div>
          <button type="button" disabled={working || busy} title={why} onClick={() => void create()} className={primary}>
            <Link2 className="size-3.5" />
            Create link
          </button>
        </div>
      )}
    </section>
  );
}
