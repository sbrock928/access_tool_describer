from pathlib import Path

from openpyxl import load_workbook

from portfolio_analyzer.models import ArtifactStatus, StagedArtifact
from portfolio_analyzer.reporting.writers import write_csv, write_executive_pdf, write_workbook


def test_report_writers_create_outputs(tmp_path: Path) -> None:
    artifact = StagedArtifact(
        tool_inventory_id="1",
        original_source_path=Path("source.accdb"),
        filename="source.accdb",
        extension=".accdb",
        status=ArtifactStatus.FAILED,
        error="missing",
    )
    workbook = tmp_path / "Portfolio_Analysis.xlsx"
    write_workbook(workbook, [], [artifact], [], [], [], [])
    assert "Applications" in load_workbook(workbook).sheetnames
    csv_path = tmp_path / "evidence.csv"
    write_csv(csv_path, [{"Tool": "1", "Finding": "test"}])
    assert "Finding" in csv_path.read_text()
    pdf_path = tmp_path / "Portfolio_Analysis.pdf"
    write_executive_pdf(pdf_path, [], [artifact], [])
    assert pdf_path.read_bytes().startswith(b"%PDF")
