"""What an export wrote: importable without loading the CAD kernel, so a geometry worker
(`geometry.worker`) can hand it back to the server."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class ExportedFile:
    format: str
    name: str
    media_type: str
    path: Path


@dataclass(frozen=True)
class Revision:
    """One build in the history a drawing's revision table lists."""

    build_id: str
    date: date
    description: str
