"""The product metrics over every recorded session.

```sh
cd backend && .venv/bin/python -m app.telemetry report            # Markdown
cd backend && .venv/bin/python -m app.telemetry report --json     # the numbers
```

- **Proposals accepted without edits**: proposals whose changes were all kept (none rejected)
  with no hand edit before the next run.
- **Rounds to pass**: fix rounds of the runs that ended with every check passing.
- **Asks vs. guesses**: runs that asked the user something vs. runs that proposed changes
  without asking.
- **Checks that fail most**: how many builds failed each check.
- **Where users override Claude**: hand edits by parameter and rejected changes.
- **Cost per accepted change**: the runs' total cost over the changes kept.
"""

from __future__ import annotations

import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from app.telemetry.log import log_dirs, read_log


def load_sessions(directory: Path) -> list[list[dict]]:
    """The logs under `directory`, oldest first."""
    return [read_log(folder) for folder in log_dirs(directory)]


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 2) if values else None


def _proposals(events: list[dict]) -> list[dict]:
    """Each proposal with the changes finally kept and whether the user edited around it."""
    out: list[dict] = []
    current: dict | None = None
    for e in events:
        kind = e["kind"]
        if kind == "proposal":
            current = {
                "changes": list(e["changes"]),
                "kept": set(e["accepted"]),
                "rejected": set(),
                "edited": False,
                "decisions": [],
            }
            out.append(current)
        elif current is None:
            continue
        elif kind == "change_decision":
            if e["decision"] == "accept":
                current["kept"].add(e["change_id"])
                current["rejected"].discard(e["change_id"])
            else:
                current["kept"].discard(e["change_id"])
                current["rejected"].add(e["change_id"])
            if e.get("since_proposal_ms") is not None:
                current["decisions"].append(e["since_proposal_ms"])
        elif kind == "parameter_edit":
            current["edited"] = True
        elif kind == "run_start":
            current = None
    return out


def metrics(sessions: list[list[dict]]) -> dict[str, Any]:
    runs = [e for s in sessions for e in s if e["kind"] == "run_end"]
    proposals = [p for s in sessions for p in _proposals(s)]
    clean = [p for p in proposals if p["kept"] and not p["rejected"] and not p["edited"]]
    kept = sum(len(p["kept"]) for p in proposals)
    passing = [r["rounds"] for r in runs if r["passing"]]
    failing = Counter(c for s in sessions for e in s if e["kind"] == "checks" for c in e["failing"])
    edits = Counter(e["name"] for s in sessions for e in s if e["kind"] == "parameter_edit")
    rejected = Counter(c for p in proposals for c in p["rejected"])
    costs = [r["cost_usd"] for r in runs if r.get("cost_usd") is not None]
    total_cost = round(sum(costs), 6)
    abandoned = 0
    for s in sessions:
        started = sum(e["kind"] == "run_start" for e in s)
        ended = sum(e["kind"] == "run_end" for e in s)
        if any(e["kind"] == "abandoned" for e in s) or started > ended:
            abandoned += 1
    answers = [
        e["since_question_ms"]
        for s in sessions
        for e in s
        if e["kind"] == "answer" and e.get("since_question_ms") is not None
    ]
    return {
        "sessions": len(sessions),
        "runs": len(runs),
        "abandoned_sessions": abandoned,
        "proposals": {
            "total": len(proposals),
            "accepted_without_edits": len(clean),
            "rate": round(len(clean) / len(proposals), 3) if proposals else None,
            "median_ms_to_decision": _median([d for p in proposals for d in p["decisions"]]),
        },
        "rounds_to_pass": {
            "runs": len(passing),
            "mean": _mean(passing),
            "median": _median(passing),
            "max": max(passing) if passing else None,
        },
        "asks_vs_guesses": {
            "asks": sum(1 for r in runs if r["questions"] > 0),
            "guesses": sum(1 for r in runs if r["questions"] == 0 and r["changes"] > 0),
            "questions": sum(r["questions"] for r in runs),
            "median_ms_to_answer": _median(answers),
        },
        "failing_checks": failing.most_common(),
        "overrides": {
            "parameter_edits": edits.most_common(),
            "rejected_changes": rejected.most_common(),
        },
        "cost": {
            "total_usd": total_cost,
            "input_tokens": sum(r.get("input_tokens") or 0 for r in runs),
            "output_tokens": sum(r.get("output_tokens") or 0 for r in runs),
            "accepted_changes": kept,
            "per_accepted_change_usd": round(total_cost / kept, 6) if kept else None,
            "median_run_ms": _median([r["latency_ms"] for r in runs]),
        },
        "errors": Counter(
            e["source"] for s in sessions for e in s if e["kind"] == "error"
        ).most_common(),
    }


def _pairs(items: list) -> str:
    return ", ".join(f"{k} ({n})" for k, n in items) or "none"


def render(m: dict[str, Any]) -> str:
    p, r, a, c, o = (
        m["proposals"],
        m["rounds_to_pass"],
        m["asks_vs_guesses"],
        m["cost"],
        m["overrides"],
    )
    lines = [
        "# Telemetry report",
        "",
        f"{m['sessions']} sessions, {m['runs']} runs, {m['abandoned_sessions']} abandoned.",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Proposals accepted without edits | {p['accepted_without_edits']} of {p['total']}"
        f" ({p['rate'] if p['rate'] is not None else 'n/a'}) |",
        f"| Median time from proposal to decision (ms) | {p['median_ms_to_decision']} |",
        f"| Rounds to pass (mean / median / max over {r['runs']} runs) | "
        f"{r['mean']} / {r['median']} / {r['max']} |",
        f"| Asks vs. guesses | {a['asks']} vs. {a['guesses']} ({a['questions']} questions) |",
        f"| Checks that fail most | {_pairs(m['failing_checks'][:5])} |",
        f"| Hand edits (overrides) | {_pairs(o['parameter_edits'][:5])} |",
        f"| Rejected changes | {_pairs(o['rejected_changes'][:5])} |",
        f"| Cost per accepted change (USD) | {c['per_accepted_change_usd']} |",
        f"| Total cost (USD) / tokens in / out | {c['total_usd']} / {c['input_tokens']} / "
        f"{c['output_tokens']} |",
        f"| Median run time (ms) | {c['median_run_ms']} |",
        f"| Errors by source | {_pairs(m['errors'])} |",
    ]
    return "\n".join(lines) + "\n"
