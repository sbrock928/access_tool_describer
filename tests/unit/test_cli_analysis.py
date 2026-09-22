import json
from pathlib import Path

from typer.testing import CliRunner

from portfolio_analyzer.cli.main import app
from portfolio_analyzer.models import ExtractedApplication, ExtractedObject


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


def test_semantic_init_is_non_destructive_and_loopback_only(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    runner = CliRunner()
    first = runner.invoke(app, ["semantic-init", "--workspace", str(workspace)])
    assert first.exit_code == 0, first.output
    config = workspace / "semantic" / "semantic.toml"
    context = workspace / "semantic" / "business_context.csv"
    gold = workspace / "semantic" / "gold_set.csv"
    assert config.exists() and context.exists() and gold.exists()
    contents = config.read_text(encoding="utf-8")
    assert "127.0.0.1" in contents
    assert "allow_remote" not in contents

    config.write_text(contents + "\n# operator note\n", encoding="utf-8")
    second = runner.invoke(app, ["semantic-init", "--workspace", str(workspace)])
    assert second.exit_code == 0, second.output
    assert config.read_text(encoding="utf-8").endswith("# operator note\n")
