"""Generate the sample sessions in `demos/sessions/` from `demos/<board>/` and `standards.yaml`.

A sample session is a read-only session with a short recorded conversation and pre-made builds,
served by the real session API and the mock (`app.samples`), so the canvas can be opened and
tested without Claude. Every build is made by the real pipeline: the default geometry backend's
enclosure generator, tessellated with stable face and edge IDs, checked by the check runner and
analyzed, as a session's build is.

The samples follow the revisions named in `demos/<board>/CHANGES.md`:

1. the existing case on the older revision, the board it was designed for (every check passes);
2. the newer revision arrives: the same case checked on it (fails where `CHANGES.md` says);
3. the case updated for the newer revision: the generator run with its parameters.

Titles and conversation text name only what `demos/<board>/` and `standards.yaml` contain.

The other samples show the range of the tool: designs made from a plain request with no board
("make a box ..."). Each is a folder in `demos/requests/`: `request.json` (the title,
the request, what the reply says before the build, and the parameters) and `design.py`, the
generator code a run writes for it. It goes through the same pipeline, checked against the
standards that apply to any design.

    cd backend && .venv/bin/python -m app.fixtures.samples          # write demos/sessions/
    cd backend && .venv/bin/python -m app.fixtures.samples --check  # fail if it is out of date

The real pipeline takes minutes, so `--check` compares a digest of the inputs (the board files,
`CHANGES.md`, `standards.yaml`, the enclosure generator, the sample requests and this script)
with the one recorded
in `index.json` instead of building everything again.

A sample can instead be a recorded run: a real session turned into a sample by
`python -m app.fixtures.from_session` (`demos/README.md`). `index.json` lists those under
`recorded`, with a digest of their files. This script never overwrites one: it writes only the
samples that are not recorded, and `--check` checks a recorded sample against its own digest
(it was not changed since it was recorded) instead of the inputs, since no generator made it.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.boards import BOARDS_DIR, Board
from app.boards.changes import Change, parse_changes_md
from app.contracts.common import DEMOS_DIR, REPO_ROOT
from app.fixtures.generate import boards
from app.standards import StandardsFile

SAMPLES_DIR = DEMOS_DIR / "sessions"
REQUESTS_DIR = DEMOS_DIR / "requests"
INDEX = "index.json"
SESSION_FILE = "session.json"
# Fixed so the samples are reproducible. Real sessions list before samples whatever their dates.
CREATED_AT = "2026-01-01T00:00:00Z"
EXAMPLE = "enclosure"
MESH_FILE = "mesh.gltf"
CHECKS_FILE = "checks.json"
SOURCE_FILE = "source.json"
DESIGN_FILE = "design.json"
BUILD_FILE = "build.json"


# --- Inputs ------------------------------------------------------------------------------------


def input_files() -> list[Path]:
    """Everything a sample is made from, for the digest that says whether they are current."""
    from geometry import DEFAULT_BACKEND, get_backend

    doc = parse_changes_md((BOARDS_DIR / "CHANGES.md").read_text())
    engine = get_backend(DEFAULT_BACKEND)
    return [
        BOARDS_DIR / "CHANGES.md",
        BOARDS_DIR / doc.old_file,
        BOARDS_DIR / doc.new_file,
        REPO_ROOT / "standards.yaml",
        engine.directory / "examples" / f"{EXAMPLE}.py",
        *sorted(p for p in REQUESTS_DIR.rglob("*") if p.is_file()),
        Path(__file__),
    ]


def inputs_digest() -> str:
    h = hashlib.sha256()
    for path in input_files():
        h.update(path.relative_to(REPO_ROOT).as_posix().encode() + b"\0")
        h.update(path.read_bytes() + b"\0")
    return h.hexdigest()


# --- Builds ------------------------------------------------------------------------------------


@dataclass
class Made:
    """One build made by the real pipeline, with a placeholder build ID in its documents."""

    board: Board | None  # checked against; None for a design without a board
    params: dict[str, Any]
    mesh: Any  # geometry.tessellate.Tessellation
    report: dict
    maps: dict[str, dict]
    source: dict
    features: list[dict]


def make_design(
    code: str,
    params: dict[str, Any],
    board: Board | None,
    standards: StandardsFile,
    filename: str,
    result=None,
    previous=None,
) -> tuple[Made, Any]:
    """Build generator `code` with `params`, mesh it, check it on `board` (None: the standards
    that apply to any design) and analyze it. `result` reuses a build of the same code and
    parameters; `previous` is a tessellation whose face IDs are kept where the faces match.
    Returns the build and its `geometry.BuildResult`."""
    from app.contracts.mesh import validate_mesh
    from geometry import DEFAULT_BACKEND, get_backend
    from geometry.analysis import analysis_maps, analyze
    from geometry.diff import feature_summary
    from geometry.report import check_report
    from geometry.tessellate import tessellate

    engine = get_backend(DEFAULT_BACKEND)
    if result is None:
        result = engine.build(code, params)
    mesh = tessellate(result, previous=previous)
    report = check_report(board, standards, result, "build", mesh)
    maps = analysis_maps(
        analyze(result, standards), standards, validate_mesh(mesh.to_gltf()), "build"
    )
    source = {
        "build_id": "build",
        "backend": engine.name,
        "language": "python",
        "filename": filename,
        "code": code,
        "features": result.feature_sources(),
    }
    return Made(board, params, mesh, report, maps, source, feature_summary(result)), result


def make(
    design_board: Board, board: Board, standards: StandardsFile, result=None, previous=None
) -> tuple[Made, Any]:
    """The enclosure generator with `design_board`'s parameters, checked on `board`."""
    from geometry import DEFAULT_BACKEND, enclosure_parameters, get_backend

    code = get_backend(DEFAULT_BACKEND).example(EXAMPLE)
    params = enclosure_parameters(design_board, standards)
    return make_design(code, params, board, standards, f"{EXAMPLE}.py", result, previous)


def parameters_of(params: dict[str, Any]) -> list[dict]:
    """The design's top-level numbers and texts, as the session API lists a design's
    parameters."""
    return [
        {"name": name, "value": value, "locked": False}
        for name, value in params.items()
        if isinstance(value, int | float | str) and not isinstance(value, bool)
    ]


# --- Sessions ----------------------------------------------------------------------------------


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def revision(board: Board) -> str:
    return f"v{board.revision}"


@dataclass
class SampleSpec:
    title: str
    # Builds before the conversation: shown with no message, as a session's first build.
    before: list[Made]
    prompt: str
    # The reply: paragraphs of text and builds, in order.
    reply: list[str | Made]
    builds: list[Made] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.builds = self.before + [r for r in self.reply if isinstance(r, Made)]


def check_lines(report: dict) -> str:
    s = report["summary"]
    text = f"{s['pass']} checks pass, {s['fail']} fail and {s['untested']} are untested"
    if s.get("not_applicable"):
        text += f" ({s['not_applicable']} don't apply to this design)"
    failed = [r for r in report["results"] if r["status"] == "fail"]
    if not failed:
        return text + "."
    return text + ":" + "".join(f"\n- {r['summary']}" for r in failed)


def board_ref(board: Board | None) -> dict | None:
    return None if board is None else {"name": board.name, "revision": board.revision}


def change_lines(changes: list[Change]) -> str:
    return "".join(f"\n- {c.component} ({c.role}): {c.change}" for c in changes)


def specs(old: Board, new: Board, changes: list[Change], made: dict[str, Made]) -> list[SampleSpec]:
    case_old, case_new, updated = made["case_old"], made["case_new"], made["updated"]
    name = old.name
    return [
        SampleSpec(
            title=f"{name} case on {revision(old)}",
            before=[],
            prompt=f"Open the existing {name} case on revision {old.revision}, the board it "
            "was made for, and check it.",
            reply=[
                f"This is the existing case on {name} {old.revision}, the board it was "
                "designed for.",
                case_old,
                check_lines(case_old.report),
            ],
        ),
        SampleSpec(
            title=f"{name} {revision(new)} arrives",
            before=[case_old],
            prompt=f"Here is revision {new.revision}. Check the current case against it.",
            reply=[
                f"Revision {new.revision} changes {len(changes)} things that affect the case:"
                + change_lines(changes),
                case_new,
                f"The case is unchanged, checked on {new.revision}: "
                + check_lines(case_new.report),
            ],
        ),
        SampleSpec(
            title=f"{name} case updated for {revision(new)}",
            before=[case_new],
            prompt=f"Update the case for revision {new.revision}: move and add the openings "
            "it needs.",
            reply=[
                f"I updated the case for {new.revision}, one change for each difference that "
                "affects it:" + change_lines(changes),
                updated,
                check_lines(updated.report),
            ],
        ),
    ]


def request_params(request: dict, standards: StandardsFile) -> dict[str, Any]:
    """The request's parameters, plus the ones it takes from the team standards (`standards`:
    parameter -> the standard's value, `plus` a margin over a minimum)."""
    params = dict(request["params"])
    for name, use in request.get("standards", {}).items():
        value = standards.get(use["standard"]).value
        params[name] = float(value) + use["plus"] if "plus" in use else value
    return params


def request_specs(standards: StandardsFile, root: Path = REQUESTS_DIR) -> list[SampleSpec]:
    """A sample per folder of `demos/requests/`: a design made from a plain request,
    with no board."""
    out = []
    for folder in sorted(p for p in root.iterdir() if (p / "request.json").is_file()):
        request = json.loads((folder / "request.json").read_text())
        code = (folder / "design.py").read_text()
        made, _ = make_design(
            code, request_params(request, standards), None, standards, "design.py"
        )
        out.append(
            SampleSpec(
                title=request["title"],
                before=[],
                prompt=request["prompt"],
                reply=[request["reply"], made, check_lines(made.report)],
            )
        )
    return out


def _at(n: int) -> str:
    """The n-th fixed timestamp of a sample, a minute apart."""
    return f"2026-01-01T00:{n:02d}:00Z"


def sample_files(spec: SampleSpec, standards: StandardsFile) -> tuple[str, dict[str, str]]:
    """A sample session's ID and its files (relative path -> text)."""
    sid = f"sample-{slug(spec.title)}"
    files: dict[str, str] = {}
    builds: dict[int, dict] = {}  # id(Made) -> Build resource
    clock = iter(range(60))

    def build(m: Made) -> dict:
        if id(m) in builds:
            return builds[id(m)]
        bid = f"build-{len(builds) + 1}"
        url = f"/api/sessions/{sid}/builds/{bid}/"
        report = {**copy.deepcopy(m.report), "build_id": bid}
        maps = {t: {**copy.deepcopy(doc), "build_id": bid} for t, doc in m.maps.items()}
        resource = {
            "id": bid,
            "created_at": _at(next(clock)),
            "board": board_ref(m.board),
            "status": "ready",
            "checks": report["summary"],
            "maps": sorted(maps),
            # Absolute, like a real session's builds: whoever reads a build (the build list, one
            # build, a `build_ready` event) can fetch its files without knowing the session.
            "mesh_url": f"{url}mesh",
            "checks_url": f"{url}checks",
            "source_url": f"{url}source",
            "board_url": f"{url}board",
            "step_url": None,
            "restored_from": None,
            "failure": None,
        }
        root = f"builds/{bid}/"
        files[root + BUILD_FILE] = _json(resource)
        files[root + MESH_FILE] = json.dumps(m.mesh.to_gltf(), separators=(",", ":")) + "\n"
        files[root + CHECKS_FILE] = _json(report)
        files[root + SOURCE_FILE] = _json({**m.source, "build_id": bid})
        files[root + DESIGN_FILE] = _json({"params": m.params, "features": m.features})
        for t, doc in maps.items():
            files[root + f"maps/{t}.json"] = _json(doc)
        builds[id(m)] = resource
        return resource

    events: list[dict] = []

    def emit(event: dict) -> None:
        events.append({"id": len(events) + 1, "event": event})

    for m in spec.before:
        emit({"type": "build_ready", "message_id": None, "build": build(m)})
    user = {"id": "msg-1", "role": "user", "text": spec.prompt, "created_at": _at(next(clock))}
    reply_id = "msg-2"
    emit({"type": "message_start", "message_id": reply_id, "role": "assistant"})
    texts: list[str] = []
    for part in spec.reply:
        if isinstance(part, Made):
            emit({"type": "build_ready", "message_id": reply_id, "build": build(part)})
        else:
            # The chat starts a new block after a build card, so the text needs no separator.
            texts.append(part)
            emit({"type": "text", "message_id": reply_id, "delta": part})
    emit({"type": "message_end", "message_id": reply_id})
    assistant = {
        "id": reply_id,
        "role": "assistant",
        "text": "\n\n".join(texts),
        "created_at": _at(next(clock)),
    }
    # Every diff between two of the sample's builds, made here so the server never needs the
    # geometry code that measures features.
    from geometry.diff import diff_features, diff_parameters

    for old_m in spec.builds:
        for new_m in spec.builds:
            a, b = builds[id(old_m)]["id"], builds[id(new_m)]["id"]
            if a == b:
                continue
            files[f"builds/{b}/diffs/{a}.json"] = _json(
                {
                    "old": a,
                    "new": b,
                    "parameters": diff_parameters(old_m.params, new_m.params),
                    "features": diff_features(old_m.features, new_m.features),
                }
            )
    latest = spec.builds[-1]
    session = {
        "id": sid,
        "title": spec.title,
        "created_at": CREATED_AT,
        "board": board_ref(latest.board),
        "status": "idle",
        "latest_build_id": builds[id(latest)]["id"],
        "next_revision": None,
        "standards": standards.id,
        "sample": True,
    }
    files[SESSION_FILE] = _json(
        {
            "session": session,
            "messages": [user, assistant],
            "events": events,
            "parameters": parameters_of(latest.params),
        }
    )
    return sid, files


def recorded(root: Path = SAMPLES_DIR) -> dict[str, dict]:
    """The recorded samples (`app.fixtures.from_session`) in `index.json`, by ID."""
    index = root / INDEX
    return json.loads(index.read_text()).get("recorded", {}) if index.is_file() else {}


def generate(
    standards: StandardsFile | None = None, keep: dict[str, dict] | None = None
) -> dict[str, str]:
    """Every file of `demos/sessions/` this script makes: relative path -> text. `keep`: the
    recorded samples, which it leaves alone; the index lists them first, then the rest."""
    keep = keep or {}
    if keep and all(f"sample-{slug(t)}" in keep for t in _all_titles()):
        return {INDEX: _json({"inputs": None, "samples": list(keep), "recorded": keep})}
    from app.examples import example_standards

    standards = standards or example_standards()  # the board example's, with what it brings
    old, new, changes = boards()
    case_old, result_old = make(old, old, standards)
    # The same design on the newer board: the same shapes and faces, checked on another board.
    case_new, _ = make(old, new, standards, result=result_old, previous=case_old.mesh)
    updated, _ = make(new, new, standards, previous=case_old.mesh)
    made = {"case_old": case_old, "case_new": case_new, "updated": updated}
    files: dict[str, str] = {}
    order = []
    for spec in specs(old, new, changes, made) + request_specs(standards):
        sid, sample = sample_files(spec, standards)
        if sid in keep:
            continue
        order.append(sid)
        files |= {f"{sid}/{name}": text for name, text in sample.items()}
    index = {"inputs": inputs_digest(), "samples": list(keep) + order}
    if keep:
        index["recorded"] = keep
    files[INDEX] = _json(index)
    return files


def _all_titles() -> list[str]:
    """The titles of the samples this script makes, without building them."""
    old, new, _ = boards()
    name = old.name
    titles = [
        f"{name} case on {revision(old)}",
        f"{name} {revision(new)} arrives",
        f"{name} case updated for {revision(new)}",
    ]
    for folder in sorted(p for p in REQUESTS_DIR.iterdir() if (p / "request.json").is_file()):
        titles.append(json.loads((folder / "request.json").read_text())["title"])
    return titles


def write(files: dict[str, str], root: Path = SAMPLES_DIR, keep: Iterable[str] = ()) -> None:
    """Write the samples, replacing every one but the recorded samples in `keep`."""
    keep = set(keep)
    if root.is_dir():
        for path in root.iterdir():
            if path.name in keep:
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def stale(root: Path = SAMPLES_DIR) -> list[str]:
    """Why the samples are out of date: missing, or made from other inputs."""
    index = root / INDEX
    if not index.is_file():
        return [f"{index.relative_to(REPO_ROOT)} is missing"]
    from app.fixtures.from_session import files_digest

    doc = json.loads(index.read_text())
    kept = doc.get("recorded", {})
    problems = []
    made = [sid for sid in doc.get("samples", []) if sid not in kept]
    if made and doc.get("inputs") != inputs_digest():
        problems.append("the board files, standards, enclosure generator or sample script changed")
    for sid in doc.get("samples", []):
        if not (root / sid / SESSION_FILE).is_file():
            problems.append(f"sample {sid} is missing")
        elif sid in kept and files_digest(root / sid) != kept[sid].get("files"):
            problems.append(
                f"sample {sid} changed since it was recorded: record it again "
                "(python -m app.fixtures.from_session, demos/README.md)"
            )
    return problems


def _json(data) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    if "--check" in argv:
        problems = stale()
        for p in problems:
            print(f"demos/sessions is out of date: {p}", file=sys.stderr)
        if problems:
            print("run: cd backend && .venv/bin/python -m app.fixtures.samples", file=sys.stderr)
        return 1 if problems else 0
    keep = recorded()
    files = generate(keep=keep)
    write(files, keep=keep)
    print(f"wrote {len(files)} files to {SAMPLES_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
