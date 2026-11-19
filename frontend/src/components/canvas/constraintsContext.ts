"use client";

import { createContext, useContext } from "react";
import type { Constraint } from "@/lib/session-api";

/**
 * The design's constraints for the canvas's Constraints panel: the chat session owns them (from
 * `constraints` events) and their edits; the workspace provides them to the canvas.
 */
export type ConstraintsValue = {
  constraints: Constraint[];
  /** A new value for a constraint, or `undefined` to drop the user's value (the set's applies again). */
  onChange: (id: string, value?: number | string) => void;
  /** Why the constraints can't change now (Claude is replying, a question waits, a shared link), if so. */
  disabledReason: string | null;
  /** The session's constraint set, by name ("Default constraints", or the user's own set). */
  setName: string | null;
};

const NONE: ConstraintsValue = { constraints: [], onChange: () => {}, disabledReason: null, setName: null };

export const ConstraintsContext = createContext<ConstraintsValue>(NONE);

export const useConstraints = () => useContext(ConstraintsContext);
