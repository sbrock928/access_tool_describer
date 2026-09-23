import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from portfolio_analyzer.cli.main import _format_duration, _semantic_progress_reporter, app
from portfolio_analyzer.models import ExtractedApplication, ExtractedObject


def test_semantic_progress_reports_wall_step_and_total_times() -> None:
    output: list[str] = []
    ticks = iter((100.0, 102.5, 165.25))
    progress = _semantic_progress_reporter(
        output=output.append,
        clock=lambda: next(ticks),
        wall_clock=lambda: datetime(2026, 9, 22, 14, 30, tzinfo=UTC),
    )

    progress("Completed first step")
    progress("Completed second step")

    assert _format_duration(3661.125) == "01:01:01.125"
    assert output == [
        "[2026-09-22T14:30:00+00:00] "
        "[+00:00:02.500 step | +00:00:02.500 total] Completed first step",
        "[2026-09-22T14:30:00+00:00] "
        "[+00:01:02.750 step | +00:01:05.250 total] Completed second step",
    ]


def test_analyze_reuses_saved_extraction_without_windows_or_access(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    extraction_dir = workspace / "extracted"
    extraction_dir.mkdir(parents=True)
    snapshot = ExtractedApplication(
        tool_inventory_id="42",
        staged_path=workspace / "staged_tools" / "42" / "primary" / "tool.accdb",
        extractor_version="fixture",
        objects=[
            ExtractedObject(
                object_type="module",
                name="modOutput",
                definition='Set app = CreateObject("Excel.Application")',
            )
        ],
    )
    (extraction_dir / "extraction_state.json").write_text(
        json.dumps(
            {
                "applications": [
                    {
                        "tool_inventory_id": "42",
                        "sha256": "fixture-hash",
                        "extractor_version": "fixture",
                        "extracted": snapshot.model_dump(mode="json"),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["analyze", "--workspace", str(workspace), "--force"])

    assert result.exit_code == 0, result.output
    state = json.loads((workspace / "analysis" / "analysis_state.json").read_text())
    assert state["applications"][0]["evidence"][0]["inference"] == "Excel automation"


def test_semantic_init_is_non_destructive_and_uses_approved_local_model(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    runner = CliRunner()
    first = runner.invoke(app, ["semantic-init", "--workspace", str(workspace)])
    assert first.exit_code == 0, first.output
    config = workspace / "semantic" / "semantic.toml"
    context = workspace / "semantic" / "business_context.csv"
    gold = workspace / "semantic" / "gold_set.csv"
    assert config.exists() and context.exists() and gold.exists()
    contents = config.read_text(encoding="utf-8")
    assert "ibm-granite/granite-3.3-2b-instruct" in contents
    assert 'revision = "707f574c62054322f6b5b04b6d075f0a8f05e0f0"' in contents
    assert "base_url" not in contents
    assert "embedding" not in contents
    assert "profile_output_tokens = 256" in contents
    assert "max_profile_characters = 12000" in contents
    assert "architecture_output_tokens = 768" in contents
    assert "batch_output_tokens" not in contents
    assert "cluster_output_tokens" not in contents
    assert 'device = "cpu"' in contents
    assert "cpu_threads = 4" in contents
    assert "cpu_interop_threads = 1" in contents
    assert "model_generation = false" in contents

    config.write_text(contents + "\n# operator note\n", encoding="utf-8")
    second = runner.invoke(app, ["semantic-init", "--workspace", str(workspace)])
    assert second.exit_code == 0, second.output
    assert config.read_text(encoding="utf-8").endswith("# operator note\n")


def test_semantic_check_fails_closed_when_approved_model_is_missing(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    runner = CliRunner()
    assert runner.invoke(app, ["semantic-init", "--workspace", str(workspace)]).exit_code == 0

    result = runner.invoke(
        app,
        ["semantic-check", "--workspace", str(workspace)],
        env={"COLUMNS": "240"},
    )

    assert result.exit_code != 0
    assert "manifest is missing" in result.output
