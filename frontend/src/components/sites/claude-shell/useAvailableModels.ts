"use client";

import { useEffect, useState } from "react";
import { listModels, type ModelCatalog } from "@/lib/session-api";
import { DEFAULT_CHOICES, MODELS, MORE_MODELS } from "./data";
import { MODELS_CHANGED } from "./useChatSession";

/** The whole catalogue (contracts/models.json): what the picker shows until the server answers. */
export const CATALOG: ModelCatalog = { default: DEFAULT_CHOICES.model, models: [...MODELS], more_models: [...MORE_MODELS] };

let cached: Promise<ModelCatalog> | null = null;

/** The models the server's API key can use (`GET /api/models`); the whole catalogue if it can't say. */
export function loadModels(fresh = false): Promise<ModelCatalog> {
  if (fresh || !cached)
    cached = listModels()
      // Anything but a catalogue (an old server, a proxy's page) means it can't say.
      .then((c) => (c && Array.isArray(c.models) && Array.isArray(c.more_models) && typeof c.default === "string" ? c : CATALOG))
      .catch(() => CATALOG);
  return cached;
}

/**
 * The picker's models: the catalogue entries the server's key can use, loaded once and again
 * whenever a run finds a model unavailable (`MODELS_CHANGED`), so it drops out of the picker.
 */
export function useAvailableModels(): ModelCatalog {
  const [catalog, setCatalog] = useState<ModelCatalog>(CATALOG);
  useEffect(() => {
    let live = true;
    const load = (fresh: boolean) => void loadModels(fresh).then((c) => live && setCatalog(c));
    load(false);
    const changed = () => load(true);
    window.addEventListener(MODELS_CHANGED, changed);
    return () => {
      live = false;
      window.removeEventListener(MODELS_CHANGED, changed);
    };
  }, []);
  return catalog;
}
