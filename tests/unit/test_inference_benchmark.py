from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from portfolio_analyzer.qwen import benchmark
from portfolio_analyzer.qwen.pipeline import build_logical_units


def test_synthetic_workloads_have_stable_coverage() -> None:
    counts = {
        name: len(build_logical_units(benchmark.synthetic_bundle(name)))
        for name in benchmark.WORKLOADS
    }
    assert counts == {"mixed": 6, "procedures": 30, "oversized": 7, "duplicates": 12}
    assert benchmark.synthetic_bundle().model_dump() == benchmark.synthetic_bundle().model_dump()
    with pytest.raises(ValueError):
        benchmark.synthetic_bundle("unapproved-path")


def test_matrix_runs_fresh_processes_and_only_exports_worker_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        count = command[command.index("--threads") + 1]
        assert kwargs["env"]["OMP_NUM_THREADS"] == count
        assert kwargs["env"]["MKL_NUM_THREADS"] == count
        destination = Path(command[command.index("--output") + 1])
        assert destination.parent != tmp_path
        destination.write_text(json.dumps({"threads_requested": int(count)}))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(benchmark.subprocess, "run", run)
    destination = tmp_path / "matrix.json"
    assert benchmark.run_matrix(
        model_dir=tmp_path / "model", threads=(1, 2, 3, 4), repetitions=3,
        output=destination, suite="micro", workload="mixed",
    )
    assert len(calls) == 4
    assert all(call[1:3] == ["-m", "portfolio_analyzer.qwen.benchmark"] for call in calls)
    result = json.loads(destination.read_text())
    assert [item["threads_requested"] for item in result["results"]] == [1, 2, 3, 4]
    assert str(tmp_path) not in destination.read_text()
    assert not Path(calls[0][-1]).exists()


def test_failed_worker_has_safe_diagnostics_and_does_not_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, stderr="PRIVATE EXCEPTION VALUE")

    monkeypatch.setattr(benchmark.subprocess, "run", fail)
    destination = tmp_path / "matrix.json"
    assert not benchmark.run_matrix(
        model_dir=tmp_path / "model", threads=(1, 2), repetitions=1,
        output=destination, suite="micro", workload="mixed",
    )
    result = json.loads(destination.read_text())
    assert result["results"] == [{"threads_requested": 1, "outcome": "worker_failed"}]
    assert "PRIVATE" not in destination.read_text()


def test_matrix_refuses_model_directory_and_existing_output(tmp_path: Path) -> None:
    for destination in (tmp_path / "model" / "extra.json", tmp_path / "existing.json"):
        if destination.name == "existing.json":
            destination.write_text("keep")
        with pytest.raises(ValueError):
            benchmark.run_matrix(
                model_dir=tmp_path / "model", threads=(4,), repetitions=1,
                output=destination, suite="micro", workload="mixed",
            )
    assert (tmp_path / "existing.json").read_text() == "keep"
