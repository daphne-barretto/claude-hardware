"""FastAPI entry point. All routes live under /api so the frontend can proxy them.

The OpenAPI document is served at `/api/openapi.json` and browsable at `/docs`. It includes the
session API (`app.session_api`) and the server-sent event schemas, as in the contract
(`contracts/session-api.openapi.json`).
"""

import asyncio
import errno
import importlib
import logging
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app import demos, runtime, samples, shares
from app.contracts.session import openapi_with_events
from app.history_api import router as history_router
from app.message_queue import router as queue_router
from app.model_availability import AVAILABILITY
from app.modeling_api import router as modeling_router
from app.owners import OwnerMiddleware
from app.security import budget, quota, settings
from app.security.scrub import install_log_filter, redact_outgoing
from app.security.web import SecurityMiddleware
from app.session_api import SESSIONS, router
from geometry.capacity import capacity

log = logging.getLogger("uvicorn.error")  # configured by uvicorn: startup lines
# Server logs never carry a key or token (`app.security.scrub`).
install_log_filter()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Refuse an unsafe production setup before serving anything (`docs/security.md`).
    settings.check_startup()
    # Hosted filesystems can be wiped between runs, so always start from whatever is there.
    runtime.ensure_data_dir()
    # How much runs at once here: sized from the machine unless set (`geometry.capacity`).
    sized = capacity()
    log.info("Capacity: %s", sized.describe())
    # Geometry runs in threads (`asyncio.to_thread`), and a build waiting for a build slot waits
    # in its thread. Enough threads that waiting builds (a change set builds every change at
    # once) never hold up other sessions' threaded work, such as reading a mesh.
    workers = max(32, 4 * (sized.build_slots + sized.agent_runs))
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=workers, thread_name_prefix="server")
    )
    # The agent SDK is imported by the first run; import it now, off the event loop, so that run
    # doesn't stall every other session's requests and streams while it loads.
    await asyncio.to_thread(importlib.import_module, "claude_agent_sdk")
    # Which models the API key can use, for the picker; in the background, never blocking.
    refresh = asyncio.create_task(AVAILABILITY.refresh())
    quota.cleanup_exports(force=True)
    yield
    refresh.cancel()
    await SESSIONS.close()


app = FastAPI(
    title="Claude Hardware",
    version=runtime.app_version(),
    description="The session API: sessions, messages, builds, parameters, comments and "
    "exports, with server-sent events for live progress (`contracts/README.md`).",
    lifespan=lifespan,
    docs_url="/docs",
    openapi_url="/api/openapi.json",
)


SERVER_ERROR = "Something went wrong on our side. Please try again in a moment."


@app.exception_handler(HTTPException)
async def _http_error(_: Request, error: HTTPException) -> JSONResponse:
    # A detail never carries a server path, env value or key (`app.security.scrub`).
    return JSONResponse(
        {"detail": redact_outgoing(str(error.detail))},
        status_code=error.status_code,
        headers=getattr(error, "headers", None),
    )


@app.exception_handler(Exception)
async def _server_error(request: Request, error: Exception) -> JSONResponse:
    """Anything unexpected: the details go to the server log, the browser gets a plain line."""
    log.exception("%s %s failed", request.method, request.url.path)
    return JSONResponse({"detail": SERVER_ERROR}, status_code=500)


@app.exception_handler(OSError)
async def _disk_error(request: Request, error: OSError) -> JSONResponse:
    """A full disk (or quota) is the server's problem, said plainly (507); anything else is a
    plain 500."""
    if error.errno not in (errno.ENOSPC, errno.EDQUOT):
        return await _server_error(request, error)  # anything else: a plain 500
    detail = "The server is out of disk space, so this couldn't be saved. Try again later."
    return JSONResponse({"detail": detail}, status_code=507)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    """Liveness plus build identity, today's Claude usage against the daily budget, and how
    much the instance runs at once (`geometry.capacity`): its build slots and agent runs, the
    memory and CPUs they were sized from, and whether each was set or sized. Reports whether
    the API key is set, never its value. It needs no worker thread and reads the usage at most
    once a second (`budget.cached_status`), so it stays fast under load."""
    try:
        usage: dict[str, Any] | None = budget.cached_status()
    except Exception:  # health never fails over the usage
        log.exception("can't read today's usage")
        usage = None
    return {
        "status": "ok",
        "version": runtime.app_version(),
        "commit": runtime.commit_sha(),
        "api_key_present": runtime.api_key_present(),
        "capacity": capacity().as_dict(),
        "usage": usage,
    }


# The demos' read-only originals: their routes match only their IDs, so they go first
# (`app.samples`).
app.include_router(samples.router)
app.include_router(demos.router)
app.include_router(router)
# CSRF, security headers, body limits and stream caps (`app.security.web`); inside the owner
# middleware, so it knows the owner.
app.add_middleware(SecurityMiddleware)
# Share links (`app.shares`): owner routes on a session, read-only routes on a link.
app.include_router(shares.router)
# Each browser sees only its own sessions.
app.add_middleware(OwnerMiddleware)
app.include_router(modeling_router)
app.include_router(queue_router)
app.include_router(history_router)


def _openapi() -> dict:
    if app.openapi_schema is None:
        app.openapi_schema = openapi_with_events(app)
    return app.openapi_schema


app.openapi = _openapi  # type: ignore[method-assign]
