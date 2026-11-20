"""Names for exported files: the session's title and the build, not a fixed "enclosure".

The exporters write fixed names (`geometry.export.FILE_NAMES`); an export is renamed here once
written, so a session titled "Humanoid tripod robot" exporting build 4 downloads
`humanoid-tripod-robot-build-4.step`, `...-drawing.pdf`, `...-parameters.json` and so on.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from geometry.export import STEM

MAX_SLUG = 60


def slug(title: str | None) -> str:
    """Lowercase ASCII words joined by hyphens; "design" when nothing is left."""
    text = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode()
    words = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return words[:MAX_SLUG].rstrip("-") or "design"


def export_name(original: str, title: str | None, number: int) -> str:
    """The download name for an exporter's file `original`."""
    prefix = f"{slug(title)}-build-{number}"
    if original.startswith(STEM):
        return prefix + original[len(STEM) :]
    return f"{prefix}-{original}"


def rename_exports(out: Path, written: list[dict], title: str | None, number: int) -> None:
    """Rename the written files in `out` in place and update their `name`s."""
    for f in written:
        new = export_name(f["name"], title, number)
        if new != f["name"] and (out / f["name"]).exists():
            (out / f["name"]).rename(out / new)
        f["name"] = new
