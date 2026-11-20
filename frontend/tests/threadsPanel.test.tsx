import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { applyEvent, type ChatMessage } from "@/components/sites/claude-shell/chat";
import { ThreadsPanel } from "@/components/sites/claude-shell/ThreadsPanel";
import { ChatMessages } from "@/components/sites/claude-shell/ChatMessages";
import { OPEN_THREAD, type Conversation, type SessionEvent } from "@/lib/session-api";

// Each comment thread is its own conversation: the main chat shows where each stands, answers a
// thread's question from its pointer, and gets an entry when a thread's run ends.
afterEach(cleanup);

const question = {
  type: "question" as const,
  message_id: "msg-4",
  question_id: "q-1",
  text: "Hinge on the long side?",
  options: [
    { id: "yes", label: "Yes" },
    { id: "no", label: "No" },
  ],
};
const conversations: Conversation[] = [
  { id: "main", kind: "main", title: "Main chat", status: "queued", queued_behind: "c-1", queued_behind_title: "Grow it" },
  { id: "c-1", kind: "thread", title: "Grow it", status: "working" },
  { id: "c-2", kind: "thread", title: "Round the corners", status: "queued", queued_behind: "c-1", queued_behind_title: "Grow it" },
  { id: "c-3", kind: "thread", title: "Which hinge?", status: "waiting_for_answer", question },
  { id: "c-4", kind: "thread", title: "Done already", status: "idle" },
];

describe("threads in the main chat", () => {
  it("shows each busy thread's status, and answers a thread's question from its pointer", () => {
    const onAnswer = vi.fn();
    render(<ThreadsPanel conversations={conversations} busy onAnswer={onAnswer} />);
    const rows = screen.getAllByTestId("thread-status");
    expect(rows.map((r) => r.dataset.status)).toEqual(["working", "queued", "waiting_for_answer"]);
    expect(rows[1].textContent).toContain("Queued behind “Grow it”");
    expect(rows[2].textContent).toContain("Claude has a question in “Which hinge?”");
    expect(screen.getByTestId("main-queued").textContent).toContain("queued behind “Grow it”");
    const yes = within(rows[2]).getByRole("button", { name: "Yes" });
    expect(yes).toHaveProperty("disabled", false); // never held by another run: it waits its turn
    fireEvent.click(yes);
    expect(onAnswer).toHaveBeenCalledWith("q-1", "yes");

    const opened = vi.fn();
    window.addEventListener(OPEN_THREAD, (e) => opened((e as CustomEvent).detail));
    fireEvent.click(within(rows[0]).getByRole("button", { name: /Open thread/ }));
    expect(opened).toHaveBeenCalledWith("c-1");
  });

  it("adds a thread's report to the main chat, and keeps the thread's own run out of it", () => {
    const events: SessionEvent[] = [
      {
        type: "comment",
        comment: {
          id: "c-1",
          build_id: "b-1",
          face_id: "f-1",
          point: [0, 0, 0],
          text: "Grow it",
          author: "Demo user",
          created_at: "2026-01-01T00:00:00Z",
          replies: [],
          request: { message_id: "msg-2", status: "running" },
          resolved: false,
        },
      },
      { type: "message_start", message_id: "msg-2", role: "assistant" },
      { type: "text", message_id: "msg-2", delta: "Working in the thread." },
      { type: "message_end", message_id: "msg-2" },
      {
        type: "thread_report",
        message_id: "msg-3",
        comment_id: "c-1",
        thread_title: "Grow it",
        run_id: "msg-2",
        outcome: "changed",
        summary: "Made it 10% bigger.",
        changes: ["Change length from 50 to 55"],
        build_id: "build-2",
        build_number: 2,
        checks: { pass: 7, fail: 1, untested: 0 },
      },
    ];
    const messages = events.reduce(applyEvent, [] as ChatMessage[]);
    render(<ChatMessages messages={messages} busy={false} waiting={false} onAnswer={() => {}} />);
    expect(screen.queryByText("Working in the thread.")).toBeNull();
    const report = screen.getByTestId("thread-report");
    expect(report.dataset.thread).toBe("c-1");
    expect(report.textContent).toContain("Made it 10% bigger.");
    expect(report.textContent).toContain("Change length from 50 to 55");
    expect(screen.getByTestId("thread-report-build").textContent).toBe("Build 2 · 7 passed, 1 failing");
  });
});
