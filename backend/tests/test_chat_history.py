"""Each reply is told the session's chat so far: a fresh run of Claude sees the earlier messages,
Claude's replies, and the questions it asked with how they were answered, not only the new
message. Without it, an answer sent after the question was cut off (stopped, or its slot given
to another session) reaches Claude with nothing before it.

Claude is the gated `ScriptedDriver` of the message queue tests.
"""

from __future__ import annotations

import test_message_queue as mq
from test_message_queue import events, replies, send, start, wait_for, wait_until

data, client, claude = mq.data, mq.client, mq.claude


def test_a_reply_is_told_the_earlier_messages_and_replies(client, claude) -> None:
    sid, _ = start(client)
    send(client, sid, text="Design a laundry robot")
    claude.release(0)
    wait_for(client, sid, lambda f: "Reply to: Design a laundry robot" in replies(f))

    send(client, sid, text="Make it red")
    claude.release(1)
    wait_for(client, sid, lambda f: "Reply to: Make it red" in replies(f))
    prompt = claude.prompts[1]
    assert prompt.startswith("Make it red")
    assert "Design a laundry robot" in prompt
    assert "Reply to: Design a laundry robot" in prompt
    # The new message isn't repeated as history.
    assert prompt.count("Make it red") == 1


def test_an_answer_after_the_question_was_cut_off_reaches_claude_with_the_question(
    client, claude
) -> None:
    sid, found = start(client)
    send(client, sid, text="Ask me")
    claude.release(0)
    asked = wait_until(client, sid, "waiting_for_answer", found[-1][0])
    assert client.post(f"/api/sessions/{sid}/stop").status_code == 202
    assert [t for _, t, _ in events(client, sid, asked[-1][0])][-1] == "message_end"

    send(client, sid, text="Whichever can do all clothing items")
    claude.release(1)
    wait_for(client, sid, lambda f: "Reply to: Whichever can do all clothing items" in replies(f))
    prompt = claude.prompts[1]
    # Claude knows what it asked and what the options were.
    assert "Ask me" in prompt
    assert "Which?" in prompt and "A" in prompt and "B" in prompt
