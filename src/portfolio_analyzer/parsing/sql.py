"""Safe, dialect-tolerant SQL classification.

The parser is deliberately lexical: it never submits SQL to a database, but it
does distinguish read and write targets and ignores keywords inside comments and
string literals.  This is more useful for Access portfolios than assigning the
query's leading verb to every referenced object.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_IDENTIFIER_PART = r"(?:\[[^\]]+\]|[A-Za-z_][\w$#@]*)"
_QUALIFIED_IDENTIFIER = rf"{_IDENTIFIER_PART}(?:\s*\.\s*{_IDENTIFIER_PART}){{0,3}}"
_OBJECT = re.compile(
    rf"\b(?P<context>delete\s+from|merge\s+into|insert\s+into|from|join|into|update|"
    rf"exec(?:ute)?)\s+(?P<name>{_QUALIFIED_IDENTIFIER})",
    re.I,
)


@dataclass(frozen=True)
class SqlFinding:
    operation: str
    object_names: tuple[str, ...]
    references: tuple[SqlObjectReference, ...]


@dataclass(frozen=True)
class SqlObjectReference:
    name: str
    operation: str


def classify_sql(sql: str) -> SqlFinding:
    parseable = _mask_literals_and_comments(sql)
    normalized = parseable.lstrip()
    normalized = re.sub(r"^PARAMETERS\b.*?;", "", normalized, count=1, flags=re.I | re.S).lstrip()
    operation = "UNKNOWN"
    for prefix, label in (
        ("TRANSFORM", "READ"),
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
        if normalized.upper().startswith(prefix):
            operation = label
            break
    if operation == "READ" and re.search(r"\bSELECT\b.*?\bINTO\b", normalized, re.I | re.S):
        operation = "MAKE_TABLE"

    references: list[SqlObjectReference] = []
    seen: set[tuple[str, str]] = set()
    for match in _OBJECT.finditer(parseable):
        context = re.sub(r"\s+", " ", match.group("context").casefold())
        name = _normalize_identifier(match.group("name"))
        reference_operation = {
            "from": "READ",
            "join": "READ",
            "insert into": "INSERT",
            "into": "CREATE" if operation == "MAKE_TABLE" else "INSERT",
            "update": "UPDATE",
            "delete from": "DELETE",
            "merge into": "MERGE",
            "exec": "EXECUTE",
            "execute": "EXECUTE",
        }[context]
        key = (name.casefold(), reference_operation)
        if key not in seen:
            seen.add(key)
            references.append(SqlObjectReference(name=name, operation=reference_operation))
    objects = tuple(dict.fromkeys(reference.name for reference in references))
    return SqlFinding(operation=operation, object_names=objects, references=tuple(references))


def split_qualified_name(name: str) -> tuple[str | None, str]:
    parts = [part.strip().removeprefix("[").removesuffix("]") for part in name.split(".")]
    return (".".join(parts[:-1]) or None, parts[-1])


def _normalize_identifier(value: str) -> str:
    return ".".join(
        part.strip().removeprefix("[").removesuffix("]") for part in re.split(r"\s*\.\s*", value)
    )


def _mask_literals_and_comments(sql: str) -> str:
    """Replace comments and quoted values with spaces while preserving offsets."""
    output = list(sql)
    index = 0
    while index < len(sql):
        if sql.startswith("--", index):
            end = sql.find("\n", index)
            end = len(sql) if end == -1 else end
            output[index:end] = " " * (end - index)
            index = end
        elif sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            end = len(sql) if end == -1 else end + 2
            output[index:end] = " " * (end - index)
            index = end
        elif sql[index] in {"'", '"'}:
            quote = sql[index]
            end = index + 1
            while end < len(sql):
                if sql[end] == quote:
                    if end + 1 < len(sql) and sql[end + 1] == quote:
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            output[index:end] = " " * (end - index)
            index = end
        else:
            index += 1
    return "".join(output)
