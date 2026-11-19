"""The telemetry log of one session: append-only JSON Lines, one event per line.

- `TELEMETRY_DIR` is where logs go. Unset, they go to `~/.local/state/claude-hardware/telemetry`
  (`$XDG_STATE_HOME` if set), outside the repository. `off` turns recording off.
- `TELEMETRY_RETENTION_DAYS` (default 90) is how long a log is kept: older ones are deleted
  whenever a new session starts.

Each session gets a directory `<TELEMETRY_DIR>/<session id>-<time>/` with `events.jsonl` and
`artifacts/` (the mesh, checks, source and maps each build served).

Secrets never reach the disk: every event is redacted first (`redact`), which removes the
values of secret environment variables (names with KEY, TOKEN, SECRET, PASSWORD or CREDENTIAL)
and anything shaped like an API key or a bearer token.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.contracts.telemetry import validate_telemetry_event

log = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 90
OFF = {"off", "0", "false", "none", "no"}
EVENTS = "events.jsonl"
ARTIFACTS = "artifacts"

REDACTED = "[redacted]"
SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.IGNORECASE)
SECRET_FIELDS = re.compile(
    r"^(api[_-]?key|x-api-key|authorization|password|secret|token|access[_-]?token)$",
    re.IGNORECASE,
)
SECRET_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{6,}"),  # Anthropic API keys and OAuth tokens
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),  # other providers' keys
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{8,}=*"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub tokens
)


# --- Configuration ----------------------------------------------------------------------------


def default_dir() -> Path:
    state = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(state) if state else Path.home() / ".local" / "state"
    return base / "claude-hardware" / "telemetry"


def telemetry_dir() -> Path | None:
    """Where logs go, or None when recording is off."""
    value = os.environ.get("TELEMETRY_DIR", "").strip()
    if value.lower() in OFF:
        return None
    return Path(value) if value else default_dir()


def retention_days() -> int:
    try:
        return max(1, int(os.environ.get("TELEMETRY_RETENTION_DAYS", "")))
    except ValueError:
        return DEFAULT_RETENTION_DAYS


def prune(directory: Path, days: int | None = None, now: float | None = None) -> list[Path]:
    """Delete the session logs in `directory` older than the retention limit."""
    if not directory.is_dir():
        return []
    cutoff = (now or time.time()) - timedelta(days=days or retention_days()).total_seconds()
    removed = []
    for path in directory.iterdir():
        events = path / EVENTS
        if path.is_dir() and events.exists() and events.stat().st_mtime < cutoff:
            shutil.rmtree(path, ignore_errors=True)
            removed.append(path)
    return removed


# --- Redaction --------------------------------------------------------------------------------


def secret_values() -> list[str]:
    """The values of the secret environment variables, longest first."""
    found = {
        v.strip() for k, v in os.environ.items() if SECRET_ENV.search(k) and len(v.strip()) >= 8
    }
    return sorted(found, key=len, reverse=True)


def redact_text(text: str, secrets: list[str]) -> str:
    for value in secrets:
        text = text.replace(value, REDACTED)
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def redact(value: Any, secrets: list[str] | None = None) -> Any:
    """`value` (JSON data) with every secret replaced by `[redacted]`."""
    secrets = secret_values() if secrets is None else secrets
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, dict):
        return {
            k: REDACTED if SECRET_FIELDS.match(str(k)) and v else redact(v, secrets)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(v, secrets) for v in value]
    return value


def jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [jsonable(v) for v in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


# --- The log ----------------------------------------------------------------------------------


class TelemetryLog:
    """Appends a session's events to `<directory>/<session id>-<time>/events.jsonl`."""

    def __init__(self, directory: Path, session_id: str):
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        self.dir = directory / f"{session_id}-{stamp}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / EVENTS
        self.session_id = session_id
        self.seq = 0
        self.step = 0
        self.started = time.monotonic()
        self.last_stream = self.started

    def ms_since(self, began: float | None) -> int | None:
        return None if began is None else max(0, round((time.monotonic() - began) * 1000))

    def write(self, kind: str, **fields: Any) -> dict | None:
        """Append one event. Telemetry never breaks a session: a bad event is logged and
        dropped."""
        try:
            data = {
                "v": 1,
                "session_id": self.session_id,
                "seq": self.seq + 1,
                "at": datetime.now(UTC).isoformat(),
                "t_ms": self.ms_since(self.started),
                "kind": kind,
                **jsonable(fields),
            }
            data = redact(data)
            validate_telemetry_event(data)
            with self.path.open("a", encoding="utf-8") as out:
                out.write(json.dumps(data, ensure_ascii=False) + "\n")
            self.seq += 1
            return data
        except Exception:
            log.exception("telemetry: dropped a %s event of %s", kind, self.session_id)
            return None

    def stream(self, event: Any) -> None:
        now = time.monotonic()
        delay = max(0, round((now - self.last_stream) * 1000))
        self.last_stream = now
        self.write("stream", delay_ms=delay, event=event)

    def save_artifacts(self, build_id: str, artifacts: Any) -> str | None:
        """Keep what a build served (`BuildArtifacts`); returns the file name."""
        try:
            name = f"{re.sub(r'[^A-Za-z0-9_.-]', '_', build_id)}.json"
            folder = self.dir / ARTIFACTS
            folder.mkdir(exist_ok=True)
            data = redact(jsonable(artifacts))
            (folder / name).write_text(json.dumps(data), encoding="utf-8")
            return name
        except Exception:
            log.exception("telemetry: could not keep the artifacts of %s", build_id)
            return None

    def canvas(self, after: str, state: Any) -> None:
        if after != "session_start":
            self.step += 1
        self.write("canvas", step=self.step, after=after, state=state)


def open_log(session_id: str) -> TelemetryLog | None:
    directory = telemetry_dir()
    if directory is None:
        return None
    try:
        prune(directory)
        return TelemetryLog(directory, session_id)
    except OSError:
        log.exception("telemetry: can't write to %s; this session is not recorded", directory)
        return None


def read_log(path: Path) -> list[dict]:
    """The events of a log (its directory or its `events.jsonl`)."""
    path = path / EVENTS if path.is_dir() else path
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def log_dirs(directory: Path) -> list[Path]:
    """Every session log under `directory`, oldest first."""
    if not directory.is_dir():
        return []
    return sorted((p.parent for p in directory.glob(f"*/{EVENTS}")), key=lambda p: p.name)
