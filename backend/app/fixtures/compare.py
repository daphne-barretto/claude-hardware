"""Compare generated fixtures with the checked-in ones, allowing float noise between platforms."""

from __future__ import annotations

import json
from pathlib import Path

from app.contracts.mesh import load_gltf, read_accessor

TOLERANCE = 1e-3  # mm or degrees
HAND_WRITTEN = {"README.md"}


def _close(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return abs(a - b) <= TOLERANCE
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_close(a[k], b[k]) for k in a)
    if isinstance(a, list | tuple) and isinstance(b, list | tuple):
        return len(a) == len(b) and all(_close(x, y) for x, y in zip(a, b, strict=True))
    return a == b


def _decoded_gltf(text: str) -> dict:
    doc, buffers = load_gltf(json.loads(text))
    accessors = [read_accessor(doc, buffers, i) for i in range(len(doc.get("accessors", [])))]
    doc = {k: v for k, v in doc.items() if k not in ("buffers", "bufferViews")}
    doc["accessors"] = [
        {k: v for k, v in acc.items() if k != "bufferView"} | {"data": data}
        for acc, data in zip(doc.get("accessors", []), accessors, strict=True)
    ]
    return doc


def same(name: str, new: str, old: str) -> bool:
    if new == old:
        return True
    try:
        if name.endswith(".gltf"):
            return _close(_decoded_gltf(new), _decoded_gltf(old))
        if name.endswith(".json"):
            return _close(json.loads(new), json.loads(old))
        if name.endswith(".jsonl"):
            a, b = new.splitlines(), old.splitlines()
            return len(a) == len(b) and all(
                _close(json.loads(x), json.loads(y)) for x, y in zip(a, b, strict=True)
            )
    except ValueError:
        return False
    return False


def stale_files(files: dict[str, str], root: Path) -> list[str]:
    """Generated files that are missing or differ, and checked-in files nothing generates."""
    stale = []
    for name, text in sorted(files.items()):
        path = root / name
        if not path.exists() or not same(name, text, path.read_text()):
            stale.append(name)
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if path.is_file() and name not in files and name not in HAND_WRITTEN:
            stale.append(name)
    return stale
