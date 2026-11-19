"use client";

import { useState } from "react";
import { setSessionStandards } from "@/lib/standards-api";
import { StandardsPicker } from "../StandardsPicker";
import { shownError } from "@/lib/errors";

/**
 * The session's team standards, in the chat header: shows the set the session is designed and
 * checked against, and changes it. Choosing a set (or saving the one in use) rechecks the
 * current design; the chat posts what changed (the session's event stream), and Claude gets the
 * new set from the next message. Only the user changes standards.
 */
export function SessionStandards({
  sessionId,
  value,
  disabledReason,
  className = "",
}: {
  sessionId: string;
  className?: string;
  /** The session's set (`Session.standards`); null: the built-in set. */
  value: string | null;
  disabledReason: string | null;
}) {
  // The set chosen here, until the session's own value changes (a reload shows the server's).
  const [picked, setPicked] = useState<{ from: string | null; id: string } | null>(null);
  const current = picked && picked.from === value ? picked.id : value;
  const [error, setError] = useState<string | null>(null);
  const choose = async (id: string) => {
    setError(null);
    try {
      const session = await setSessionStandards(sessionId, id);
      setPicked({ from: value, id: session.standards ?? id });
    } catch (e) {
      setError(`Could not change the constraint set: ${shownError(e)}`);
    }
  };
  return (
    <div className={`flex min-w-0 items-center gap-1.5 ${className}`}>
      {error && (
        <span role="alert" className="max-w-[200px] truncate text-[12px] text-[var(--cs-accent)]" title={error}>
          {error}
        </span>
      )}
      <StandardsPicker variant="session" value={current} onChange={choose} disabledReason={disabledReason} />
    </div>
  );
}
