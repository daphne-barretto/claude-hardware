// Chat state built from the session API's event stream (contracts/README.md, "Server-sent events").
import type { FindResult } from "@/components/canvas/api";
import type {
  Answer,
  Build,
  ChangeSet,
  ConstraintConflict,
  Effort,
  Location,
  Message,
  LimitReason,
  Mode,
  Notice,
  QuestionOption,
  RiskFlags,
  Selection,
  SessionEvent,
} from "@/lib/session-api";

/** The model, effort and Manual/Auto choices sent with the next message. */
export type Choices = { model: string; effort: Effort; mode: Mode };

/** One piece of an assistant message, in the order it streamed in. */
export type Block =
  | { kind: "text"; text: string }
  | { kind: "progress"; text: string; round?: number | null; rounds?: number | null }
  | { kind: "build"; build: Build }
  | { kind: "changes"; changeSet: ChangeSet }
  | { kind: "risks"; riskFlags: RiskFlags }
  | { kind: "plan"; steps: string[] }
  | {
      kind: "question";
      questionId: string;
      text: string;
      options: QuestionOption[];
      answer?: string;
      /**
       * Claude went on past the question without the chat seeing the answer: a resumed run,
       * resent on reopening the session (the stream has no event for the answer).
       */
      passed?: boolean;
      /** The spot on the model the question is about: the canvas pins it there. */
      location?: Location | null;
      /** Why Claude asks: constraints that can't all be met. */
      conflict?: ConstraintConflict | null;
    }
  /** Faces Claude found from chat (`find_in_model`), on build `buildId`. */
  | { kind: "found"; buildId: string; result: FindResult }
  /**
   * The reply stopped (or a request failed). `retryable`: Retry picks up from the work so far;
   * `reason` says why (`LimitReason`).
   */
  | { kind: "error"; text: string; retryable?: boolean; reason?: LimitReason | null }
  /** A notice while the reply goes on: retrying, waiting in line, or a warning. */
  /** A notice apart from Claude's text: retrying, waiting in line, a warning, a refusal or a safety notice. */
  | { kind: "notice"; notice: Notice; at: number }
  /** The chat's session is no longer on the server: the chat offers a new one. */
  | { kind: "session_gone" };

export type ChatMessage = {
  /** Message ID from the stream; local user messages get a client ID. */
  id: string;
  role: "user" | "assistant";
  /** User messages: what was sent. Assistant messages keep their content in `blocks`. */
  text: string;
  files?: string[];
  selection?: Selection | null;
  /** The chip text for the selection, such as the selected face's feature. */
  selectionLabel?: string;
  /** A user message that answered a question. */
  answer?: Answer | null;
  blocks?: Block[];
  /** An assistant message that is still streaming. */
  streaming?: boolean;
  /**
   * The stream's message ID, on the part of a reply that came after the user's answer: a live
   * run resumes in the same message, so its continuation shows below the answer as a part of its own.
   */
  streamId?: string;
  /**
   * A run for comments sent to Claude: it answers in the comment threads on the canvas, so the
   * chat doesn't show it (its question, if any, is answered in the thread).
   */
  thread?: boolean;
};

/**
 * A session's stored messages, as the chat starts from when it opens an existing session.
 * Assistant messages start empty: their content comes from applying the event stream,
 * which keeps the progress lines, builds and questions the stored text does not have.
 */
export function fromStored(stored: Message[]): ChatMessage[] {
  return stored.map((m) =>
    m.role === "assistant"
      ? { id: m.id, role: "assistant", text: "", blocks: [] }
      : {
          id: m.id,
          role: "user",
          text: m.text,
          ...(m.attachments?.length ? { files: m.attachments.map((a) => a.name) } : {}),
          selection: m.selection ?? null,
          answer: m.answer ?? null,
        },
  );
}

/**
 * Marks the questions that user messages answered, and shows an answer's option label as its
 * text. Stored answers have no text, and their question only appears once its event is resent.
 */
export function withAnswers(messages: ChatMessage[]): ChatMessage[] {
  let out = messages;
  for (const m of messages) {
    if (m.role !== "user" || !m.answer) continue;
    const { question_id, option_id } = m.answer;
    const question = out
      .flatMap((x) => x.blocks ?? [])
      .find((b): b is Extract<Block, { kind: "question" }> => b.kind === "question" && b.questionId === question_id);
    if (!question) continue;
    if (question.answer !== option_id) out = answerQuestion(out, question_id, option_id);
    const label = question.options.find((o) => o.id === option_id)?.label;
    if (!m.text && label) out = out.map((x) => (x.id === m.id ? { ...x, text: label } : x));
  }
  return out;
}

/** A retrying or waiting notice only holds until the reply moves on. */
export const isPassing = (b: Block) => b.kind === "notice" && (b.notice.kind === "retrying" || b.notice.kind === "waiting");

function withBlock(blocks: Block[], block: Block): Block[] {
  // Anything new in the reply (a build may come from elsewhere) ends a retrying or waiting notice.
  if (block.kind !== "build" && blocks.some(isPassing)) blocks = blocks.filter((b) => !isPassing(b));
  const last = blocks[blocks.length - 1];
  if (block.kind === "text" && last?.kind === "text") return [...blocks.slice(0, -1), { kind: "text", text: last.text + block.text }];
  return [...blocks, block];
}

/** Marks the unanswered questions in `blocks` as gone past: Claude went on, so they are no longer open. */
function passQuestions(blocks: Block[]): Block[] {
  return blocks.some((b) => b.kind === "question" && !b.answer && !b.passed)
    ? blocks.map((b) => (b.kind === "question" && !b.answer ? { ...b, passed: true } : b))
    : blocks;
}

function updateAssistant(
  messages: ChatMessage[],
  id: string | null | undefined,
  update: (m: ChatMessage) => ChatMessage,
  continues = true,
) {
  // Events without a message ID (a build or an error outside a message) go to the latest reply.
  let index = -1;
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].role === "assistant" && (id == null || (messages[i].streamId ?? messages[i].id) === id)) {
      index = i;
      break;
    }
  }
  if (index < 0) {
    if (id == null) return [...messages, update({ id: `event-${messages.length}`, role: "assistant", text: "", blocks: [] })];
    return [...messages, update({ id, role: "assistant", text: "", blocks: [], streaming: true })];
  }
  // The user answered this part's question since it streamed: more of it streams in below the
  // answer. A stored session's events apply to its stored messages, whose answers are already there,
  // so a part only continues below a user message once it has reached its question.
  const answeredAt = messages.findIndex((m, i) => i > index && m.role === "user");
  if (id != null && continues && answeredAt >= 0 && messages[index].blocks?.some((b) => b.kind === "question")) {
    const parts = messages.filter((m) => (m.streamId ?? m.id) === id).length;
    const before = messages.map((m, i) => (i === index ? { ...m, streaming: false } : m));
    const part = update({ id: `${id}~${parts}`, streamId: id, role: "assistant", text: "", blocks: [], streaming: true });
    return [...before.slice(0, answeredAt + 1), part, ...before.slice(answeredAt + 1)];
  }
  return messages.map((m, i) => (i === index ? update(m) : m));
}

/** A question's location and conflict, when it has them. */
function pinned(event: Extract<SessionEvent, { type: "question" }>) {
  const location = event.location ?? event.conflict?.location;
  return { ...(location ? { location } : {}), ...(event.conflict ? { conflict: event.conflict } : {}) };
}

/** Applies one server-sent event to the chat. Pure, so a recorded stream can be applied in tests. */
export function applyEvent(messages: ChatMessage[], event: SessionEvent): ChatMessage[] {
  // A live run pauses on a question without ending its message, and the answer resumes it
  // (docs/architecture.md), so the reply streams again once more of it arrives.
  // More of a reply after its question (other than a build, which may come from a hand edit)
  // means the run went on: the question was answered.
  const add = (block: Block) => (m: ChatMessage): ChatMessage => ({
    ...m,
    blocks: withBlock(block.kind === "build" ? (m.blocks ?? []) : passQuestions(m.blocks ?? []), block),
    ...(m.streaming === false && event.message_id === (m.streamId ?? m.id) ? { streaming: true } : {}),
  });
  switch (event.type) {
    case "message_start":
      // A reconnect repeats it: nothing changes. A stored message that is still empty (see
      // fromStored) is being resent from the start, so it is in progress until its message_end.
      if (messages.some((m) => m.id === event.message_id))
        return messages.map((m) => (m.id === event.message_id && m.blocks?.length === 0 && !m.streaming ? { ...m, streaming: true } : m));
      // A new reply starts only once no question waits (docs/architecture.md).
      return [...messages.map((m) => (m.blocks ? { ...m, blocks: passQuestions(m.blocks) } : m)), { id: event.message_id, role: "assistant", text: "", blocks: [], streaming: true }];
    case "text":
      return updateAssistant(messages, event.message_id, add({ kind: "text", text: event.delta }));
    case "progress":
      return updateAssistant(
        messages,
        event.message_id,
        add({ kind: "progress", text: event.text, round: event.round, rounds: event.rounds }),
      );
    case "build_ready":
      return updateAssistant(messages, event.message_id, add({ kind: "build", build: event.build }));
    case "changes_ready":
      return updateAssistant(messages, event.message_id, add({ kind: "changes", changeSet: event.change_set }));
    case "plan":
      return updateAssistant(messages, event.message_id, add({ kind: "plan", steps: event.steps }));
    case "constraints":
      // The chips above the input show these; the transcript doesn't change.
      return messages;
    case "risks_ready":
      // A review (no message) updates the flags where they are; a run's flags go in its reply.
      if (event.message_id == null && hasRisks(messages)) return withRisks(messages, event.risk_flags);
      return updateAssistant(messages, event.message_id, add({ kind: "risks", riskFlags: event.risk_flags }));
    case "question":
      return updateAssistant(
        messages,
        event.message_id,
        // Claude waits for the answer: the reply is not streaming, so the options can be chosen.
        (m) => ({
          ...add({
            kind: "question",
            questionId: event.question_id,
            text: event.text,
            options: event.options,
            ...pinned(event),
          })(m),
          streaming: false,
        }),
      );
    case "found":
      return updateAssistant(messages, event.message_id, add({ kind: "found", buildId: event.build_id, result: event.result }));
    case "notice": {
      const { kind, text, reason, retry_in_s, attempt, attempts, place, categories, regulations, region } = event;
      const notice = { kind, text, reason, retry_in_s, attempt, attempts, place, categories, regulations, region };
      return updateAssistant(messages, event.message_id, add({ kind: "notice", notice, at: Date.now() }));
    }
    case "error":
      return updateAssistant(messages, event.message_id, (m) => ({
        ...add({ kind: "error", text: event.message, retryable: event.retryable, reason: event.reason })(m),
        streaming: false,
      }));
    case "message_end":
      return updateAssistant(
        messages,
        event.message_id,
        (m) => ({ ...m, streaming: false, ...(m.blocks?.some(isPassing) ? { blocks: m.blocks.filter((b) => !isPassing(b)) } : {}) }),
        false,
      );
    case "user_message": {
      const stored = event.message;
      if (messages.some((m) => m.id === stored.id)) return messages;
      // This tab's own message, shown when it was sent: it takes the stored ID.
      const mine = messages.findIndex((m) => isLocalUser(m) && sameMessage(m, stored));
      if (mine >= 0) return messages.map((m, i) => (i === mine ? { ...m, id: stored.id } : m));
      // Sent from another tab (or before this one opened): shown as a reload would show it.
      return [...messages, ...fromStored([stored])];
    }
    case "comment": {
      // A comment sent to Claude names its run before the run starts: keep that reply out of the chat.
      const request = event.comment.request;
      if (request?.status !== "running" || messages.some((m) => m.id === request.message_id)) return messages;
      return [...messages, { id: request.message_id, role: "assistant", text: "", blocks: [], streaming: true, thread: true }];
    }
    default:
      return messages;
  }
}

/** A user message this tab showed when it was sent, before the server stored it. */
const isLocalUser = (m: ChatMessage) => m.role === "user" && m.id.startsWith("local-");

/** Whether a message this tab showed is the stored `message`: the same answer, else the same words. */
function sameMessage(shown: ChatMessage, message: Message): boolean {
  if (message.answer)
    return shown.answer?.question_id === message.answer.question_id && shown.answer?.option_id === message.answer.option_id;
  return !shown.answer && shown.text === message.text;
}

/**
 * The message this tab showed as `localId` was stored as `storedId`: it takes that ID, or goes
 * if the stored message is already shown (its `user_message` event came first, with the words
 * as kept, such as after a secret was taken out).
 */
export function adoptStored(messages: ChatMessage[], localId: string, storedId: string): ChatMessage[] {
  if (!messages.some((m) => m.id === localId)) return messages;
  if (messages.some((m) => m.id === storedId)) return messages.filter((m) => m.id !== localId);
  return messages.map((m) => (m.id === localId ? { ...m, id: storedId } : m));
}

const hasRisks = (messages: ChatMessage[]) => messages.some((m) => m.blocks?.some((b) => b.kind === "risks"));

/** Replaces the latest risk flags shown in the chat with `flags` (after a review). */
export function withRisks(messages: ChatMessage[], flags: RiskFlags): ChatMessage[] {
  let index = -1;
  messages.forEach((m, i) => m.blocks?.some((b) => b.kind === "risks") && (index = i));
  if (index < 0) return messages;
  return messages.map((m, i) =>
    i === index ? { ...m, blocks: m.blocks!.map((b) => (b.kind === "risks" ? { kind: "risks", riskFlags: flags } : b)) } : m,
  );
}

/** What a flag is about, for its controls' names: the feature, else the component, else its ID. */
export function riskSubject(flag: RiskFlags["flags"][number]) {
  return flag.location.feature || flag.location.component || flag.id;
}

/** Marks a question as answered with one of its options. */
export function answerQuestion(messages: ChatMessage[], questionId: string, optionId: string): ChatMessage[] {
  return messages.map((m) =>
    m.blocks?.some((b) => b.kind === "question" && b.questionId === questionId)
      ? {
          ...m,
          blocks: m.blocks.map((b) => (b.kind === "question" && b.questionId === questionId ? { ...b, answer: optionId } : b)),
        }
      : m,
  );
}

/** The open question pinned to a spot on the model, if any. */
export function pinnedQuestion(messages: ChatMessage[]) {
  const question = openQuestion(messages);
  return question?.location ? { ...question, location: question.location } : undefined;
}

/**
 * The question still waiting for an answer, if any: an unanswered question that nothing came
 * after. A reply that went on past its question (a resumed run, resent) was answered.
 */
export function openQuestion(messages: ChatMessage[]) {
  // A build without a message (a hand edit's rebuild) lands in the latest reply but is not Claude going on.
  const blocks = (messages[messages.length - 1]?.blocks ?? []).filter((b) => b.kind !== "build");
  const block = blocks[blocks.length - 1];
  return block?.kind === "question" && !block.answer ? block : undefined;
}

/**
 * The reply Retry would pick up: the latest reply, if it stopped with a retryable error and
 * nothing was sent after it.
 */
export function retryableStop(messages: ChatMessage[]) {
  const last = messages[messages.length - 1];
  if (last?.role !== "assistant" || last.streaming) return undefined;
  const blocks = last.blocks ?? [];
  const error = blocks[blocks.length - 1];
  return error?.kind === "error" && error.retryable ? error : undefined;
}

/** The latest faces Claude found in chat, with a key that changes with each new find. */
export function latestFound(messages: ChatMessage[]) {
  for (let i = messages.length - 1; i >= 0; i--) {
    const blocks = messages[i].blocks ?? [];
    for (let j = blocks.length - 1; j >= 0; j--) {
      const b = blocks[j];
      if (b.kind === "found") return { block: b, key: `${messages[i].id}:${j}` };
    }
  }
  return undefined;
}
