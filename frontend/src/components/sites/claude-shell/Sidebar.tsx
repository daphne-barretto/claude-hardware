"use client";

import Image from "next/image";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";
import { ChevronDown, PanelLeft, Plus, Search } from "lucide-react";
import { useShell } from "./AppShell";
import { DEMO_USER } from "./data";
import { listedHref, sessionHref } from "@/lib/session-api";
import { RecentRow } from "./RecentRow";

const row =
  "flex h-[34px] w-full items-center gap-3 rounded-lg px-2.5 text-left text-[14px] text-[var(--cs-text)] transition-colors hover:bg-[var(--cs-hover)]";

/** The prototype keeps only New, Search and Recents. */
export function Sidebar({ className = "" }: { className?: string }) {
  const pathname = usePathname();
  const { phone, setSidebarOpen, openSearch, startNewCanvas, sessions, refreshSessions } = useShell();
  // On a phone the sidebar is a drawer over the page: following a link closes it.
  const closeDrawer = () => phone && setSidebarOpen(false);
  const [recentsOpen, setRecentsOpen] = useState(true);

  return (
    <aside
      aria-label="Sidebar"
      className={`flex h-full w-[272px] shrink-0 flex-col border-r border-[var(--cs-border)] bg-[var(--cs-sidebar)] ${className}`}
    >
      <div data-testid="sidebar-header" className="flex h-12 items-center gap-1.5 px-2">
        <button
          type="button"
          aria-label="Hide sidebar"
          onClick={() => setSidebarOpen(false)}
          className="grid size-8 place-items-center rounded-lg text-[var(--cs-muted)] hover:bg-[var(--cs-hover)] hover:text-[var(--cs-text)]"
        >
          <PanelLeft className="size-[18px]" strokeWidth={1.6} />
        </button>
        {/* The wordmark: the welcome page's mark (public/claude-logo.svg) next to "Claude". Nothing else sits here. */}
        <div data-testid="sidebar-wordmark" className="flex items-center gap-1.5">
          <Image src="/claude-logo.svg" alt="" aria-hidden="true" width={20} height={20} className="size-5" />
          <span className="cs-serif text-[19px] font-[460] leading-none tracking-[-0.01em] text-[var(--cs-text)]">
            Claude
          </span>
        </div>
      </div>

      <nav className="flex flex-col gap-0.5 px-2" aria-label="Navigation">
        <Link
          href="/new"
          onClick={() => {
            startNewCanvas();
            closeDrawer();
          }}
          aria-current={pathname === "/new" ? "page" : undefined}
          className={`${row} ${pathname === "/new" ? "bg-[var(--cs-active)]" : ""}`}
        >
          <Plus className="size-[18px]" strokeWidth={1.6} />
          New
        </Link>
        <button type="button" onClick={openSearch} className={row}>
          <Search className="size-[18px]" strokeWidth={1.6} />
          Search
        </button>
      </nav>

      <div className="cs-scroll mt-6 min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        <button
          type="button"
          aria-expanded={recentsOpen}
          onClick={() => setRecentsOpen((v) => !v)}
          className="flex h-7 items-center gap-1 px-2.5 text-[12px] font-[520] text-[var(--cs-muted)]"
        >
          Recents
          <ChevronDown className={`size-3 transition-transform ${recentsOpen ? "" : "-rotate-90"}`} strokeWidth={2} />
        </button>
        {recentsOpen && (
          <ul aria-label="Recents" className="mt-0.5 flex flex-col gap-0.5">
            {sessions?.map((s) => (
              <RecentRow
                key={s.id}
                session={s}
                active={pathname === sessionHref(s.id) || pathname === listedHref(s)}
                onOpen={closeDrawer}
                onRenamed={refreshSessions}
              />
            ))}
            {sessions?.length === 0 && (
              <li className="px-2.5 py-1 text-[13px] text-[var(--cs-faint)]">No sessions yet</li>
            )}
          </ul>
        )}
      </div>

      <div className="flex h-14 items-center gap-2 px-4">
        <span className="grid size-6 place-items-center rounded-full bg-[#0b7d5e] text-[11px] font-semibold text-white">
          {DEMO_USER.initial}
        </span>
        <span className="text-[14px]">{DEMO_USER.name}</span>
        <span className="text-[14px] text-[var(--cs-muted)]">· {DEMO_USER.plan}</span>
      </div>
    </aside>
  );
}
