"use client";

import { createContext, useCallback, useContext, useEffect, useState, useSyncExternalStore } from "react";
import { usePathname } from "next/navigation";
import { listSessions, recentsOrder, type Session } from "@/lib/session-api";
import type { Choices } from "./chat";
import { Sidebar } from "./Sidebar";
import { SearchDialog } from "./SearchDialog";

/** What a new canvas opens with. Kept in client state, never in the URL, so it stays out of history and shared links. */
export type NewCanvasStart = { id: number; prompt?: string; files: string[]; choices?: Choices };

/** Below this width the shell is the phone layout: a sidebar drawer, and one pane at a time. */
export const PHONE_QUERY = "(max-width: 767px)";

function subscribePhone(onChange: () => void) {
  const query = window.matchMedia?.(PHONE_QUERY);
  query?.addEventListener("change", onChange);
  return () => query?.removeEventListener("change", onChange);
}

/** True at phone width. The server renders the desktop layout; CSS keeps the first paint right on a phone. */
export function usePhone() {
  return useSyncExternalStore(
    subscribePhone,
    () => window.matchMedia?.(PHONE_QUERY).matches ?? false,
    () => false,
  );
}

type ShellState = {
  /** The sidebar is shown: in the layout on a desktop, as a drawer over the page on a phone. */
  sidebarOpen: boolean;
  /** The phone layout (PHONE_QUERY): one pane at a time. */
  phone: boolean;
  newCanvas: NewCanvasStart;
  /** Starts a fresh canvas, optionally with a first message. Navigate to /new after calling it. */
  startNewCanvas: (prompt?: string, files?: string[], choices?: Choices) => void;
  setSidebarOpen: (open: boolean) => void;
  openSearch: () => void;
  /** Sessions from the session API, newest first; null until loaded, or when the API is unreachable. */
  sessions: Session[] | null;
  /** Reloads `sessions`, for example after a session was created. */
  refreshSessions: () => void;
};

const ShellContext = createContext<ShellState | null>(null);

export function useShell() {
  const ctx = useContext(ShellContext);
  if (!ctx) throw new Error("useShell must be used inside AppShell");
  return ctx;
}

export function AppShell({ children }: { children: React.ReactNode }) {
  // On a desktop the sidebar starts open (as in the mockups) and then keeps the user's last
  // choice across navigations; pages never reset it. On a phone it is a drawer over the page,
  // as in the Claude app: closed until opened, and closed again on navigating.
  const phone = usePhone();
  const [desktopOpen, setDesktopOpen] = useState(true);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const sidebarOpen = phone ? drawerOpen : desktopOpen;
  const pathname = usePathname();
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => setDrawerOpen(false), [pathname]);
  const [searchOpen, setSearchOpen] = useState(false);
  // Marks the shell interactive once React has hydrated (e2e tests wait for it).
  const [ready, setReady] = useState(false);
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => setReady(true), []);
  const setSidebarOpen = useCallback((open: boolean) => (phone ? setDrawerOpen : setDesktopOpen)(open), [phone]);
  const [newCanvas, setNewCanvas] = useState<NewCanvasStart>({ id: 0, files: [] });
  const startNewCanvas = useCallback(
    (prompt?: string, files: string[] = [], choices?: Choices) =>
      setNewCanvas((c) => ({ id: c.id + 1, prompt: prompt || undefined, files, choices })),
    [],
  );

  const [sessions, setSessions] = useState<Session[] | null>(null);
  const [sessionsVersion, setSessionsVersion] = useState(0);
  const refreshSessions = useCallback(() => setSessionsVersion((v) => v + 1), []);
  const openSearch = useCallback(() => {
    setDrawerOpen(false);
    setSearchOpen(true);
    refreshSessions();
  }, [refreshSessions]);
  useEffect(() => {
    let live = true;
    listSessions()
      .then((list) => live && setSessions(recentsOrder(list)))
      .catch(() => live && setSessions(null));
    return () => {
      live = false;
    };
  }, [sessionsVersion]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        openSearch();
      } else if (e.key === "Escape") setDrawerOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openSearch]);

  return (
    <ShellContext.Provider
      value={{ sidebarOpen, phone, setSidebarOpen, openSearch, newCanvas, startNewCanvas, sessions, refreshSessions }}
    >
      <div data-shell-ready={ready ? "true" : undefined} className="flex h-full min-h-0 w-full overflow-hidden">
        {/* Shown by CSS per layout, so a phone never paints the desktop sidebar before hydrating. */}
        {(desktopOpen || drawerOpen) && (
          <Sidebar
            className={`${desktopOpen ? "" : "md:hidden"} ${
              drawerOpen ? "max-md:fixed max-md:inset-y-0 max-md:left-0 max-md:z-40 max-md:w-[min(288px,85vw)] max-md:shadow-2xl" : "max-md:hidden"
            }`}
          />
        )}
        {drawerOpen && (
          // Tapping the page beside the drawer closes it (Escape and Hide sidebar do too).
          <div aria-hidden="true" data-testid="sidebar-backdrop" onClick={() => setDrawerOpen(false)} className="fixed inset-0 z-30 bg-black/50 md:hidden" />
        )}
        <main className="relative flex min-w-0 flex-1 flex-col">{children}</main>
      </div>
      {searchOpen && (
        <SearchDialog
          sessions={sessions ?? []}
          onClose={() => setSearchOpen(false)}
        />
      )}
    </ShellContext.Provider>
  );
}
