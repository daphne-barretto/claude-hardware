"use client";

import { useEffect, useState } from "react";
import { listStandards, type StandardsSet } from "@/lib/session-api";

/**
 * The name of the standards set `id` (null: the built-in team set), as GET /api/standards lists
 * it: what a constraint chip names as its source. Null until the sets load, or if it's gone.
 */
export function standardsSetName(sets: StandardsSet[], id: string | null | undefined): string | null {
  const set = id ? sets.find((s) => s.id === id) : (sets.find((s) => s.built_in) ?? sets[0]);
  return set?.name ?? null;
}

export function useStandardsSetName(id: string | null | undefined): string | null {
  const [sets, setSets] = useState<StandardsSet[]>([]);
  useEffect(() => {
    let live = true;
    listStandards()
      .then((s) => live && Array.isArray(s) && setSets(s))
      .catch(() => undefined);
    return () => {
      live = false;
    };
    // A set chosen or renamed since: list them again.
  }, [id]);
  return standardsSetName(sets, id);
}
