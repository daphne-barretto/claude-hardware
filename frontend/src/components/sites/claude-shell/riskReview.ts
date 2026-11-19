import { ApiError } from "@/lib/session-api";

/** An Accept or Ignore on a risk flag that didn't go through, and why, in plain words. */
export type RiskReviewError = { flagId: string; text: string };

/** The fix isn't there to review any more (the server has no such flags or flag). */
export const RISK_FIX_GONE =
  "This fix can't be applied any more: the design has changed since it was flagged. Ask Claude to check the design again.";

/** What the flag says when its review fails: a missing fix in plain words, else `fallback`'s text. */
export function riskReviewErrorText(e: unknown, fallback: (e: unknown) => string): string {
  if (e instanceof ApiError && e.status === 404) return RISK_FIX_GONE;
  return fallback(e);
}
