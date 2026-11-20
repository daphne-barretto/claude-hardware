import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import type { Comment } from "@/lib/session-api";
import type { ThreadQuestion } from "@/components/canvas/comments/comments";

// Claude's open question in one comment thread never holds another thread: its "Send to Claude"
// stays enabled, and what it sends goes to Claude (which is told the question was left open).
const state: { comments: Comment[]; questions: Record<string, ThreadQuestion> } = { comments: [], questions: {} };
vi.mock("@/components/canvas/comments/useComments", () => ({
  useComments: () => ({ comments: state.comments, questions: state.questions, put: () => {} }),
}));

import { CommentsLayer } from "@/components/canvas/comments/CommentsLayer";
import type { CommentTool } from "@/components/canvas/comments/useComments";
import type { Build } from "@/components/canvas/api";

const comment = (id: string, text: string, request?: Comment["request"]): Comment => ({
  id,
  build_id: "build-1",
  face_id: "face-1",
  point: [0, 0, 0],
  text,
  author: "Demo user",
  created_at: new Date().toISOString(),
  location: { point: [0, 0, 0], faces: ["face-1"] },
  replies: [],
  request: request ?? null,
  resolved: false,
});

function tool(openId: string | null): CommentTool {
  return {
    active: false,
    setActive: () => {},
    toggle: () => {},
    draft: null,
    place: () => {},
    openId,
    open: () => {},
    listOpen: false,
    setListOpen: () => {},
  };
}

function renderLayer(openId: string, props: { busy?: boolean; blocked?: string } = {}) {
  const onSend = vi.fn();
  render(
    <CommentsLayer
      sessionId="session-1"
      build={{ id: "build-1" } as Build}
      ready
      tool={tool(openId)}
      toScreen={() => ({ x: 100, y: 100 })}
      width={800}
      height={600}
      changeSet={null}
      onReviewed={() => {}}
      onSend={onSend}
      onAnswer={() => {}}
      busy={props.busy ?? false}
      blocked={props.blocked}
      onFocus={() => {}}
    />,
  );
  return onSend;
}

afterEach(() => {
  cleanup();
  state.comments = [];
  state.questions = {};
});

describe("Send to Claude while a question is open", () => {
  const asking = comment("c-ask", "Which way should the lid open?", { message_id: "msg-2", status: "running" });
  const other = comment("c-other", "Round the corners");
  const question: ThreadQuestion = {
    messageId: "msg-2",
    questionId: "q-1",
    text: "Hinge on the long side or the short side?",
    options: [
      { id: "long", label: "Long side" },
      { id: "short", label: "Short side" },
    ],
  };

  it("is enabled on another thread, and sends it", () => {
    state.comments = [asking, other];
    state.questions = { "msg-2": question };
    const onSend = renderLayer("c-other");
    const thread = screen.getByTestId("comment-thread");
    const send = within(thread).getByRole("button", { name: /Send to Claude/ });
    expect(send).toHaveProperty("disabled", false);
    fireEvent.click(send);
    expect(onSend).toHaveBeenCalledWith(["c-other"]);
  });

  it("is enabled in the thread that asked, beside the question's options", () => {
    state.comments = [asking, other];
    state.questions = { "msg-2": question };
    renderLayer("c-ask");
    const thread = screen.getByTestId("comment-thread");
    expect(thread.dataset.state).toBe("waiting");
    expect(within(thread).getByRole("button", { name: /Long side/ })).toHaveProperty("disabled", false);
    expect(within(thread).getByRole("button", { name: /Send again/ })).toHaveProperty("disabled", false);
  });

  it("is enabled while Claude works: it waits in the queue", () => {
    state.comments = [other];
    renderLayer("c-other", { busy: true });
    const send = within(screen.getByTestId("comment-thread")).getByRole("button", { name: /Send to Claude/ });
    expect(send).toHaveProperty("disabled", false);
    expect(send.getAttribute("title")).toMatch(/sent when the run ends/);
  });

  it("is disabled only when nothing can be sent at all", () => {
    state.comments = [other];
    renderLayer("c-other", { blocked: "This is a read-only link." });
    const send = within(screen.getByTestId("comment-thread")).getByRole("button", { name: /Send to Claude/ });
    expect(send).toHaveProperty("disabled", true);
    expect(send.getAttribute("title")).toBe("This is a read-only link.");
  });
});
