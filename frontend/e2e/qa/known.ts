import type { Finding } from "./live";

// Who a finding belongs to. A finding an in-flight branch owns is noted, not
// failed, and gets a test.fail() case naming the owner, so it fails loudly once the branch lands
// and the mark can go. A finding tagged "fix brief: ..." still fails; the brief says how to fix it.

export const OWNERS = {
  canvasLayout: "local/fix-canvas-layout",
  undo: "local/undo-history",
  friendly: "local/friendly-limits",
  queue: "local/message-queue",
  concurrency: "local/concurrency",
  edges: "local/edge-case-suite",
  abuse: "local/abuse-hardening",
  standards: "local/standards-editor",
  share: "local/share-links",
  batch3: "local/batch3",
} as const;

type Rule = { owner: string; kind?: string; where?: RegExp; detail: RegExp; why: string };

// Every in-flight branch is merged into local/product now (367ba4c), so no finding is owned:
// what the sweep still finds is a failure, with a fix brief below where it needs one.
// The 18 fix briefs are fixed (local/fix-ui-text, local/fix-canvas-1280,
// local/fix-phone-a11y-infra), so no finding is excused: whatever the sweep finds fails.
export const KNOWN: Rule[] = [];

/** A finding an in-flight branch owns (noted, not failed), as opposed to one with a fix brief (still a failure). */
export const isOwned = (f: Finding) => !!f.owner && !f.owner.startsWith("fix brief");

export function ownerOf(f: Finding): string | undefined {
  return KNOWN.find((r) => (!r.kind || r.kind === f.kind) && (!r.where || r.where.test(f.where)) && r.detail.test(f.detail))?.owner;
}
