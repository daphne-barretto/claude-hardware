"""A browser's own copy of a demo: a session that starts as the demo's hardcoded original
(`app.samples`) left off, made when the browser first opens the demo (`app.demos`), in the real
API and the mock.

The originals are read-only files. The copy has what a saved session of the browser's own would
have had, had it run that conversation: the title, the conversation and every build with its
files, the latest build's design (code and parameters), the board and the next revision, the
standards set, the constraints its messages set (`app.spec`) and the design timeline. The
original is never changed.

The board comes from the sample's latest build, and is matched to the examples in
`demos/<board>/examples.yaml` the same way an example session is set up, so nothing here names a
board:

- the copy is on the board its recorded session used (its latest build's), and is checked
  against it until a newer revision is loaded;
- a build on an example's previous revision gets that example's new revision as
  `next_revision`, exactly as opening the example does (Load next revision is offered);
- a build on an example's new revision is on it already: no next revision;
- a build without a board starts a session without one;
- a recorded run that hadn't loaded its next revision (`Session.next_revision` of the sample)
  offers it, whichever revision its latest build was checked on.

In the real API the builds are copied into the build store (`app.builds`) under new IDs, so they
are served, diffed and exported like any build the session made; the server never loads the CAD
kernel. The mock serves the sample's own files for them.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from app import samples
from app.contracts.common import BoardRef
from app.contracts.session import Build, Message, Parameter, Session
from app.examples import Example, examples

_MESSAGE_NUMBER = re.compile(r"^msg-(\d+)$")


@dataclass
class Continuation:
    """How a session continuing from `sample` starts."""

    sample: samples.Sample
    board: BoardRef | None  # the board the recorded session used (its latest build's)
    next_revision: BoardRef | None  # what Load next revision loads, if anything
    example: Example | None  # the example whose boards these are; None without a board

    @property
    def title(self) -> str:
        return self.sample.session.title

    def latest(self) -> Build:
        session = self.sample.session
        builds = self.sample.builds
        if session.latest_build_id in builds:
            return builds[session.latest_build_id]
        if not builds:
            raise HTTPException(422, f"sample {session.id} has no builds")
        return list(builds.values())[-1]


def _ref(board: Any) -> BoardRef:
    return BoardRef(name=board.name, revision=board.revision)


def continuation(sample_id: str) -> Continuation:
    """Where a copy of the demo `sample_id` starts. 404 for a demo that doesn't exist, 422 for
    one on a board revision no example has."""
    sample = samples.catalogue().get(sample_id)
    if sample is None:
        raise HTTPException(404, f"no demo {sample_id}")
    start = Continuation(sample, None, None, None)
    # The board the recorded session used, never the example's newer one it hadn't loaded.
    board = start.latest().board or sample.session.board
    recorded = sample.session.next_revision
    if recorded is not None:
        # A recorded run that hasn't loaded its next revision yet offers it, whichever board
        # its latest build was checked on.
        for example in examples():
            previous, new = example.boards()
            if _ref(new) == recorded:
                return Continuation(sample, board or _ref(new), _ref(new), example)
    if board is None:
        return start
    for example in examples():
        previous, new = example.boards()
        if _ref(previous) == board:
            return Continuation(sample, board, _ref(new), example)
    for example in examples():
        _, new = example.boards()
        if _ref(new) == board:
            return Continuation(sample, board, None, example)
    raise HTTPException(422, f"no example has board {board.name} {board.revision}")


# --- What is copied ----------------------------------------------------------------------------


def with_urls(build: Build, session_id: str, build_id: str, ids: dict[str, str]) -> Build:
    """A sample build as the new session serves it: under `build_id`, with the session's URLs."""
    data = build.model_dump()
    base = f"/api/sessions/{session_id}/builds/{build_id}/"
    for key in ("mesh_url", "checks_url", "source_url", "board_url", "step_url"):
        if data[key]:
            data[key] = base + data[key].rsplit("/", 1)[-1]
    data["id"] = build_id
    if data.get("restored_from"):
        data["restored_from"] = ids.get(data["restored_from"], data["restored_from"])
    return Build.model_validate(data)


def _renamed(value: Any, ids: dict[str, str]) -> Any:
    """`value` with every string that is one of the sample's build IDs replaced by its new ID."""
    if isinstance(value, str):
        return ids.get(value, value)
    if isinstance(value, list):
        return [_renamed(v, ids) for v in value]
    if isinstance(value, dict):
        return {k: _renamed(v, ids) for k, v in value.items()}
    return value


def copied_events(
    sample: samples.Sample, session_id: str, ids: dict[str, str]
) -> list[tuple[int, Any]]:
    """The sample's events, numbered as they were, with its builds under their new IDs (a
    recorded run's change set and risks name its builds too)."""
    from app.contracts.session import validate_event

    out = []
    for event_id, event in sample.events:
        if event.type == "build_ready":
            build = with_urls(event.build, session_id, ids[event.build.id], ids)
            event = event.model_copy(update={"build": build})
        else:
            event = validate_event(_renamed(event.model_dump(mode="json"), ids))
        out.append((event_id, event))
    return out


def copied_messages(sample: samples.Sample) -> list[Message]:
    return [m.model_copy(deep=True) for m in sample.messages]


def describe_spec(spec: Any, messages: list[Message], locked: dict[str, Any] | None = None) -> bool:
    """Take the constraints from the copied user messages, as each was sent (`app.spec`), so the
    copy's chips are the ones the conversation set. True if it set any."""
    changed = False
    for m in messages:
        if m.role == "user" and m.answer is None and m.text:
            changed = spec.describe(m.text, m.id, locked=locked) or changed
    return changed


def copied_parameters(sample: samples.Sample) -> list[Parameter]:
    return [p.model_copy(deep=True) for p in sample.parameters]


def next_message_number(messages: list[Message]) -> int:
    """The number after the highest `msg-N`, so new message IDs never repeat a copied one."""
    numbers = [int(m.group(1)) for m in (_MESSAGE_NUMBER.match(x.id) for x in messages) if m]
    return max(numbers, default=0) + 1


def design_of(sample: samples.Sample, build_id: str) -> dict[str, Any]:
    """The design a sample build was made from, as `Design.snapshot()` keeps it."""
    source = sample.json(build_id, "source.json")
    params = sample.json(build_id, "design.json")["params"]
    return {"code": source["code"], "params": params, "backend": source["backend"]}


# --- The real API ------------------------------------------------------------------------------


def _store_builds(sample: samples.Sample, store: Any) -> dict[str, str]:
    """Copy the sample's builds into the build store, oldest first, each the child of the one
    before. Returns the new ID of each sample build."""
    from app.agent.services import MESH_FILE, map_file
    from app.builds import new_build_id

    ids: dict[str, str] = {}
    parent = None
    for build_id, build in sample.builds.items():
        new_id = new_build_id()
        ids[build_id] = new_id
        if build.status != "ready":
            continue
        source = sample.json(build_id, "source.json")
        design = sample.json(build_id, "design.json")
        files: dict[str, Any] = {MESH_FILE: sample.file(build_id, "mesh.gltf")}
        for map_type in build.maps:
            doc = {**sample.json(build_id, f"maps/{map_type}.json"), "build_id": new_id}
            files[map_file(map_type)] = json.dumps(doc, separators=(",", ":")).encode()
        store.save(
            code=source["code"],
            parameters=design["params"],
            features=design.get("features", []),
            backend=source["backend"],
            board=build.board,
            checks={**sample.json(build_id, "checks.json"), "build_id": new_id},
            files=files,
            parent=parent,
            build_id=new_id,
            sources=source.get("features", []),
        )
        parent = new_id
    return ids


def _timeline(state: Any, sample: samples.Sample, ids: dict[str, str]) -> None:
    """The design timeline the copy would have had, had it run the conversation: a step of
    Claude's for each reply whose build changed the design, as a run records it."""
    from app.agent.design import Design
    from app.agent.tools import empty_design
    from app.design_history import capture, run_label

    ws, session = state.ws, state.session
    design, latest = ws.design, session.latest_build_id
    replies = {m.id: m for m in sample.messages}
    try:
        ws.design, session.latest_build_id = empty_design(), None
        before = capture(state)
        for _, event in sample.events:
            if event.type != "build_ready" or event.build.status != "ready":
                continue
            ws.design = Design(**design_of(sample, event.build.id))
            session.latest_build_id = ids[event.build.id]
            after = capture(state)
            reply = replies.get(event.message_id or "")
            if reply is not None:
                said = reply.text.strip().split("\n")
                state.history.record(
                    before,
                    after,
                    actor="claude",
                    kind="run",
                    label=run_label([], said[0]),
                    created_at=reply.created_at,
                    message_id=reply.id,
                )
            before = after
    finally:
        ws.design, session.latest_build_id = design, latest


async def create_session(sample_id: str) -> Session:
    """The browser's own copy of the demo `sample_id` in the real API (`app.demos`): a saved
    session like any other, marked as that demo (`Session.demo`)."""
    from app import session_api as api
    from app.agent.design import Design
    from app.agent.events import EventSink
    from app.agent.run import empty_workspace
    from app.agent.tools import Workspace
    from app.owners import current_owner
    from app.session_store import session_store
    from app.standards import load_standards
    from app.telemetry.session import SessionTelemetry

    start = continuation(sample_id)
    sample = start.sample
    standards = await asyncio.to_thread(load_standards)
    latest = start.latest()
    design = Design(**design_of(sample, latest.id))
    if start.example is None:
        ws = empty_workspace(services=api.CONFIG.services(None, standards), standards=standards)
        ws.design = design
    else:
        # The example's workspace, as opening the example sets it up, on the sample's design
        # instead of the generated one (`existing_design`), so nothing is generated or built.
        standards = start.example.with_standards(standards)  # with what the example brings
        previous, board = start.example.boards()
        ws = Workspace(
            previous=previous,
            board=board,
            changes=start.example.changes_text(),
            standards=standards,
            design=design,
            services=api.CONFIG.services(board, standards),
            sink=EventSink(),
        )
        # Checked against the board the recorded session used, until the next one is loaded.
        ws.services = api.CONFIG.services(api.board_named(ws, start.board), standards)
    ids = await asyncio.to_thread(_store_builds, sample, api.CONFIG.store())
    chain = getattr(ws.services, "chain", None)
    if chain is not None and latest.status == "ready":
        chain.parent = ids[latest.id]  # the session's next build is the latest one's child

    sid = api.SESSIONS.next_id()
    session = Session(
        id=sid,
        title=start.title,
        created_at=api.now(),
        board=start.board,
        status="idle",
        latest_build_id=ids[latest.id],
        next_revision=start.next_revision,
        standards=sample.session.standards,
        demo=sample.session.id,
    )
    state = api.SessionState(
        session, ws, telemetry=SessionTelemetry.open(sid), example=start.example
    )
    state.parameters = copied_parameters(sample)
    state.messages = copied_messages(sample)
    state.next_message = next_message_number(state.messages)
    if describe_spec(state.spec, state.messages, ws.design.locked):
        state.apply_spec()
    state.events = copied_events(sample, sid, ids)
    for _, event in state.events:
        if event.type == "build_ready":
            state.builds[event.build.id] = event.build
    for old, new in ids.items():
        if new in state.builds:
            state.designs[new] = design_of(sample, old)
    _timeline(state, sample, ids)
    state.risks = copied_risks(sample, state.events, ids, ws)
    ws.sink = EventSink(listeners=[state.emit])
    if state.telemetry is not None:
        ws.observer = state.telemetry.observe
        state.telemetry.session_start(state)
    state.owner = current_owner.get()
    api.SESSIONS.add(state)
    state.store = session_store()
    for event_id, event in state.events:
        state.store.append_event(sid, event_id, event.model_dump(mode="json"))
    state.save()
    return session


def copied_risks(
    sample: samples.Sample, events: list[tuple[int, Any]], ids: dict[str, str], ws: Any
) -> Any:
    """The risk flags the copy's chat shows (its last `risks_ready`, with the copy's build IDs),
    so their fixes can be accepted and ignored in the copy, as in the session that was recorded.
    None if the run that flagged them isn't the last one."""
    from app.agent.risks import Flags

    risks = [e for _, e in events if e.type == "risks_ready"]
    runs = [m for _, e in events if (m := getattr(e, "message_id", None))]
    if not risks or not risks[-1].risk_flags.flags or runs[-1] != risks[-1].message_id:
        return None
    flags = risks[-1].risk_flags
    original = {new: old for old, new in ids.items()}[flags.build]
    design = design_of(sample, original)
    return Flags.from_contract(ws, flags, {f.id: design for f in flags.flags})


__all__ = [
    "Continuation",
    "continuation",
    "copied_risks",
    "copied_events",
    "copied_messages",
    "copied_parameters",
    "create_session",
    "describe_spec",
    "design_of",
    "next_message_number",
]
