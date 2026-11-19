"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useShell } from "./AppShell";
import { openDemo, sessionHref } from "@/lib/session-api";
import { shownError } from "@/lib/errors";

/** The small tag after a demo's title in Recents. Everything else about a demo is a session like any other. */
export function DemoTag() {
  return (
    <span
      data-testid="demo-tag"
      className="ml-auto shrink-0 rounded-[5px] border border-[var(--cs-border)] px-1.5 text-[11px] leading-[18px] text-[var(--cs-muted)]"
    >
      Demo
    </span>
  );
}

/**
 * A demo's page (`/demo/<id>`): opens this browser's own copy of the demo, made from its
 * hardcoded original the first time, and shows it at its session page. Reloading or sharing
 * the link always lands on the viewer's own copy.
 */
export function DemoSession({ demoId }: { demoId: string }) {
  const { refreshSessions } = useShell();
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  // React's development double effect must not open it twice (the server would give the same copy anyway).
  const started = useRef(false);
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    openDemo(demoId)
      .then((session) => {
        refreshSessions();
        router.replace(sessionHref(session.id));
      })
      .catch((e) => setError(shownError(e, "Couldn't open the demo")));
  }, [demoId, refreshSessions, router]);
  return (
    <div role="status" data-testid="demo-opening" className="mx-auto flex max-w-[360px] flex-col items-center gap-2 py-16 text-center">
      {error ? (
        <p role="alert" className="text-[14px] text-[var(--cs-text)]">
          {error}
        </p>
      ) : (
        <p className="text-[14px] text-[var(--cs-muted)]">Opening the demo...</p>
      )}
    </div>
  );
}
