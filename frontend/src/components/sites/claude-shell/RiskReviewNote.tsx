import { CircleAlert } from "lucide-react";

/** Why an Accept or Ignore on a risk flag didn't go through, under that flag (one note, not a chat line per click). */
export function RiskReviewNote({ text }: { text: string }) {
  return (
    <p role="alert" className="flex items-start gap-1.5 text-[12px] text-red-400">
      <CircleAlert aria-hidden className="mt-0.5 size-3.5 shrink-0" />
      <span>{text}</span>
    </p>
  );
}
