from __future__ import annotations

import pytest

from portfolio_analyzer.parsing.connections import (
    REDACTED,
    ConnectionParseIssue,
    ConnectionStringParseError,
    normalize_connection_identity,
    parse_connection_string,
    parse_connection_string_details,
    redact_connection_string,
)


def test_finite_state_parser_preserves_semicolons_inside_quotes_and_braces() -> None:
    parsed = parse_connection_string_details(
        "ODBC;Driver={ODBC Driver 18 for SQL Server};"
        'Server="SQL;01";Database={Finance;Ops};UID=reader;PWD={s;e;c}'
    )

    assert parsed.issues == ()
    assert parsed.markers == ("odbc",)
    assert parsed.values_for("server") == ("SQL;01",)
    assert parsed.values_for("database") == ("Finance;Ops",)
    assert parsed.values_for("uid") == (REDACTED,)
    assert parsed.values_for("pwd") == (REDACTED,)


def test_parser_unescapes_doubled_quote_and_brace_characters() -> None:
    parsed = parse_connection_string_details(
        'Server="SQL""01";Database={Finance}}Archive};Driver={SQL Server}'
    )

    assert parsed.values_for("server") == ('SQL"01',)
    assert parsed.values_for("database") == ("Finance}Archive",)


def test_duplicate_parameters_remain_visible_and_conflicts_are_not_guessed() -> None:
    parsed = parse_connection_string_details(
        "Server=SQL01;Server=SQL02;Password=first;Password=second"
    )

    assert parsed.duplicates == ("password", "server")
    assert parsed.values_for("server") == ("SQL01", "SQL02")
    assert parsed.values_for("password") == (REDACTED, REDACTED)
    # Backward-compatible mapping semantics remain last-value-wins, but only for safe values.
    assert parsed.as_dict()["server"] == "SQL02"
    assert normalize_connection_identity(parsed).conflicts == ("server",)


def test_parse_mapping_exposes_only_allowlisted_lineage_values() -> None:
    parsed = parse_connection_string(
        "Server=SQL01;Database=Finance;UID=reader;Password=secret;Custom=private"
    )

    assert parsed == {
        "server": "SQL01",
        "database": "Finance",
        "uid": REDACTED,
        "password": REDACTED,
        "unknown": REDACTED,
    }
    assert "reader" not in repr(parsed)
    assert "secret" not in repr(parsed)
    assert "private" not in repr(parsed)


def test_embedded_credentials_cause_even_allowlisted_value_to_be_withheld() -> None:
    parsed = parse_connection_string(
        "Server=https://reader:secret@example.invalid;Database=Password=do-not-print"
    )

    assert parsed["server"] == REDACTED
    assert parsed["database"] == REDACTED


def test_malformed_input_never_appears_in_errors_or_redacted_output() -> None:
    source = "Server={SQL01;Password=super-secret"

    parsed = parse_connection_string_details(source)
    redacted = redact_connection_string(source)
    with pytest.raises(ConnectionStringParseError) as caught:
        parse_connection_string_details(source, strict=True)

    assert parsed.issues == (ConnectionParseIssue.UNTERMINATED_BRACE,)
    assert parsed.values_for("server") == (REDACTED,)
    assert "super-secret" not in redacted
    assert "super-secret" not in str(caught.value)
    assert "super-secret" not in repr(caught.value)


def test_password_value_cannot_smuggle_a_server_parameter() -> None:
    source = 'PWD="abc;SERVER=secretpart";Driver={ODBC Driver 18 for SQL Server}'

    parsed = parse_connection_string(source)

    assert parsed["pwd"] == REDACTED
    assert "server" not in parsed
    assert "secretpart" not in redact_connection_string(source)


def test_unknown_unstructured_text_is_replaced_instead_of_echoed() -> None:
    source = "opaque-secret-material"

    assert redact_connection_string(source) == "<malformed>"
    assert source not in redact_connection_string(source)


def test_sql_server_identity_is_canonical_and_file_identity_is_lexical() -> None:
    sql_server = normalize_connection_identity(
        "Driver={Microsoft ODBC Driver 18 for SQL Server};"
        "DSN=Finance Warehouse;Server=tcp:SQL01,1433;Initial Catalog=Reporting"
    )
    access_file = normalize_connection_identity(
        r"Provider=Microsoft.ACE.OLEDB.16.0;Data Source=C:/Data/../Data/Book.accdb"
    )

    assert sql_server.platform == "sql_server"
    assert sql_server.driver == "odbc driver 18 for sql server"
    assert sql_server.dsn == "finance warehouse"
    assert sql_server.server == "sql01"
    assert sql_server.database == "reporting"
    assert access_file.platform == "access"
    assert access_file.file == r"c:\data\book.accdb"
    assert access_file.server is None


def test_platform_is_not_inferred_from_dsn_server_or_database_names() -> None:
    identity = normalize_connection_identity(
        "DSN=SQL Server Production;Server=postgres-primary;Database=oracle_reporting"
    )

    assert identity.platform == "unknown"


def test_bare_isam_marker_produces_a_file_identity() -> None:
    identity = normalize_connection_identity(
        r"Excel 12.0 Xml;HDR=YES;DATABASE=C:\Portfolio\Inputs.xlsx"
    )

    assert identity.platform == "excel"
    assert identity.file == r"c:\portfolio\inputs.xlsx"
    assert identity.database is None
    summary = redact_connection_string(
        r"Excel 12.0 Xml;HDR=YES;DATABASE=C:\Portfolio\Inputs.xlsx"
    )
    assert summary == (
        r"excel 12.0 xml;unknown=<redacted>;database=C:\Portfolio\Inputs.xlsx"
    )


def test_sql_server_port_and_attached_file_are_part_of_normalized_identity() -> None:
    identity = normalize_connection_identity(
        r"Driver=C:\Windows\System32\msodbcsql18.dll;Server=SQL01;Port=1444;"
        r"AttachDbFilename=C:\Data\Warehouse.mdf"
    )

    assert identity.platform == "sql_server"
    assert identity.driver == "odbc driver 18 for sql server"
    assert identity.server == "sql01,1444"
    assert identity.file == r"c:\data\warehouse.mdf"
