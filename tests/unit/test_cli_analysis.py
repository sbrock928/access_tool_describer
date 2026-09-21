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
