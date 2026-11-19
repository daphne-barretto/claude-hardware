"use client";

import { useEffect, useState } from "react";
import { Check, Lock } from "lucide-react";

/** Copies the page link (path only), on a page anyone can open: a share link. A session of one's own is shared with `ShareMenu`. */
export function ShareButton() {
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const t = setTimeout(() => setCopied(false), 2000);
    return () => clearTimeout(t);
  }, [copied]);

  const share = async () => {
    // Only the page itself: no query string or fragment, which could carry user input.
    const link = window.location.origin + window.location.pathname;
    try {
      await navigator.clipboard.writeText(link);
      setCopied(true);
    } catch {
      window.prompt("Copy this link", link);
    }
  };

  return (
    <button
      type="button"
      onClick={share}
      className="flex h-8 items-center gap-1.5 rounded-lg px-2.5 text-[14px] hover:bg-[var(--cs-hover)]"
    >
      {copied ? <Check className="size-3.5" /> : <Lock className="size-3.5" />}
      <span aria-live="polite">{copied ? "Link copied" : "Share"}</span>
    </button>
  );
}
