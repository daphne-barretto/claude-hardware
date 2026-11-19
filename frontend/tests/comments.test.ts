import { describe, expect, it } from "vitest";
import type { Comment, SessionEvent } from "@/lib/session-api";
import {
  applyQuestion,
  changesFor,
  featureLabel,
  mentionsClaude,
  pinnedTo,
  threadState,
  THREAD_LABEL,
  isAnswerFirst,
  timeAgo,
  undoReview,
  visiblePins,
  withComment,
} from "@/components/canvas/comments/comments";
import { applyEvent, openQuestion, type ChatMessage } from "@/components/sites/claude-shell/chat";
import type { Change, ChangeSet } from "@/components/canvas/changes";

const comment = (over: Partial<Comment> = {}): Comment => ({
  id: "comment-1",
  build_id: "build-1",
  face_id: "lid.opening_x.1",
  point: [1, 2, 3],
  text: "Can we add a light pipe here?",
  author: "Demo user",
  created_at: "2026-01-01T00:00:00Z",
  part: "lid",
  feature: "opening_X1",
  location: { point: [1, 2, 3], part: "lid", faces: ["lid.opening_x.1"] },
  replies: [],
  request: null,
  resolved: false,
  ...over,
});

const change = (id: string, status: Change["status"], commentId: string | null): Change => ({
  id,
  title: `Change ${id}`,
  reason: "Because",
  status,
  delta: { kind: "modified", path: id, fields: [] },
  checks: [],
  comment: commentId,
});

describe("comments", () => {
  it("a mention of @Claude makes a comment a request", () => {
    expect(mentionsClaude("@Claude can you move this?")).toBe(true);
    expect(mentionsClaude("hey @claude.")).toBe(true);
    expect(mentionsClaude("Claude should see this")).toBe(false);
    expect(mentionsClaude("mail me@claude.example")).toBe(false);
  });

  it("names where a comment is pinned from its part and feature", () => {
    expect(featureLabel("opening_Q4")).toBe("opening for Q4");
    expect(featureLabel("boss_2")).toBe("boss 2");
    expect(pinnedTo(comment())).toBe("Pinned to Lid · opening for X1");
    expect(pinnedTo(comment({ feature: "lid" }))).toBe("Pinned to Lid");
    expect(pinnedTo(comment({ part: null, feature: null, location: null }))).toBe("Pinned to the model");
  });

  it("draws a pin only for open comments that have a place on the shown build", () => {
    const pins = visiblePins([
      comment(),
      comment({ id: "resolved", resolved: true }),
      comment({ id: "gone", location: null }),
    ]);
    expect(pins.map((c) => c.id)).toEqual(["comment-1"]);
  });

  it("a pin follows its re-resolved location, not where it was first pinned", () => {
    const moved = comment({ location: { point: [4, 5, 3], part: "lid", faces: ["lid.opening_x.1"] } });
    expect(visiblePins([moved])[0].location.point).toEqual([4, 5, 3]);
  });

  it("finds the changes made for a comment and undoes the applied ones", () => {
    const set = {
      mode: "auto",
      base_build: "b1",
      build: "b2",
      checks: { pass: 1, fail: 0, untested: 0 },
      changes: [change("a", "accepted", "comment-1"), change("b", "accepted", "comment-2"), change("c", "proposed", "comment-1")],
    } as ChangeSet;
    const mine = changesFor(set, "comment-1");
    expect(mine.map((c) => c.id)).toEqual(["a", "c"]);
    expect(undoReview(mine)).toEqual({ accept: [], reject: ["a"] });
    expect(undoReview([change("c", "proposed", "comment-1")])).toBeNull();
    expect(changesFor(null, "comment-1")).toEqual([]);
  });

  it("says where a thread stands", () => {
    expect(threadState(comment())).toBe("note");
    expect(threadState(comment({ request: { message_id: "m", status: "running" } }))).toBe("working");
    expect(threadState(comment({ request: { message_id: "m", status: "answered" } }))).toBe("answered");
    expect(threadState(comment({ resolved: true, request: { message_id: "m", status: "answered" } }))).toBe("resolved");
  });

  // Regression: a thread whose run was paused on Claude's question said "Claude is working".
  it("is waiting, not working, while Claude's question to the user is open", () => {
    const running = comment({ request: { message_id: "m", status: "running" } });
    expect(threadState(running, true)).toBe("waiting");
    expect(THREAD_LABEL.waiting).toBe("Waiting for your answer");
    expect(isAnswerFirst("Answer Claude's question first")).toBe(true);
    expect(isAnswerFirst("Answer Claude's question in the comment thread")).toBe(true);
    expect(isAnswerFirst("Claude is working: send when the run has finished")).toBe(false);
    expect(isAnswerFirst(undefined)).toBe(false);
  });

  it("replaces a comment in place and appends a new one", () => {
    const list = [comment(), comment({ id: "comment-2" })];
    expect(withComment(list, comment({ text: "Edited" })).map((c) => c.text)).toEqual(["Edited", list[1].text]);
    expect(withComment(list, comment({ id: "comment-3" })).map((c) => c.id)).toEqual(["comment-1", "comment-2", "comment-3"]);
  });

  it("shows how long ago", () => {
    const now = Date.parse("2026-01-01T01:00:00Z");
    expect(timeAgo("2026-01-01T00:59:50Z", now)).toBe("now");
    expect(timeAgo("2026-01-01T00:58:00Z", now)).toBe("2m");
    expect(timeAgo("2025-12-31T22:00:00Z", now)).toBe("3h");
  });

  it("keeps a run's question open until more of the run arrives", () => {
    const q: SessionEvent = {
      type: "question",
      message_id: "m1",
      question_id: "q-1",
      text: "Which way?",
      options: [
        { id: "a", label: "A" },
        { id: "b", label: "B" },
      ],
    };
    let open = applyQuestion({}, q);
    expect(open.m1.questionId).toBe("q-1");
    open = applyQuestion(open, { type: "comment", message_id: "m1", comment: comment() });
    expect(open.m1).toBeDefined();
    open = applyQuestion(open, { type: "progress", message_id: "m1", text: "Going on" });
    expect(open).toEqual({});
  });
});

describe("the chat and comment runs", () => {
  const sent = comment({ request: { message_id: "msg-3", status: "running" } });

  it("keeps a comment run's reply out of the chat", () => {
    let messages: ChatMessage[] = [];
    messages = applyEvent(messages, { type: "comment", comment: sent });
    messages = applyEvent(messages, { type: "message_start", message_id: "msg-3", role: "assistant" });
    messages = applyEvent(messages, { type: "text", message_id: "msg-3", delta: "Replied in the thread." });
    expect(messages).toHaveLength(1);
    expect(messages[0]).toMatchObject({ id: "msg-3", thread: true, streaming: true });
    messages = applyEvent(messages, { type: "message_end", message_id: "msg-3" });
    expect(messages[0].streaming).toBe(false);
  });

  it("ignores comment events that start no run", () => {
    const messages = applyEvent([], { type: "comment", comment: comment() });
    expect(messages).toEqual([]);
    const answered = applyEvent([], { type: "comment", comment: comment({ request: { message_id: "m", status: "answered" } }) });
    expect(answered).toEqual([]);
  });

  it("a question in a comment run is open, to be answered in the thread", () => {
    let messages: ChatMessage[] = applyEvent([], { type: "comment", comment: sent });
    messages = applyEvent(messages, {
      type: "question",
      message_id: "msg-3",
      question_id: "q-1",
      text: "Which way?",
      options: [
        { id: "a", label: "A" },
        { id: "b", label: "B" },
      ],
    });
    expect(openQuestion(messages)?.questionId).toBe("q-1");
    expect(messages[messages.length - 1].thread).toBe(true);
  });
});
