"""The agent-eval report lists every run, pass or fail."""

from __future__ import annotations

from test_agent_live import print_report, run_lines

from app.agent.run import RunResult


def outcomes():
    return [
        (None, RunResult(finished={"outcome": "done"}, rounds=2, passing=True, questions=0)),
        (None, RunResult(finished=None, rounds=4, passing=False, questions=1, error="timeout")),
    ]


def test_every_run_has_a_line_with_its_rounds():
    assert run_lines(outcomes()) == [
        "run 1: outcome=done rounds=2 passing=True",
        "run 2: outcome=none rounds=4 passing=False error=timeout",
    ]


def test_report_goes_to_stdout_and_the_job_summary(tmp_path, monkeypatch, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    print_report(outcomes(), 1)
    out = capsys.readouterr().out
    assert "1 of 2 runs passed within 4 rounds" in out and "run 2: outcome=none" in out
    assert "- run 1: outcome=done rounds=2 passing=True" in summary.read_text()
