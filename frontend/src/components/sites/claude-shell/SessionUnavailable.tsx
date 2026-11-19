"use client";

import { useEffect, useState } from "react";
import { sessionRoute } from "@/lib/share";

/**
 * Whether the session API has no such session for this browser: sessions belong to the
 * browser that started them, so another browser's session answers 404, as an unknown one does.
 */
export function useSessionMissing(sessionId: string | undefined): boolean {
  const [missing, setMissing] = useState(false);
  useEffect(() => {
    if (!sessionId) return;
    let live = true;
    fetch(`/api${sessionRoute(sessionId)}`)
      .then((r) => live && setMissing(r.status === 404))
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [sessionId]);
  return missing;
}

/**
 * In place of the chat when the session isn't this browser's (or doesn't exist). It adds no
 * control of its own: New, in the sidebar, starts a session here.
 */
export function SessionUnavailable() {
  return (
    <div role="status" data-testid="session-unavailable" className="mx-auto flex max-w-[360px] flex-col items-center gap-2 py-16 text-center">
      <p className="text-[15px] text-[var(--cs-text)]">This session isn&apos;t available in this browser.</p>
      <p className="text-[13px] text-[var(--cs-muted)]">
        Each browser keeps its own sessions, so one person&apos;s work never shows up for another. Start a new one with New.
      </p>
    </div>
  );
}
