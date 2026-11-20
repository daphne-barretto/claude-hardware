import { buildFixture, readRepoFile, recordedEvents, sessionFixture } from "./fixtures";
import { FakeEventSource } from "./fakes";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { Build, Message, Session, SessionEvent } from "@/lib/session-api";
import type { CheckReport } from "@/components/canvas/api";
import { summaryLine } from "@/components/canvas/history";
import {
  countItems,
  openChecksLabel,
  pinFilterFor,
  useOpenChecksRequest,
  type OpenChecksRequest,
} from "@/components/canvas/openChecks";
import { ConstraintsPanel } from "@/components/canvas/ConstraintsPanel";
import { ConstraintsContext, type ConstraintsValue } from "@/components/canvas/constraintsContext";
import { ChatMessages } from "@/components/sites/claude-shell/ChatMessages";
import type { ChatMessage } from "@/components/sites/claude-shell/chat";

// The checks come from the fixtures (fixtures/build.json, fixtures/checks.json), so the tests hold
// for any board.
const report = JSON.parse(readRepoFile("fixtures/checks.json")) as CheckReport;
const build = buildFixture as Build;
const STATUSES = ["pass", "fail", "untested", "not_applicable"] as const;
const WORDS = { pass: "pass", fail: "fail", untested: "untested", not_applicable: "not applicable" };
const label = (status: (typeof STATUSES)[number]) => `Open constraints: ${build.checks[status] ?? 0} ${WORDS[status]}`;

vi.mock("next/navigation", () => ({ usePathname: () => window.location.pathname, useRouter: () => ({ push: () => {} }) }));
// The real canvas needs WebGL. This stand-in does what it does with a request: it opens the
// Constraints panel with the request's pin filter and marks its group.
vi.mock("@/components/canvas/HardwareCanvas", () => ({
  HardwareCanvas: function Stub() {
    const [open, setOpen] = useState<OpenChecksRequest | null>(null);
    useOpenChecksRequest(true, setOpen);
    return (
      <div data-testid="canvas-stub">
        {open && (
          <ConstraintsPanel unit="mm" report={report} pinFilter={pinFilterFor(open.status)} onPinFilter={() => {}} reveal={open} />
        )}
      </div>
    );
  },
}));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("check counts", () => {
  it("read as the summary line, each with its group", () => {
    expect(countItems(build.checks).map((c) => c.text).join(" · ")).toBe(summaryLine(build.checks));
    expect(countItems({ pass: 1, fail: 0, untested: 2 }).map((c) => c.status)).toEqual(["pass", "fail", "untested"]);
    expect(openChecksLabel("not_applicable", 3)).toBe("Open constraints: 3 not applicable");
  });

  it("open the panel on Failed for failures and on All otherwise", () => {
    expect(pinFilterFor("fail")).toBe("failed");
    for (const s of ["pass", "untested", "not_applicable"] as const) expect(pinFilterFor(s)).toBe("all");
  });
});

describe("a chat build card's counts", () => {
  const messages = [{ id: "m-1", role: "assistant", blocks: [{ kind: "build", build }] }] as ChatMessage[];

  it("are buttons named for the panel they open, apart from the card's own button", () => {
    const onOpenChecks = vi.fn();
    const onShowBuild = vi.fn();
    render(<ChatMessages messages={messages} busy={false} onAnswer={() => {}} builds={[build]} onShowBuild={onShowBuild} onOpenChecks={onOpenChecks} />);
    for (const status of STATUSES) {
      const count = screen.getByRole("button", { name: label(status) });
      expect(count.className).toContain("cursor-pointer");
      fireEvent.click(count);
      expect(onOpenChecks).toHaveBeenLastCalledWith(build.id, status);
    }
    expect(onShowBuild).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("build-ready"));
    expect(onShowBuild).toHaveBeenCalledWith(build.id);
    expect(screen.getByTestId("check-counts").textContent).toBe(summaryLine(build.checks));
  });

  it("are plain text without a canvas to open", () => {
    render(<ChatMessages messages={messages} busy={false} onAnswer={() => {}} builds={[build]} />);
    expect(screen.queryByRole("button", { name: /^Open constraints/ })).toBeNull();
    expect(screen.getByTestId("check-counts").textContent).toBe(summaryLine(build.checks));
  });
});

describe("the Constraints panel opened on a group", () => {
  const ctx: ConstraintsValue = { constraints: [], onChange: () => {}, disabledReason: null, setName: null };
  const renderPanel = (reveal: OpenChecksRequest) =>
    render(
      <ConstraintsContext.Provider value={ctx}>
        <ConstraintsPanel unit="mm" report={report} reveal={reveal} />
      </ConstraintsContext.Provider>,
    );
  const marked = () => screen.getAllByTestId("constraint-item").filter((r) => r.dataset.revealed);

  it("marks that group's rows and moves to the first", () => {
    renderPanel({ status: "untested", n: 1 });
    const rows = marked();
    expect(rows.length).toBeGreaterThan(0);
    expect(rows.every((r) => r.dataset.status === "untested")).toBe(true);
    expect(document.activeElement).toBe(rows[0]);
  });

  it("shows the not applicable rows, hidden otherwise", () => {
    renderPanel({ status: "not_applicable", n: 1 });
    expect(screen.getByRole("button", { name: "Hide not applicable" })).toBeTruthy();
    expect(marked().length).toBe(report.results.filter((r) => r.status === "not_applicable").length);
  });
});

// The session page: a count in the chat opens the canvas, hidden or not, on the Constraints panel.
const SESSIONS: Session[] = [
  { id: "older", title: sessionFixture.title, created_at: new Date(Date.UTC(2026, 0, 1)).toISOString(), board: sessionFixture.board, status: "waiting_for_answer" },
];
const replyId = (recordedEvents[0] as Extract<SessionEvent, { type: "message_start" }>).message_id;
const stored: Message[] = [
  { id: "m-user", role: "user", text: sessionFixture.prompt, created_at: SESSIONS[0].created_at },
  { id: replyId, role: "assistant", text: "", created_at: SESSIONS[0].created_at },
];

describe("a count in a session's chat", () => {
  beforeEach(() => {
    FakeEventSource.reset();
    window.history.replaceState(null, "", "/session/older");
    vi.stubGlobal("EventSource", FakeEventSource);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        const json =
          url === "/api/sessions" ? SESSIONS : url.endsWith("/messages") ? stored : url.endsWith("/builds") ? [] : SESSIONS[0];
        return new Response(JSON.stringify(json));
      }),
    );
  });

  async function openSession() {
    const { AppShell } = await import("@/components/sites/claude-shell/AppShell");
    const { HardwareWorkspace } = await import("@/components/sites/claude-shell/HardwareWorkspace");
    render(
      <AppShell>
        <HardwareWorkspace sessionId="older" />
      </AppShell>,
    );
    const chat = screen.getByRole("region", { name: "Chat" });
    await waitFor(() => expect(FakeEventSource.all.some((s) => s.url.endsWith("/older/events"))).toBe(true));
    const source = FakeEventSource.all.find((s) => s.url.endsWith("/older/events"))!;
    act(() => recordedEvents.forEach((e) => source.emit(e)));
    return chat;
  }
  const pins = () => screen.getByRole("radiogroup", { name: "Check pins" });
  const checkedPin = () => within(pins()).getAllByRole("radio").find((r) => r.getAttribute("aria-checked") === "true")?.textContent;

  it("shows the hidden canvas and opens the panel filtered to its group", async () => {
    const chat = await openSession();
    fireEvent.click(within(chat).getByRole("button", { name: "Hide canvas" }));
    expect(screen.queryByRole("region", { name: "Canvas" })).toBeNull();

    const card = within(chat).getAllByTestId("check-counts")[0];
    fireEvent.click(within(card).getByRole("button", { name: /^Open constraints: \d+ fail$/ }));
    const canvas = screen.getByRole("region", { name: "Canvas" });
    expect(within(canvas).getByRole("region", { name: "Constraints" })).toBeTruthy();
    expect(checkedPin()).toBe("Failed");

    fireEvent.click(within(card).getByRole("button", { name: /^Open constraints: \d+ pass$/ }));
    expect(checkedPin()).toBe("All");
  });
});
