"use client";

import { useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { Copy } from "lucide-react";
import { createSession, sessionHref } from "@/lib/session-api";
import { SHARED_READ_ONLY } from "@/lib/share";
import { useShell } from "./AppShell";
import { shownError } from "@/lib/errors";

/**
 * Starts a session of the browser's own from a share link (`POST /api/sessions` with
 * `from_share`) and opens it. Returns the action and its error, if it failed.
 */
export function useCopyFromShare(token: string): [() => Promise<void>, string | null, boolean] {
  const router = useRouter();
  const { refreshSessions } = useShell();
  const [error, setError] = useState<string | null>(null);
  const [working, setWorking] = useState(false);
  const started = useRef(false);
  const start = async () => {
    if (started.current) return;
    started.current = true;
    setWorking(true);
    setError(null);
    try {
      const session = await createSession({ from_share: token });
      refreshSessions();
      router.push(sessionHref(session.id));
    } catch (e) {
      started.current = false;
      setWorking(false);
      setError(`Could not make a copy: ${shownError(e)}`);
    }
  };
  return [start, error, working];
}

/** Takes the composer's place on a read-only shared link: says so and offers Continue from this. */
export function SharedNotice({ token }: { token: string }) {
  const [start, error, working] = useCopyFromShare(token);
  return (
    <div
      role="status"
      data-testid="shared-notice"
      className="flex flex-col gap-1 rounded-[20px] border border-[var(--cs-border)] bg-[var(--cs-surface)] px-4 py-3 text-[13px] text-[var(--cs-muted)]"
    >
      <div className="flex items-center gap-3">
        <p className="min-w-0 flex-1">{SHARED_READ_ONLY}</p>
        <button
          type="button"
          disabled={working}
          onClick={() => void start()}
          title="Make your own copy of this session, with its chat and model, and keep working on it"
          className="flex h-8 shrink-0 items-center gap-1.5 rounded-lg bg-[var(--cs-text)] px-3 text-[13px] font-[520] text-[var(--cs-bg)] enabled:hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Copy className="size-3.5" strokeWidth={2} />
          Continue from this
        </button>
      </div>
      {error && <p className="text-[12px] text-[var(--cs-accent)]">{error}</p>}
    </div>
  );
}
