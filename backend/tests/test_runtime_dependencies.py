"""The image installs only the runtime dependencies, so app code must not import a dev-only one."""

import ast
import re
import tomllib
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
# Distribution name -> the module it is imported as, where they differ.
MODULE = {"pytest-xdist": "xdist"}


def _names(specs: list[str]) -> set[str]:
    return {re.split(r"[<>=\[ ;]", s, maxsplit=1)[0].strip().lower() for s in specs}


def test_app_code_imports_no_dev_only_dependency():
    project = tomllib.loads((BACKEND / "pyproject.toml").read_text())["project"]
    runtime = _names(project["dependencies"])
    dev_only = _names(project["optional-dependencies"]["dev"]) - runtime
    modules = {MODULE.get(n, n.replace("-", "_")) for n in dev_only}
    found = []
    for root in ("app", "geometry"):
        for path in (BACKEND / root).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                names = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                    if isinstance(node, ast.ImportFrom) and node.level == 0
                    else []
                )
                found += [
                    f"{path.relative_to(BACKEND)}: {n}" for n in names if n.split(".")[0] in modules
                ]
    assert not found, f"app code imports dev-only dependencies: {found}"
