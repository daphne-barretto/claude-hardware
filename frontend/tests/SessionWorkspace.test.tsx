import { buildFixture, recordedEvents, sessionFixture } from "./fixtures";
import { FakeEventSource } from "./fakes";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { Build, Message, Session, SessionEvent } from "@/lib/session-api";

vi.mock("next/navigation", () => ({ usePathname: () => window.location.pathname, useRouter: () => ({ push: () => {} }) }));
// The real canvas needs WebGL; this stand-in shows which build the workspace asks it to show.
vi.mock("@/components/canvas/HardwareCanvas", () => ({
  HardwareCanvas: ({ builds, selectedId, onSelect }: { builds: Build[]; selectedId?: string | null; onSelect?: (id: string) => void }) => (
    <div data-testid="canvas-stub" data-shown={selectedId ?? builds.at(-1)?.id}>
      {builds.map((b) => (
        <button key={b.id} type="button" onClick={() => onSelect?.(b.id)}>
          Revision {b.id}
        </button>
      ))}
    </div>
  ),
}));

import { AppShell } from "@/components/sites/claude-shell/AppShell";
import { HardwareWorkspace } from "@/components/sites/claude-shell/HardwareWorkspace";

const question = recordedEvents.find((e) => e.type === "question") as Extract<SessionEvent, { type: "question" }>;
const firstBuild = buildFixture as Build;
// A second build, derived from the fixture, so there is a choice of builds.
const secondBuild: Build = { ...firstBuild, id: `${firstBuild.id}-next` };
const buildEvent = (build: Build): SessionEvent => ({ type: "build_ready", message_id: null, build });
// The recorded stream has more than one build (the older revision's, then the run's): the card of a given build.
const buildCard = (chat: HTMLElement, id: string) =>
  within(chat)
    .getAllByTestId("build-ready")
    .find((c) => c.dataset.build === id)!; // the card names the build by its number, not its ID

const session = (id: string, day: number): Session => ({
  id,
  title: `${sessionFixture.title} ${id}`,
  created_at: new Date(Date.UTC(2026, 0, day)).toISOString(),
  board: sessionFixture.board,
  status: "waiting_for_answer",
});
const SESSIONS = [session("older", 1), session("newer", 2)];

// What the API has stored for "older": the first message and its reply (rebuilt from events).
const replyId = (recordedEvents[0] as Extract<SessionEvent, { type: "message_start" }>).message_id;
const stored: Message[] = [
  { id: "m-user", role: "user", text: sessionFixture.prompt, created_at: SESSIONS[0].created_at },
  { id: replyId, role: "assistant", text: "", created_at: SESSIONS[0].created_at },
];

let builds: Build[];

beforeEach(() => {
  FakeEventSource.reset();
  builds = [];
  window.history.replaceState(null, "", "/session/older");
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const json =
        url === "/api/sessions"
          ? SESSIONS
          : url.endsWith("/messages")
            ? stored
            : url.endsWith("/builds")
              ? builds
              : SESSIONS.find((s) => url.endsWith(`/${s.id}`));
      return new Response(JSON.stringify(json));
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderSession(id: string) {
  render(
    <AppShell>
      <HardwareWorkspace sessionId={id} />
    </AppShell>,
  );
  return screen.getByRole("region", { name: "Chat" });
}

/** Waits for the chat's event stream, then plays events into it (and the canvas's). */
async function play(events: SessionEvent[]) {
  await waitFor(() => expect(FakeEventSource.all.some((s) => s.url.endsWith("/older/events"))).toBe(true));
  const source = FakeEventSource.all.find((s) => s.url.endsWith("/older/events"))!;
  act(() => events.forEach((e) => source.emit(e)));
}

describe("a session page", () => {
  it("loads the stored messages, then the replies, builds and the pending question from the stream", async () => {
    const chat = renderSession("older");
    expect(await within(chat).findByText(sessionFixture.prompt)).toBeTruthy();
    // Loading: no empty state in between.
    expect(within(chat).queryByText(/Describe a part/)).toBeNull();
    await play(recordedEvents);

    expect(within(chat).getAllByTestId("assistant-message")).toHaveLength(1);
    expect(buildCard(chat, firstBuild.id)).toBeTruthy();
    const group = within(chat).getByRole("group", { name: question.text });
    for (const o of question.options) {
      expect(within(group).getByRole("button", { name: new RegExp(o.label) })).toHaveProperty("disabled", false);
    }
    // The canvas shows the build and the header the session's title.
    expect((await screen.findByTestId("canvas-stub")).dataset.shown).toBe(firstBuild.id);
    expect(within(chat).getByRole("heading", { name: SESSIONS[0].title })).toBeTruthy();
  });

  it("reopens on the model and effort its last message was sent with, not the default", async () => {
    const first = stored[0];
    stored[0] = { ...first, model: "claude-haiku-5-5", effort: "high" };
    try {
      renderSession("older");
      expect(await screen.findByRole("button", { name: /Haiku 5\.5/ })).toBeTruthy();
      expect(screen.queryByRole("button", { name: /Opus 5\.5/ })).toBeNull();
    } finally {
      stored[0] = first;
    }
  });

  it("shows a stored answer as answered, with its option as the user's reply", async () => {
    const option = question.options[1];
    stored.push({
      id: "m-answer",
      role: "user",
      text: "",
      created_at: SESSIONS[0].created_at,
      answer: { question_id: question.question_id, option_id: option.id },
    });
    try {
      const chat = renderSession("older");
      await play(recordedEvents);
      const group = within(chat).getByRole("group", { name: question.text });
      expect(within(group).getByRole("button", { name: new RegExp(option.label) })).toHaveProperty("ariaPressed", "true");
      expect(within(chat).getAllByText(option.label).length).toBe(2);
    } finally {
      stored.pop();
    }
  });

  it("shows a build in the canvas when its card in the chat is clicked", async () => {
    const chat = renderSession("older");
    await within(chat).findByText(sessionFixture.prompt);
    await play([...recordedEvents, buildEvent(secondBuild)]);
    const canvas = await screen.findByTestId("canvas-stub");
    await waitFor(() => expect(canvas.dataset.shown).toBe(secondBuild.id));

    const firstCard = buildCard(chat, firstBuild.id);
    expect(firstCard.tagName).toBe("BUTTON");
    fireEvent.click(firstCard);
    expect(canvas.dataset.shown).toBe(firstBuild.id);

    // The revision menu and the chat share the choice; a newer build shows when it arrives.
    fireEvent.click(within(canvas).getByRole("button", { name: `Revision ${secondBuild.id}` }));
    expect(canvas.dataset.shown).toBe(secondBuild.id);
    fireEvent.click(firstCard);
    const third = { ...firstBuild, id: `${firstBuild.id}-third` };
    await play([buildEvent(third)]);
    expect(canvas.dataset.shown).toBe(third.id);
  });

  it("opens the canvas when a build card is clicked while it is closed", async () => {
    const chat = renderSession("older");
    await play(recordedEvents);
    fireEvent.click(within(chat).getByRole("button", { name: "Hide canvas" }));
    expect(screen.queryByRole("region", { name: "Canvas" })).toBeNull();
    fireEvent.click(buildCard(chat, firstBuild.id));
    expect(screen.getByRole("region", { name: "Canvas" })).toBeTruthy();
  });
});

describe("Recents", () => {
  it("lists the API's sessions newest first and links to their pages", async () => {
    renderSession("older");
    const recents = screen.getByRole("list", { name: "Recents" });
    await waitFor(() => expect(within(recents).getAllByRole("link")).toHaveLength(SESSIONS.length));
    const links = within(recents).getAllByRole("link");
    expect(links.map((l) => l.textContent)).toEqual([SESSIONS[1].title, SESSIONS[0].title]);
    expect(links.map((l) => l.getAttribute("href"))).toEqual(["/session/newer", "/session/older"]);
    expect(links[1].getAttribute("aria-current")).toBe("page");
    expect(links[0].getAttribute("aria-current")).toBeNull();
  });

  it("says there are no sessions inside the list when the API has none", async () => {
    const answer = vi.mocked(fetch).getMockImplementation()!;
    vi.mocked(fetch).mockImplementation(async (url) =>
      url === "/api/sessions" ? new Response("[]") : answer(url as string),
    );
    renderSession("older");
    const recents = screen.getByRole("list", { name: "Recents" });
    await waitFor(() => expect(within(recents).getByText("No sessions yet")).toBeTruthy());
    expect(within(recents).queryAllByRole("link")).toHaveLength(0);
  });
});
