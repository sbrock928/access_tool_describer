"""Connection-string normalization and secret redaction."""

from __future__ import annotations

import re

SECRET_KEYS = frozenset(
    {
        "password",
        "pwd",
        "token",
        "access token",
        "secret",
        "client secret",
        "user id",
        "userid",
        "uid",
    }
)


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
    oracle_hint = any(
        key in {"driver", "dsn", "provider"} and value.casefold().startswith("ora")
        for key, value in pairs.items()
    )
    if "oracle" in flattened or oracle_hint:
        return "Oracle"
    if "postgres" in flattened or "psqlodbc" in flattened:
        return "PostgreSQL"
    if "mysql" in flattened or "mariadb" in flattened:
        return "MySQL/MariaDB"
    if "snowflake" in flattened:
        return "Snowflake"
    if "sql server" in flattened or "sqloledb" in flattened or "msoledbsql" in flattened:
        return "SQL Server"
    if ".accdb" in flattened or ".mdb" in flattened:
        return "Access"
    return "Unknown"
