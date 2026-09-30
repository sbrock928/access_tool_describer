from typing import Any

import pytest

from portfolio_analyzer.access.com_diagnostics import (
    record_com_error,
    validate_com_error_counts,
)


class ComFailure(Exception):
    hresult = -2147352567
    excepinfo = (0, "PRIVATE-SOURCE", "PWD=SECRET-CANARY", "PRIVATE-PATH", 0, 0x800A0001)

    def __str__(self) -> str:
        pytest.fail("numeric diagnostics must not stringify exceptions")


def test_com_diagnostics_only_read_numeric_fields() -> None:
    counts: dict[str, int] = {}
    record_com_error(ComFailure(), counts)
    record_com_error(ComFailure(), counts)
    assert counts == {"hresult:80020009": 2, "scode:800a0001": 2}
    assert validate_com_error_counts(counts) == counts
    assert "SECRET" not in str(counts)


@pytest.mark.parametrize("value", [
    {"hresult:PRIVATE": 1}, {"scode:800a0001": "SECRET"}, {"hresult:80020009": True},
    {"hresult:80020009": -1}, {"hresult:80020009": 0}, [], None,
])
def test_invalid_com_diagnostics_rejected(value: Any) -> None:
    with pytest.raises(ValueError, match="invalid COM error counts"):
        validate_com_error_counts(value)


def test_com_diagnostics_are_bounded_and_ignore_non_integer_codes() -> None:
    counts: dict[str, int] = {}
    for code in range(40):
        failure = Exception()
        failure.hresult = code  # type: ignore[attr-defined]
        record_com_error(failure, counts)
    assert len(counts) == 32
    invalid = Exception()
    invalid.hresult = "PRIVATE-SOURCE"  # type: ignore[attr-defined]
    invalid.excepinfo = (True, None, None, None, None, 2**50)  # type: ignore[attr-defined]
    record_com_error(invalid, counts)
    assert len(counts) == 32
    assert "PRIVATE" not in str(counts)
