"""Hooks on the agent's tool calls.

- `guard_locks` (PreToolUse on the edit tools) denies an edit that would change a locked value
  without the user's approval, any change to the generator code while a value is locked
  (new code could override the locked value in the geometry) without the user's approval, and
  any edit once the fix rounds are used up, any edit after Claude refused the request, and any
  edit on a new design before Claude has proposed the build plan (`propose_plan`).
- `recheck` (PostToolUse on the edit tools) reruns the build and the checks after every edit
  and hands the report to Claude, so a fix is always judged by the checks.

The tools refuse the same edits themselves (and the parameter store refuses changes to locked
values), so a hook that doesn't fire still can't let a locked value through.
"""

from __future__ import annotations

import json
from typing import Any

from app.agent.design import LockedCodeError, PathError
from app.agent.tools import (
    _CODE_HINT,
    _PLAN_FIRST,
    EDIT_TOOLS,
    REFUSED,
    TOOLS,
    Workspace,
    _out_of_rounds,
    tool_name,
)


def _short(name: str) -> str:
    return name.rsplit("__", 1)[-1]


def deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def make_hooks(ws: Workspace) -> dict[str, Any]:
    """The hooks for one workspace, as `ClaudeAgentOptions.hooks` expects them."""

    async def guard_locks(input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        out = _guard(input_data)
        reason = (out.get("hookSpecificOutput") or {}).get("permissionDecisionReason")
        if reason:  # the tool never runs: record the refused call for telemetry
            ws.observe(
                "tool_call",
                tool=_short(input_data.get("tool_name", "")),
                input=input_data.get("tool_input") or {},
                is_error=True,
                denied=reason,
                latency_ms=0,
            )
        return out

    def _guard(input_data: dict) -> dict:
        tool = _short(input_data.get("tool_name", ""))
        if tool not in EDIT_TOOLS:
            return {}
        if ws.refused:
            return deny(REFUSED)
        if ws.needs_plan:
            return deny(_PLAN_FIRST)
        if ws.rounds >= ws.max_rounds:
            return deny(_out_of_rounds(ws.max_rounds))
        try:
            params = ws.edit_params(tool, input_data.get("tool_input") or {})
        except (PathError, ValueError, KeyError, TypeError):
            return {}  # the tool reports the bad input itself
        blocked = ws.design.locked_changes(params)
        if blocked:
            return deny(
                f"{', '.join(blocked)} {'is' if len(blocked) == 1 else 'are'} locked: set by "
                "hand, a constraint to design around. Don't change it. If the design can't "
                "meet the checks without changing it, ask the user (ask_user) with an option "
                "that approves the change."
            )
        code = (input_data.get("tool_input") or {}).get("code")
        if tool == "write_design" and ws.design.code_blocked(code):
            return deny(f"{LockedCodeError(sorted(ws.design.locked))}. {_CODE_HINT}")
        return {}

    async def recheck(input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        tool = _short(input_data.get("tool_name", ""))
        if tool not in EDIT_TOOLS or not ws.needs_recheck:
            return {}  # not an edit, or the edit was refused and the last report stands
        ws.needs_recheck = False
        ws.progress("Rerunning the checks")
        report = await ws.check()
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": "Checks after this edit:\n" + json.dumps(report, default=str),
            }
        }

    ours = {tool_name(t) for t in TOOLS}

    async def only_app_tools(input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        """Deny any tool that isn't one of the app's: the options already leave the built-in
        tools out and deny them by name; this holds even if a CLI default changes."""
        name = input_data.get("tool_name", "")
        if name in ours:
            return {}
        return deny(f"{name} is not available here. Use only the hardware tools.")

    from claude_agent_sdk import HookMatcher

    matcher = "|".join(tool_name(t) for t in EDIT_TOOLS)
    return {
        "PreToolUse": [
            HookMatcher(matcher=None, hooks=[only_app_tools]),
            HookMatcher(matcher=matcher, hooks=[guard_locks]),
        ],
        # A build and a full check run take seconds; the default hook timeout is 60 s.
        "PostToolUse": [HookMatcher(matcher=matcher, hooks=[recheck], timeout=600)],
    }
