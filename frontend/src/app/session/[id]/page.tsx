import { HardwareWorkspace } from "@/components/sites/claude-shell/HardwareWorkspace";

/** A session on the session API: chat and the canvas with the session's builds. */
export default async function SessionPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <HardwareWorkspace key={id} sessionId={decodeURIComponent(id)} />;
}
