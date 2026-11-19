"use client";

import { useCallback, useEffect, useState } from "react";
import { getHistory, moveHistory, type Build, type DesignHistory, type HistoryMove } from "./api";
import { plainError, type Direction } from "./designHistory";

export type DesignTimeline = {
  history: DesignHistory | null;
  /** An undo or redo on its way. */
  stepping: boolean;
  /** The last undo or redo, for the toast; cleared by `dismiss`. */
  last: HistoryMove | null;
  /** Why the last undo or redo failed, in plain words. */
  error: string | null;
  move: (direction: Direction) => Promise<HistoryMove | null>;
  dismiss: () => void;
};

/**
 * The session's design timeline, reloaded whenever a build arrives or Claude starts or stops
 * (every change to the design ends in a build or a run), with undo and redo.
 */
export function useDesignHistory(sessionId: string, builds: Build[], busy: boolean): DesignTimeline {
  const [history, setHistory] = useState<DesignHistory | null>(null);
  const [stepping, setStepping] = useState(false);
  const [last, setLast] = useState<HistoryMove | null>(null);
  const [error, setError] = useState<string | null>(null);
  const key = builds.map((b) => b.id).join(",");

  useEffect(() => {
    let live = true;
    getHistory(sessionId)
      .then((h) => live && setHistory(h))
      .catch(() => live && setHistory(null));
    return () => {
      live = false;
    };
  }, [sessionId, key, busy]);

  const move = useCallback(
    (direction: Direction): Promise<HistoryMove | null> => {
      setStepping(true);
      setError(null);
      return moveHistory(sessionId, direction)
        .then((m) => {
          setHistory(m.history);
          setLast(m);
          return m;
        })
        .catch((e: Error) => {
          setError(plainError(e.message, `Couldn't ${direction} that step. Try again.`));
          setLast(null);
          return null;
        })
        .finally(() => setStepping(false));
    },
    [sessionId],
  );

  const dismiss = useCallback(() => {
    setLast(null);
    setError(null);
  }, []);

  return { history, stepping, last, error, move, dismiss };
}
