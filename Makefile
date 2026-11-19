# Commands for local development and CI. See CLAUDE.md.
SHELL := /bin/bash
PYTHON ?= python3.12
VENV := backend/.venv
PY := $(VENV)/bin/python
BACKEND_PORT ?= 8000
FRONTEND_PORT ?= 3000
# MOCK_API=1 runs the mock session API (fixtures and a scripted run) instead of the real backend.
BACKEND_APP := $(if $(MOCK_API),app.mock_api:app,app.main:app)

.PHONY: install install-backend install-frontend dev lint test e2e edge qa docker clean

install: install-backend install-frontend

install-backend: $(VENV)/.installed

$(VENV)/.installed: backend/pyproject.toml
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install --quiet --upgrade pip
	$(PY) -m pip install --quiet -e 'backend[dev]'
	touch $@

install-frontend: frontend/node_modules/.installed

frontend/node_modules/.installed: frontend/package-lock.json
	cd frontend && npm ci --no-audit --no-fund
	touch $@

# Backend on :$(BACKEND_PORT) with reload, frontend on :$(FRONTEND_PORT) proxying /api to it. The
# backend keeps idle sockets longer than the proxy does (docs/architecture.md, "Keep-alive").
# `make dev MOCK_API=1` serves the contract fixtures instead (docs/architecture.md).
dev: install
	@set -a; [ -f .env ] && . ./.env; set +a; \
	trap 'kill 0' EXIT INT TERM; \
	(cd backend && .venv/bin/uvicorn $(BACKEND_APP) --reload --host 127.0.0.1 --port $(BACKEND_PORT) --timeout-keep-alive 75 --timeout-graceful-shutdown 3 --reload-dir app) & \
	(cd frontend && BACKEND_URL=http://127.0.0.1:$(BACKEND_PORT) npx next dev --port $(FRONTEND_PORT)) & \
	wait

lint: install
	cd backend && .venv/bin/ruff check . && .venv/bin/ruff format --check .
	cd frontend && npm run lint && npm run typecheck

# The backend tests run in parallel and list the slowest tests. `make test SHARD=k/n` runs
# only the k-th of n shards of them (CI runs each shard in its own job).
PYTEST_PARALLEL ?= -n auto --dist worksteal
test: lint
	cd backend && .venv/bin/pytest -q $(PYTEST_PARALLEL) --durations=20 $(if $(SHARD),--shard=$(SHARD))
	$(PY) -m unittest discover -s .github/scripts
	cd frontend && npm test

e2e: install
	cd frontend && npx playwright install chromium
	cd frontend && npx playwright test

# The edge-case suite (docs/security.md): backend abuse tests, then the Playwright edge specs
# against the real backend with a scripted Claude. Never calls the real model. Ports default to
# Playwright's; set EDGE_PORT, EDGE_MOCK_PORT and EDGE_LIVE_PORT to run beside other servers.
# Off Linux the build sandbox's kernel limits can't apply, so local builds run without them.
EDGE_PORT ?= 3100
EDGE_MOCK_PORT ?= 8100
EDGE_LIVE_PORT ?= 8200
EDGE_KERNEL_LIMITS := $(if $(filter Linux,$(shell uname -s)),,GEOMETRY_SANDBOX_KERNEL_LIMITS=optional)
edge: install
	cd backend && $(EDGE_KERNEL_LIMITS) .venv/bin/pytest -q $(PYTEST_PARALLEL) tests/edge
	cd frontend && npx playwright install chromium
	cd frontend && $(EDGE_KERNEL_LIMITS) E2E_PORT=$(EDGE_PORT) E2E_MOCK_PORT=$(EDGE_MOCK_PORT) \
		E2E_LIVE_PORT=$(EDGE_LIVE_PORT) npx playwright test e2e/edge

# The QA harness: every state and control on the real backend with a scripted
# Claude (never the real model), at phone, laptop and wide sizes. Ports QA_PORT (3250),
# QA_LIVE_PORT (8250) and QA_MOCK_PORT (8251); QA_WORKERS sets the parallel browsers (4).
# Writes frontend/test-results/qa/summary.md and screenshots under frontend/test-results/screenshots/qa/.
qa: install
	cd frontend && npx playwright install chromium
	rm -rf frontend/test-results/qa/findings frontend/test-results/qa/results*.json
	cd frontend && npx playwright test -c e2e/qa/qa.config.ts; status=$$?; node e2e/qa/report.mts; exit $$status

docker:
	docker build -t claude-hardware .

clean:
	rm -rf $(VENV) frontend/node_modules frontend/.next frontend/test-results frontend/playwright-report
