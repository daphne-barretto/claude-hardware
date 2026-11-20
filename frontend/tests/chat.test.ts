import { recordedEvents, sessionOpenApi } from "./fixtures";
import { describe, expect, it } from "vitest";
import { answerQuestion, applyEvent, fromStored, openQuestion, withAnswers, type Block, type ChatMessage } from "@/components/sites/claude-shell/chat";
import { EFFORTS } from "@/components/sites/claude-shell/data";
import type { SessionEvent } from "@/lib/session-api";

// The recorded run and the contract come from the generated files, never copied values.
const recorded = recordedEvents;
const openapi = sessionOpenApi;

const play = (events: SessionEvent[], start: ChatMessage[] = []) => events.reduce(applyEvent, start);
const ofType = <T extends SessionEvent["type"]>(type: T) =>
  recorded.filter((e): e is Extract<SessionEvent, { type: T }> => e.type === type);
const blocks = <K extends Block["kind"]>(m: ChatMessage, kind: K) =>
  (m.blocks ?? []).filter((b): b is Extract<Block, { kind: K }> => b.kind === kind);

describe("applyEvent on the recorded session", () => {
  const messages = play(recorded);
  const reply = messages[0];

  it("builds one finished assistant message", () => {
    expect(messages).toHaveLength(1);
    expect(reply.role).toBe("assistant");
    expect(reply.streaming).toBe(false);
  });

  it("keeps the streamed text and the progress lines in order", () => {
    expect(blocks(reply, "text").map((b) => b.text).join("")).toBe(ofType("text").map((e) => e.delta).join(""));
    expect(blocks(reply, "progress").map((b) => b.text)).toEqual(ofType("progress").map((e) => e.text));
    expect(blocks(reply, "progress")[0].text).toMatch(/round 1 of \d+/);
    // Progress interleaves with the text instead of collecting at the top.
    const kinds = reply.blocks!.map((b) => b.kind);
    expect(kinds.lastIndexOf("progress")).toBeGreaterThan(kinds.indexOf("text"));
  });

  it("shows the build and the question with its options", () => {
    expect(blocks(reply, "build").map((b) => b.build)).toEqual(ofType("build_ready").map((e) => e.build));
    const [question] = ofType("question");
    expect(blocks(reply, "question")).toEqual([
      { kind: "question", questionId: question.question_id, text: question.text, options: question.options },
    ]);
    expect(openQuestion(messages)?.questionId).toBe(question.question_id);
  });

  it("is streaming until the question", () => {
    const partial = play(recorded.slice(0, recorded.findIndex((e) => e.type === "question")));
    expect(partial[0].streaming).toBe(true);
  });

  it("marks an answered question so it is no longer open", () => {
    const [question] = ofType("question");
    const answered = answerQuestion(messages, question.question_id, question.options[1].id);
    expect(blocks(answered[0], "question")[0].answer).toBe(question.options[1].id);
    expect(openQuestion(answered)).toBeUndefined();
  });

  it("is idempotent for a repeated message_start (a reconnect)", () => {
    expect(play([recorded[0]], messages)).toEqual(messages);
  });
});

describe("reopening a stored session", () => {
  const start = recorded[0] as Extract<SessionEvent, { type: "message_start" }>;
  const [question] = ofType("question");
  const option = question.options[0];
  const stored = fromStored([
    { id: "u1", role: "user", text: "Check it", created_at: "2026-01-01T00:00:00Z" },
    { id: start.message_id, role: "assistant", text: "stored text is not used", created_at: "2026-01-01T00:00:01Z" },
    { id: "u2", role: "user", text: "", created_at: "2026-01-01T00:00:02Z", answer: { question_id: question.question_id, option_id: option.id } },
  ]);

  it("rebuilds the replies from the resent stream, in the stored order", () => {
    const partial = play([recorded[0]], stored);
    expect(partial[1]).toMatchObject({ id: start.message_id, blocks: [], streaming: true });
    const done = withAnswers(play(recorded, stored));
    expect(done.map((m) => m.id)).toEqual(["u1", start.message_id, "u2"]);
    expect(done[1].streaming).toBe(false);
    expect(blocks(done[1], "text").map((b) => b.text).join("")).toBe(ofType("text").map((e) => e.delta).join(""));
  });

  it("marks stored answers and shows the chosen option as the reply", () => {
    const done = withAnswers(play(recorded, stored));
    expect(openQuestion(done)).toBeUndefined();
    expect(blocks(done[1], "question")[0].answer).toBe(option.id);
    expect(done[2].text).toBe(option.label);
  });
});

describe("applyEvent", () => {
  it("ends a message on an error and shows it", () => {
    const m = play([
      { type: "message_start", message_id: "a", role: "assistant" },
      { type: "error", message_id: "a", message: "the run failed", retryable: true },
    ]);
    expect(m[0].streaming).toBe(false);
    expect(m[0].blocks).toEqual([{ kind: "error", text: "the run failed", retryable: true, reason: undefined }]);
  });

  it("stops streaming while a live run waits on its question, and resumes after the answer", () => {
    // The real session API pauses the run on a question and resumes the same message on the answer.
    const [question] = ofType("question");
    const asked = play([
      { type: "message_start", message_id: "a", role: "assistant" },
      { type: "progress", message_id: "a", text: "Running the checks" },
      question,
    ].map((e) => ({ ...e, message_id: "a" }) as SessionEvent));
    expect(asked[0].streaming).toBe(false);
    expect(openQuestion(asked)?.questionId).toBe(question.question_id);

    const resumed = play([{ type: "text", message_id: "a", delta: "Applying it." }], asked);
    expect(resumed).toHaveLength(1);
    expect(resumed[0].streaming).toBe(true);
    expect(play([{ type: "message_end", message_id: "a" }], resumed)[0].streaming).toBe(false);
  });

  it("shows a live run's continuation below the user's answer", () => {
    // The real session API resumes the paused run in the same message after the answer.
    const [question] = ofType("question");
    const asked = play([
      { type: "message_start", message_id: "a", role: "assistant" },
      { type: "progress", message_id: "a", text: "Running the checks" },
      { ...question, message_id: "a" },
    ] as SessionEvent[]);
    const answered: ChatMessage[] = [
      ...answerQuestion(asked, question.question_id, question.options[0].id),
      { id: "local-0", role: "user", text: question.options[0].label },
    ];
    const resumed = play(
      [
        { type: "progress", message_id: "a", text: "Applying the answer" },
        { type: "text", message_id: "a", delta: "Done." },
      ],
      answered,
    );
    expect(resumed.map((m) => m.role)).toEqual(["assistant", "user", "assistant"]);
    expect(resumed[0].streaming).toBe(false);
    expect(resumed[0].blocks!.map((b) => b.kind)).toEqual(["progress", "question"]);
    expect(resumed[2]).toMatchObject({
      streamId: "a",
      streaming: true,
      blocks: [{ kind: "progress", text: "Applying the answer" }, { kind: "text", text: "Done." }],
    });
    expect(new Set(resumed.map((m) => m.id)).size).toBe(3);
    const ended = play([{ type: "message_end", message_id: "a" }], resumed);
    expect(ended).toHaveLength(3);
    expect(ended[2].streaming).toBe(false);
  });

  it("treats a question the run went on past as no longer open", () => {
    // Reopening a live session resends the paused run and its continuation in one message.
    const [question] = ofType("question");
    const asked = play([
      { type: "message_start", message_id: "a", role: "assistant" },
      { ...question, message_id: "a" },
    ] as SessionEvent[]);
    expect(openQuestion(asked)?.questionId).toBe(question.question_id);
    // A rebuild from a hand edit (no message ID) is not Claude going on.
    const build = ofType("build_ready")[0];
    const rebuilt = play([{ ...build, message_id: null }], asked);
    expect(openQuestion(rebuilt)?.questionId).toBe(question.question_id);
    expect(openQuestion(play([{ type: "text", message_id: "a", delta: "Done." }], asked))).toBeUndefined();
  });

  it("marks a question the run went on past, so its options say why they can't be chosen", () => {
    const [question] = ofType("question");
    const asked = play([
      { type: "message_start", message_id: "a", role: "assistant" },
      { ...question, message_id: "a" },
    ] as SessionEvent[]);
    expect(blocks(asked[0], "question")[0].passed).toBeUndefined();
    // A rebuild from a hand edit doesn't pass it.
    const rebuilt = play([{ ...ofType("build_ready")[0], message_id: null }], asked);
    expect(blocks(rebuilt[0], "question")[0].passed).toBeUndefined();
    // More of the run does, and so does a later reply.
    const went = play([{ type: "text", message_id: "a", delta: "Done." }], asked);
    expect(blocks(went[0], "question")[0].passed).toBe(true);
    const later = play([{ type: "message_start", message_id: "b", role: "assistant" }], asked);
    expect(blocks(later[0], "question")[0].passed).toBe(true);
    // An answered question stays answered, not passed.
    const answered = play([{ type: "text", message_id: "a", delta: "Done." }], answerQuestion(asked, question.question_id, question.options[0].id));
    expect(blocks(answered[0], "question")[0]).toMatchObject({ answer: question.options[0].id });
    expect(blocks(answered[0], "question")[0].passed).toBeUndefined();
  });

  it("puts events for an unknown message into a new reply", () => {
    const m = play([{ type: "text", message_id: "late", delta: "hi" }]);
    expect(m).toMatchObject([{ id: "late", role: "assistant", blocks: [{ kind: "text", text: "hi" }] }]);
  });
});

describe("picker values match the session API contract", () => {
  const body = openapi.components.schemas.MessageCreate.properties;
  it("efforts", () => {
    const allowed = body.effort.anyOf.find((s: { enum?: string[] }) => s.enum).enum;
    for (const e of EFFORTS) expect(allowed).toContain(e.id);
  });
});
