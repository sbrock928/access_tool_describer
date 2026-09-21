"""Connection-string normalization and secret redaction."""

from __future__ import annotations

import re

SECRET_KEYS = frozenset({"password", "pwd", "token", "access token", "secret", "client secret"})


def parse_connection_string(value: str) -> dict[str, str]:
    """Parse semicolon-delimited key/value pairs without exposing secrets."""
    pairs: dict[str, str] = {}
    for part in re.split(r";(?![^{}]*\})", value):
        if "=" not in part:
            continue
        key, raw_value = part.split("=", 1)
        pairs[key.strip().casefold()] = raw_value.strip().strip('"')
    return pairs


def redact_connection_string(value: str) -> str:
    pairs = parse_connection_string(value)
    if not pairs:
        return value
    return ";".join(
        f"{key}={'<redacted>' if key in SECRET_KEYS else item}" for key, item in pairs.items()
    )


def infer_platform(pairs: dict[str, str]) -> str:
    flattened = " ".join(f"{key}={value}" for key, value in pairs.items()).casefold()
    if "oracle" in flattened or "ora" in flattened:
        return "Oracle"
    if "sql server" in flattened or "sqloledb" in flattened or "server=" in flattened:
        return "SQL Server"
    if ".accdb" in flattened or ".mdb" in flattened:
        return "Access"
    return "Unknown"
