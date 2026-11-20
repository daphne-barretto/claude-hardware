"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  createSession,
  listMessages,
  isSessionGone,
  importFile,
  ImportError,
  listConversations,
  isStepFile,
  postMessage,
  queueMessage,
  removeConstraint,
  removeQueued,
  retryRun,
  reviewRisks,
  sendQueued,
  stopRun,
  updateQueued,
  subscribe,
  ApiError,
  type StreamStatus,
  updateConstraint,
  uploadAttachment,
  type Attachment,
  type Constraint,
  type Conversation,
  type Effort,
  type Message as StoredMessage,
  type MessageCreate,
  type MessageQueue,
  type QuestionOption,
  type Selection,
  type SessionEvent,
} from "@/lib/session-api";
import {
  adoptStored,
  answerQuestion,
  applyEvent,
  fromStored,
  retryableStop,
  withAnswers,
  withRisks,
  type Block,
  type ChatMessage,
  type Choices,
} from "./chat";
import { filesFor, type PickedFile } from "./files";
import { EMPTY_QUEUE, applyQueueEvent, chatQueue, edited, moved } from "./messageQueue";
import { limitMessage, limitNotice, REMOVED_NOTE, wasScrubbed } from "@/lib/limits";
import { shownError } from "@/lib/errors";
import { riskReviewErrorText, type RiskReviewError } from "./riskReview";

/**
 * What the chat says when a request fails: the server's own words for a refused request (they
 * say what to do), plain words for a server that can't be reached. Never a raw error.
 */
export function errorText(e: unknown): string {
  if (e instanceof ImportError) return e.message;
  const limit = limitMessage(e); // an abuse limit's own words (`@/lib/limits`)
  if (limit) return limit;
  if (e instanceof ApiError && e.status < 500) return shownError(e);
  if (e instanceof ApiError) return "The server isn't responding right now, so this didn't go through. Nothing is lost; try again in a moment.";
  return "Couldn't reach the server, so this didn't go through. Nothing is lost; check your connection and try again.";
}
const NO_REPLY =
  "Claude hasn't started replying after a minute. Your message and work are kept; send it again, or reload the page to see if the reply arrived.";
/** The window event that makes the model pickers load the available models again. */
export const MODELS_CHANGED = "models:changed";
/** How long to wait for a reply to start before saying so. */
export const REPLY_TIMEOUT_MS = 60_000;

/** What the chat shows for a failed request: the session being gone gets its own message. */
// An abuse limit (rate, daily budget, size) shows as a notice, as the run's limits do (./RunNotice).
const failure = (e: unknown): Block => {
  if (isSessionGone(e)) return { kind: "session_gone" };
  const notice = limitNotice(e);
  return notice ? { kind: "notice", notice, at: Date.now() } : { kind: "error", text: errorText(e) };
};

/** Why nothing more can be sent once the session is gone. */
export const SESSION_GONE_REASON = "This chat is no longer on the server. Start a new chat to go on.";

/**
 * Sends picked files to the session: STEP files are imported as builds (the canvas shows them and
 * Claude is told about them), the rest are uploaded as the message's attachments.
 */
export async function sendFiles(sessionId: string, picked: PickedFile[]): Promise<Attachment[]> {
  for (const f of picked.filter((f) => isStepFile(f.name))) await importFile(sessionId, f.file, f.name);
  return Promise.all(picked.filter((f) => !isStepFile(f.name)).map((f) => uploadAttachment(sessionId, f.file, f.name)));
}

/**
 * A chat backed by the session API. With `existing`, it opens that session: its stored
 * messages, then its event stream from the start, which resends the replies with their
 * progress, builds and questions and then follows new events. Without it, the first message
 * creates a session and `onCreated` gets its ID.
 */
export function useChatSession(existing?: string, onCreated?: (id: string) => void) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  // An existing session is loading: the chat shows neither the empty state nor stale messages.
  const [loading, setLoading] = useState(!!existing);
  // Between posting a message and its reply starting, so the user cannot send twice.
  const [pending, setPending] = useState(false);
  // The design's spec, as the chips above the input show it (from `constraints` events).
  const [constraints, setConstraints] = useState<Constraint[]>([]);
  const session = useRef<Promise<string> | null>(existing ? Promise.resolve(existing) : null);
  const unsubscribe = useRef<(() => void) | null>(null);
  const nextLocal = useRef(0);
  const created = useRef(onCreated);
  useEffect(() => {
    created.current = onCreated;
  });

  const fail = useCallback((text: string) => {
    setPending(false);
    setMessages((m) => [
      ...m.map((msg) => (msg.streaming ? { ...msg, streaming: false } : msg)),
      { id: `local-${nextLocal.current++}`, role: "assistant", text: "", blocks: [{ kind: "error", text }] },
    ]);
  }, []);

  // The session's message queue (./messageQueue), as the latest `queue` event has it.
  const [queue, setQueue] = useState<MessageQueue>(EMPTY_QUEUE);
  const queued = useRef<MessageQueue>(EMPTY_QUEUE);
  const showQueue = useCallback((next: MessageQueue) => {
    queued.current = next;
    setQueue(next);
  }, []);
  // Where the main chat and each comment thread sent to Claude stand (`conversations` events).
  const [conversations, setConversations] = useState<Conversation[]>([]);
  // The model and effort of the session's last message, so the picker reopens on them.
  const [storedChoices, setStoredChoices] = useState<StoredChoices | null>(null);
  // The event stream's connection (`subscribe`): the chat shows "Reconnecting…" or a banner.
  const [connection, setConnection] = useState<StreamStatus | null>(null);

  const onEvent = useCallback((event: SessionEvent) => {
    if (event.type === "conversations") {
      setConversations(event.conversations);
      return;
    }
    if (event.type === "queue") {
      // A message the queue sent becomes the user's message in the chat, before its reply.
      const before = queued.current;
      showQueue(event.queue);
      setMessages((m) => applyQueueEvent(before, m, event).messages);
      return;
    }
    // The reply has started. An answer resumes the paused run, so its reply has no
    // message_start: any event of a message counts (not the session's first build).
    // The user's own message (stored, from this tab or another) is not the reply.
    if ((event.message_id != null && event.type !== "user_message") || event.type === "error") setPending(false);
    // A model the server found unavailable: the pickers load the models that work again.
    if (event.type === "error" && event.reason === "model_unavailable" && typeof window !== "undefined")
      window.dispatchEvent(new Event(MODELS_CHANGED));
    if (event.type === "constraints") setConstraints(event.constraints);
    setMessages((m) => withAnswers(applyEvent(m, event)));
  }, [showQueue]);

  /**
   * Follows a session's event stream. A dropped stream reconnects by itself (`subscribe`); a
   * session that is gone from the server gets its own message.
   */
  const follow = useCallback(
    (id: string) =>
      subscribe(
        id,
        onEvent,
        () => {
          unsubscribe.current = null;
          setConnection(null);
          setPending(false);
          setMessages((m) => [
            ...m.map((msg) => (msg.streaming ? { ...msg, streaming: false } : msg)),
            { id: `local-${nextLocal.current++}`, role: "assistant", text: "", blocks: [{ kind: "session_gone" }] },
          ]);
        },
        setConnection,
      ),
    [onEvent],
  );

  // A reply that hasn't started a minute after sending won't: say so instead of waiting forever.
  useEffect(() => {
    if (!pending) return;
    const timer = setTimeout(() => fail(NO_REPLY), REPLY_TIMEOUT_MS);
    return () => clearTimeout(timer);
  }, [pending, fail]);

  useEffect(() => {
    if (!existing) return () => unsubscribe.current?.();
    let live = true;
    let close: (() => void) | null = null;
    listMessages(existing)
      .then((stored) => {
        if (!live) return;
        setMessages(fromStored(stored));
        setStoredChoices(lastChoices(stored));
        close = follow(existing);
        // Changes stream from now on; this is where they stand already.
        listConversations(existing)
          .then((c) => live && setConversations(c))
          .catch(() => {});
      })
      .catch((e) =>
        live &&
        setMessages([{ id: "load-error", role: "assistant", text: "", blocks: [{ kind: "error", text: errorText(e) }] }]),
      )
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
      close?.();
    };
  }, [existing, follow]);

  const sessionId = useCallback(() => {
    session.current ??= createSession().then((s) => {
      unsubscribe.current = follow(s.id);
      created.current?.(s.id);
      return s.id;
    });
    // A failed create can be retried with the next message.
    session.current.catch(() => (session.current = null));
    return session.current;
  }, [follow]);

  // `body` may be a function of the session ID, for work that needs the session (uploads); it
  // returns null when there is nothing to send (only files imported).
  // The message being posted, so a message queued right behind it goes after it.
  const inflight = useRef<Promise<void> | null>(null);
  // `shown`: the ID the chat shows the message under until the server stores it.
  const post = useCallback(
    (body: MessageCreate | ((sid: string) => Promise<MessageCreate | null>), onFail?: () => void, shown?: string) => {
      const posting = (async () => {
        setPending(true);
        try {
          const sid = await sessionId();
          const message = typeof body === "function" ? await body(sid) : body;
          if (message) {
            const stored = await postMessage(sid, message);
            // The server took a secret or an email address out: show the message as it was kept.
            if (wasScrubbed(message.text ?? "", stored)) {
              setMessages((m) => [
                ...m.map((msg) => (msg.role === "user" && msg.text === message.text ? { ...msg, text: stored.text } : msg)),
                { id: `local-${nextLocal.current++}`, role: "assistant", text: "", blocks: [{ kind: "progress", text: REMOVED_NOTE }] },
              ]);
            }
            const storedId = (stored as { id?: unknown } | null)?.id;
            if (shown && typeof storedId === "string") setMessages((m) => adoptStored(m, shown, storedId));
          } else setPending(false);
        } catch (e) {
          setPending(false);
          onFail?.();
          setMessages((m) => [...m, { id: `local-${nextLocal.current++}`, role: "assistant", text: "", blocks: [failure(e)] }]);
        }
      })();
      inflight.current = posting;
      return posting;
    },
    [sessionId],
  );

  /** Reports a failed queue action in the chat. */
  const failed = useCallback((e: unknown) => {
    setMessages((m) => [...m, { id: `local-${nextLocal.current++}`, role: "assistant", text: "", blocks: [failure(e)] }]);
  }, []);

  // While Claude works, a message goes to the session's queue instead (./messageQueue): the
  // queue shows it until it is sent, then it becomes the user's message.
  const working = useRef(false);
  const enqueue = useCallback(
    async (make: (sid: string) => Promise<MessageCreate | null>) => {
      try {
        await inflight.current;
        const sid = await sessionId();
        const body = await make(sid);
        if (body) await queueMessage(sid, body);
      } catch (e) {
        failed(e);
      }
    },
    [sessionId, failed],
  );

  const send = useCallback(
    (
      text: string,
      files: string[],
      choices: Choices,
      selection: Selection | null = null,
      selectionLabel?: string,
      /** Other fields of the message, such as the session's standards with its first message. */
      extra: Partial<MessageCreate> = {},
    ) => {
      const picked = filesFor(files);
      const make = async (sid: string) => {
        const attachments = await sendFiles(sid, picked);
        if (!text.trim() && !attachments.length && picked.length) return null;
        return { ...extra, text, ...choices, selection, attachments };
      };
      if (working.current) return enqueue(make);
      const shown = `local-${nextLocal.current++}`;
      setMessages((m) => [
        ...m,
        {
          id: shown,
          role: "user",
          text,
          ...(files.length ? { files } : {}),
          selection,
          ...(selectionLabel ? { selectionLabel } : {}),
        },
      ]);
      // The staged files are sent first: STEP files are imported, the rest go with the message
      // as its attachments. A file imported on its own needs no message.
      return post(make, undefined, shown);
    },
    [post, enqueue],
  );

  // Queue actions: shown at once, then confirmed by the `queue` event every tab gets.
  const editQueued = useCallback(
    async (id: string, text: string) => {
      const sid = await session.current;
      if (!sid) return;
      showQueue(edited(queued.current, id, text));
      await updateQueued(sid, id, { text }).catch(failed);
    },
    [showQueue, failed],
  );
  const moveQueued = useCallback(
    async (id: string, by: -1 | 1) => {
      const sid = await session.current;
      const move = moved(queued.current, id, by);
      if (!sid || !move) return;
      showQueue({ ...queued.current, items: move.items });
      await updateQueued(sid, id, { position: move.position }).catch(failed);
    },
    [showQueue, failed],
  );
  const removeQueuedItem = useCallback(
    async (id: string) => {
      const sid = await session.current;
      if (!sid) return;
      showQueue(edited(queued.current, id, null));
      await removeQueued(sid, id).catch(failed);
    },
    [showQueue, failed],
  );
  /** "Send anyway": the next queued message goes now, and Claude's open question stays unanswered. */
  const sendAnyway = useCallback(async () => {
    const sid = await session.current;
    const next = chatQueue(queued.current).items[0];
    if (!sid || !next) return;
    await sendQueued(sid, next.id).catch(failed);
  }, [failed]);
  const answer = useCallback(
    (questionId: string, option: QuestionOption, choices: Choices) => {
      const shown = `local-${nextLocal.current++}`;
      const reply = { question_id: questionId, option_id: option.id };
      setMessages((m) => [...answerQuestion(m, questionId, option.id), { id: shown, role: "user", text: option.label, answer: reply }]);
      return post({ text: "", ...choices, answer: reply }, () =>
        // Let the user pick again.
        setMessages((m) =>
          m.map((msg) => ({
            ...msg,
            blocks: msg.blocks?.map((b) => (b.kind === "question" && b.questionId === questionId ? { ...b, answer: undefined } : b)),
          })),
        ),
        shown,
      );
    },
    [post],
  );

  /** Edits or removes a chip. The rebuild and recheck stream in as Claude's next message. */
  const changeConstraint = useCallback(
    async (id: string, value?: number | string) => {
      if (!session.current) return;
      setPending(true);
      try {
        const sid = await session.current;
        await (value === undefined ? removeConstraint(sid, id) : updateConstraint(sid, id, value));
      } catch (e) {
        setPending(false);
        setMessages((m) => [
          ...m,
          { id: `local-${nextLocal.current++}`, role: "assistant", text: "", blocks: [{ kind: "error", text: errorText(e) }] },
        ]);
      }
    },
    [],
  );

  // Accepting or ignoring a risk's fix: the design rebuilds, and the flags come back updated.
  const [reviewing, setReviewing] = useState(false);
  // Why the last Accept or Ignore didn't go through: shown once, on its flag, not in the chat.
  const [riskError, setRiskError] = useState<RiskReviewError | null>(null);
  const reviewRisk = useCallback(async (flagId: string, decision: "accept" | "reject") => {
    const sid = await session.current;
    if (!sid) return;
    setReviewing(true);
    try {
      const flags = await reviewRisks(sid, { [decision]: [flagId] });
      setRiskError(null);
      setMessages((m) => withRisks(m, flags));
    } catch (e) {
      setRiskError({ flagId, text: riskReviewErrorText(e, errorText) });
    } finally {
      setReviewing(false);
    }
  }, []);


  // Comments sent to Claude from the canvas: one run answers each in its thread, so the chat
  // shows no message for them (`thread` in ./chat).
  // While Claude works they wait in the queue, like a message.
  const sendComments = useCallback(
    (ids: string[], choices: Choices) => {
      const body: MessageCreate = { text: "", ...choices, comments: ids };
      return working.current ? enqueue(async () => body) : post(body);
    },
    [post, enqueue],
  );
  // An answer to a question Claude asked in a comment's run, given in the thread or from its
  // pointer in the main chat. While another run works it waits its turn in the queue.
  const answerInThread = useCallback(
    (questionId: string, optionId: string, choices: Choices) => {
      setMessages((m) => answerQuestion(m, questionId, optionId));
      const body: MessageCreate = { text: "", ...choices, answer: { question_id: questionId, option_id: optionId } };
      return working.current ? enqueue(async () => body) : post(body);
    },
    [post, enqueue],
  );

  // Retry the reply that stopped, or stop the one going: the work so far is kept either way.
  const [acting, setActing] = useState(false);
  const retry = useCallback(
    async (choices: Choices) => {
      const sid = await session.current;
      if (!sid) return;
      setActing(true);
      setMessages((m) => [...m, { id: `local-${nextLocal.current++}`, role: "user", text: "Retry" }]);
      setPending(true);
      try {
        await retryRun(sid, choices);
      } catch (e) {
        setPending(false);
        setMessages((m) => [...m, { id: `local-${nextLocal.current++}`, role: "assistant", text: "", blocks: [failure(e)] }]);
      } finally {
        setActing(false);
      }
    },
    [],
  );
  const stop = useCallback(async () => {
    const sid = await session.current;
    if (!sid) return;
    setActing(true);
    try {
      await stopRun(sid);
    } catch (e) {
      setMessages((m) => [...m, { id: `local-${nextLocal.current++}`, role: "assistant", text: "", blocks: [failure(e)] }]);
    } finally {
      setActing(false);
    }
  }, []);
  const reconnect = useCallback(() => window.dispatchEvent(new Event("session-stream:retry")), []);

  const busy = pending || loading || messages.some((m) => m.streaming);
  // Claude is working on a reply: Send queues the message (not while the session loads).
  const queues = busy && !loading;
  working.current = queues;
  // Nothing waits on Claude's open question: a message sent meanwhile goes to Claude, which is
  // told the question was left open (docs/architecture.md). Only a session that is gone blocks.
  const gone = messages.some((m) => m.blocks?.some((b) => b.kind === "session_gone"));
  const blocked = gone ? SESSION_GONE_REASON : undefined;
  return {
    storedChoices,
    messages,
    busy,
    waiting: pending,
    loading,
    blocked,
    reviewing,
    send,
    answer,
    constraints,
    changeConstraint,
    reviewRisk,
    riskError,
    sendComments,
    answerInThread,
    queue,
    queues,
    editQueued,
    moveQueued,
    removeQueued: removeQueuedItem,
    sendAnyway,
    retry,
    /** Stop the run (`POST .../stop`): one Stop for the queue and the notices. */
    stop,
    stopping: acting,
    acting,
    /** The stopped reply Retry applies to, if any. */
    latestStop: retryableStop(messages),
    connection,
    reconnect,
    /** The main chat and each comment thread sent to Claude, and where each stands. */
    conversations,
  };
}

export type StoredChoices = { model?: string; effort?: Effort };

/** The model and effort the session's last user message was sent with (null: none recorded). */
export function lastChoices(stored: StoredMessage[]): StoredChoices | null {
  const last = [...stored].reverse().find((m) => m.role === "user" && (m.model || m.effort));
  if (!last) return null;
  return { ...(last.model ? { model: last.model } : {}), ...(last.effort ? { effort: last.effort } : {}) };
}
