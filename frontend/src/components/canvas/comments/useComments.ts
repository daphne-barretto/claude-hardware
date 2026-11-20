"use client";

import { useCallback, useEffect, useState } from "react";
import { listConversations, subscribe, type Comment, type Conversation } from "@/lib/session-api";
import { applyQuestion, listComments, withComment, type Draft, type ThreadQuestion } from "./comments";

/** How long to wait for more `comment` events before reloading the list (the stream resends them all on connect). */
const RELOAD_MS = 120;

/**
 * The session's comments, located on the shown build, and the open questions of comment runs.
 * Loaded for the build, then kept up to date from the session's events: a `comment` event
 * updates its comment at once, and the list is reloaded so every pin is placed on the shown
 * build (events place them on the latest).
 */
export function useComments(sessionId: string, buildId: string | undefined) {
  const [loaded, setLoaded] = useState<{ key: string; comments: Comment[] } | null>(null);
  const [questions, setQuestions] = useState<Record<string, ThreadQuestion>>({});
  // Each thread sent to Claude is its own conversation: where it stands, by comment ID.
  const [conversations, setConversations] = useState<Record<string, Conversation>>({});
  const [version, setVersion] = useState(0);
  const key = `${sessionId}/${buildId ?? ""}`;

  useEffect(() => {
    if (!buildId) return;
    let live = true;
    listComments(sessionId, buildId)
      .then((comments) => live && setLoaded({ key, comments }))
      .catch(() => live && setLoaded((l) => l ?? { key, comments: [] }));
    return () => {
      live = false;
    };
  }, [sessionId, buildId, key, version]);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const byId = (list: Conversation[]) => Object.fromEntries(list.map((c) => [c.id, c]));
    listConversations(sessionId)
      .then((list) => setConversations(byId(list)))
      .catch(() => {});
    const close = subscribe(sessionId, (event) => {
      if (event.type === "conversations") setConversations(byId(event.conversations));
      setQuestions((q) => applyQuestion(q, event));
      if (event.type !== "comment") return;
      setLoaded((l) => (l ? { ...l, comments: withComment(l.comments, event.comment) } : l));
      clearTimeout(timer);
      timer = setTimeout(() => setVersion((v) => v + 1), RELOAD_MS);
    });
    return () => {
      clearTimeout(timer);
      close();
    };
  }, [sessionId]);

  /** Shows a comment the API returned at once (its event and the reload follow). */
  const put = useCallback(
    (comment: Comment) => setLoaded((l) => (l ? { ...l, comments: withComment(l.comments, comment) } : { key, comments: [comment] })),
    [key],
  );

  // Until the shown build's list arrives, keep the last one.
  return { comments: loaded?.comments ?? [], questions, conversations, put };
}

/**
 * The comment tool and the thread that is open. With the tool on, a click on the model places a
 * draft comment there (`place`) instead of selecting.
 */
export function useCommentTool() {
  const [active, setActive] = useState(false);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [listOpen, setListOpen] = useState(false);
  return {
    active,
    setActive,
    toggle: () => setActive((a) => !a),
    draft,
    place: (d: Draft | null) => {
      setDraft(d);
      if (d) setOpenId(null);
    },
    openId,
    open: (id: string | null) => {
      setOpenId(id);
      if (id) setDraft(null);
    },
    listOpen,
    setListOpen,
  };
}

export type CommentTool = ReturnType<typeof useCommentTool>;
