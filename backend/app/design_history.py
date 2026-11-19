"""The session's design timeline (undo and redo): one history of design states, whoever changed
the design.

Every change to the design is one step, with who made it and what it was ("You: push/pull
1 mm", "Claude: move opening_Q4 by (1, 0) mm and 3 more"): a hand edit (a parameter, a modeling
edit, a sketch, the code), a Claude run's applied changes (one step per run), a change-set or
risk review, a restore and an import. Each step keeps the state the change left: the design
(code, parameters, locks and approvals), the standards set, the change set with what was
accepted and rejected, and the build that showed it.

Undo moves back one step and Redo forward (`app.history_api` rebuilds the state); a new change
after an undo drops the steps ahead of it. Builds are never deleted. The history is saved with
the session (`SessionState.saved`), and undoing a Claude step tells Claude on its next run.

Kept free of `app.session_api` (which holds a `History` per session) so both can use it.
"""

from __future__ import annotations

import copy
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.contracts.session import DesignHistory, HistoryStep

ACTORS = {"user": "You", "claude": "Claude"}

# Why a build failed, in plain words, by the sandbox's error kind (`geometry.sandbox`). The
# error's own text is internal (a traceback line, a kernel message) and never shown.
PLAIN_FAILURES = {
    "syntax": "the design's code has a mistake in it",
    "runtime": "the modeler couldn't make the shape with these values",
    "no_result": "the design's code didn't produce any parts",
    "blocked_import": "the design's code uses something builds aren't allowed to use",
    "forbidden": "the design's code does something builds aren't allowed to do",
    "timeout": "it took too long to build",
    "memory": "it needed more memory than a build may use",
    "output": "it made more output than a build may",
    "disk": "it made more files than a build may",
    "crash": "the build stopped unexpectedly",
    "sandbox_unavailable": "the build service wasn't available",
}
DEFAULT_FAILURE = "the modeler couldn't make the shape"


def plain_failure(error: dict | None) -> str:
    """Why a build failed, in words for the user."""
    kind = error.get("kind") if isinstance(error, dict) else None
    return PLAIN_FAILURES.get(kind or "", DEFAULT_FAILURE)


# --- Labels -----------------------------------------------------------------------------------


def _n(value: Any) -> str:
    return f"{value:g}" if isinstance(value, int | float) else str(value)


def _mm(value: Any) -> str:
    return f"{_n(value)} mm"


EDIT_NAMES = {"push_pull": "push/pull"}


def edit_label(edit: Any) -> str:
    """A modeling edit (`Edit`) in a few words: "push/pull 1 mm", "hole ⌀3 mm"."""
    kind = edit.kind
    name = EDIT_NAMES.get(kind, kind)
    if kind == "push_pull":
        return f"{name} {_mm(edit.distance)}"
    if kind in ("hole", "boss"):
        return f"{name} ⌀{_mm(edit.diameter)}" if edit.diameter is not None else f"{name}"
    if kind == "rib":
        return f"{name} {_mm(edit.length)}"
    if kind == "fillet":
        return f"{name} {_mm(edit.radius)}"
    if kind == "chamfer":
        return f"{name} {_mm(edit.distance)}"
    if kind == "shell":
        return f"{name} {_mm(edit.thickness)}"
    if kind == "move":
        return f"move {edit.part}"
    if kind == "mirror":
        return f"mirror {edit.source} across {edit.axis}"
    if kind == "pattern":
        return f"pattern {edit.count} × {edit.source}"
    return name


def sketch_label(sketch: Any) -> str:
    """A sketch in a few words: "sketch a circle cut"."""
    return f"sketch a {sketch.shape} {sketch.operation}"


def parameter_label(name: str, value: Any, unit: str | None) -> str:
    return f"set {name} to {_n(value)}{f' {unit}' if unit else ''}"


def review_label(accept: list[str], reject: list[str], what: str = "change") -> str:
    """A review in a few words: "accepted 2 changes", "accepted 1 and rejected 1 change"."""

    def count(n: int) -> str:
        return f"{n} {what}{'' if n == 1 else 's'}"

    if accept and reject:
        return f"accepted {len(accept)} and rejected {count(len(reject))}"
    if accept:
        return f"accepted {count(len(accept))}"
    return f"rejected {count(len(reject))}"


def run_label(titles: list[str], summary: str | None = None) -> str:
    """A Claude run's step: its first change and how many more, else its summary."""
    if titles:
        first = titles[0][:1].lower() + titles[0][1:]
        return first if len(titles) == 1 else f"{first} and {len(titles) - 1} more"
    text = (summary or "").strip().split("\n")[0].rstrip(".")
    if text:
        text = text[:1].lower() + text[1:]
        return text if len(text) <= 80 else text[:79].rstrip() + "…"
    return "changed the design"


# --- States -----------------------------------------------------------------------------------


def capture(state: Any) -> dict[str, Any]:
    """The session's design state now (a `SessionState`): what a step keeps."""
    design = state.ws.design
    latest = state.session.latest_build_id
    return {
        "design": {
            "code": design.code,
            "params": copy.deepcopy(design.params),
            "backend": design.backend,
            "store": design.store.to_json(),
            "approvals": {k: a.model_dump(mode="json") for k, a in design.approvals.items()},
        },
        "standards": state.session.standards,
        # Copied: a review changes the proposal's statuses in place.
        "proposal": copy.deepcopy(state.proposal.to_json()) if state.proposal is not None else None,
        "build_id": latest,
        "import": latest if latest in state.imports else None,
    }


def _key(saved: dict[str, Any]) -> str:
    """What makes two states different: everything but the build that showed it."""
    data = {k: v for k, v in saved.items() if k != "build_id"}
    design = data.get("design")
    if isinstance(design, dict) and "store" in design:
        # A lock's record carries when it was set; the value and whether it is locked count.
        store = sorted(
            (p.get("name"), json.dumps(p.get("value"), sort_keys=True), p.get("locked"))
            for p in design["store"]
        )
        data["design"] = {**design, "store": store}
    return json.dumps(data, sort_keys=True, default=str)


def same(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return _key(a) == _key(b)


@dataclass
class History:
    """The timeline: the `start` state, then each step with the state it left. `position` steps
    are applied (0: at the start); the ones after it are the redo branch."""

    start: dict[str, Any] | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    position: int = 0
    next_id: int = 1
    # Claude steps the user undid, not yet told to Claude: {"id", "label"}.
    undone: list[dict[str, str]] = field(default_factory=list)

    def state_at(self, index: int) -> dict[str, Any] | None:
        if index < 0 or index > len(self.steps):
            return None
        return self.start if index == 0 else self.steps[index - 1]["state"]

    def current(self) -> dict[str, Any] | None:
        return self.state_at(self.position)

    def can_undo(self) -> bool:
        """There is a step to take back, to a state that had a design (a session's empty start
        has none to rebuild)."""
        before = self.state_at(self.position - 1) if self.position > 0 else None
        return before is not None and before.get("build_id") is not None

    def can_redo(self) -> bool:
        return self.position < len(self.steps)

    def record(
        self,
        before: dict[str, Any],
        after: dict[str, Any],
        *,
        actor: str,
        kind: str,
        label: str,
        created_at: datetime,
        message_id: str | None = None,
    ) -> dict[str, Any] | None:
        """A change from state `before` to `after`, as a new step: it drops the redo branch.
        Nothing is recorded when nothing changed."""
        if same(before, after):
            return None
        if self.start is None:
            self.start = before
        elif not same(self.current() or before, before):
            # Changed outside the timeline since the last step (an unlock, a spec chip): the
            # current state takes it, so undo comes back to the design as it really was.
            if self.position == 0:
                self.start = before
            else:
                self.steps[self.position - 1]["state"] = before
        del self.steps[self.position :]
        step = {
            # Random, as every ID a browser sees (`docs/security.md`): not found by counting.
            "id": f"step-{secrets.token_hex(8)}",
            "actor": actor,
            "kind": kind,
            "label": label,
            "created_at": created_at.isoformat(),
            "build_id": after.get("build_id"),
            "message_id": message_id,
            "state": after,
        }
        self.next_id += 1
        self.steps.append(step)
        self.position = len(self.steps)
        return step

    def moved(self, direction: str, step: dict[str, Any]) -> None:
        """An undo or redo of `step` took place: move along the timeline, and keep track of the
        Claude steps that are undone."""
        if direction == "undo":
            self.position -= 1
            if step["actor"] == "claude":
                self.undone.append({"id": step["id"], "label": step["label"]})
        else:
            self.position += 1
            self.undone = [u for u in self.undone if u["id"] != step["id"]]

    def take_note(self) -> str | None:
        """What Claude is told on its next run about its changes the user undid, once."""
        undone, self.undone = self.undone, []
        if not undone:
            return None
        lines = "\n".join(f"- {u['label']}" for u in undone)
        return (
            "The user undid these changes of yours (Undo), so the design is back to how it was "
            f"before them:\n{lines}\nTreat them like rejected changes: don't make them again "
            "unless the user asks for them."
        )

    # --- The contract and saving ------------------------------------------------------------

    @staticmethod
    def step_model(step: dict[str, Any]) -> HistoryStep:
        return HistoryStep(
            id=step["id"],
            actor=step["actor"],
            kind=step["kind"],
            label=step["label"],
            text=f"{ACTORS[step['actor']]}: {step['label']}",
            created_at=step["created_at"],
            build_id=step["build_id"],
            message_id=step["message_id"],
        )

    def model(self) -> DesignHistory:
        steps = [self.step_model(s) for s in self.steps]
        return DesignHistory(
            steps=steps,
            position=self.position,
            undo=steps[self.position - 1] if self.can_undo() else None,
            redo=steps[self.position] if self.can_redo() else None,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "steps": self.steps,
            "position": self.position,
            "next_id": self.next_id,
            "undone": self.undone,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> History:
        if not data:
            return cls()
        return cls(
            start=data["start"],
            steps=data["steps"],
            position=data["position"],
            next_id=data["next_id"],
            undone=data.get("undone", []),
        )


__all__ = [
    "History",
    "capture",
    "edit_label",
    "parameter_label",
    "plain_failure",
    "review_label",
    "run_label",
    "same",
    "sketch_label",
]
