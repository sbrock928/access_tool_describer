"""Safe SQL classification; it never submits SQL to a database."""

from __future__ import annotations

import re
from dataclasses import dataclass

_OBJECT = re.compile(
    r"\b(?:from|join|into|update|delete\s+from|merge\s+into|exec(?:ute)?)\s+([\[\]\w.]+)", re.I
)


@dataclass(frozen=True)
class SqlFinding:
    operation: str
    object_names: tuple[str, ...]


def classify_sql(sql: str) -> SqlFinding:
    normalized = re.sub(r"^\s*(?:--[^\n]*\n|/\*.*?\*/\s*)*", "", sql, flags=re.S).upper()
    operation = "UNKNOWN"
    for prefix, label in (
        ("SELECT", "READ"),
        ("INSERT", "INSERT"),
        ("UPDATE", "UPDATE"),
        ("DELETE", "DELETE"),
        ("MERGE", "MERGE"),
        ("EXEC", "EXECUTE"),
        ("CREATE", "DDL"),
        ("ALTER", "DDL"),
        ("DROP", "DDL"),
    ):
        if normalized.startswith(prefix):
            operation = label
            break
    objects = tuple(dict.fromkeys(match.group(1).strip("[]") for match in _OBJECT.finditer(sql)))
    return SqlFinding(operation=operation, object_names=objects)


def split_qualified_name(name: str) -> tuple[str | None, str]:
    parts = [part.strip("[]") for part in name.split(".")]
    return (".".join(parts[:-1]) or None, parts[-1])
