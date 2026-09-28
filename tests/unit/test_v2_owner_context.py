from pathlib import Path

import pytest

from portfolio_analyzer.v2.owner_context import (
    OWNER_CONTEXT_HEADERS,
    load_owner_context,
)


def test_owner_context_is_attributed_and_sanitized(tmp_path: Path) -> None:
    path = tmp_path / "owner_context.csv"
    path.write_text(
        ",".join(OWNER_CONTEXT_HEADERS)
        + "\n"
        + "app-1,Alice,,Reconciles deals,PASSWORD=secret,,,,,\n",
        encoding="utf-8",
    )

    claims = load_owner_context(path, application_ids={"app-1"})["app-1"]

    assert {item.field for item in claims} == {
        "business_owner",
        "business_purpose",
        "criticality",
    }
    assert all(item.source == "owner_context.csv" for item in claims)
    assert "secret" not in " ".join(item.value for item in claims)


def test_owner_context_rejects_unknown_and_duplicate_claims(tmp_path: Path) -> None:
    path = tmp_path / "owner_context.csv"
    header = ",".join(OWNER_CONTEXT_HEADERS)
    path.write_text(
        header
        + "\napp-1,Alice,,,,,,,,\n"
        + "app-1,Bob,,,,,,,,\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="repeats business_owner"):
        load_owner_context(path, application_ids={"app-1"})

    path.write_text(header + "\nmissing,Alice,,,,,,,,\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown application"):
        load_owner_context(path, application_ids={"app-1"})
