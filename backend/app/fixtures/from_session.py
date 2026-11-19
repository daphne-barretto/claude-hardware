"""Turn a saved session into a sample in `demos/sessions/` (`app.samples`): a recorded run.

    cd backend && PYTHONPATH=$PWD .venv/bin/python -m app.fixtures.from_session \\
        <session id> <sample id> [--title TITLE] [--turns N] [--data-dir DIR]

The session is read from `DATA_DIR` (or `--data-dir`), as the session API saved it: its state,
its events and its builds in the build store. The sample gets everything a scripted sample has,
from the run itself: the conversation (messages and events, with the constraints, change set and
risks the run streamed), every build with its mesh, checks, source, design and analysis maps,
the diffs between every two builds, and the parameters. Nothing is rebuilt: the builds are the
ones the run made.

`--turns N` keeps the first N user turns and what came before them (0: the session as it was
opened, before the first message), so one recorded session can give a sample for each step of a
story. The sample's latest build, parameters and next revision are those at that point.

What the sample keeps is sample data only. The owner, the attachments, exports and the share
links are left out; the session and build IDs are replaced by the sample's (`build-1`, ...)
wherever they appear, Claude's text included; a sample with attachments is refused. The output
is checked for the values of secret environment variables and for local paths before it is
written.

The sample is marked as recorded in `demos/sessions/index.json`, with the model, effort and
mode of its messages and a digest of its files, so `python -m app.fixtures.samples --check`
checks that it was not changed since instead of asking to regenerate it from the generators.
`demos/README.md` says how to record the demos again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from app.contracts.common import REPO_ROOT

SESSION_FILE = "session.json"
INDEX = "index.json"


class Refused(Exception):
    """The session can't become a sample."""


# --- Reading the saved session --------------------------------------------------------------------


def load(session_id: str, data: Path) -> tuple[dict, list[tuple[int, dict]]]:
    """The saved state and the events of `session_id` in the data directory `data`."""
    from app.session_store import SqliteStore

    if not (data / "sessions.db").is_file():
        raise Refused(f"no sessions.db in {data}")
    store = SqliteStore(data)
    try:
        state = store.load(session_id)
        if state is None:
            raise Refused(f"no session {session_id} in {data}")
        return state, store.events(session_id)
    finally:
        store.close()


def cut(
    state: dict, events: list[tuple[int, dict]], turns: int | None
) -> tuple[list[dict], list[tuple[int, dict]]]:
    """The messages and events of the first `turns` user turns (None: all of them)."""
    messages = state["messages"]
    if turns is None:
        return messages, events
    users = [i for i, (_, e) in enumerate(events) if e["type"] == "user_message"]
    if turns < len(users):
        events = events[: users[turns]]
    user_ids = [m["id"] for m in messages if m["role"] == "user"]
    if turns < len(user_ids):
        stop = next(i for i, m in enumerate(messages) if m["id"] == user_ids[turns])
        messages = messages[:stop]
    return messages, events


# --- Writing the sample ---------------------------------------------------------------------------


def _json(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


class Renamer:
    """Replaces the session's and its builds' IDs with the sample's, in any text."""

    def __init__(self, session_id: str, sample_id: str):
        self.pairs = [(session_id, sample_id)]
        self.builds: dict[str, str] = {}

    def build(self, build_id: str) -> str:
        if build_id not in self.builds:
            new = f"build-{len(self.builds) + 1}"
            self.builds[build_id] = new
            self.pairs.append((build_id, new))
        return self.builds[build_id]

    def text(self, text: str) -> str:
        for old, new in self.pairs:
            text = text.replace(old, new)
        return text

    def data(self, value: Any) -> Any:
        return json.loads(self.text(json.dumps(value, ensure_ascii=False)))


def _next_revision(state: dict, messages: list[dict]) -> dict | None:
    """The board revision Load next revision would load at this point of the session: the
    example's new revision until a message loads it."""
    from app.examples import example_for_board
    from app.revisions import revision_message

    board = state["session"].get("board")
    if board is None:
        return None
    try:
        example = example_for_board(board["name"], board["revision"])
    except KeyError:
        return None
    previous, new = example.boards()
    loaded = revision_message(previous, new)
    if any(m["role"] == "user" and m["text"] == loaded for m in messages):
        return None
    return {"name": new.name, "revision": new.revision}


def _parameters(state: dict, params: dict[str, Any]) -> list[dict]:
    """The session's parameters with the values of the design `params`: those it lists that the
    design has, then the design's other top-level values (as a session lists a design written
    in it). Where each is measured is worked out when it is served (`app.dimensions`)."""
    from app.agent.design import get_path, has_path
    from app.contracts.session import Parameter

    out: list[dict] = []
    for saved in state["parameters"]:
        if not has_path(params, saved["name"]):
            continue
        value = get_path(params, saved["name"])
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            continue
        p = Parameter.model_validate({**saved, "dimension": None, "drag": None})
        p.value = float(value) if isinstance(value, int) else value
        p.locked = saved.get("locked", False)
        out.append(p.model_dump(mode="json", exclude={"dimension", "drag"}))
    named = {p["name"] for p in out}
    if state["session"].get("board") is None:
        for name, value in params.items():
            if name in named or isinstance(value, bool) or not isinstance(value, int | float | str):
                continue
            try:
                p = Parameter(name=name, value=value, locked=False)
            except ValueError:
                continue
            out.append(p.model_dump(mode="json", exclude={"dimension", "drag"}))
    return out


def sample_files(
    session_id: str,
    sample_id: str,
    data: Path,
    *,
    title: str | None = None,
    turns: int | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """The sample's files (relative path -> text) and what `index.json` records about it."""
    from app.agent.services import MESH_FILE, map_file
    from app.builds import BuildStore, diff_builds
    from app.contracts.session import Build, Message, Session, validate_event
    from app.session_api import stored_source

    state, events = load(session_id, data)
    if state["session"].get("status") != "idle":
        raise Refused(f"session {session_id} is {state['session']['status']}, not idle")
    messages, events = cut(state, events, turns)
    if any(m.get("attachments") for m in messages):
        raise Refused("a sample can't keep attachments")
    if events and events[-1][1]["type"] not in ("message_end", "build_ready"):
        raise Refused(f"the cut ends in the middle of a turn ({events[-1][1]['type']})")
    title = title or state["session"]["title"]
    rename = Renamer(session_id, sample_id)
    store = BuildStore(data / "builds")
    base = f"/api/sessions/{sample_id}/builds/"

    files: dict[str, str] = {}
    records = {}
    kept: list[tuple[int, dict]] = []
    for event_id, event in events:
        if event["type"] == "build_ready":
            real = event["build"]["id"]
            bid = rename.build(real)
            build = dict(event["build"])
            for key in ("mesh_url", "checks_url", "source_url", "board_url"):
                build[key] = f"{base}{bid}/{key.removesuffix('_url')}"
            build["step_url"] = None  # a sample serves no STEP; exports rebuild it
            event = {**event, "build": build}
            if build["status"] == "ready" and real not in records:
                records[real] = store.get(real)
        elif event["type"] == "title":
            event = {**event, "title": title}
        kept.append((event_id, event))
    # Rename every build first, so the text of any event names the sample's IDs.
    kept = [(i, rename.data(e)) for i, e in kept]
    messages = rename.data(messages)

    for real, record in records.items():
        bid = rename.builds[real]
        root = f"builds/{bid}/"
        build_doc = next(
            e["build"] for _, e in kept if e["type"] == "build_ready" and e["build"]["id"] == bid
        )
        Build.model_validate(build_doc)
        files[root + "build.json"] = _json(build_doc)
        files[root + "mesh.gltf"] = rename.text(store.file(real, MESH_FILE).read_text())
        checks = {**(record.checks or {}), "build_id": real}
        files[root + "checks.json"] = _json(rename.data(checks))
        files[root + "source.json"] = _json(rename.data(stored_source(record)))
        files[root + "design.json"] = _json(
            {"params": record.parameters, "features": record.features}
        )
        for map_type in build_doc["maps"]:
            doc = json.loads(store.file(real, map_file(map_type)).read_text())
            files[root + f"maps/{map_type}.json"] = _json(rename.data({**doc, "build_id": real}))
    for old in records.values():
        for new in records.values():
            if old.id != new.id:
                name = f"builds/{rename.builds[new.id]}/diffs/{rename.builds[old.id]}.json"
                files[name] = _json(rename.data(diff_builds(old, new)))

    ready = [rename.builds[r] for r in records]
    if not ready:
        raise Refused("the sample would have no ready build")
    latest_real = next(r for r, b in rename.builds.items() if b == ready[-1])
    standards = (state.get("standards") or {}).get("id")
    session = Session(
        id=sample_id,
        title=title,
        created_at=state["session"]["created_at"],
        # The board of the latest build, as the samples made by the generators have it: the
        # session's own board is the one a revision would load.
        board=records[latest_real].board or state["session"].get("board"),
        status="idle",
        latest_build_id=ready[-1],
        next_revision=_next_revision(state, messages),
        standards=standards,
        sample=True,
    ).model_dump(mode="json", exclude={"demo", "shared", "title_locked"})
    for m in messages:
        Message.model_validate(m)
    numbered = [{"id": n, "event": e} for n, (_, e) in enumerate(kept, start=1)]
    for e in numbered:
        validate_event(e["event"])
    files[SESSION_FILE] = _json(
        {
            "session": session,
            "messages": messages,
            "events": numbered,
            "parameters": _parameters(state, records[latest_real].parameters),
        }
    )
    users = [m for m in messages if m["role"] == "user"]
    recorded = {
        "models": sorted({m["model"] for m in users if m.get("model")}),
        "efforts": sorted({m["effort"] for m in users if m.get("effort")}),
        "modes": sorted({m["mode"] for m in users if m.get("mode")}),
        "turns": len(users),
        "builds": len(ready),
        "recorded_at": state["session"]["created_at"],
    }
    check_private(files)
    return files, recorded


# --- Nothing private ------------------------------------------------------------------------------

_SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|COOKIE", re.IGNORECASE)
_PRIVATE = [
    re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),  # an Anthropic key, set or not
    re.compile(r"(?<![A-Za-z])(/Users/|/home/|/private/|/tmp/|/var/folders/)[^\s\"']+"),
    re.compile(r"hardware_owner"),
]


def check_private(files: dict[str, str]) -> None:
    """Refuse output that holds a secret environment variable's value, a key, an owner cookie
    or a local path."""
    secrets = [
        v.strip() for k, v in os.environ.items() if _SECRET_ENV.search(k) and len(v.strip()) >= 8
    ]
    for name, text in files.items():
        if any(s in text for s in secrets):
            raise Refused(f"{name} holds the value of a secret environment variable")
        for pattern in _PRIVATE:
            found = pattern.search(text)
            if found:
                raise Refused(f"{name} holds private data: {found.group(0)[:40]!r}")


# --- The index ------------------------------------------------------------------------------------


def files_digest(root: Path) -> str:
    """A digest of a sample folder's files, so `--check` sees one changed after recording."""
    h = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        h.update(path.relative_to(root).as_posix().encode() + b"\0")
        h.update(path.read_bytes() + b"\0")
    return h.hexdigest()


def write(files: dict[str, str], sample_id: str, recorded: dict[str, Any], root: Path) -> None:
    """Write the sample's folder and record it in `index.json` (in its place there, or last)."""
    folder = root / sample_id
    shutil.rmtree(folder, ignore_errors=True)
    for name, text in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    index_path = root / INDEX
    index = json.loads(index_path.read_text()) if index_path.is_file() else {}
    index.setdefault("inputs", None)
    order = index.setdefault("samples", [])
    if sample_id not in order:
        order.append(sample_id)
    entries = index.setdefault("recorded", {})
    entries[sample_id] = {**recorded, "files": files_digest(folder)}
    index["recorded"] = {sid: entries[sid] for sid in order if sid in entries}
    index_path.write_text(_json(index))


def main(argv: list[str] | None = None) -> int:
    from app.fixtures.samples import SAMPLES_DIR
    from app.runtime import data_dir

    parser = argparse.ArgumentParser(prog="python -m app.fixtures.from_session")
    parser.add_argument("session_id")
    parser.add_argument("sample_id", help="the sample's ID, e.g. sample-open-box")
    parser.add_argument("--title", help="the sample's title (default: the session's)")
    parser.add_argument(
        "--turns", type=int, help="keep the first N user turns (0: the session as opened)"
    )
    parser.add_argument("--data-dir", type=Path, help="the data directory (default: DATA_DIR)")
    parser.add_argument("--out", type=Path, default=SAMPLES_DIR, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"sample-[a-z0-9-]+", args.sample_id):
        parser.error("the sample ID is sample-<kebab-case>")
    try:
        files, recorded = sample_files(
            args.session_id,
            args.sample_id,
            (args.data_dir or data_dir()).resolve(),
            title=args.title,
            turns=args.turns,
        )
    except Refused as error:
        print(f"can't make the sample: {error}", file=sys.stderr)
        return 1
    write(files, args.sample_id, recorded, args.out)
    shown = args.out.relative_to(REPO_ROOT) if args.out.is_relative_to(REPO_ROOT) else args.out
    print(f"wrote {len(files)} files to {shown / args.sample_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
