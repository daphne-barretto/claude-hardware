"""Where generator code creates each feature: the line range of the statement that made it.

Generator code makes features in loops and through helpers, so the place a feature comes from
can't be found by searching the code for its tag. Instead the code is instrumented before it
runs: every dict literal (or `dict(...)` call) with a `tag` key is wrapped so that, when it is
created, the statement that caused it is recorded against that dict. `GeometryBackend.build`
then looks up each returned feature's dict.

The statement is:

- the statement that creates the dict, such as `features.append({"tag": ..., ...})` in
  `build`, or
- when the dict is made inside a helper that only passes its `tag` argument through, such as
  `def feature(tag, part, *tools)`, the statement that called the helper:
  `feature(boss["tag"], "lid", tool)`. This repeats through helpers that call helpers.

Lines are 1-based and inclusive, in the code as given.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass, field
from typing import Any

Lines = tuple[int, int]
HOOK = "__feature_site__"

_Func = ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda


@dataclass
class FeatureSites:
    """Instrumented code and, once it has run, the line range each feature dict came from."""

    tree: ast.Module
    filename: str
    passthrough: set[tuple[str, int]]
    statements: list[tuple[int, int, int]]  # (start, end, depth) of every statement
    _sites: dict[int, tuple[Any, Lines]] = field(default_factory=dict)

    def lines(self, obj: Any) -> Lines | None:
        entry = self._sites.get(id(obj))
        return entry[1] if entry and entry[0] is obj else None

    def hook(self, value: Any) -> Any:
        frame = sys._getframe(1)
        while (
            _key(frame.f_code) in self.passthrough
            and frame.f_back is not None
            and frame.f_back.f_code.co_filename == self.filename
        ):
            frame = frame.f_back
        lines = self._statement(_position(frame))
        if lines is not None:
            self._sites[id(value)] = (value, lines)  # keep it alive so the id stays unique
        return value

    def _statement(self, span: Lines | None) -> Lines | None:
        if span is None:
            return None
        start, end = span
        best = None
        for s, e, depth in self.statements:
            if s <= start and end <= e:
                key = (e - s, -depth)
                if best is None or key < best[0]:
                    best = (key, (s, e))
        return best[1] if best else span


def instrument(code: str, filename: str) -> FeatureSites:
    """Parse the code and wrap its feature dicts. Raises SyntaxError like `compile`."""
    tree = ast.parse(code, filename)
    passthrough = _passthrough(tree)
    statements = _statements(tree)
    _Wrap().visit(tree)
    ast.fix_missing_locations(tree)
    keys = {_def_key(f) for f in passthrough}
    return FeatureSites(tree, filename, keys, statements)


def _is_feature_dict(node: ast.AST) -> ast.expr | None:
    """The `tag` value expression if the node builds a dict with a `tag` key."""
    if isinstance(node, ast.Dict):
        for k, v in zip(node.keys, node.values, strict=True):
            if isinstance(k, ast.Constant) and k.value == "tag":
                return v
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict":
        for kw in node.keywords:
            if kw.arg == "tag":
                return kw.value
    return None


class _Wrap(ast.NodeTransformer):
    def visit(self, node: ast.AST) -> Any:
        node = self.generic_visit(node)
        if _is_feature_dict(node) is not None:
            call = ast.Call(ast.Name(HOOK, ast.Load()), [node], [])
            return ast.copy_location(call, node)
        return node


def _functions(tree: ast.Module) -> list[_Func]:
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda)
    ]


def _own_nodes(func: _Func) -> list[ast.AST]:
    """Nodes in the function's body, not inside nested functions."""
    out: list[ast.AST] = []
    body = func.body if isinstance(func.body, list) else [func.body]
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        out.append(node)
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                stack.append(child)
    return out


def _params(func: _Func) -> set[str]:
    a = func.args
    names = [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]
    return {n.arg for n in names if n is not None}


def _passthrough(tree: ast.Module) -> list[_Func]:
    """Functions that make a feature whose tag is one of their arguments, directly or by calling
    another such function (by name, including `feature = lambda tag, ...: ...`)."""
    functions = _functions(tree)
    found: list[_Func] = []
    changed = True
    while changed:
        changed = False
        names = _names(tree, found)
        for func in functions:
            if func in found:
                continue
            params = _params(func)
            for node in _own_nodes(func):
                tag = _is_feature_dict(node)
                if tag is None:
                    tag = _helper_tag(node, names)
                if isinstance(tag, ast.Name) and tag.id in params:
                    found.append(func)
                    changed = True
                    break
    return found


def _names(tree: ast.Module, found: list[_Func]) -> set[str]:
    """The names the found functions can be called by."""
    names = {f.name for f in found if not isinstance(f, ast.Lambda)}
    lambdas = [f for f in found if isinstance(f, ast.Lambda)]
    if lambdas:
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and node.value in lambdas:
                names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    return names


def _helper_tag(node: ast.AST, helpers: set[str]) -> ast.expr | None:
    """The tag passed to a passthrough helper: its first argument or `tag=`."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
        return None
    if node.func.id not in helpers:
        return None
    for kw in node.keywords:
        if kw.arg == "tag":
            return kw.value
    return node.args[0] if node.args else None


def _def_key(func: _Func) -> tuple[str, int]:
    if isinstance(func, ast.Lambda):
        return ("<lambda>", func.lineno)
    first = min([func.lineno, *(d.lineno for d in func.decorator_list)])
    return (func.name, first)


def _key(code: Any) -> tuple[str, int]:
    return (code.co_name, code.co_firstlineno)


def _statements(tree: ast.Module) -> list[tuple[int, int, int]]:
    out = []

    def walk(node: ast.AST, depth: int) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                out.append((child.lineno, child.end_lineno or child.lineno, depth))
            walk(child, depth + 1)

    walk(tree, 0)
    return out


def _position(frame: Any) -> Lines | None:
    """The line span of the instruction the frame is running."""
    index = frame.f_lasti // 2
    for i, pos in enumerate(frame.f_code.co_positions()):
        if i == index:
            start, end = pos[0], pos[1]
            if start is None:
                break
            return (start, end or start)
    return (frame.f_lineno, frame.f_lineno) if frame.f_lineno else None
