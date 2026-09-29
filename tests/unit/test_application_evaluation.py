from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from openpyxl import Workbook
from typer.testing import CliRunner

from portfolio_analyzer.access.windows_extractor import WindowsAccessExtractor
from portfolio_analyzer.cli import v2
from portfolio_analyzer.models import AccessExtractedObject, AccessExtractionResult
from portfolio_analyzer.qwen import application_evaluation as evaluation
from portfolio_analyzer.qwen.benchmark_summary import summarize_micro_benchmark
from portfolio_analyzer.qwen.provider import PromptBudget
from portfolio_analyzer.v2.models import ModelProvenance


@pytest.fixture
def environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    source = tmp_path / "source.accdb"
    source.write_bytes(b"synthetic-access")
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["INVENTORY_ID", "EUCTNAME", "FILE_NAME", "FULLPATH", "DESCRIPTION"])
    sheet.append(["PRIVATE-APP", "PRIVATE-NAME", source.name, str(source), "PRIVATE-CLAIM"])
    inventory = tmp_path / "inventory.xlsx"
    workbook.save(inventory)
    workspace = tmp_path / "workspace"
    runner = CliRunner()
    assert runner.invoke(v2.app, ["stage", "--inventory", str(inventory),
                                  "--workspace", str(workspace)]).exit_code == 0

    def extract(artifact: Any, *_args: Any, **_kwargs: Any) -> AccessExtractionResult:
        return AccessExtractionResult(
            tool_inventory_id=artifact.tool_inventory_id, artifact_id=artifact.artifact_id,
            staged_path=artifact.local_staged_path,
            extractor_version=WindowsAccessExtractor.version,
            objects=[AccessExtractedObject(object_type="query", name="PRIVATE-QUERY",
                                           definition="SELECT Id FROM Synthetic")],
        )

    monkeypatch.setattr(v2.platform, "system", lambda: "Windows")
    monkeypatch.setattr(v2, "run_extraction_with_timeout", extract)
    assert runner.invoke(v2.app, ["extract", "--workspace", str(workspace)]).exit_code == 0
    state: dict[str, Any] = {"calls": [], "interrupt": False}

    class Provider:
        def __init__(self, _runtime: Any, *, progress: Any) -> None:
            self.progress = progress
            self.performance = progress.performance
            self.verified = SimpleNamespace(manifest=SimpleNamespace(manifest_sha256="a" * 64))

        def measure_prompt(self, **kwargs: Any) -> PromptBudget:
            return PromptBudget(
                prompt_tokens=100, reserved_output_tokens=kwargs["max_output_tokens"],
                context_tokens=100_000,
            )

        def complete_json(self, **kwargs: Any) -> dict[str, Any]:
            if state["interrupt"] and state["calls"]:
                state["interrupt"] = False
                raise KeyboardInterrupt()
            payload = json.loads(kwargs["user"])
            self.performance.begin_generation(user=kwargs["user"], prompt_tokens=100,
                                              output_limit=kwargs["max_output_tokens"])
            state["calls"].append(payload["stage"])
            if payload["stage"] == "logical_unit":
                return {"purpose": "Read records", "evidence_ids":
                        payload["allowed_ids"]["evidence_ids"][:1]}
            return {"summary": "Read records", "business_purpose": "Unknown",
                    "evidence_ids": payload["allowed_ids"]["evidence_ids"][:1]}

    monkeypatch.setattr(evaluation, "LocalQwenProvider", Provider)
    monkeypatch.setattr(v2, "_model_provenance", lambda provider: ModelProvenance(
        model_manifest_sha256="a" * 64, prompt_version=provider.experiment.identity,
        output_schema_version="test-v2", inference_library_version="test-transformers",
    ))
    model = tmp_path / "model"
    model.mkdir()
    state.update(workspace=workspace, evaluation_dir=tmp_path / "evaluation", model_dir=model)
    return state


def _arguments(environment: dict[str, Any]) -> dict[str, Any]:
    return {key: environment[key] for key in ("workspace", "evaluation_dir", "model_dir")} | {
        "experiment": "contract-private",
    }


@pytest.mark.parametrize("stop", ["limit", "interrupt"])
def test_isolated_evaluation_preserves_source_and_resumes(
    environment: dict[str, Any], stop: str,
) -> None:
    args = _arguments(environment)
    workspace = args["workspace"]
    before = {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob("*") if p.is_file()}
    environment["interrupt"] = stop == "interrupt"
    report, code = evaluation.evaluate_applications(
        **args, max_calls=1 if stop == "limit" else None,
    )
    assert code == (2 if stop == "limit" else 130)
    assert report["applications_remaining"] == 1
    assert list(args["evaluation_dir"].rglob("inference-cache/**/*.json"))
    first_calls = len(environment["calls"])
    report, code = evaluation.evaluate_applications(**args, resume=True)
    assert code == 0
    assert len(environment["calls"]) == first_calls + 1
    assert report["applications"][0]["units_expected"] == 1
    assert report["applications"][0]["units_completed"] == 1
    assert report["metrics"]["cache"]["hits"] >= 1
    assert report["portfolio_synthesis"] == "not_evaluated"
    after = {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob("*") if p.is_file()}
    assert after == before
    metrics_path = args["evaluation_dir"] / "metrics-0002.json"
    assert "PRIVATE" not in metrics_path.read_text()
    summary = summarize_micro_benchmark(metrics_path)
    assert "ApplicationsRemaining=0" in summary
    assert "PRIVATE" not in summary
    assert (args["evaluation_dir"] / "LOCAL-REVIEW-0002.json").exists()


def test_existing_target_and_policy_mismatch_are_refused(environment: dict[str, Any]) -> None:
    args = _arguments(environment)
    assert evaluation.evaluate_applications(**args)[1] == 0
    with pytest.raises(ValueError):
        evaluation.evaluate_applications(**args)
    with pytest.raises(ValueError):
        evaluation.evaluate_applications(**{**args, "experiment": "baseline"}, resume=True)
    with pytest.raises(ValueError):
        evaluation.evaluate_applications(**args, resume=True, threads=3)


@pytest.mark.parametrize("protected", ["workspace", "model_dir"])
def test_evaluation_rejects_nested_output_before_writes(
    environment: dict[str, Any], protected: str,
) -> None:
    args = _arguments(environment)
    destination = args[protected] / "evaluation"
    with pytest.raises(ValueError):
        evaluation.evaluate_applications(**{**args, "evaluation_dir": destination})
    assert not destination.exists()


def test_evaluation_cache_tampering_fails_without_exporting_exception(
    environment: dict[str, Any],
) -> None:
    args = _arguments(environment)
    assert evaluation.evaluate_applications(**args)[1] == 0
    for path in args["evaluation_dir"].rglob("inference-cache/**/*.json"):
        path.write_text("PRIVATE-TAMPERED")
    report, code = evaluation.evaluate_applications(**args, resume=True)
    assert code == 1
    assert report["outcome"] == "failed"
    assert "PRIVATE" not in json.dumps(report)



def test_cli_prints_safe_summary_and_refuses_changed_selection(environment: dict[str, Any]) -> None:
    args = _arguments(environment)
    command = ["evaluate-applications", "--workspace", str(args["workspace"]),
               "--model-dir", str(args["model_dir"]),
               "--evaluation-dir", str(args["evaluation_dir"])]
    result = CliRunner().invoke(v2.app, command)
    assert result.exit_code == 0, result.output
    assert "ApplicationsRemaining=0" in result.output
    assert "PRIVATE" not in result.output
    identity = args["evaluation_dir"] / "evaluation-identity.json"
    value = json.loads(identity.read_text())
    value["source"] = "b" * 64
    identity.write_text(json.dumps(value))
    result = CliRunner().invoke(v2.app, command + ["--resume"])
    assert result.exit_code == 1
    assert "setup failed" in result.output
    assert "PRIVATE" not in result.output


def test_application_summary_accepts_other_schema_fields_without_displaying_content(
    environment: dict[str, Any],
) -> None:
    args = _arguments(environment)
    assert evaluation.evaluate_applications(**args)[1] == 0
    path = args["evaluation_dir"] / "metrics-0001.json"
    report = json.loads(path.read_text())
    report["metrics"]["generations"][0]["validation_issues"] = [
        {"field": "business_purpose", "category": "missing_field", "count": 1},
        {"field": "PRIVATE-UNKNOWN", "category": "PRIVATE", "count": 1},
    ]
    path.write_text(json.dumps(report))
    summary = summarize_micro_benchmark(path)
    assert "PRIVATE" not in summary
    assert "Application evaluation" in summary
