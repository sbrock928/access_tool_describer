import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from portfolio_analyzer.cli.v2 import app
from portfolio_analyzer.qwen.benchmark_summary import summarize_micro_benchmark


def _report(path: Path, *, old: bool = False, unsafe: bool = False) -> None:
    runs = []
    for case in (1, 2, 3):
        events = []
        for attempt in (1, 2):
            event = {
                "attempt": attempt, "generation_seconds": float(case * 10),
                "outcome": "valid" if case == 2 and attempt == 2 else "invalid",
            }
            if not old:
                event["validation_issues"] = [] if event["outcome"] == "valid" else [{
                    "category": "missing_field", "field": "SECRET" if unsafe else "status",
                    "count": 1,
                }]
            events.append(event)
        runs.append({
            "warmup": False, "case": case, "repetition": 1,
            "schema_valid": case == 2, "metrics": {"generations": events},
            "ignored_content": "SECRET",
        })
    runs.append({**runs[0], "warmup": True, "schema_valid": True})
    path.write_text(json.dumps({
        "schema_version": "inference-benchmark-matrix-v1",
        "results": [{"threads_requested": 4, "suite": "micro", "runs": runs}],
    }))


def test_summary_counts_requests_and_repairs_separately(tmp_path: Path) -> None:
    path = tmp_path / "metrics.json"
    _report(path)
    summary = summarize_micro_benchmark(path)
    assert summary.splitlines()[3].split() == ["4", "3", "1", "3", "0", "40.00"]
    assert "missing_field:status=1" in summary
    assert "valid   none" in summary
    assert "SECRET" not in summary
    assert len(summary.splitlines()) == 13


def test_old_report_cannot_recover_field_diagnostics(tmp_path: Path) -> None:
    path = tmp_path / "metrics.json"
    _report(path, old=True)
    assert "not_recorded_in_older_report" in summarize_micro_benchmark(path)


@pytest.mark.parametrize("unsafe", [False, True])
def test_summary_cli_never_echoes_invalid_report_content(tmp_path: Path, unsafe: bool) -> None:
    path = tmp_path / "metrics.json"
    if unsafe:
        _report(path, unsafe=True)
    else:
        path.write_text("SECRET malformed JSON")
    result = CliRunner().invoke(app, ["benchmark-summary", "--input", str(path)])
    assert result.exit_code == 1
    assert "no report contents displayed" in result.output
    assert "SECRET" not in result.output
