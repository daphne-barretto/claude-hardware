"use client";

import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { Box, Search } from "lucide-react";
import { listedHref, type Session } from "@/lib/session-api";

/** Search covers sessions: their titles and boards. */
export function searchSessions(sessions: Session[], query: string) {
  const q = query.trim().toLowerCase();
  if (!q) return sessions;
  return sessions.filter((s) =>
    [s.title, s.board?.name, s.board?.revision].some((v) => v?.toLowerCase().includes(q)),
  );
}

export function SearchDialog({ sessions, onClose }: { sessions: Session[]; onClose: () => void }) {
  const router = useRouter();
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const results = useMemo(() => searchSessions(sessions, query), [sessions, query]);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const go = (i: number) => {
    const p = results[i];
    if (!p) return;
    router.push(listedHref(p));
    onClose();
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/50 px-4 pt-[12vh]"
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Search sessions"
        className="w-full max-w-[640px] overflow-hidden rounded-[14px] border border-[var(--cs-border)] bg-[var(--cs-surface-2)] shadow-2xl"
      >
        <div className="flex items-center gap-3 border-b border-[var(--cs-border)] px-4">
          <Search className="size-[18px] text-[var(--cs-muted)]" strokeWidth={1.6} />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setIndex(0);
            }}
            onKeyDown={(e) => {
              if (e.key === "Escape") onClose();
              if (e.key === "ArrowDown") {
                e.preventDefault();
                setIndex((i) => Math.min(i + 1, results.length - 1));
              }
              if (e.key === "ArrowUp") {
                e.preventDefault();
                setIndex((i) => Math.max(i - 1, 0));
              }
              if (e.key === "Enter" && !e.nativeEvent.isComposing) go(index);
            }}
            placeholder="Search sessions…"
            className="h-12 flex-1 bg-transparent text-[15px] outline-none placeholder:text-[var(--cs-faint)]"
          />
        </div>
        <div className="px-2 py-2">
          <div className="px-2.5 pb-1 pt-1 text-[12px] font-[520] text-[var(--cs-muted)]">
            {query ? `${results.length} session${results.length === 1 ? "" : "s"}` : "Sessions"}
          </div>
          {results.length === 0 ? (
            <p className="px-2.5 py-6 text-center text-[var(--cs-muted)]">{query ? <>No sessions match “{query}”.</> : "No sessions yet."}</p>
          ) : (
            <ul className="cs-scroll max-h-[50vh] overflow-y-auto">
              {results.map((p, i) => (
                <li key={p.id}>
                  <button
                    type="button"
                    onMouseEnter={() => setIndex(i)}
                    onClick={() => go(i)}
                    className={`flex w-full items-center gap-3 rounded-lg px-2.5 py-2 text-left ${
                      i === index ? "bg-[var(--cs-hover)]" : ""
                    }`}
                  >
                    <Box className="size-4 shrink-0 text-[var(--cs-muted)]" strokeWidth={1.6} />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate">{p.title}</span>
                      {p.board && (
                        <span className="block truncate text-[12px] text-[var(--cs-muted)]">
                          {p.board.name} {p.board.revision}
                        </span>
                      )}
                    </span>
                    <span className="shrink-0 text-[12px] text-[var(--cs-faint)]">
                      {new Date(p.created_at).toLocaleDateString()}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
