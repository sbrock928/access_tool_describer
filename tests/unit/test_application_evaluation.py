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


def test_check_only_is_read_only_and_never_generates(environment: dict[str, Any]) -> None:
    args = _arguments(environment)
    workspace = args["workspace"]
    before = {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob("*") if p.is_file()}
    report, code = evaluation.evaluate_applications(**args, check_only=True)
    assert code == 0
    assert report == {"applications_selected": 1, "outcome": "preflight_passed"}
    assert environment["calls"] == []
    assert not args["evaluation_dir"].exists()
    assert before == {
        p.relative_to(workspace): p.read_bytes() for p in workspace.rglob("*") if p.is_file()
    }


@pytest.mark.parametrize("phase,code", [
    ("stage", "current_stage_unavailable"), ("extract", "current_extraction_unavailable"),
])
def test_setup_errors_identify_phase_without_raw_exception(
    environment: dict[str, Any], monkeypatch: pytest.MonkeyPatch, phase: str, code: str,
) -> None:
    original = evaluation.V2StateStore.load_current_run

    def load(store: Any, selected_phase: Any) -> Any:
        if selected_phase.value == phase:
            raise FileNotFoundError("PRIVATE-SERVER PRIVATE-APP Password=SECRET")
        return original(store, selected_phase)

    monkeypatch.setattr(evaluation.V2StateStore, "load_current_run", load)
    args = _arguments(environment)
    result = CliRunner().invoke(v2.app, [
        "evaluate-applications", "--workspace", str(args["workspace"]),
        "--model-dir", str(args["model_dir"]), "--evaluation-dir", str(args["evaluation_dir"]),
        "--check-only",
    ])
    assert result.exit_code == 1
    assert f"[{code}]" in result.output
    assert "PRIVATE" not in result.output
    assert "SECRET" not in result.output
    assert not args["evaluation_dir"].exists()


def test_existing_directory_message_preserves_files(environment: dict[str, Any]) -> None:
    args = _arguments(environment)
    args["evaluation_dir"].mkdir()
    keep = args["evaluation_dir"] / "keep.txt"
    keep.write_text("PRIVATE-EXISTING")
    with pytest.raises(evaluation.EvaluationSetupError) as error:
        evaluation.evaluate_applications(**args, check_only=True)
    assert error.value.code == "output_exists"
    assert "--resume" in str(error.value)
    assert keep.read_text() == "PRIVATE-EXISTING"


def test_model_setup_exception_is_safe_and_distinct(
    environment: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("PRIVATE-MODEL-PATH")

    monkeypatch.setattr(evaluation, "LocalQwenProvider", fail)
    with pytest.raises(evaluation.EvaluationSetupError) as error:
        evaluation.evaluate_applications(**_arguments(environment), check_only=True)
    assert error.value.code == "model_verification_failed"
    assert "PRIVATE" not in str(error.value)


def test_resume_check_only_preserves_evaluation_files(environment: dict[str, Any]) -> None:
    args = _arguments(environment)
    assert evaluation.evaluate_applications(**args)[1] == 0
    destination = args["evaluation_dir"]
    before = {p.relative_to(destination): p.read_bytes()
              for p in destination.rglob("*") if p.is_file()}
    calls = len(environment["calls"])
    assert evaluation.evaluate_applications(**args, resume=True, check_only=True)[1] == 0
    assert len(environment["calls"]) == calls
    assert before == {p.relative_to(destination): p.read_bytes()
                      for p in destination.rglob("*") if p.is_file()}


@pytest.mark.parametrize("status", ["failed", "partial"])
def test_incomplete_extraction_lists_local_identity_without_errors_or_writes(
    environment: dict[str, Any], monkeypatch: pytest.MonkeyPatch, status: str,
) -> None:
    from portfolio_analyzer.v2.state import RunPhase, RunStatus

    original = evaluation.V2StateStore.load_current_run

    def load(store: Any, phase: Any) -> Any:
        manifest = original(store, phase)
        if phase == RunPhase.EXTRACT:
            record = manifest.applications[0].model_copy(update={
                "status": RunStatus(status), "errors": ("PRIVATE-PATH Password=SECRET",),
            })
            return manifest.model_copy(update={"applications": (record,)})
        return manifest

    monkeypatch.setattr(evaluation.V2StateStore, "load_current_run", load)
    args = _arguments(environment)
    workspace = args["workspace"]
    before = {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob("*") if p.is_file()}
    result = CliRunner().invoke(v2.app, [
        "evaluate-applications", "--workspace", str(workspace),
        "--model-dir", str(args["model_dir"]), "--evaluation-dir", str(args["evaluation_dir"]),
        "--check-only",
    ])
    assert result.exit_code == 1
    assert "[extraction_incomplete]" in result.output
    assert "LOCAL ONLY" in result.output
    assert f'1 | "PRIVATE-APP" | "PRIVATE-NAME" | {status}' in result.output
    assert "PRIVATE-PATH" not in result.output
    assert "SECRET" not in result.output
    assert "--application <ID>" in result.output
    assert environment["calls"] == []
    assert not args["evaluation_dir"].exists()
    assert before == {
        p.relative_to(workspace): p.read_bytes() for p in workspace.rglob("*") if p.is_file()
    }
    with pytest.raises(evaluation.EvaluationSetupError) as error:
        evaluation.evaluate_applications(**args, check_only=True)
    assert "PRIVATE" not in str(error.value)
    assert error.value.incomplete[0].application_id == "PRIVATE-APP"


def test_incomplete_local_details_escape_controls_and_redact_credentials(
    environment: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from portfolio_analyzer.v2.state import RunStatus

    def fail(**_kwargs: Any) -> Any:
        raise evaluation.EvaluationSetupError("extraction_incomplete", incomplete=(
            evaluation.IncompleteExtraction(1, "APP\nESC\x1b", "Password=SECRET", RunStatus.FAILED),
        ))

    monkeypatch.setattr(evaluation, "evaluate_applications", fail)
    args = _arguments(environment)
    result = CliRunner().invoke(v2.app, [
        "evaluate-applications", "--workspace", str(args["workspace"]),
        "--model-dir", str(args["model_dir"]), "--evaluation-dir", str(args["evaluation_dir"]),
    ])
    assert result.exit_code == 1
    assert "SECRET" not in result.output
    assert "APP\\nESC\\u001b" in result.output


def test_extraction_reports_failure_and_status_reads_saved_reason_without_rerunning(
    environment: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        calls.append("extract")
        raise RuntimeError("Synthetic DAO failure\nPassword=HIDDEN-CREDENTIAL; code=123\x1b")

    monkeypatch.setattr(v2, "run_extraction_with_timeout", fail)
    runner = CliRunner()
    workspace = environment["workspace"]
    failed = runner.invoke(v2.app, ["extract", "--workspace", str(workspace), "--force"])
    assert failed.exit_code == 1
    assert "Synthetic DAO failure" in failed.output
    assert "HIDDEN-CREDENTIAL" not in failed.output
    assert "\\u001b" in failed.output
    assert "PRIVATE-APP" in failed.output
    assert "snapshots=0/1" in failed.output
    assert calls == ["extract"]
    before = {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob("*") if p.is_file()}
    command = ["extraction-status", "--workspace", str(workspace)]
    summary = runner.invoke(v2.app, command)
    assert summary.exit_code == 0
    assert "Synthetic DAO failure" not in summary.output
    assert "failed" in summary.output
    details = runner.invoke(v2.app, command + ["--details", "--application", "PRIVATE-APP"])
    assert details.exit_code == 0
    assert "Synthetic DAO failure" in details.output
    assert "HIDDEN-CREDENTIAL" not in details.output
    assert "LOCAL ONLY" in details.output
    assert calls == ["extract"]
    assert runner.invoke(v2.app, command + ["--application", "nonexistent"]).exit_code == 1
    assert before == {p.relative_to(workspace): p.read_bytes()
                      for p in workspace.rglob("*") if p.is_file()}


def test_extraction_status_does_not_display_integrity_exception(
    environment: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("PRIVATE-PATH Password=HIDDEN")

    monkeypatch.setattr(v2.V2StateStore, "load_current_run", fail)
    result = CliRunner().invoke(v2.app, ["extraction-status", "--workspace",
                                        str(environment["workspace"]), "--details"])
    assert result.exit_code == 1
    assert "No exception content displayed" in result.output
    assert "PRIVATE" not in result.output
    assert "HIDDEN" not in result.output


@pytest.mark.parametrize("seconds", [None, 900])
def test_extraction_timeout_option_reaches_worker(
    environment: dict[str, Any], monkeypatch: pytest.MonkeyPatch, seconds: int | None,
) -> None:
    original = v2.run_extraction_with_timeout
    limits = []

    def extract(*args: Any, **kwargs: Any) -> Any:
        limits.append(kwargs["timeout_seconds"])
        return original(*args, **kwargs)

    monkeypatch.setattr(v2, "run_extraction_with_timeout", extract)
    command = ["extract", "--workspace", str(environment["workspace"]), "--force"]
    if seconds is not None:
        command += ["--timeout-seconds", str(seconds)]
    result = CliRunner().invoke(v2.app, command)
    assert result.exit_code == 0, result.output
    assert limits == [seconds or 300]
    for invalid in (0, 29, 3601):
        assert CliRunner().invoke(v2.app, command[:4] + [
            "--timeout-seconds", str(invalid),
        ]).exit_code != 0
    assert len(limits) == 1
