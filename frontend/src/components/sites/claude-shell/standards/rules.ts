// The standards editor's rules as the user edits them (drafts), and how they read in plain
// language. The API validates everything (docs/constraints.md); `valueProblem` only gives the
// same answer early, next to the field, from the catalogue's ranges.
import type { StandardRule, StandardsSet } from "@/lib/session-api";
import type { CheckKind, StandardInput, StandardsProblem } from "@/lib/standards-api";

export type AppliesTo = "any" | "board" | "object";

/** A rule being edited: text fields hold what was typed. */
export type RuleDraft = {
  check: string;
  title: string;
  description: string;
  value: string;
  applies_to: AppliesTo;
  enabled: boolean;
  aliases: string;
  dimensions: Record<string, string>;
  material: string;
  /** The value the description states, so a new value can replace it there. */
  stated: string;
};

export const APPLIES_TO_LABELS: Record<AppliesTo, string> = {
  any: "Every design",
  board: "Designs with a board",
  object: "Designs that fit an object",
};

/** A number as the team writes sizes: at least one decimal (2.0, 0.2, 15.1). */
export function formatNumber(n: number): string {
  return Number.isInteger(n) ? n.toFixed(1) : String(Number(n.toPrecision(6)));
}

/** A value with its unit: "2.0 mm", "1.0°", or the text of a spec. */
export function valueText(value: number | string | null | undefined, unit?: string | null): string {
  if (value === null || value === undefined || value === "") return "…";
  if (typeof value === "string") {
    const n = Number(value);
    if (!unit || value.trim() === "" || !Number.isFinite(n)) return value;
    value = n;
  }
  const text = formatNumber(value);
  return unit === "deg" ? `${text}°` : unit ? `${text} ${unit}` : text;
}

/** The rule in plain language ("Walls at least 2.0 mm"), from its kind's phrase. */
export function plainLabel(draft: Pick<RuleDraft, "check" | "value" | "title">, kind: CheckKind | undefined): string {
  if (!kind) return `${draft.title}: ${draft.value}`;
  return kind.phrase.replace("{value}", valueText(draft.value, kind.unit));
}

export function toDraft(rule: StandardRule): RuleDraft {
  const value = rule.value === undefined || rule.value === null ? "" : String(rule.value);
  return {
    stated: value,
    check: rule.check,
    title: rule.title,
    description: rule.description,
    value,
    applies_to: (rule.applies_to ?? "any") as AppliesTo,
    enabled: rule.enabled !== false,
    aliases: (rule.aliases ?? []).join(", "),
    dimensions: Object.fromEntries(Object.entries(rule.dimensions ?? {}).map(([k, v]) => [k, String(v)])),
    material: rule.material ?? "",
  };
}

/** A new rule of a kind: the built-in set's rule of that kind if it has one, else empty. */
export function newDraft(kind: CheckKind): RuleDraft {
  if (kind.suggested) return { ...toDraft(kind.suggested), enabled: true };
  return {
    check: kind.check,
    title: kind.title,
    description: "",
    value: "",
    applies_to: kind.applies_to,
    enabled: true,
    aliases: "",
    dimensions: Object.fromEntries(kind.dimensions.map((d) => [d.name, ""])),
    material: "",
    stated: "",
  };
}

const numberOrText = (raw: string): number | string => {
  const t = raw.trim();
  return t !== "" && Number.isFinite(Number(t)) ? Number(t) : t;
};

/** What the editor sends for a draft (numbers parsed; anything else sent as typed, for the API to name). */
export function toInput(draft: RuleDraft, kind: CheckKind | undefined): StandardInput {
  const spec = kind?.kind === "spec";
  return {
    check: draft.check,
    title: draft.title.trim(),
    description: draft.description.trim(),
    value: spec ? draft.value.trim() : numberOrText(draft.value),
    applies_to: draft.applies_to,
    enabled: draft.enabled,
    aliases: draft.aliases
      .split(",")
      .map((a) => a.trim())
      .filter(Boolean),
    dimensions: kind?.dimensions.length
      ? Object.fromEntries(Object.entries(draft.dimensions).map(([k, v]) => [k, numberOrText(v)]))
      : null,
    material: kind?.material ? draft.material.trim() || null : null,
  };
}

/** Why the typed value can't be saved, from the kind's range; null when it can. */
export function valueProblem(draft: RuleDraft, kind: CheckKind | undefined): string | null {
  if (!kind) return null;
  if (kind.kind === "spec") return draft.value.trim() ? null : `Give the ${kind.value_label.toLowerCase()}.`;
  const t = draft.value.trim();
  const n = Number(t);
  if (t === "" || !Number.isFinite(n)) return `Enter a number in ${kind.unit}.`;
  if (kind.min !== null && kind.min !== undefined && n < kind.min) return `At least ${valueText(kind.min, kind.unit)}.`;
  if (kind.max !== null && kind.max !== undefined && n > kind.max) return `At most ${valueText(kind.max, kind.unit)}.`;
  return null;
}

/** The kinds a set doesn't have a rule of yet: what "Add rule" offers. */
export function availableKinds(kinds: CheckKind[], drafts: Pick<RuleDraft, "check">[]): CheckKind[] {
  const have = new Set(drafts.map((d) => d.check));
  return kinds.filter((k) => !have.has(k.check));
}

/** A name for a copy of `name` that none of the sets has yet. */
export function copyName(name: string, sets: Pick<StandardsSet, "name">[]): string {
  const taken = new Set(sets.map((s) => s.name.toLowerCase()));
  for (let n = 1; ; n++) {
    const candidate = n === 1 ? `${name} (copy)` : `${name} (copy ${n})`;
    if (!taken.has(candidate.toLowerCase())) return candidate;
  }
}

/** A name for a new empty set. */
export function newSetName(sets: Pick<StandardsSet, "name">[]): string {
  const taken = new Set(sets.map((s) => s.name.toLowerCase()));
  for (let n = 1; ; n++) {
    const candidate = n === 1 ? "My constraints" : `My constraints ${n}`;
    if (!taken.has(candidate.toLowerCase())) return candidate;
  }
}

/** The API's problems, by rule index (-1: the set itself). */
export function problemsByRule(problems: StandardsProblem[]): Map<number, StandardsProblem[]> {
  const out = new Map<number, StandardsProblem[]>();
  for (const p of problems) {
    const key = p.rule ?? -1;
    out.set(key, [...(out.get(key) ?? []), p]);
  }
  return out;
}

/** Whether the drafts differ from the saved rules. */
export function changed(saved: StandardsSet, name: string, drafts: RuleDraft[]): boolean {
  if (name !== saved.name) return true;
  const before = saved.standards.map(toDraft);
  return JSON.stringify(before) !== JSON.stringify(drafts);
}

/**
 * A new typed value. A description that states the old value ("No wall is thinner than 2.0 mm")
 * is updated to the new one, so the words Claude reads never contradict the rule.
 */
export function withValue(draft: RuleDraft, value: string, kind: CheckKind | undefined): Partial<RuleDraft> {
  const old = Number(draft.stated.trim());
  const next = Number(value.trim());
  if (!kind?.unit || draft.stated.trim() === "" || value.trim() === "" || !Number.isFinite(old) || !Number.isFinite(next)) {
    return { value };
  }
  const unit = kind.unit === "deg" ? "°" : ` ${kind.unit}`;
  const spellings = [...new Set([formatNumber(old), String(old)])].map((n) => `${n}${unit}`);
  let description = draft.description;
  for (const s of spellings) description = description.split(s).join(`${formatNumber(next)}${unit}`);
  return description === draft.description ? { value } : { value, description, stated: value };
}
