from portfolio_analyzer.parsing.connections import redact_connection_string
from portfolio_analyzer.parsing.paths import extract_windows_paths
from portfolio_analyzer.parsing.sql import classify_sql, split_qualified_name
from portfolio_analyzer.parsing.vba import analyze_vba, extract_procedures


def test_connection_redaction() -> None:
    value = "Server=SQL01;Database=Reporting;User ID=reader;Password=dont-print"
    redacted = redact_connection_string(value)
    assert "dont-print" not in redacted
    assert "<redacted>" in redacted
    assert "SQL01" in redacted


def test_sql_operation_and_object_extraction() -> None:
    finding = classify_sql("UPDATE dbo.Tranche SET balance = 1")
    assert finding.operation == "UPDATE"
    assert finding.object_names == ("dbo.Tranche",)
    assert split_qualified_name("dbo.Tranche") == ("dbo", "Tranche")


def test_windows_path_extraction() -> None:
    paths = extract_windows_paths(r'Open "\\server\share\out.csv" and "P:\templates\report.xlsx"')
    assert paths == [r"\\server\share\out.csv", r"P:\templates\report.xlsx"]


def test_vba_procedures_and_automation() -> None:
    source = """Public Sub MakeReport()
Set app = CreateObject("Excel.Application")
End Sub
"""
    assert extract_procedures(source) == ["MakeReport"]
    assert any(item.kind == "Excel automation" for item in analyze_vba(source))
