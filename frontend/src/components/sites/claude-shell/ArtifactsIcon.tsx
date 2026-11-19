import type { SVGProps } from "react";

/** Two portrait cards, the back one up and to the right, overlapping at one corner: the artifacts panel (here, the canvas). Drawn to match lucide's
 * stroke style so it sits beside the other header icons. */
export function ArtifactsIcon({ className, ...props }: SVGProps<SVGSVGElement>) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className={className}
      {...props}
    >
      {/* Back card, up and to the right; only the part outside the front card is drawn. */}
      <path d="M11.5 7.5v-2a2 2 0 0 1 2-2h5a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-4" />
      {/* Front card. */}
      <rect x="4.5" y="7.5" width="10" height="13" rx="2" />
    </svg>
  );
}
