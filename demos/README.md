# Demos

Everything here is demo content, kept apart from the product. The app itself names no board, part or design, and a new session starts empty.

The demos are completed sessions of the app: real runs with Claude Opus 5.5 (effort medium, Auto mode), saved with their conversation and every build the run made (mesh, checks, source, analysis maps, and diffs). They open without an API key. The originals are read-only (`backend/app/samples.py`); opening one gives each user their own copy (`backend/app/demos.py`, `backend/app/sample_copies.py`), which they can keep working in.

| Folder | Contents |
|---|---|
| `b-parasite/` | The example board: two revisions of the b-parasite soil moisture sensor as board JSON (format: `backend/app/boards/SCHEMA.md`), `CHANGES.md` (every difference that affects the enclosure), the original case STEP, the example a session can open (`examples.yaml`), the KiCad sources (`source/`), `demo-script.md` (the walk the session API tests follow), and `LICENSE`. See `b-parasite/README.md`. |
| `sessions/` | The six recorded demo sessions, in the order `sessions/index.json` lists them: three cut from one session of the board revision example (the case on the older revision, the newer revision arriving, the case updated for it), and an open box, an L bracket, and a phone stand made from a plain request. |
| `requests/` | The requests the last three were made from (`<name>/request.json`), with the `design.py` the scripted samples build. |

Each `sessions/<id>/` has `session.json` (the session, its messages, its recorded events, and its parameters) and `builds/<build id>/` with `build.json`, `mesh.gltf`, `checks.json`, `source.json`, `maps/<type>.json`, `design.json`, and `diffs/<other build>.json`. `index.json` marks them as recorded, with a digest of their files; `make test` fails if one changes without being recorded again.

## Making or refreshing a demo

Recording calls the real model and is billed (about $0.85 for all six). Run the backend with the key, a data directory of its own, and real builds:

```sh
set -a; . ./.env; set +a
export DATA_DIR=$(mktemp -d) GEOMETRY_SANDBOX_KERNEL_LIMITS=optional DAILY_BUDGET_USD=30
cd backend && PYTHONPATH=$PWD .venv/bin/uvicorn app.main:app --port 8743
```

Then, over the session API (or in the app), with `{"model": "claude-opus-5-5", "effort": "medium", "mode": "auto"}` on every message:

- for each folder of `requests/`: `POST /api/sessions` with `{}`, then post the `prompt` of its `request.json`;
- for the board revision: `POST /api/sessions` with `{"example": "board-revision"}`, wait for the first build, post `Open the existing <board> case on revision <older>, the board it was made for, and check it.`, then `POST /api/sessions/{id}/revisions/next`. If Claude asks a question, answer with the option that keeps to the constraints and finishes the job.

When each run ends (the session is `idle`), turn it into a demo with the converter, using the same data directory. The IDs and titles are those in `sessions/index.json`:

```sh
cd backend
F() { PYTHONPATH=$PWD .venv/bin/python -m app.fixtures.from_session "$@"; }
F <revision session> <demo 1 id> --title "<demo 1 title>" --turns 0
F <revision session> <demo 2 id> --title "<demo 2 title>" --turns 1
F <revision session> <demo 3 id> --title "<demo 3 title>"
F <box session> sample-open-box
F <bracket session> sample-l-bracket
F <stand session> sample-phone-stand
```

`--turns N` keeps the first N user turns (0: the session as it was opened). The converter writes to `sessions/`, leaves out the owner, attachments, exports, and share links, replaces the session's and builds' IDs with the demo's, and refuses output that holds a secret, an API key, an owner cookie, or a local path. Check the diff before committing it all the same.

`python -m app.fixtures.samples` makes scripted demos without Claude instead (a short scripted conversation, builds from the real pipeline). It never overwrites a recorded demo; delete a demo's entry under `recorded` in `sessions/index.json` to let it.

## Attribution

The example board is [b-parasite](https://github.com/rbaron/b-parasite), an open-source soil moisture sensor by rbaron. Its hardware is licensed under CC BY-SA 4.0. The board data, `CHANGES.md`, and the case STEP in `b-parasite/` are derived from it and shared under the same license: see [`b-parasite/LICENSE`](b-parasite/LICENSE).
