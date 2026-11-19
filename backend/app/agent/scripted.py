"""A driver that plays a fixed script instead of calling Claude, for tests without a model.

It does what the Claude Code CLI does with each step: text becomes an assistant message; a
tool call goes through the PreToolUse hooks (a deny skips the tool), then the tool, then the
PostToolUse hooks. The results are kept in `calls` for assertions.

```python
driver = ScriptedDriver(ws, [("text", "Looking."), ("run_checks", {}), ("finish", {...})])
await run_agent(ws, "prompt", driver=driver)
```

A step's payload can be a function of the workspace, called when the step plays, so a step
after `ask_user` can follow the answer; a function that returns None skips the step.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.agent.tools import Workspace, tool_name


@dataclass
class Call:
    tool: str
    args: dict
    denied: str | None = None  # the PreToolUse hook's reason
    result: dict | None = None
    context: list[str] = field(default_factory=list)  # PostToolUse additionalContext

    @property
    def text(self) -> str:
        if self.denied:
            return self.denied
        assert self.result is not None
        return "".join(c.get("text", "") for c in self.result["content"])

    @property
    def is_error(self) -> bool:
        return bool(self.denied) or bool(self.result and self.result.get("is_error"))


# A design written from scratch, as Claude would write it for "make a box": an open box whose
# walls and floor are `wall` thick, every size a parameter. Scripted runs and tests use it.
BOX_CODE = """\
from build123d import Box, Pos


def build(params):
    length, width, height = params["length"], params["width"], params["height"]
    wall = params["wall"]
    outer = Pos(0, 0, height / 2) * Box(length, width, height)
    depth = height - wall + 1  # out through the open top
    cavity = Pos(0, 0, wall + depth / 2) * Box(length - 2 * wall, width - 2 * wall, depth)
    return {
        "parts": {"box": outer - cavity},
        "features": [{"tag": "box", "part": "box", "tools": [outer, cavity]}],
        "materials": {"box": params["material"]},
    }
"""


def box_design(
    length: float, width: float, height: float, wall: float, material: str
) -> tuple[str, dict]:
    """`write_design` arguments for an open box: the code and its parameters (mm)."""
    params = {
        "length": length,
        "width": width,
        "height": height,
        "wall": wall,
        "material": material,
    }
    return BOX_CODE, params


# "An L bracket with two M4 holes": a base plate and an upright, a clearance hole in each.
BRACKET_CODE = """\
from build123d import Box, Cylinder, Pos, Rot


def build(params):
    length, width, height = params["length"], params["width"], params["height"]
    t, d = params["thickness"], params["hole_diameter"]
    base = Pos(length / 2, 0, t / 2) * Box(length, width, t)
    upright = Pos(t / 2, 0, height / 2) * Box(t, width, height)
    hole_1 = Pos((length + t) / 2, 0, t / 2) * Cylinder(d / 2, t + 2)
    hole_2 = Pos(t / 2, 0, (height + t) / 2) * Rot(0, 90, 0) * Cylinder(d / 2, t + 2)
    return {
        "parts": {"bracket": base + upright - hole_1 - hole_2},
        "features": [
            {"tag": "bracket", "part": "bracket", "tools": [base, upright]},
            {"tag": "hole_1", "part": "bracket", "tools": [hole_1]},
            {"tag": "hole_2", "part": "bracket", "tools": [hole_2]},
        ],
        "materials": {"bracket": params["material"]},
    }
"""


def bracket_design(
    length: float,
    width: float,
    height: float,
    thickness: float,
    hole_diameter: float,
    material: str,
) -> tuple[str, dict]:
    """`write_design` arguments for an L bracket with a hole in each leg (mm)."""
    params = {
        "length": length,
        "width": width,
        "height": height,
        "thickness": thickness,
        "hole_diameter": hole_diameter,
        "material": material,
    }
    return BRACKET_CODE, params


class ScriptedDriver:
    def __init__(self, ws: Workspace, steps: list[tuple[str, Any]]):
        self.ws = ws
        self.steps = steps
        self.calls: list[Call] = []
        self.options: Any = None
        self.prompt: str | None = None  # as the run gave it: an `app.agent.photo.Prompt`
        # with the message's photos

    def __call__(self, prompt: str, options: Any) -> AsyncIterator[Any]:
        self.options = options
        self.prompt = prompt
        return self._play()

    async def _hooks(self, event: str, name: str, data: dict) -> list[dict]:
        out = []
        for matcher in (self.options.hooks or {}).get(event, []):
            if matcher.matcher and not re.fullmatch(matcher.matcher, name):
                continue
            for hook in matcher.hooks:
                out.append(await hook(data, None, {"signal": None}))
        return out

    async def _play(self) -> AsyncIterator[Any]:
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

        handlers = self.ws.handlers()
        for n, (kind, payload) in enumerate(self.steps):
            if callable(payload):  # a step that follows the run so far (an answer)
                payload = payload(self.ws)
                if payload is None:  # nothing to do at this point of this run
                    continue
            if kind == "text":
                yield AssistantMessage(content=[TextBlock(payload)], model="scripted")
                continue
            name = tool_name(kind)
            yield AssistantMessage(
                content=[ToolUseBlock(id=f"tool-{n}", name=name, input=payload)],
                model="scripted",
            )
            call = Call(kind, payload)
            self.calls.append(call)
            data = {"hook_event_name": "PreToolUse", "tool_name": name, "tool_input": payload}
            for out in await self._hooks("PreToolUse", name, data):
                spec = out.get("hookSpecificOutput") or {}
                if spec.get("permissionDecision") == "deny":
                    call.denied = spec.get("permissionDecisionReason", "denied")
            if call.denied:
                continue
            call.result = await handlers[kind](payload)
            data = {**data, "hook_event_name": "PostToolUse", "tool_response": call.result}
            for out in await self._hooks("PostToolUse", name, data):
                context = (out.get("hookSpecificOutput") or {}).get("additionalContext")
                if context:
                    call.context.append(context)
        yield ResultMessage(
            subtype="success",
            duration_ms=0,
            duration_api_ms=0,
            is_error=False,
            num_turns=len(self.steps),
            session_id="scripted",
            # Made-up usage, so telemetry has tokens and cost to record.
            usage={"input_tokens": 1000 * len(self.steps), "output_tokens": 100 * len(self.steps)},
            total_cost_usd=0.001 * len(self.steps),
        )
