# Claude Hardware

An AI-first hardware design output type: chat on the left, a 3D hardware canvas on the right. Claude does the design work by default; manual tools sit alongside it and feed Claude (a selection becomes context, a hand edit becomes a locked constraint).

It is a general tool: a user can start from nothing and describe any part, or bring a board, a CAD file or a photo. Nothing in the app may assume a particular board, part or design; a new session must start empty.

## Single sources of truth

Never hardcode scenario (board names, parts, dimensions) in application code or tests outside these files:

- `demos/b-parasite/` holds the board revisions as data, plus `demos/b-parasite/CHANGES.md`, which lists every difference that affects the enclosure.
- `standards.yaml` holds the team standards. Every standard must have a check. A standard without a check is a bug, because the agent will satisfy the checks and nothing else.
- `demos/b-parasite/demo-script.md` is the end-to-end walk the session API tests follow.

Tests load the scenario from these files, so swapping the board means changing only them.

## Layout

- `backend/` Python 3.12, FastAPI, Claude Agent SDK.
- `backend/geometry/` geometry backend interface. build123d is the default; CadQuery must stay swappable behind the same interface (code and parameters in; shapes, feature tags and STEP out).
- `frontend/` Next.js, React, TypeScript, Tailwind, shadcn/ui, three.js.
- `demos/` everything demo-related, kept apart from the product code (`demos/README.md`): the example board (`b-parasite/`), the recorded demo sessions (`sessions/`), and the requests they were made from (`requests/`). The board file format and loader are product code (`backend/app/boards/`).
- `docs/` the three reference docs: `architecture.md` (how it fits together, hosting and capacity), `security.md` (protections and the edge-case suite), `constraints.md` (constraint sets), plus `images/`. The README is the front page.
- `contracts/` the backend/frontend data formats (mesh, check results, analysis maps, session API), generated from `backend/app/contracts/`; `fixtures/` sample data in those formats, generated from `demos/b-parasite/` and `standards.yaml`. Frontend work runs against the mock API (`make dev MOCK_API=1`); backend work runs the contract tests (both in `docs/architecture.md`).

Commands: `make dev`, `make test`, `make e2e`.

- `make install` creates `backend/.venv` and installs the frontend packages (the other targets run it as needed).
- `make dev` runs the backend on :8000 and the frontend on :3000; the frontend proxies `/api/*` to the backend. Reads `.env` if present.
- `make test` lints (ruff, ESLint, tsc) and runs the backend tests.
- `make e2e` runs the Playwright tests in `frontend/e2e` (set `E2E_BASE_URL` to test a running app instead of starting a dev server). Every control is listed in `frontend/e2e/ui-inventory.yaml`.
- `make docker` builds the single image that serves both; CI (`.github/workflows/ci.yml`, job `ci`) runs all of the above on every PR and push to `main`.

## Making a change

- Branch from the latest `main` with a short name for the change.
- Write tests for what the change does; `make test` must pass (and `make e2e` for UI work).
- PR descriptions say what changed and how it was verified, with screenshots for UI work (sample data only).
- Every bug fix ships a regression test that fails before the fix: commit the failing test first and the fix after it, so the history shows the test failing.

## Rules

- Commit messages and PR descriptions describe the change only. No co-author lines, session links, tool attribution, or generated-by footers.
- Never commit secrets. `ANTHROPIC_API_KEY` lives only in the Render environment group `claude-hardware` and in a local `.env` (gitignored).
- Never commit personal data. Screenshots use sample data only ("Demo user"), never real chat history, names, or emails.
- Geometry checks use exact solid booleans, not mesh approximations.
- Keep `main` releasable. If a merge breaks staging, revert it.
