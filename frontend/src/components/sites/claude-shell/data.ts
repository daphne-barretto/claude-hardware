// Neutral sample data for the prototype shell. Nothing here comes from a real account.

import modelCatalog from "@/generated/models.json";

/** A paste this long becomes a "Pasted content" chip in the composer instead of flooding the box. */
export const LONG_PASTE_CHARS = 2000;
export const LONG_PASTE_LINES = 40;

/** The standards picker's label before a set is chosen (New mockup). */
export const CHOOSE_STANDARDS = "Choose a constraint set…";
export const EMPTY_HEADING = "Describe a part or a change and turn it into a design";

export const DEMO_USER = { name: "Demo user", initial: "D", plan: "Prototype" };

/**
 * Models in the picker, from the shared catalogue (contracts/models.json, generated from
 * backend/app/contracts/models.py). `id` is the model ID sent to the session API; the API
 * accepts only these. `MORE_MODELS` sit under "More models", which is hidden when it is empty.
 */
export type ModelOption = { id: string; name: string; blurb: string };
export const MODELS: readonly ModelOption[] = modelCatalog.models;
export const MORE_MODELS: readonly ModelOption[] = modelCatalog.more_models;
export const ALL_MODELS: readonly ModelOption[] = [...MODELS, ...MORE_MODELS];

/** Effort levels in the picker. `id` is the session API's effort value. */
export const EFFORTS = [
  { id: "low", name: "Low" },
  { id: "medium", name: "Medium", note: "Recommended" },
  { id: "high", name: "High" },
  { id: "xhigh", name: "Extra" },
  { id: "max", name: "Max", note: "4× or more usage" },
] as const;

/** What a new chat starts with. Auto matches the session API's default. */
export const DEFAULT_CHOICES = { model: modelCatalog.default, effort: "medium", mode: "auto" } as const;
