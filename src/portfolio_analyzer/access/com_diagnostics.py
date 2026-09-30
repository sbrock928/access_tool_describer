"""Numeric-only COM diagnostics; never inspect exception descriptions or arguments."""

from __future__ import annotations

import re
from typing import Any

_CODE = re.compile(r"(?:hresult|scode|wcode):[0-9a-f]{8}\Z")
MAX_DISTINCT_CODES = 32


def record_com_error(exc: Exception, counts: dict[str, int]) -> None:
    def record(label: str, value: Any) -> None:
        if type(value) is not int or not -(2**31) <= value < 2**32:
            return
        key = f"{label}:{value & 0xffffffff:08x}"
        if key in counts or len(counts) < MAX_DISTINCT_CODES:
            counts[key] = counts.get(key, 0) + 1

    record("hresult", getattr(exc, "hresult", None))
    info = getattr(exc, "excepinfo", None)
    if isinstance(info, tuple) and len(info) == 6:
        # Entries 1–4 can contain private source/description/help text. Ignore them.
        if info[0] != 0:
            record("wcode", info[0])
        if info[5] != 0:
            record("scode", info[5])


def validate_com_error_counts(value: Any) -> dict[str, int]:
    if not isinstance(value, dict) or len(value) > MAX_DISTINCT_CODES or any(
        not isinstance(key, str) or not _CODE.fullmatch(key)
        or type(count) is not int or count <= 0
        for key, count in value.items()
    ):
        raise ValueError("invalid COM error counts")
    return dict(value)
