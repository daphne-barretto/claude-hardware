"""Disk use per owner, and old exports cleaned up (`docs/security.md`).

Before a session saves a new file (an export, an attachment or an imported STEP file), the
owner's files are added up: the exports and attachments of every session they own, and the
builds of their sessions in memory. At `OWNER_DISK_QUOTA_MB` (500) or more, the save is
refused with a plain message (413).

Exports older than `EXPORT_MAX_AGE_HOURS` (24) are deleted at startup and, at most every ten
minutes, before an export is written; asking for one afterwards says it was cleared.
"""

from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Iterable
from pathlib import Path

from fastapi import HTTPException

from app.runtime import data_dir
from app.security import settings

log = logging.getLogger(__name__)

CLEANUP_EVERY_SECONDS = 600
_last_cleanup = 0.0


def exports_root() -> Path:
    return data_dir() / "exports"


def size_of(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    if not path.is_dir():
        return 0
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def owner_usage(session_ids: Iterable[str], build_ids: Iterable[str] = ()) -> int:
    """Bytes on disk for these sessions' exports and attachments and these builds."""
    root = data_dir()
    total = 0
    for sid in session_ids:
        if "/" in sid or sid in (".", ".."):
            continue
        total += size_of(root / "exports" / sid)
        total += size_of(root / "sessions" / sid)
    for bid in build_ids:
        if "/" not in bid and bid not in (".", ".."):
            total += size_of(root / "builds" / bid)
    return total


def quota_bytes() -> int:
    return int(settings.number("OWNER_DISK_QUOTA_MB") * 1024 * 1024)


def full_message() -> str:
    hours = settings.number("EXPORT_MAX_AGE_HOURS")
    return (
        f"This demo keeps up to {settings.number('OWNER_DISK_QUOTA_MB'):g} MB of files for each "
        f"person, and yours is full, so this file can't be saved. Exported files are cleared "
        f"after {hours:g} hours, which frees up space."
    )


def check(session_ids: Iterable[str], build_ids: Iterable[str] = ()) -> None:
    """Refuse (413) when the owner is at or over their disk quota."""
    if owner_usage(session_ids, build_ids) >= quota_bytes():
        raise HTTPException(413, full_message())


def cleanup_exports(force: bool = False) -> int:
    """Delete export directories older than `EXPORT_MAX_AGE_HOURS`. Returns how many."""
    global _last_cleanup
    now = time.time()
    if not force and now - _last_cleanup < CLEANUP_EVERY_SECONDS:
        return 0
    _last_cleanup = now
    cutoff = now - settings.number("EXPORT_MAX_AGE_HOURS") * 3600
    removed = 0
    root = exports_root()
    if not root.is_dir():
        return 0
    for session_dir in root.iterdir():
        if not session_dir.is_dir():
            continue
        for export in session_dir.iterdir():
            try:
                if export.is_dir() and export.stat().st_mtime < cutoff:
                    shutil.rmtree(export, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
    if removed:
        log.info("cleared %d old exports", removed)
    return removed


__all__ = ["check", "cleanup_exports", "full_message", "owner_usage"]
