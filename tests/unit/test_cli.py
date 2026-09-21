import json
from pathlib import Path

from openpyxl import Workbook
from typer.testing import CliRunner

from portfolio_analyzer.cli.main import app


def test_stage_and_report_keep_source_and_staged_paths_separate(tmp_path: Path) -> None:
    source = tmp_path / "source" / "tool.accdb"
    source.parent.mkdir()
    source.write_bytes(b"synthetic database")
    inventory = tmp_path / "inventory.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["INVENTORY_ID", "EUCTNAME", "FILE_NAME", "FULLPATH", "DESCRIPTION"])
    sheet.append(["42", "Tool", "tool.accdb", str(source), "Claim"])
    workbook.save(inventory)
    workspace = tmp_path / "workspace"
    runner = CliRunner()

    staged = runner.invoke(
        app, ["stage", "--inventory", str(inventory), "--workspace", str(workspace)]
    )
    reported = runner.invoke(app, ["report", "--workspace", str(workspace)])

    assert staged.exit_code == 0, staged.output
    assert reported.exit_code == 0, reported.output
    state = json.loads((workspace / "analysis" / "staging_state.json").read_text())
    primary = next(item for item in state["artifacts"] if item["is_primary"])
    assert Path(primary["original_source_path"]) == source
    assert Path(primary["local_staged_path"]).is_relative_to(workspace / "staged_tools")
    assert (workspace / "reports" / "Portfolio_Analysis.xlsx").exists()
    assert (workspace / "reports" / "applications.csv").exists()
    assert (workspace / "reports" / "analysis_coverage.csv").exists()
    assert (workspace / "reports" / "evidence.csv").exists()
