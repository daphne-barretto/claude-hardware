import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { parse } from "yaml";
import type { Page } from "@playwright/test";
import { collectControls, type Control } from "../inventory/ui-inventory";

// The controls on screen against frontend/e2e/ui-inventory.yaml, whatever session is open: an entry's
// templated name ({session.title}, {it.label}) matches any text, since the QA states run on
// the real backend's sessions, not the fixtures the inventory was written for.

type Raw = { id: string; role: string; name?: string };
const raw = (parse(readFileSync(resolve(__dirname, "../ui-inventory.yaml"), "utf8")) as { entries: Raw[] }).entries;
const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&");

export const PATTERNS = raw.map((e) => {
  const name = String(e.name ?? "");
  const m = /^\/(.*)\/$/.exec(name);
  const source = m
    ? m[1].replace(/\{[a-zA-Z_][\w.]*\}/g, ".+")
    : `^${name.split(/\{[a-zA-Z_][\w.]*\}/).map(esc).join(".+")}$`;
  return { id: e.id, role: e.role, name: new RegExp(source) };
});

/** The inventory entry a control is, if any. */
export function inventoryId(c: { role: string; name: string }) {
  return PATTERNS.find((p) => p.role === c.role && p.name.test(c.name))?.id;
}

/** The controls a user can reach in the page, each with its inventory entry (if it has one). */
export async function census(page: Page): Promise<(Control & { entry?: string })[]> {
  return (await collectControls(page)).map((c) => ({ ...c, entry: inventoryId(c) }));
}

export const describeControl = (c: Control) =>
  [...c.ancestors.filter((a) => a.name).slice(-2).map((a) => `${a.role} "${a.name}"`), `${c.role} "${c.name}"`].join(" >> ");
