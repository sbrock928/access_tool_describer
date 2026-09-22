from portfolio_analyzer.parsing.connections import (
    infer_platform,
    parse_connection_string,
    redact_connection_string,
)
from portfolio_analyzer.parsing.paths import extract_windows_paths
from portfolio_analyzer.parsing.sql import classify_sql, split_qualified_name
from portfolio_analyzer.parsing.vba import analyze_vba, extract_procedures


def test_connection_redaction() -> None:
    value = "Server=SQL01;Database=Reporting;User ID=reader;Password=dont-print"
    redacted = redact_connection_string(value)
    assert "dont-print" not in redacted
    assert "<redacted>" in redacted
    assert "SQL01" in redacted
    assert "reader" not in redacted


def test_connection_platforms_include_common_odbc_targets() -> None:
    postgres = parse_connection_string("Driver={PostgreSQL};Server=db01")
    snowflake = parse_connection_string("Driver={SnowflakeDSIIDriver};Server=acme")
    assert infer_platform(postgres) == "PostgreSQL"
    assert infer_platform(snowflake) == "Snowflake"
    assert infer_platform(parse_connection_string("Server=db01;Database=Corporate")) == "Unknown"


def test_sql_operation_and_object_extraction() -> None:
    finding = classify_sql("UPDATE dbo.Tranche SET balance = 1")
    assert finding.operation == "UPDATE"
    assert finding.object_names == ("dbo.Tranche",)
    assert split_qualified_name("dbo.Tranche") == ("dbo", "Tranche")


def test_sql_references_have_contextual_operations_and_ignore_literals() -> None:
    finding = classify_sql(
        """PARAMETERS pId Long;
        INSERT INTO [audit].[Deal Archive]
        SELECT * FROM [dbo].[Deal] AS d
        INNER JOIN dbo.Tranche AS t ON d.Id = t.DealId
        WHERE d.Note = 'FROM dbo.NotARealTable'
        -- JOIN dbo.AlsoNotReal
        """
    )

    assert finding.operation == "INSERT"
    assert [(item.name, item.operation) for item in finding.references] == [
        ("audit.Deal Archive", "INSERT"),
        ("dbo.Deal", "READ"),
        ("dbo.Tranche", "READ"),
    ]


def test_access_make_table_query_distinguishes_source_and_target() -> None:
    finding = classify_sql("SELECT * INTO Snapshot FROM CurrentData")

    assert finding.operation == "MAKE_TABLE"
    assert [(item.name, item.operation) for item in finding.references] == [
        ("Snapshot", "CREATE"),
        ("CurrentData", "READ"),
    ]


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


def test_vba_comments_do_not_create_findings() -> None:
    assert analyze_vba("' Shell(\"not-real.exe\")") == []
