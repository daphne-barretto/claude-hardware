"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Check, ChevronDown, Ruler, Settings2 } from "lucide-react";
import { listStandards, type StandardsSet } from "@/lib/session-api";
import { CHOOSE_STANDARDS } from "./data";
import { MANAGE_STANDARDS, StandardsEditor } from "./standards/StandardsEditor";

type Props = {
  /** The chosen set's ID, or null before one is chosen. */
  value: string | null;
  onChange: (id: string) => void;
  /**
   * "new": the menu under the empty chat; "session": the compact button in a session's chat
   * header, showing the set the session is checked against (the built-in one until chosen).
   */
  variant?: "new" | "session";
  /** Why the set can't be changed right now (e.g. Claude is working); null when it can. */
  disabledReason?: string | null;
};

/**
 * The "Choose team standards" menu (New mockup), and in a session the button that shows and
 * changes the session's set. Lists the sets the session API serves (the built-in set and the
 * browser's own); "Manage constraint sets…" opens the editor (docs/constraints.md). Hidden until it has
 * sets, so it is never a dead control.
 */
export function StandardsPicker({ value, onChange, variant = "new", disabledReason = null }: Props) {
  const [sets, setSets] = useState<StandardsSet[]>([]);
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  const load = useCallback(() => {
    let live = true;
    listStandards()
      .then((s) => live && setSets(s))
      .catch(() => live && setSets([]));
    return () => {
      live = false;
    };
  }, []);
  useEffect(load, [load]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  if (!sets.length) return null;
  const session = variant === "session";
  // A session that chose nothing is checked against the built-in set.
  const current = value ?? (session ? (sets.find((s) => s.built_in) ?? sets[0]).id : null);
  const chosen = sets.find((s) => s.id === current);

  const editor = editing && (
    <StandardsEditor
      initial={current}
      active={current}
      onUse={(id) => {
        onChange(id);
        setEditing(false);
        load();
      }}
      onSaved={(saved) => {
        load();
        // The chat uses the set that changed: recheck against the saved rules.
        if (session && saved.id === current) onChange(saved.id);
      }}
      onClose={() => {
        setEditing(false);
        load();
      }}
    />
  );

  return (
    // In a session's chat header the button shrinks (its name truncates) rather than run over
    // the header's buttons, such as Show canvas on a phone.
    <div ref={ref} className={session ? "relative min-w-0" : "relative mt-5"}>
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={session ? `Constraint set: ${chosen?.name ?? current}` : undefined}
        disabled={!!disabledReason}
        title={disabledReason ?? (session ? "The constraint set this chat is designed and checked against" : undefined)}
        onClick={() => setOpen((v) => !v)}
        className={
          session
            ? "flex h-7 min-w-0 max-w-[min(220px,100%)] items-center gap-1.5 rounded-lg border border-[var(--cs-border)] px-2 text-[12px] enabled:hover:bg-[var(--cs-hover)] disabled:cursor-not-allowed disabled:opacity-50"
            : "flex h-8 items-center gap-1.5 rounded-lg bg-[var(--cs-active)] px-3 text-[13px] hover:bg-[var(--cs-hover)]"
        }
      >
        {session && <Ruler aria-hidden className="size-3.5 shrink-0 text-[var(--cs-muted)]" />}
        <span className="truncate">{chosen?.name ?? CHOOSE_STANDARDS}</span>
        <ChevronDown className="size-3 shrink-0 text-[var(--cs-muted)]" />
      </button>
      {open && (
        <div
          role="menu"
          aria-label="Constraint set"
          className={`absolute z-40 w-[304px] rounded-[14px] border border-[var(--cs-border)] bg-[var(--cs-surface)] p-1.5 text-left shadow-[0_8px_30px_rgba(0,0,0,0.35)] ${
            session ? "right-0 top-[calc(100%+6px)]" : "left-1/2 top-[calc(100%+6px)] -translate-x-1/2"
          }`}
        >
          {sets.map((s) => {
            const on = s.standards.filter((r) => r.enabled !== false).length;
            return (
              <button
                key={s.id}
                type="button"
                role="menuitemradio"
                aria-checked={s.id === current}
                title={s.standards.map((st) => st.label ?? st.title).join(", ")}
                onClick={() => {
                  onChange(s.id);
                  setOpen(false);
                }}
                className="flex w-full items-center gap-2 rounded-lg px-3 py-1.5 text-left hover:bg-[var(--cs-hover)]"
              >
                <span className="flex-1">
                  <span className="block text-[14px]">{s.name}</span>
                  <span className="block text-[13px] text-[var(--cs-muted)]">
                    {s.standards.length} {s.standards.length === 1 ? "constraint" : "constraints"}
                    {on < s.standards.length ? ` · ${s.standards.length - on} off` : ""}
                  </span>
                </span>
                {s.id === current && <Check className="size-4 text-[var(--cs-link)]" />}
              </button>
            );
          })}
          <div className="my-1 border-t border-[var(--cs-border)]" />
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              setEditing(true);
            }}
            className="flex w-full items-center gap-2 rounded-lg px-3 py-1.5 text-left text-[14px] hover:bg-[var(--cs-hover)]"
          >
            <Settings2 className="size-4 text-[var(--cs-muted)]" />
            {MANAGE_STANDARDS}
          </button>
        </div>
      )}
      {editor}
    </div>
  );
}
