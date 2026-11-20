"""Conversations: the main chat and each comment thread sent to Claude are separate
conversations about the one design (`GET .../conversations`).

- Each conversation has its own history and its own question. A message belongs to the main
  chat; a comment sent to Claude ("Send to Claude") to that comment's thread. A question waits
  on its own conversation only: when another conversation's run is to start, the run paused on
  the question is parked (`app.session_api.park_question`): it ends, its question stays open in
  its conversation, and the answer starts a new run on the same request.
- All conversations edit the one design, so their runs take turns: a run that starts while
  another is going waits in the message queue (`app.message_queue`) and starts from the latest
  design.
- Shared context: a thread's run gets the main timeline (the runs of every conversation, as
  the main chat shows them) next to its own thread; a main chat run gets each thread's summary.
- A thread's run reports back to the main chat when it ends (`ThreadReportEvent`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.contracts.session import Comment, Conversation, MessageCreate
from app.security.scrub import untrusted

if TYPE_CHECKING:
    from app.session_api import SessionState

MAIN = "main"
MAIN_TITLE = "Main chat"
TITLE_LENGTH = 48
# How much of the timeline a run is told: the latest entries, each cut short.
TIMELINE_ENTRIES = 12
ENTRY_LENGTH = 300


def conversation_of(body: MessageCreate) -> str:
    """The conversation a message belongs to: its comment's thread, else the main chat."""
    return body.comments[0] if body.comments else MAIN


def thread_title(comment: Comment) -> str:
    """A thread's name: the start of its comment's first line."""
    line = comment.text.strip().splitlines()[0] if comment.text.strip() else "Comment"
    return line if len(line) <= TITLE_LENGTH else line[: TITLE_LENGTH - 1].rstrip() + "…"


def title_of(state: SessionState, conversation: str) -> str:
    if conversation == MAIN:
        return MAIN_TITLE
    comment = next((c for c in state.comments if c.id == conversation), None)
    return thread_title(comment) if comment is not None else "Comment"


def live_conversation(state: SessionState) -> str | None:
    """The conversation of the run that is going or paused on its question."""
    if state.session.status == "idle":
        return None
    return state.run_conversation or MAIN


def waiting(state: SessionState) -> set[str]:
    """The conversations with Claude's question open: their queued messages wait for it."""
    out = set(state.parked)
    if state.session.status == "waiting_for_answer" and state.question is not None:
        out.add(live_conversation(state) or MAIN)
    return out


def can_send(state: SessionState, body: MessageCreate) -> bool:
    """A queued message can go: an answer to a parked question always can; anything else once
    its conversation has no open question (it is never taken as the answer)."""
    if body.answer is not None:
        asked = body.answer.question_id
        return any(p["question"].question_id == asked for p in state.parked.values())
    return conversation_of(body) not in waiting(state)


def _latest_summary(state: SessionState, conversation: str) -> str | None:
    return next(
        (r["summary"] for r in reversed(state.reports) if r["conversation"] == conversation), None
    )


def conversations(state: SessionState) -> list[Conversation]:
    """Every conversation and where it stands: the main chat first, then each thread sent to
    Claude, in the order the comments were made. Comments sent together share their run, so
    they show where that run stands."""
    live = live_conversation(state)
    live_run = (state.last_run or {}).get("id") if live is not None else None
    parked_runs = {p["run"].get("id"): k for k, p in state.parked.items() if p["run"].get("id")}
    queued = {conversation_of(q.message) for q in state.queue}
    queued |= {
        conversation
        for q in state.queue
        if q.message.answer is not None
        for conversation, p in state.parked.items()
        if p["question"].question_id == q.message.answer.question_id
    }

    def owner(c: Comment) -> str:
        """The conversation whose run a comment waits on: its own, or the batch it went with."""
        if c.request is not None and c.request.status == "running":
            if c.request.message_id in parked_runs:
                return parked_runs[c.request.message_id]
            if live is not None and c.request.message_id == live_run:
                return live
        return c.id

    threads = [
        c
        for c in state.comments
        if c.request is not None or c.id in queued | set(state.parked) | {live}
    ]
    out = []
    for cid, run_of in [(MAIN, MAIN), *((c.id, owner(c)) for c in threads)]:
        question = None
        if run_of in state.parked:
            question = state.parked[run_of]["question"]
        elif run_of == live and state.session.status == "waiting_for_answer":
            question = state.question
        if question is not None:
            status = "waiting_for_answer"
        elif run_of == live:
            status = "working"
        elif cid in queued:
            status = "queued"
        else:
            status = "idle"
        behind = live if status == "queued" and live is not None and live != cid else None
        out.append(
            Conversation(
                id=cid,
                kind="main" if cid == MAIN else "thread",
                title=title_of(state, cid),
                status=status,
                queued_behind=behind,
                queued_behind_title=title_of(state, behind) if behind is not None else None,
                question=question,
                summary=_latest_summary(state, cid),
            )
        )
    return out


def _cut(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= ENTRY_LENGTH else text[: ENTRY_LENGTH - 1] + "…"


def _entry(state: SessionState, report: dict[str, Any]) -> str:
    said = f"Claude: {_cut(report['summary'])}" if report.get("summary") else "Claude replied."
    if report["conversation"] == MAIN:
        asked = _cut(report.get("request") or "")
        return f"- Main chat. {'The user asked: ' + asked + ' ' if asked else ''}{said}"
    title = title_of(state, report["conversation"])
    return f'- Comment thread "{title}" ({report["conversation"]}). {said}'


def main_timeline(state: SessionState) -> str:
    """The main timeline as a thread's run is told it: the latest runs of every conversation."""
    entries = [_entry(state, r) for r in state.reports[-TIMELINE_ENTRIES:]]
    if not entries:
        return ""
    return (
        "The main chat's timeline so far (oldest first; every conversation edits the same "
        "design, which is where these left it):\n"
        + untrusted("conversation timeline", "\n".join(entries))
    )


def thread_summaries(state: SessionState, but: str | None = None) -> str:
    """Each comment thread's conversation as the main chat's run is told it."""
    lines = []
    for c in conversations(state):
        if c.kind != "thread" or c.id == but:
            continue
        where = {
            "working": "Claude is working on it",
            "queued": "its next run waits its turn",
            "waiting_for_answer": f'Claude asked: "{c.question.text}"' if c.question else "",
            "idle": "",
        }[c.status]
        said = f"Latest: {_cut(c.summary)}" if c.summary else "No run has finished yet."
        lines.append(f'- "{c.title}" ({c.id}): {said}{" " + where + "." if where else ""}')
    if not lines:
        return ""
    return (
        "Comment threads sent to you, each its own conversation about this same design (their "
        "changes are in the design already):\n"
        + untrusted("comment thread summaries", "\n".join(lines))
    )


def context_for(state: SessionState, conversation: str) -> str:
    """What a run in `conversation` is told about the others."""
    if conversation == MAIN:
        return thread_summaries(state)
    parts = [
        "This run is the conversation in the comment thread above; the main chat and other "
        "threads are separate conversations about the same design.",
        main_timeline(state),
        thread_summaries(state, but=conversation),
    ]
    return "\n\n".join(p for p in parts if p)


__all__ = [
    "MAIN",
    "MAIN_TITLE",
    "can_send",
    "context_for",
    "conversation_of",
    "conversations",
    "live_conversation",
    "main_timeline",
    "thread_summaries",
    "thread_title",
    "title_of",
    "waiting",
]
