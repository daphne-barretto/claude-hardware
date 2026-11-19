# Claude Hardware

A hardware design tool: chat with Claude on the left, a 3D canvas on the right. Describe a part, attach a photo or a STEP file, or start from a board, and Claude writes the design as code with named features and parameters, builds it, and shows it on the canvas. Every build is checked against the session's constraint set (wall thickness, draft, clearances, fit, height, and more), and the results show on the model: what passes, what fails, and what wasn't tested. You can edit by hand (a typed value becomes a locked constraint Claude designs around), review Claude's changes one at a time, and export STEP, STL, 3MF, a PDF drawing, and more.

It is a general tool: a new session starts empty, and nothing in the app is written for a particular board or part. The demos show one example it handles: a board revision arrives, Claude proposes the enclosure changes, and each change comes with the checks that prove it.

## Quick start

Requires Python 3.12 and Node.js 24.

```sh
make install                    # backend/.venv and the frontend packages
cp .env.example .env            # then set ANTHROPIC_API_KEY
make dev                        # backend on :8000, app on http://localhost:3000
```

Without a key, `make dev MOCK_API=1` runs the mock session API instead: the same routes, served from the fixtures in `fixtures/` and a scripted run, so the whole interface works with no model call. The demos in Recents are completed demo sessions, so they open without a key too.

## Demos

Recents lists six demos, tagged **Demo**: saved sessions with their chat and model already there. Opening one gives you your own copy, so you can keep going with new prompts, edits, and exports; **Reset demo** in its row menu starts it over.

- **b-parasite case on v1.2**: the existing enclosure for the previous board revision, with every check passing.
- **b-parasite v2.0 arrives**: the next board revision is loaded, and Claude lists what changed and which checks now fail.
- **b-parasite case updated for v2.0**: Claude's proposed changes, each proved by the checks, plus a flagged risk.
- **Open box**, **L bracket**, and **Desk phone stand**: parts designed from a prompt, with no board.

## Tests

```sh
make test     # lint (ruff, ESLint, tsc), backend tests, script tests, and frontend unit tests
make e2e      # Playwright, against the mock API and the real backend with a scripted Claude
make edge     # the edge-case suite: abuse and boundary cases (docs/security.md)
```

None of these call the real model. Tests read the scenario from `demos/b-parasite/`, `standards.yaml`, `fixtures/`, and `demos/b-parasite/demo-script.md`, never from hardcoded values.

## Deploy

One Docker image (`Dockerfile`, `make docker`) serves both the Next.js app and the FastAPI backend. `render.yaml` defines two [Render](https://render.com) services from it: staging, which deploys every commit on `main`, and demo, which deploys only through its deploy hook. Both use `/health` as the health check; it reports the commit, whether a key is set, the instance's capacity, and today's spend. `ANTHROPIC_API_KEY` comes from the Render environment group `claude-hardware`, never from the repo.

The variables that matter (unset means the default):

| Variable | Default | What it does |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | none | The key every run uses. Without it a run ends at once with a plain error |
| `DATA_DIR` | `data/` locally, `/app/data` in `render.yaml` | Sessions (`sessions.db`), attachments, builds, and exports |
| `DAILY_BUDGET_USD` | 50 | What Claude may cost the whole app in a UTC day; new replies are refused once it's reached |
| `TRUSTED_PROXY_HOPS` | 1 on Render, else 0 | Proxies trusted in `X-Forwarded-For`, for the per-IP limits. Add one for each extra proxy or CDN |
| `GEOMETRY_SANDBOX_KERNEL_LIMITS` | unset (limits required) | `optional` runs builds without the sandbox's Linux kernel limits, for local development on macOS only. The server refuses to start with it in production |
| `TELEMETRY_DIR` | `~/.local/state/claude-hardware/telemetry` | Where session logs go; `off` turns them off |

Rate limits, upload sizes, and the per-run caps have their own variables, listed in [Security](docs/security.md#settings). Capacity (`BUILD_CONCURRENCY`, `MAX_AGENT_RUNS`, `BUILD_MEMORY_MB`) is sized from the machine unless set; see [Architecture](docs/architecture.md#hosting-and-capacity).

**The disk.** On Render's free plan the filesystem is wiped by every deploy and every spin-down, so `DATA_DIR` starts empty and old chats are gone. Mount a persistent disk at `/app/data` (a paid plan) to keep them; no code change is needed. Builds need an instance of at least 2 GB: the free plan's 512 MB runs one build at a time and may run out of memory.

## Feature flags

Some finished features are hidden by default. Each is turned on by setting its variable to `1` at build time (`frontend/src/lib/flags.ts`). The browser tests turn them all on.

| Flag | Shows |
| --- | --- |
| `NEXT_PUBLIC_FEATURE_MANUAL_MODE` | The Manual/Auto toggle (Manual applies nothing until you accept it). Off, every message goes in Auto mode |
| `NEXT_PUBLIC_FEATURE_SKETCH` | Sketch on a face, in the canvas toolbar |
| `NEXT_PUBLIC_FEATURE_HISTORY` | The History panel of versions, to show or restore. Undo and redo stay either way |
| `NEXT_PUBLIC_FEATURE_ADD_FOLDER` | Add folder in the composer's + menu |
| `NEXT_PUBLIC_FEATURE_MODELING_TOOLS` | The modeling toolbar (move, push/pull, hole, boss, rib, fillet, chamfer, shell, mirror, and pattern). Off, hand edits go through Dimensions and Parameters |
| `NEXT_PUBLIC_FEATURE_SELECT_FILTERS` | The parts panel's Select filters (face, edge, part, and component), box select, and Select by ID. Off, a click on the model selects a face |
| `NEXT_PUBLIC_FEATURE_FIND` | Find in model, the parts panel's search. Claude can still find parts from the chat |
| `NEXT_PUBLIC_FEATURE_LOAD_REVISION` | Load next revision in the chat header. The "b-parasite v2.0 arrives" demo still shows the result |
| `NEXT_PUBLIC_FEATURE_EXPLODE_TOOL` | The canvas toolbar's Explode button and amount slider. The Assembled, Exploded, and Print layout switch stays |
| `NEXT_PUBLIC_FEATURE_BOARD_FILES` | Start from a board file on a new session. Off, a design starts from a prompt, a STEP file, or a photo |
| `NEXT_PUBLIC_FEATURE_MEASURE_TOOL` | The Measure tool: click two points for the distance between them |

**Files you can add.** For now an upload is a STEP file (`.step`, `.stp`) to import or an image for reference. The file pickers offer only those, other files are left out with a note under the message box, and the session API refuses anything else with 415.

## Docs

- [Architecture](docs/architecture.md): the frontend, session API, agent, build sandbox, and geometry, plus the mock API, contracts, telemetry, and hosting capacity.
- [Security](docs/security.md): what someone trying to break it might try, what stops it, and the edge-case suite.
- [Constraints](docs/constraints.md): constraint sets, the rule catalogue, and the editor.

The data formats and sample data have their own READMEs in `contracts/`, `fixtures/`, and `backend/geometry/`. Everything demo-related (the example board, the recorded demo sessions, and how to record them again) is in [`demos/`](demos/README.md), apart from the product code.

## Attribution

The demo uses the open-source [b-parasite](https://github.com/rbaron/b-parasite) soil moisture sensor by rbaron. Its hardware is licensed under CC BY-SA 4.0; board data and case files derived from it (in [`demos/b-parasite/`](demos/b-parasite/), see [`demos/b-parasite/LICENSE`](demos/b-parasite/LICENSE)) are shared under the same license.

The Claude mark (`frontend/public/claude-logo.svg`) is the Claude Spark from Anthropic's press kit (<https://www.anthropic.com/press-kit>, "Claude Spark - Clay.svg"), committed byte for byte. The browser tab and home-screen icons are generated from it by `frontend/scripts/make-icons.mjs`, with nothing redrawn or recoloured.

The frontend started from [ai-website-cloner-template](https://github.com/JCodesMore/ai-website-cloner-template) (MIT); its license is kept in `frontend/LICENSE`.
