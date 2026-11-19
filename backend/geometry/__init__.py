"""Geometry: the enclosure generator behind a swappable CAD backend.

Code and parameters go in; OpenCascade shapes, feature tags and STEP come out. build123d is the
default backend and CadQuery is the alternative. Choose one per run (`backend=`) or per project
(the `GEOMETRY_BACKEND` environment variable). Tagging and checks work on the shapes and do not
care which backend made them. `run_build` runs untrusted generated code in a sandboxed child
process (see `geometry.sandbox` and `docs/architecture.md`).
"""

from __future__ import annotations

import importlib
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.boards import Board
    from app.standards import StandardsFile
    from geometry.interface import BuildResult, GeometryBackend
    from geometry.params import Params

# Everything here is loaded on first use: importing `geometry` (or a light module in it, such as
# `geometry.params` or `geometry.sandbox`) must not load the CAD kernel. The server never does
# geometry work itself (`geometry.worker`), so it never pays for the kernel.
_LAZY = {
    "BuildError": "geometry.interface",
    "BuildResult": "geometry.interface",
    "FaceRef": "geometry.interface",
    "Feature": "geometry.interface",
    "GeometryBackend": "geometry.interface",
    "Params": "geometry.params",
    "enclosure_parameters": "geometry.params",
    "run_build": "geometry.sandbox",
}
DEFAULT_BACKEND = "build123d"


def __getattr__(name: str) -> Any:
    if name == "BACKENDS":
        return _backends()
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module 'geometry' has no attribute {name!r}")
    return getattr(importlib.import_module(module), name)


def _backends() -> dict[str, type[GeometryBackend]]:
    from geometry.backends.build123d import Build123dBackend
    from geometry.backends.cadquery import CadQueryBackend

    return {Build123dBackend.name: Build123dBackend, CadQueryBackend.name: CadQueryBackend}


def get_backend(name: str | None = None) -> GeometryBackend:
    """The named backend, else `GEOMETRY_BACKEND`, else build123d."""
    name = name or os.environ.get("GEOMETRY_BACKEND") or DEFAULT_BACKEND
    backends = _backends()
    try:
        return backends[name]()
    except KeyError:
        known = sorted(backends)
        raise ValueError(f"unknown geometry backend {name!r}; choose from {known}") from None


def generate_enclosure(
    board: Board,
    standards: StandardsFile,
    params: Params | None = None,
    backend: str | GeometryBackend | None = None,
) -> BuildResult:
    """Build the enclosure for a board.

    `params` defaults to the parameters derived from this board (see `geometry.params`). Pass
    parameters derived from another revision to see that design on this board.
    """
    from geometry.interface import GeometryBackend
    from geometry.params import enclosure_parameters

    engine = backend if isinstance(backend, GeometryBackend) else get_backend(backend)
    if params is None:
        params = enclosure_parameters(board, standards)
    return engine.build(engine.example("enclosure"), params)


__all__ = [
    "BACKENDS",
    "DEFAULT_BACKEND",
    "BuildError",
    "BuildResult",
    "FaceRef",
    "Feature",
    "GeometryBackend",
    "Params",
    "enclosure_parameters",
    "generate_enclosure",
    "get_backend",
    "run_build",
]
