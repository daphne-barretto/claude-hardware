import { DemoSession } from "@/components/sites/claude-shell/DemoSession";

/** A demo (`/demo/<id>`): opens this browser's own copy of it, a session like any other. */
export default async function DemoPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <DemoSession key={id} demoId={decodeURIComponent(id)} />;
}
