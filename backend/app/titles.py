"""A session's title from its first request, like a chat's: short, in the user's own words.

Only the text the user typed is used: its first line, without leading pleasantries ("please",
"can you"), cut at a word to `MAX_CHARS`, with no dangling small word or end punctuation.
"""

from __future__ import annotations

import re

DEFAULT_TITLE = "New design"
MAX_CHARS = 40

_LEADING = re.compile(
    r"^(?:please|pls|hi|hello|hey|can you|could you|would you|will you|i want(?: you)? to|"
    r"i want|i'd like(?: you)? to|i would like(?: you)? to|i'd like|i need|help me)\b[\s,]*",
    re.I,
)
_DANGLING = {"a", "an", "the", "with", "and", "or", "of", "for", "to", "in", "on", "at", "by"}


def title_for(text: str) -> str | None:
    """The title for a session whose first request is `text`; None if it has no words."""
    line = next((ln for ln in text.strip().splitlines() if ln.strip()), "")
    line = " ".join(line.split())
    while True:
        shorter = _LEADING.sub("", line)
        if shorter == line:
            break
        line = shorter
    words = line.split()
    out: list[str] = []
    for word in words:
        if len(" ".join([*out, word])) > MAX_CHARS:
            break
        out.append(word)
    if not out and words:
        out = [words[0][:MAX_CHARS]]
    while out and out[-1].lower().strip(".,;:!?") in _DANGLING and len(out) > 1:
        out.pop()
    title = " ".join(out).rstrip(".,;:!?-– ")
    if not any(ch.isalnum() for ch in title):
        return None
    return title[0].upper() + title[1:]


MAX_TITLE = 60  # a session name, as the user or Claude gives it

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_TAG = re.compile(r"<[^>]*>")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MARKUP = re.compile(r"[*_`#>|~\[\]<>]")


def clean_title(text: str) -> str | None:
    """A session name as given (by the user or Claude), made safe to show: one line, no
    control characters, HTML or markdown, at most `MAX_TITLE` characters, cut at a word. None
    if nothing is left."""
    text = _CONTROL.sub(" ", text or "")
    text = _TAG.sub(" ", text)
    text = _LINK.sub(r"\1", text)
    text = _MARKUP.sub(" ", text)
    text = " ".join(text.split())
    if len(text) > MAX_TITLE:
        cut = text[: MAX_TITLE + 1].rsplit(" ", 1)[0]
        text = (cut if cut and len(cut) <= MAX_TITLE else text[:MAX_TITLE]).rstrip()
    return text if any(ch.isalnum() for ch in text) else None


__all__ = ["DEFAULT_TITLE", "MAX_CHARS", "MAX_TITLE", "clean_title", "title_for"]
