"""Where the session API keeps its sessions, so a deploy or restart loses no chat.

`SessionStore` is the one interface: a session's state (a JSON document: the session, its
messages, builds and their designs, comments, exports, change set and the workspace's design),
its events (appended one by one, in order) and its attachments (files). The session API saves
as it goes and loads a session on demand when it isn't in memory (`app.session_api`).

The default, `SqliteStore`, keeps the state and events in `DATA_DIR/sessions.db` and the
attachments as files under `DATA_DIR/sessions/<session id>/attachments/`. Exports already live
under `DATA_DIR/exports`. `docs/architecture.md` says where staging and demo keep `DATA_DIR`.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Protocol

from app.runtime import data_dir

_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")


class SessionStore(Protocol):
    def ids(self) -> list[str]: ...

    def owners(self) -> dict[str, str | None]: ...

    def load(self, session_id: str) -> dict[str, Any] | None: ...

    def save(self, session_id: str, state: dict[str, Any]) -> None: ...

    def append_event(self, session_id: str, event_id: int, event: dict[str, Any]) -> None: ...

    def events(self, session_id: str) -> list[tuple[int, dict[str, Any]]]: ...

    def write_attachment(self, session_id: str, attachment_id: str, data: bytes) -> None: ...

    def read_attachment(self, session_id: str, attachment_id: str) -> bytes | None: ...

    # The standards sets each browser made (`app.standards_sets`), by owner.

    def standards_sets(self, owner: str) -> list[dict[str, Any]]: ...

    def save_standards_set(self, owner: str, set_id: str, data: dict[str, Any]) -> None: ...

    def delete_standards_set(self, owner: str, set_id: str) -> bool: ...

    def add_usage(
        self, day: str, cost_usd: float, input_tokens: int, output_tokens: int
    ) -> None: ...

    def usage(self, day: str) -> dict[str, Any]: ...

    def save_share(self, token: str, session_id: str, record: dict[str, Any]) -> None: ...

    def load_share(self, token: str) -> dict[str, Any] | None: ...

    def shares_of(self, session_id: str) -> list[dict[str, Any]]: ...

    def delete_attachments(self, session_id: str) -> None: ...

    def delete(self, session_id: str) -> None: ...


def _safe(name: str) -> str:
    if not _SAFE.match(name) or name in (".", ".."):
        raise ValueError(f"not a safe file name: {name!r}")
    return name


class SqliteStore:
    """SQLite for the state and events, files for the attachments, all under `root`."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(root / "sessions.db", check_same_thread=False)
        with self._lock, self._db:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, state TEXT NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS events (session_id TEXT NOT NULL, "
                "event_id INTEGER NOT NULL, event TEXT NOT NULL, "
                "PRIMARY KEY (session_id, event_id))"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS standards_sets (owner TEXT NOT NULL, "
                "id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (owner, id))"
            )
            # Claude's usage per UTC day, for the daily spend cap (`app.security.budget`).
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS usage (day TEXT PRIMARY KEY, "
                "cost_usd REAL NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0, "
                "output_tokens INTEGER NOT NULL DEFAULT 0, runs INTEGER NOT NULL DEFAULT 0)"
            )
            # Share links (`app.shares`): a snapshot each, by token. Not sessions: they never
            # show up in anyone's session list.
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS shares (token TEXT PRIMARY KEY, "
                "session_id TEXT NOT NULL, record TEXT NOT NULL)"
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def ids(self) -> list[str]:
        with self._lock:
            rows = self._db.execute("SELECT id FROM sessions ORDER BY rowid").fetchall()
        return [r[0] for r in rows]

    def owners(self) -> dict[str, str | None]:
        """Each saved session's owner (`app.owners`), without loading the sessions."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, json_extract(state, '$.owner') FROM sessions ORDER BY rowid"
            ).fetchall()
        return {sid: owner for sid, owner in rows}

    def load(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT state FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, session_id: str, state: dict[str, Any]) -> None:
        text = json.dumps(state)
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO sessions (id, state) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET state = excluded.state",
                (session_id, text),
            )

    def append_event(self, session_id: str, event_id: int, event: dict[str, Any]) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO events (session_id, event_id, event) VALUES (?, ?, ?)",
                (session_id, event_id, json.dumps(event)),
            )

    def events(self, session_id: str) -> list[tuple[int, dict[str, Any]]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT event_id, event FROM events WHERE session_id = ? ORDER BY event_id",
                (session_id,),
            ).fetchall()
        return [(i, json.loads(e)) for i, e in rows]

    def add_usage(self, day: str, cost_usd: float, input_tokens: int, output_tokens: int) -> None:
        """Add one run's usage to the day's totals."""
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO usage (day, cost_usd, input_tokens, output_tokens, runs) "
                "VALUES (?, ?, ?, ?, 1) ON CONFLICT(day) DO UPDATE SET "
                "cost_usd = cost_usd + excluded.cost_usd, "
                "input_tokens = input_tokens + excluded.input_tokens, "
                "output_tokens = output_tokens + excluded.output_tokens, runs = runs + 1",
                (day, cost_usd, input_tokens, output_tokens),
            )

    def usage(self, day: str) -> dict[str, Any]:
        """The day's totals: cost in US dollars, tokens and runs."""
        with self._lock:
            row = self._db.execute(
                "SELECT cost_usd, input_tokens, output_tokens, runs FROM usage WHERE day = ?",
                (day,),
            ).fetchone()
        cost, inp, out, runs = row or (0.0, 0, 0, 0)
        return {"cost_usd": cost, "input_tokens": inp, "output_tokens": out, "runs": runs}

    def _attachment(self, session_id: str, attachment_id: str) -> Path:
        return self.root / "sessions" / _safe(session_id) / "attachments" / _safe(attachment_id)

    def write_attachment(self, session_id: str, attachment_id: str, data: bytes) -> None:
        path = self._attachment(session_id, attachment_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(path)

    def read_attachment(self, session_id: str, attachment_id: str) -> bytes | None:
        try:
            path = self._attachment(session_id, attachment_id)
        except ValueError:
            return None
        return path.read_bytes() if path.is_file() else None

    def standards_sets(self, owner: str) -> list[dict[str, Any]]:
        """The owner's standards sets, oldest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT data FROM standards_sets WHERE owner = ? ORDER BY rowid", (owner,)
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def save_standards_set(self, owner: str, set_id: str, data: dict[str, Any]) -> None:
        text = json.dumps(data)
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO standards_sets (owner, id, data) VALUES (?, ?, ?) "
                "ON CONFLICT(owner, id) DO UPDATE SET data = excluded.data",
                (owner, set_id, text),
            )

    def delete_standards_set(self, owner: str, set_id: str) -> bool:
        with self._lock, self._db:
            cursor = self._db.execute(
                "DELETE FROM standards_sets WHERE owner = ? AND id = ?", (owner, set_id)
            )
        return cursor.rowcount > 0

    def delete_attachments(self, session_id: str) -> None:
        """Remove every attachment file kept under `session_id` (a share's snapshot)."""
        folder = self.root / "sessions" / _safe(session_id) / "attachments"
        if folder.is_dir():
            for path in folder.iterdir():
                path.unlink(missing_ok=True)
            folder.rmdir()

    def delete(self, session_id: str) -> None:
        """Delete a session for good: its state, events and attachments (a reset demo,
        `app.demos`). Its share links keep their own snapshots."""
        with self._lock, self._db:
            self._db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            self._db.execute("DELETE FROM events WHERE session_id = ?", (session_id,))
        self.delete_attachments(session_id)
        folder = self.root / "sessions" / _safe(session_id)
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()

    # --- Share links (`app.shares`) -----------------------------------------------------------

    def save_share(self, token: str, session_id: str, record: dict[str, Any]) -> None:
        text = json.dumps(record)
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO shares (token, session_id, record) VALUES (?, ?, ?) "
                "ON CONFLICT(token) DO UPDATE SET record = excluded.record",
                (token, session_id, text),
            )

    def load_share(self, token: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT record FROM shares WHERE token = ?", (token,)).fetchone()
        return json.loads(row[0]) if row else None

    def shares_of(self, session_id: str) -> list[dict[str, Any]]:
        """Every share link of a session, oldest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT record FROM shares WHERE session_id = ? ORDER BY rowid", (session_id,)
            ).fetchall()
        return [json.loads(r[0]) for r in rows]


class MemoryStandardsStore:
    """Standards sets in memory, for the mock API (`app.mock_api`), which saves nothing."""

    def __init__(self) -> None:
        self._sets: dict[str, dict[str, dict[str, Any]]] = {}

    def standards_sets(self, owner: str) -> list[dict[str, Any]]:
        return [json.loads(json.dumps(d)) for d in self._sets.get(owner, {}).values()]

    def save_standards_set(self, owner: str, set_id: str, data: dict[str, Any]) -> None:
        self._sets.setdefault(owner, {})[set_id] = json.loads(json.dumps(data))

    def delete_standards_set(self, owner: str, set_id: str) -> bool:
        return self._sets.get(owner, {}).pop(set_id, None) is not None


_STORES: dict[Path, SqliteStore] = {}
_STORES_LOCK = threading.Lock()


def session_store() -> SessionStore:
    """The store for the current `DATA_DIR` (read each time, so tests can point it anywhere)."""
    root = data_dir().resolve()
    with _STORES_LOCK:
        store = _STORES.get(root)
        if store is None or not (root / "sessions.db").exists():
            if store is not None:
                store.close()
            store = _STORES[root] = SqliteStore(root)
        return store


__all__ = ["MemoryStandardsStore", "SessionStore", "SqliteStore", "session_store"]
