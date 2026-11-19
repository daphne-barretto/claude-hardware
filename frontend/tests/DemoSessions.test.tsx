import { readRepoFile } from "./fixtures";
import { FakeEventSource } from "./fakes";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { recentsOrder, type Build, type Message, type Session, type SessionEvent } from "@/lib/session-api";

// Demos: hardcoded saved sessions from demos/sessions/, listed after the user's own and tagged
// Demo. Opening one gives this browser its own copy, which is a session like any other.
const push = vi.hoisted(() => vi.fn());
const replace = vi.hoisted(() => vi.fn());
vi.mock("next/navigation", () => ({ usePathname: () => window.location.pathname, useRouter: () => ({ push, replace }) }));
vi.mock("@/components/canvas/HardwareCanvas", () => ({
  HardwareCanvas: ({ builds, fixBlocked, readOnly }: { builds: Build[]; fixBlocked?: string; readOnly?: string }) => (
    <div data-testid="canvas-stub" data-shown={builds.at(-1)?.id} data-fix-blocked={fixBlocked ?? ""} data-read-only={readOnly ?? ""} />
  ),
}));

import { AppShell } from "@/components/sites/claude-shell/AppShell";
import { HardwareWorkspace } from "@/components/sites/claude-shell/HardwareWorkspace";
import { DemoSession } from "@/components/sites/claude-shell/DemoSession";

type SampleDoc = { session: Session; messages: Message[]; events: { id: number; event: SessionEvent }[] };
const ids = JSON.parse(readRepoFile("demos/sessions/index.json")).samples as string[];
const DEMOS = ids.map((id) => JSON.parse(readRepoFile(`demos/sessions/${id}/session.json`)) as SampleDoc);
const demo = DEMOS[0];
const builds = (doc: SampleDoc) => doc.events.flatMap(({ event }) => (event.type === "build_ready" ? [event.build] : []));

const own = (id: string, day: number): Session => ({
  id,
  title: `Own ${id}`,
  created_at: new Date(Date.UTC(2020, 0, day)).toISOString(),
  status: "idle",
});
// This browser's copy of the first demo: a session of its own, marked as that demo.
const COPY: Session = {
  ...demo.session,
  id: "session-copy",
  sample: false,
  demo: demo.session.id,
  created_at: new Date().toISOString(),
};
const original = (doc: SampleDoc): Session => ({ ...doc.session, demo: doc.session.id });
// As the API lists them: the user's own sessions, then each demo (the copy where there is one).
const SESSIONS = [own("older", 1), own("newer", 2), COPY, ...DEMOS.slice(1).map(original)];

let writes: { url: string; method: string }[] = [];

beforeEach(() => {
  push.mockReset();
  replace.mockReset();
  writes = [];
  FakeEventSource.reset();
  window.history.replaceState(null, "", `/session/${COPY.id}`);
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (method !== "GET") writes.push({ url, method });
      if (url.startsWith("/api/demos/") && method === "POST") return new Response(JSON.stringify(COPY));
      if (url.startsWith("/api/demos/") && method === "DELETE") return new Response(null, { status: 204 });
      const all = [...SESSIONS, original(demo)];
      const json =
        url === "/api/sessions"
          ? SESSIONS
          : url.endsWith("/messages")
            ? demo.messages
            : url.endsWith("/builds")
              ? builds(demo)
              : all.find((s) => url.endsWith(`/${s.id}`));
      return new Response(JSON.stringify(json ?? {}), { status: json ? 200 : 404 });
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("recentsOrder", () => {
  it("puts the user's sessions first, newest first, then the demos in the API's order", () => {
    expect(recentsOrder(SESSIONS).map((s) => s.id)).toEqual(["newer", "older", COPY.id, ...ids.slice(1)]);
  });
});

describe("the demos in Recents", () => {
  const renderList = async () => {
    render(
      <AppShell>
        <HardwareWorkspace sessionId={COPY.id} />
      </AppShell>,
    );
    const recents = screen.getByRole("list", { name: "Recents" });
    await waitFor(() => expect(within(recents).getAllByRole("link")).toHaveLength(SESSIONS.length));
    return recents;
  };

  it("lists each demo once, tagged Demo, after the user's sessions", async () => {
    const recents = await renderList();
    const links = within(recents).getAllByRole("link");
    expect(links.map((l) => l.getAttribute("data-session"))).toEqual(["newer", "older", COPY.id, ...ids.slice(1)]);
    for (const link of links) {
      const id = link.getAttribute("data-session")!;
      expect(within(link).queryByTestId("demo-tag") !== null).toBe(id === COPY.id || ids.includes(id));
      expect(link.textContent).not.toContain("Sample");
      expect(link.textContent).not.toContain("(copy)");
    }
    // The opened demo is its copy, once: no separate Recents row for it.
    expect(links.filter((l) => l.textContent?.startsWith(demo.session.title))).toHaveLength(1);
    expect(links.find((l) => l.getAttribute("data-session") === COPY.id)!.getAttribute("href")).toBe(`/session/${COPY.id}`);
    // A demo not opened yet goes to its demo page, which makes this browser's copy.
    expect(links.find((l) => l.getAttribute("data-session") === ids[1])!.getAttribute("href")).toBe(`/demo/${ids[1]}`);
  });

  it("gives the opened demo a menu with Rename and Reset demo, and resets it", async () => {
    const recents = await renderList();
    expect(within(recents).queryByRole("button", { name: `Options for ${DEMOS[1].session.title}` })).toBeNull();
    fireEvent.click(within(recents).getByRole("button", { name: `Options for ${COPY.title}` }));
    const menu = within(recents).getByRole("menu");
    expect(within(menu).getByRole("menuitem", { name: "Rename" })).toBeTruthy();
    fireEvent.click(within(menu).getByRole("menuitem", { name: "Reset demo" }));
    // It asks first: Cancel keeps the demo as it is.
    fireEvent.click(within(menu).getByRole("menuitem", { name: "Cancel" }));
    expect(writes).toEqual([]);
    fireEvent.click(within(menu).getByRole("menuitem", { name: "Reset demo" }));
    expect(menu.textContent).toContain("Your changes to this demo are deleted");
    fireEvent.click(within(menu).getByRole("menuitem", { name: "Reset" }));
    await waitFor(() => expect(writes).toContainEqual({ url: `/api/demos/${demo.session.id}/session`, method: "DELETE" }));
    // It was open: the demo opens again, as a fresh copy.
    await waitFor(() => expect(replace).toHaveBeenCalledWith(`/demo/${demo.session.id}`));
  });
});

describe("an opened demo", () => {
  it("works like any session: composer, canvas tools, no sample notice", async () => {
    render(
      <AppShell>
        <HardwareWorkspace sessionId={COPY.id} />
      </AppShell>,
    );
    const chat = screen.getByRole("region", { name: "Chat" });
    // A demo recorded before any message (the session as it was opened) shows its build alone.
    if (demo.messages.length) expect(await within(chat).findByText(demo.messages[0].text)).toBeTruthy();
    await waitFor(() => expect(FakeEventSource.all.some((s) => s.url.endsWith(`/${COPY.id}/events`))).toBe(true));
    const source = FakeEventSource.all.find((s) => s.url.endsWith(`/${COPY.id}/events`))!;
    act(() => demo.events.forEach(({ event }) => source.emit(event)));
    if (!demo.messages.length) expect(await within(chat).findByText(/^Build 1\b/)).toBeTruthy();

    const composer = await within(chat).findByRole("textbox");
    expect((composer as HTMLTextAreaElement).disabled).toBe(false);
    expect(within(chat).queryByTestId("sample-notice")).toBeNull();
    expect(screen.queryByRole("button", { name: "Continue from this" })).toBeNull();

    const canvas = await screen.findByTestId("canvas-stub");
    expect(canvas.getAttribute("data-read-only")).toBe("");
    expect(canvas.getAttribute("data-fix-blocked")).toBe("");
    expect(replace).not.toHaveBeenCalled();
  });

  it("is never worked on at its original: that page opens the demo instead", async () => {
    window.history.replaceState(null, "", `/session/${demo.session.id}`);
    render(
      <AppShell>
        <HardwareWorkspace sessionId={demo.session.id} />
      </AppShell>,
    );
    await waitFor(() => expect(replace).toHaveBeenCalledWith(`/demo/${demo.session.id}`));
  });
});

describe("the demo page", () => {
  it("opens this browser's own copy and shows it at its session page", async () => {
    window.history.replaceState(null, "", `/demo/${demo.session.id}`);
    render(
      <AppShell>
        <DemoSession demoId={demo.session.id} />
      </AppShell>,
    );
    await waitFor(() => expect(replace).toHaveBeenCalledWith(`/session/${COPY.id}`));
    expect(writes).toEqual([{ url: `/api/demos/${demo.session.id}/session`, method: "POST" }]);
  });

  it("says what went wrong when the demo can't be opened", async () => {
    const ok = vi.mocked(fetch).getMockImplementation()!;
    vi.mocked(fetch).mockImplementation(async (url, init) =>
      init?.method === "POST" ? new Response(JSON.stringify({ detail: "no demo sample-nope" }), { status: 404 }) : ok(url, init),
    );
    render(
      <AppShell>
        <DemoSession demoId="sample-nope" />
      </AppShell>,
    );
    expect((await screen.findByRole("alert")).textContent).toContain("no demo sample-nope");
    expect(replace).not.toHaveBeenCalled();
  });
});

describe("New", () => {
  it("offers no examples", async () => {
    window.history.replaceState(null, "", "/new");
    render(
      <AppShell>
        <HardwareWorkspace />
      </AppShell>,
    );
    const chat = screen.getByRole("region", { name: "Chat" });
    expect(await within(chat).findByRole("button", { name: "Import STEP" })).toBeTruthy();
    // Nothing to reply to yet: the box asks for the part.
    expect(within(chat).getByRole("textbox", { name: "Message" }).getAttribute("placeholder")).toBe("Describe a part or a change...");
    expect(screen.queryByRole("region", { name: "Examples" })).toBeNull();
    expect(chat.textContent).not.toContain("Or open an example");
    expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes("/examples"))).toBe(false);
  });
});
