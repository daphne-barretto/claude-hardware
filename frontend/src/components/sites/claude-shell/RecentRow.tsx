"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { MoreHorizontal, Pencil, RotateCcw } from "lucide-react";
import { demoHref, listedHref, renameSession, resetDemo, sessionHref, type Session } from "@/lib/session-api";
import { DemoTag } from "./DemoSession";
import { shownError } from "@/lib/errors";

const item =
  "flex h-8 w-full items-center gap-2 rounded-lg px-2 text-left text-[13px] text-[var(--cs-text)] hover:bg-[var(--cs-hover)]";
const row =
  "flex h-[34px] w-full items-center gap-3 rounded-lg px-2.5 text-left text-[14px] text-[var(--cs-text)] transition-colors hover:bg-[var(--cs-hover)]";

/**
 * One session in Recents. Its own sessions have a row menu (⋯ → Rename, as in claude.ai): the
 * name turns into a field, Enter or leaving it saves, Escape cancels. A demo is tagged Demo and
 * is otherwise a session like any other: before it is opened the row goes to its demo page,
 * which makes this browser's copy; after, the row is that copy, with Reset demo in its menu.
 * The name is text, never markup.
 */
export function RecentRow({
  session,
  active,
  onOpen,
  onRenamed,
}: {
  session: Session;
  active: boolean;
  onOpen: () => void;
  onRenamed: () => void;
}) {
  const href = listedHref(session);
  const router = useRouter();
  const pathname = usePathname();
  // A demo not opened yet has nothing of this browser's to rename or reset.
  const hasMenu = !session.sample;
  const [menu, setMenu] = useState(false);
  // Reset demo asks first, in the menu: it deletes this browser's changes to the demo.
  const [confirming, setConfirming] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(session.title);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const shown = useRef<HTMLLIElement>(null);
  // Set once Enter or Escape has ended the edit, so the field's blur doesn't save again.
  const ended = useRef(false);

  // Close the menu on a click elsewhere.
  useEffect(() => {
    if (!menu) return;
    const close = (e: MouseEvent) => {
      if (!shown.current?.contains(e.target as Node)) {
        setMenu(false);
        setConfirming(false);
      }
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [menu]);

  const startRename = () => {
    setMenu(false);
    setDraft(session.title);
    setError(null);
    ended.current = false;
    setEditing(true);
  };

  const save = async () => {
    if (saving) return;
    const title = draft.trim();
    if (!title || title === session.title) {
      setEditing(false);
      return;
    }
    setSaving(true);
    try {
      await renameSession(session.id, title);
      setEditing(false);
      setError(null);
      onRenamed();
    } catch (e) {
      setError(shownError(e, "Couldn't rename the session"));
    } finally {
      setSaving(false);
    }
  };

  const reset = async () => {
    setMenu(false);
    setConfirming(false);
    if (!session.demo) return;
    try {
      await resetDemo(session.demo);
      setError(null);
      onRenamed();
      // Viewing it: open the fresh copy.
      if (pathname === sessionHref(session.id)) router.replace(demoHref(session.demo));
    } catch (e) {
      setError(shownError(e, "Couldn't reset the demo"));
    }
  };

  if (editing) {
    return (
      <li ref={shown} className="px-1">
        <input
          autoFocus
          aria-label="Session name"
          value={draft}
          maxLength={60}
          disabled={saving}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              ended.current = true;
              void save();
            }
            if (e.key === "Escape") {
              ended.current = true;
              setEditing(false);
            }
          }}
          onBlur={() => {
            if (!ended.current) void save();
          }}
          className="h-[34px] w-full rounded-lg border border-[var(--cs-border)] bg-[var(--cs-bg)] px-2 text-[14px] text-[var(--cs-text)] outline-none focus:border-[var(--cs-accent)]"
        />
        {error && (
          <p role="alert" className="px-1.5 pt-1 text-[12px] text-red-600">
            {error}
          </p>
        )}
      </li>
    );
  }

  return (
    <li ref={shown} className="group relative">
      <Link
        href={href}
        onClick={onOpen}
        aria-current={active ? "page" : undefined}
        data-session={session.id}
        // Every row keeps room for the options button, so the Demo tags line up.
        className={`${row} pr-9 ${active ? "bg-[var(--cs-active)]" : ""}`}
      >
        <span className="truncate">{session.title}</span>
        {session.demo && <DemoTag />}
      </Link>
      {hasMenu && (
        <button
          type="button"
          aria-label={`Options for ${session.title}`}
          aria-haspopup="menu"
          aria-expanded={menu}
          onClick={() => {
            setMenu((v) => !v);
            setConfirming(false);
          }}
          className={`absolute right-1.5 top-1/2 grid size-6 -translate-y-1/2 place-items-center rounded-md text-[var(--cs-muted)] hover:bg-[var(--cs-hover)] hover:text-[var(--cs-text)] ${menu || active ? "opacity-100" : "opacity-0 group-hover:opacity-100 focus:opacity-100"}`}
        >
          <MoreHorizontal className="size-4" strokeWidth={1.8} />
        </button>
      )}
      {menu && (
        <div
          role="menu"
          aria-label={`${session.title} options`}
          className="absolute right-1 top-[calc(100%+2px)] z-20 min-w-[140px] rounded-xl border border-[var(--cs-border)] bg-[var(--cs-bg)] p-1 shadow-lg"
        >
          {confirming ? (
            <>
              <p className="max-w-[220px] px-2 py-1.5 text-[12px] text-[var(--cs-muted)]">
                Your changes to this demo are deleted, and it starts over as it was.
              </p>
              <button type="button" role="menuitem" onClick={() => void reset()} className={item}>
                <RotateCcw className="size-3.5" strokeWidth={1.8} />
                Reset
              </button>
              <button type="button" role="menuitem" onClick={() => setConfirming(false)} className={item}>
                Cancel
              </button>
            </>
          ) : (
            <>
              <button type="button" role="menuitem" onClick={startRename} className={item}>
                <Pencil className="size-3.5" strokeWidth={1.8} />
                Rename
              </button>
              {session.demo && (
                <button type="button" role="menuitem" onClick={() => setConfirming(true)} className={item}>
                  <RotateCcw className="size-3.5" strokeWidth={1.8} />
                  Reset demo
                </button>
              )}
            </>
          )}
        </div>
      )}
      {error && !editing && (
        <p role="alert" className="px-2.5 pt-1 text-[12px] text-red-600">
          {error}
        </p>
      )}
    </li>
  );
}
