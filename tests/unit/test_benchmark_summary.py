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


def test_summary_labels_candidate_and_reports_comparison_measurements(tmp_path: Path) -> None:
    path = tmp_path / "metrics.json"
    _report(path)
    report = json.loads(path.read_text())
    worker = report["results"][0]
    worker["experiment"] = "combined"
    for run in worker["runs"]:
        run["metrics"].update(elapsed_seconds=50.0, peak_working_set_bytes=1024)
        for event in run["metrics"]["generations"]:
            event.update(prompt_tokens=100, generated_tokens=20,
                         first_token_seconds=1.0, decode_tokens_per_second=4.0)
    path.write_text(json.dumps(report))
    summary = summarize_micro_benchmark(path)
    assert "Experiment: combined" in summary
    assert "Calls=6 RetryPercent=100.0 PromptTokens=600.00 GeneratedTokens=120.00" in summary
    assert "MedianRequestSeconds=50.00 MedianFirstTokenSeconds=1.00" in summary
    assert "MedianDecodeTokensPerSecond=4.00 PeakWorkingSetBytes=1024.00" in summary
