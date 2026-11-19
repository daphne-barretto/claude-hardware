"use client";

import { useEffect, useState } from "react";
import { Copy, PanelLeft } from "lucide-react";
import { HardwareCanvas } from "@/components/canvas/HardwareCanvas";
import { useSessionBuilds } from "@/components/canvas/api";
import { linkState, NO_SUCH_LINK, shareSessionId, SHARED_BLOCKED, TURNED_OFF, type LinkState } from "@/lib/share";
import { useShell } from "./AppShell";
import { CanvasSlot } from "./CanvasSlot";
import { HardwareWorkspace } from "./HardwareWorkspace";
import { useCopyFromShare } from "./SharedNotice";

/**
 * The page at `/share/<token>`: a read-only shared session (the chat and the canvas, with
 * Continue from this), the landing of a shared design (Open a copy), or a link that was turned
 * off or never existed.
 */
export function SharePage({ token }: { token: string }) {
  const [state, setState] = useState<LinkState>({ status: "loading" });
  useEffect(() => {
    let live = true;
    linkState(token)
      .then((s) => live && setState(s))
      .catch(() => live && setState({ status: "missing" }));
    return () => {
      live = false;
    };
  }, [token]);

  if (state.status === "loading") return null;
  if (state.status === "live" && state.kind === "view") return <HardwareWorkspace sessionId={shareSessionId(token)} />;
  if (state.status === "live") return <ShareLanding token={token} title={state.title} />;
  return (
    <LinkMessage
      title={state.status === "off" ? TURNED_OFF : NO_SUCH_LINK}
      text={
        state.status === "off"
          ? "Whoever shared it stopped sharing. Ask them for a new link."
          : "Check the link, or ask whoever shared it for a new one."
      }
    />
  );
}

function SidebarToggle() {
  const { sidebarOpen, setSidebarOpen } = useShell();
  if (sidebarOpen) return null;
  return (
    <button
      type="button"
      aria-label="Show sidebar"
      onClick={() => setSidebarOpen(true)}
      className="grid size-8 place-items-center rounded-lg text-[var(--cs-muted)] hover:bg-[var(--cs-hover)] hover:text-[var(--cs-text)]"
    >
      <PanelLeft className="size-[18px]" strokeWidth={1.6} />
    </button>
  );
}

function LinkMessage({ title, text }: { title: string; text: string }) {
  return (
    <div className="flex h-full flex-col">
      <header className="flex h-12 shrink-0 items-center px-3">
        <SidebarToggle />
      </header>
      <div role="status" data-testid="share-link-message" className="mx-auto flex max-w-[360px] flex-1 flex-col items-center justify-center gap-2 pb-16 text-center">
        <p className="text-[15px] text-[var(--cs-text)]">{title}</p>
        <p className="text-[13px] text-[var(--cs-muted)]">{text}</p>
      </div>
    </div>
  );
}

/** A shared design: its title, a preview of the model and Open a copy, which makes the opener's own session. */
function ShareLanding({ token, title }: { token: string; title: string }) {
  const sessionId = shareSessionId(token);
  const { builds } = useSessionBuilds(sessionId);
  const [start, error, working] = useCopyFromShare(token);
  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex h-12 shrink-0 items-center gap-2 border-b border-[var(--cs-border)] px-3">
        <SidebarToggle />
        <h1 className="ml-2 min-w-0 truncate text-[14px]" title={`${title} · shared design`}>
          {title} <span className="text-[var(--cs-muted)]">· shared design</span>
        </h1>
        <button
          type="button"
          disabled={working}
          onClick={() => void start()}
          title="Make your own editable copy of this design"
          className="ml-auto flex h-8 shrink-0 items-center gap-1.5 rounded-lg bg-[var(--cs-text)] px-3 text-[13px] font-[520] text-[var(--cs-bg)] enabled:hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Copy className="size-3.5" strokeWidth={2} />
          Open a copy
        </button>
      </header>
      <p data-testid="share-landing-note" className="border-b border-[var(--cs-border)] px-5 py-2 text-[12px] text-[var(--cs-muted)]">
        A design shared by link. Open a copy to make it your own: you can edit it and ask Claude to change it. The person who shared it
        won&apos;t see your copy.
        {error && <span className="ml-1 text-[var(--cs-accent)]">{error}</span>}
      </p>
      <section aria-label="Design preview" className="flex min-h-0 flex-1 flex-col">
        <CanvasSlot onImport={() => {}}>
          {builds.length > 0 && <HardwareCanvas sessionId={sessionId} builds={builds} fixDisabled fixBlocked={SHARED_BLOCKED} />}
        </CanvasSlot>
      </section>
    </div>
  );
}
