import type { Metadata } from "next";
import { SharePage } from "@/components/sites/claude-shell/SharePage";

// A shared link is for whoever has it: search engines must not index it.
export const metadata: Metadata = {
  title: "Shared · Claude Hardware",
  robots: { index: false, follow: false, nocache: true, googleBot: { index: false, follow: false } },
  referrer: "no-referrer",
};

/** A share link (`/share/<token>`): a read-only session or a design to copy. */
export default async function ShareLinkPage({ params }: { params: Promise<{ token: string }> }) {
  const { token } = await params;
  return <SharePage key={token} token={decodeURIComponent(token)} />;
}
