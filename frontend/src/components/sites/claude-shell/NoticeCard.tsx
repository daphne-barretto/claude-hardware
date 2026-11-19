"use client";

import { Ban, ShieldAlert } from "lucide-react";
import type { Notice } from "@/lib/session-api";
import type { ChatMessage } from "./chat";

/** Headings of the two notices. */
export const REFUSED_HEADING = "Claude won't design this";
export const SAFETY_HEADING = "Safety notice: requirements not checked";

/**
 * A notice in Claude's reply, apart from its text:
 * - `refused`: Claude won't design this; the reason, and that nothing was changed.
 * - `safety`: Claude goes on with a regulated or safety-critical product; requirements vary by
 *   region, the kinds of regulations that may apply, and that none were checked.
 */
export function NoticeCard({ notice }: { notice: Notice }) {
  if (notice.kind === "refused") {
    return (
      <section
        role="note"
        aria-label={REFUSED_HEADING}
        data-testid="refused-notice"
        className="rounded-lg border border-red-400/40 bg-red-400/5 px-3 py-2.5 text-[13px]"
      >
        <p className="mb-1 flex items-center gap-1.5 font-[500]">
          <Ban aria-hidden className="size-3.5 shrink-0 text-red-400" />
          {REFUSED_HEADING}
        </p>
        <p className="whitespace-pre-wrap">{notice.text}</p>
        <p className="mt-1 text-[var(--cs-muted)]">Nothing was designed or changed for this request.</p>
      </section>
    );
  }
  const regulations = notice.regulations ?? [];
  return (
    <section
      role="note"
      aria-label={SAFETY_HEADING}
      data-testid="safety-notice"
      className="rounded-lg border border-amber-400/40 bg-amber-400/5 px-3 py-2.5 text-[13px]"
    >
      <p className="mb-1 flex items-center gap-1.5 font-[500]">
        <ShieldAlert aria-hidden className="size-3.5 shrink-0 text-amber-400" />
        {SAFETY_HEADING}
      </p>
      <p className="whitespace-pre-wrap">{notice.text}</p>
      {regulations.length > 0 && (
        <>
          <p className="mt-1.5 text-[var(--cs-muted)]">May apply (none were checked):</p>
          <ul aria-label="Regulations that may apply" className="list-disc pl-5">
            {regulations.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

/** The safety notices in the chat, each once, oldest first. */
export function safetyNotices(messages: ChatMessage[]): Notice[] {
  const seen = new Set<string>();
  const out: Notice[] = [];
  for (const m of messages)
    for (const b of m.blocks ?? [])
      if (b.kind === "notice" && b.notice.kind === "safety" && !seen.has(b.notice.text)) {
        seen.add(b.notice.text);
        out.push(b.notice);
      }
  return out;
}

/**
 * The canvas header's reminder that the session has a safety notice: not a control, its
 * title repeats the regulations that may apply. Renders nothing without a notice.
 */
export function SafetyBadge({ notices }: { notices: Notice[] }) {
  if (notices.length === 0) return null;
  const kinds = [...new Set(notices.flatMap((n) => n.regulations ?? []))];
  const title = ["Requirements vary by region; none were checked.", ...kinds.map((k) => `May apply: ${k}`)].join("\n");
  return (
    <span
      role="note"
      aria-label={SAFETY_HEADING}
      title={title}
      data-testid="canvas-safety-notice"
      className="ml-2 flex shrink-0 items-center gap-1 rounded-full border border-amber-400/40 px-2 py-0.5 text-[11px] text-amber-400"
    >
      <ShieldAlert aria-hidden className="size-3" />
      Regulations not checked
    </span>
  );
}
