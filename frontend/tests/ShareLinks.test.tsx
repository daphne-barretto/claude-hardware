import { FakeEventSource } from "./fakes";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { Build, Message, Session } from "@/lib/session-api";

// Share links: the Share menu on a session of one's own, the read-only shared view, the landing
// of a shared design, and a link that was turned off (backend/app/share_routes.py).
const push = vi.fn();
vi.mock("next/navigation", () => ({ usePathname: () => window.location.pathname, useRouter: () => ({ push }) }));
vi.mock("@/components/canvas/HardwareCanvas", () => ({
  HardwareCanvas: ({ builds, fixBlocked, sessionId }: { builds: Build[]; fixBlocked?: string; sessionId: string }) => (
    <div data-testid="canvas-stub" data-session={sessionId} data-shown={builds.at(-1)?.id} data-fix-blocked={fixBlocked ?? ""} />
  ),
}));

import { AppShell } from "@/components/sites/claude-shell/AppShell";
import { HardwareWorkspace } from "@/components/sites/claude-shell/HardwareWorkspace";
import { SharePage } from "@/components/sites/claude-shell/SharePage";
import { SHARE_COPY, SHARED_BLOCKED, sessionRoute, shareSessionId, shareToken, type ShareLink } from "@/lib/share";
import { sessionPath } from "@/components/canvas/api";

const TOKEN = "a".repeat(48);
const SESSION: Session = { id: "session-1", title: "Sensor case", created_at: "2026-01-01T00:00:00Z", status: "idle", latest_build_id: "build-1" };
const BUILD = {
  id: "build-1",
  created_at: "2026-01-01T00:00:00Z",
  board: null,
  status: "ready",
  checks: { pass: 1, fail: 0, untested: 0, not_applicable: 0 },
  maps: [],
  mesh_url: `/api/shares/${TOKEN}/builds/build-1/mesh`,
  checks_url: `/api/shares/${TOKEN}/builds/build-1/checks`,
  source_url: `/api/shares/${TOKEN}/builds/build-1/source`,
  board_url: `/api/shares/${TOKEN}/builds/build-1/board`,
  step_url: null,
} as unknown as Build;
const MESSAGES: Message[] = [{ id: "msg-1", role: "user", text: "A small sensor case", created_at: "2026-01-01T00:00:00Z" }];
const link = (kind: "view" | "copy", over: Partial<ShareLink> = {}): ShareLink => ({
  token: TOKEN,
  kind,
  url: `/share/${TOKEN}`,
  active: true,
  attachments: false,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  up_to_date: true,
  ...over,
});

type Route = (url: string, init?: RequestInit) => { status?: number; body: unknown } | undefined;
let route: Route;
const calls: { url: string; method: string; body: unknown }[] = [];

beforeEach(() => {
  FakeEventSource.reset();
  push.mockReset();
  calls.length = 0;
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText: vi.fn(async () => {}) } });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, method: init?.method ?? "GET", body: init?.body ? JSON.parse(String(init.body)) : null });
      const found = route(url, init) ?? (url === "/api/sessions" ? { body: [] } : { status: 404, body: { detail: "nope" } });
      return new Response(JSON.stringify(found.body), { status: found.status ?? 200 });
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("share link paths", () => {
  it("serves a share link's snapshot under /api/shares and any other session under /api/sessions", () => {
    expect(shareToken(shareSessionId(TOKEN))).toBe(TOKEN);
    expect(shareToken("session-1")).toBeNull();
    expect(sessionRoute(shareSessionId(TOKEN))).toBe(`/shares/${TOKEN}`);
    expect(sessionRoute("session-1")).toBe("/sessions/session-1");
    expect(sessionPath(shareSessionId(TOKEN))).toBe(`/api/shares/${TOKEN}`);
  });
});

describe("the Share menu", () => {
  it("creates a read-only link, copies it and offers update and stop", async () => {
    let live: ShareLink[] = [];
    route = (url, init) => {
      if (url === "/api/sessions/session-1") return { body: SESSION };
      if (url === "/api/sessions/session-1/messages") return { body: MESSAGES };
      if (url === "/api/sessions/session-1/builds") return { body: [] };
      if (url === "/api/sessions/session-1/shares" && init?.method === "POST") {
        live = [link("view")];
        return { status: 201, body: live[0] };
      }
      if (url === "/api/sessions/session-1/shares") return { body: live };
      if (url === "/api/sessions/session-1/shares/revoke") return { body: link("view", { active: false }) };
    };
    render(
      <AppShell>
        <HardwareWorkspace sessionId="session-1" />
      </AppShell>,
    );
    const canvas = screen.getByRole("region", { name: "Canvas" });
    fireEvent.click(await within(canvas).findByRole("button", { name: "Share" }));
    const menu = screen.getByRole("dialog", { name: "Share" });
    const chat = within(menu).getByRole("region", { name: SHARE_COPY.view.title });
    expect(chat.textContent).toContain("Anyone with the link can see the chat, the model and its files");
    expect(within(menu).getByRole("region", { name: SHARE_COPY.copy.title }).textContent).toContain(
      "Anyone with the link can make their own editable copy of this design. Your chat isn't included.",
    );
    fireEvent.click(within(chat).getByRole("button", { name: "Create link" }));
    expect(await within(chat).findByRole("button", { name: "Link copied" })).toBeTruthy();
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(`${window.location.origin}/share/${TOKEN}`);
    expect(calls.find((c) => c.method === "POST")!.body).toEqual({ kind: "view", attachments: false });
    expect(within(chat).getByTestId("share-status-view").textContent).toContain("Link is live");
    expect(within(chat).getByRole("button", { name: "Update link" })).toBeTruthy();

    fireEvent.click(within(chat).getByRole("button", { name: "Stop sharing" }));
    expect(await within(chat).findByRole("button", { name: "Create link" })).toBeTruthy();
    expect(calls.at(-1)).toMatchObject({ url: "/api/sessions/session-1/shares/revoke", body: { kind: "view" } });
  });
});

describe("a shared link's page", () => {
  const shared = (kind: "view" | "copy"): Route => (url) => {
    if (url === `/api/shares/${TOKEN}`) return { body: { ...SESSION, id: shareSessionId(TOKEN), shared: kind } };
    if (url === `/api/shares/${TOKEN}/messages`) return { body: kind === "view" ? MESSAGES : [] };
    if (url === `/api/shares/${TOKEN}/builds`) return { body: [BUILD] };
    if (url === "/api/sessions" ) return { status: 201, body: { ...SESSION, id: "session-7" } };
  };

  it("shows the chat and the canvas read-only, with Continue from this", async () => {
    route = shared("view");
    render(
      <AppShell>
        <SharePage token={TOKEN} />
      </AppShell>,
    );
    const chat = await screen.findByRole("region", { name: "Chat" });
    expect(await within(chat).findByText(MESSAGES[0].text)).toBeTruthy();
    const notice = await within(chat).findByTestId("shared-notice");
    expect(notice.textContent).toContain("Shared by link · read-only");
    expect(within(chat).queryByRole("textbox")).toBeNull();
    const canvas = await screen.findByTestId("canvas-stub");
    expect(canvas.getAttribute("data-fix-blocked")).toBe(SHARED_BLOCKED);

    fireEvent.click(within(notice).getByRole("button", { name: "Continue from this" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/session/session-7"));
    expect(calls.find((c) => c.url === "/api/sessions" && c.method === "POST")!.body).toEqual({ from_share: TOKEN });
  });

  it("shows a shared design's preview with Open a copy", async () => {
    route = shared("copy");
    render(
      <AppShell>
        <SharePage token={TOKEN} />
      </AppShell>,
    );
    expect(await screen.findByRole("heading", { name: /Sensor case · shared design/ })).toBeTruthy();
    expect((await screen.findByTestId("canvas-stub")).getAttribute("data-session")).toBe(shareSessionId(TOKEN));
    fireEvent.click(screen.getByRole("button", { name: "Open a copy" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/session/session-7"));
  });

  it("says when the link was turned off", async () => {
    route = (url) => (url.startsWith("/api/shares/") ? { status: 410, body: { detail: "This link was turned off" } } : undefined);
    render(
      <AppShell>
        <SharePage token={TOKEN} />
      </AppShell>,
    );
    expect((await screen.findByTestId("share-link-message")).textContent).toContain("This link was turned off");
  });
});
