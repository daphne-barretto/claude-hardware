"""The design the agent edits: generator code plus its parameters, and the locks on them.

Parameters are the JSON the generator reads (`geometry.params`). A parameter is named by its
path in that JSON: dot-separated keys, where a list element is picked by its `id`, else its
`tag`, else its index. For example `wall`, `footprint.x` or `openings.Q4-sensor_window.x`.

Locks live in the parameter store (`app.parameters`): a value set by hand is locked, and the
store refuses to change it without an approval. A lock on a path also covers everything under
it, and an edit to a parent path (replacing the whole `openings` list) is refused if it would
change a locked value inside it.

The generator code turns the parameters into geometry, so rewriting it could override a locked
value without touching the parameters. While any parameter is locked, the code is locked too:
changing it needs the user's approval of `CODE`, the same way as a locked parameter.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any

from app.parameters import Approval, LockedParameterError, ParameterStore
from geometry.modeling import edits_block
from geometry.modeling import strip_block as strip_edits
from geometry.sketch import sketch_block

Params = dict[str, Any]

AGENT = "claude"

# The approval path for changing the generator code while parameters are locked.
CODE = "code"


class PathError(KeyError):
    """A parameter path names nothing in the parameters."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else "bad parameter path"


def _child(node: Any, key: str, path: str) -> tuple[Any, Any]:
    """The child of `node` named `key`, and the key or index to reach it."""
    if isinstance(node, dict):
        if key in node:
            return node[key], key
        raise PathError(f"{path}: no key {key!r}")
    if isinstance(node, list):
        for field_name in ("id", "tag"):
            for i, item in enumerate(node):
                if isinstance(item, dict) and str(item.get(field_name)) == key:
                    return item, i
        if key.isdigit() and int(key) < len(node):
            return node[int(key)], int(key)
        raise PathError(f"{path}: no list element with id, tag or index {key!r}")
    raise PathError(f"{path}: {key!r} is inside a value, not an object or list")


def get_path(params: Params, path: str) -> Any:
    node: Any = params
    for key in path.split("."):
        node, _ = _child(node, key, path)
    return node


def has_path(params: Params, path: str) -> bool:
    try:
        get_path(params, path)
    except PathError:
        return False
    return True


def set_path(params: Params, path: str, value: Any) -> Params:
    """A copy of `params` with the value at `path` replaced. Only existing paths can be set,
    except a new key in an object (adding an opening's field, say)."""
    out = copy.deepcopy(params)
    keys = path.split(".")
    node: Any = out
    for key in keys[:-1]:
        node, _ = _child(node, key, path)
    last = keys[-1]
    if isinstance(node, dict):
        node[last] = value
    else:
        _, index = _child(node, last, path)
        node[index] = value
    return out


class LockedCodeError(Exception):
    """The code can't change while parameters are locked, without the user's approval."""

    def __init__(self, locked: list[str]):
        super().__init__(locked)
        self.locked = locked

    def __str__(self) -> str:
        return (
            f"the generator code is locked while {', '.join(self.locked)} "
            f"{'is' if len(self.locked) == 1 else 'are'} locked: new code could override a "
            "locked value"
        )


# Numbers closer than this (absolute, or relative to the larger one) are the same value: a value
# written back as JSON comes back as an int (5 for 5.0) or with float noise, and is no change.
SAME_TOLERANCE = 1e-6


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def same(a: Any, b: Any) -> bool:
    """Whether two parameter values (or designs) are the same: numbers compared as numbers
    within `SAME_TOLERANCE`, dicts and lists element by element, anything else exactly."""
    x, y = _number(a), _number(b)
    if x is not None and y is not None:
        return abs(x - y) <= SAME_TOLERANCE * max(1.0, abs(x), abs(y))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list | tuple) and isinstance(b, list | tuple):
        return len(a) == len(b) and all(same(i, j) for i, j in zip(a, b, strict=True))
    if x is not None or y is not None:
        return False
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def _canonical(value: Any) -> Any:
    x = _number(value)
    if x is not None:
        return float(f"{x:.9g}") + 0.0  # 5 and 5.0 alike, float noise dropped, no -0.0
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_canonical(v) for v in value]
    return value


def design_key(design: dict[str, Any]) -> str:
    """A key for a design snapshot that is the same for designs that build the same: numbers
    as floats to 9 significant digits."""
    return json.dumps(_canonical(design), sort_keys=True)


@dataclass
class Design:
    """Generator code (defines `build(params)`), its parameters and the parameter locks."""

    code: str
    params: Params
    backend: str = "build123d"
    store: ParameterStore = field(default_factory=ParameterStore)
    # Approvals a person gave for locked values, by path, waiting to be used once.
    approvals: dict[str, Approval] = field(default_factory=dict)

    # --- Locks --------------------------------------------------------------------------------

    def lock(self, path: str, value: Any | None = None, *, by: str = "user") -> None:
        """A hand edit: set `path` (to its current value if none is given) and lock it."""
        if value is None:
            value = get_path(self.params, path)
        self.params = set_path(self.params, path, value)
        self.store.set(path, json.dumps(value), by=by, by_hand=True)

    @property
    def locked(self) -> dict[str, Any]:
        """Locked paths and their values."""
        return {p.name: json.loads(str(p.value)) for p in self.store.locked()}

    @property
    def approvable(self) -> set[str]:
        """Paths an approval can name: the locked parameters, and the code while any are."""
        locked = set(self.locked)
        return locked | {CODE} if locked else locked

    def approve(self, path: str, approval: Approval) -> None:
        if path not in self.approvable:
            raise KeyError(f"{path} is not locked")
        self.approvals[path] = approval

    def code_blocked(self, code: str | None) -> bool:
        """Whether replacing the code with `code` needs an approval it doesn't have: the code
        changes while a parameter is locked."""
        return (
            code is not None
            and code != self.code
            and bool(self.locked)
            and CODE not in self.approvals
        )

    def locked_changes(self, params: Params) -> list[str]:
        """Locked paths whose value `params` would change, and that have no approval."""
        out = []
        for path, value in self.locked.items():
            new = get_path(params, path) if has_path(params, path) else None
            if not same(new, value) and path not in self.approvals:
                out.append(path)
        return out

    def changes_for(self, edits: dict[str, Any]) -> Params:
        """The parameters after a set of path edits, without applying them."""
        params = self.params
        for path, value in edits.items():
            params = set_path(params, path, value)
        return params

    # --- Edits --------------------------------------------------------------------------------

    def apply(self, params: Params, code: str | None = None, *, by: str = AGENT) -> list[str]:
        """Take new parameters, and new code if given. Changed locked values, and changed code
        while any value is locked, need an approval, which is used up; they are refused
        otherwise. Returns the locked paths (and `CODE`) that changed."""
        blocked = self.locked_changes(params)
        if blocked:
            raise LockedParameterError(self.store.get(blocked[0]))
        if self.code_blocked(code):
            raise LockedCodeError(sorted(self.locked))
        changed = []
        for path, value in self.locked.items():
            new = get_path(params, path) if has_path(params, path) else None
            if not same(new, value):
                self.store.set(path, json.dumps(new), by=by, approval=self.approvals.pop(path))
                changed.append(path)
        self.params = params
        if code is not None and code != self.code:
            if self.approvals.pop(CODE, None) is not None:
                changed.append(CODE)
            self.code = code
        return changed

    def layered(self, code: str) -> str:
        """`code` with the features made by hand applied at its end, whatever the code is, so new
        code can't drop them: the sketched features (`params["sketches"]`), then the
        modeled ones (`params["edits"]`)."""
        code = strip_edits(code)
        sketches = self.params.get("sketches")
        if sketches:
            code, _ = sketch_block(code, sketches, self.backend)
        edits = self.params.get("edits")
        if edits:
            code, _ = edits_block(code, edits, self.backend)
        return code

    def snapshot(self) -> dict[str, Any]:
        """What a build runs: the code with the features made by hand (`layered`)."""
        code = self.layered(self.code)
        return {"code": code, "params": copy.deepcopy(self.params), "backend": self.backend}

    def add_sketch(self, sketch: dict[str, Any]) -> str:
        """A feature sketched by hand: added to `params["sketches"]` and to the end of the code,
        and locked (`sketches.<tag>`), so Claude keeps it. Returns the locked path."""
        sketches = [*self.params.get("sketches", []), sketch]
        self.params = {**self.params, "sketches": sketches}
        self.code = self.layered(self.code)
        path = f"sketches.{sketch['tag']}"
        self.lock(path)
        return path

    # --- Modeled features ---------------------------------------------------------------

    def add_edit(self, edit: dict[str, Any]) -> str:
        """A modeling edit made by hand: added to `params["edits"]` and to the end of the code,
        and locked (`edits.<tag>`), so Claude keeps it. Returns the locked path."""
        self.params = {**self.params, "edits": [*self.params.get("edits", []), edit]}
        self.code = self.layered(self.code)
        path = f"edits.{edit['tag']}"
        self.lock(path)
        return path

    def replace_edit(self, edit: dict[str, Any], approval: Approval) -> None:
        """A person changed (or suppressed) a modeling edit: still locked, at its new values."""
        edits = [edit if e["tag"] == edit["tag"] else e for e in self.params.get("edits", [])]
        self.params = {**self.params, "edits": edits}
        self.code = self.layered(self.code)
        self.store.set(
            f"edits.{edit['tag']}", json.dumps(edit), by="user", by_hand=True, approval=approval
        )

    def remove_edits(self, tags: list[str], approval: Approval) -> None:
        """A person deleted modeling edits. Their locks stay, on no value, so their names are
        never reused and Claude can't put them back."""
        edits = [e for e in self.params.get("edits", []) if e["tag"] not in tags]
        self.params = {**self.params, "edits": edits}
        self.code = self.layered(self.code)
        for tag in tags:
            path = f"edits.{tag}"
            if path in self.store:
                self.store.set(path, json.dumps(None), by="user", by_hand=True, approval=approval)
