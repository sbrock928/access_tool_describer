"""Central defense-in-depth redaction for persisted text, diagnostics, and prompts."""

from __future__ import annotations

import re

_SECRET_KEY = re.compile(
    r"(?i)(?:"
    r"[\"']?\b(?P<general>password|passwd|pwd|token|access\s+token|api\s+key|"
    r"secret|client[\s_]+secret|account\s*key|authorization|authentication|"
    r"auth|bearer|passphrase|shared\s+access\s+signature|sas\s+token|"
    r"integrated[\s_]+security|persist[\s_]+security[\s_]+info|"
    r"trusted[\s_]+connection|user[\s_]+id|userid|username|uid)"
    r"\b[\"']?\s*(?:=|:)\s*|"
    r"[\"']?\b(?P<equals_only>client[\s_]+id|account|credential|"
    r"user[\s_]+name|user)\b[\"']?\s*=\s*)"
)
_URI_CREDENTIALS = re.compile(r"(?i)(://[^:/\s]+:)[^@/\s]+(@)")


def redact_sensitive_text(value: str) -> str:
    """Remove common credential assignments without retaining their original values."""
    output: list[str] = []
    cursor = 0
    while match := _SECRET_KEY.search(value, cursor):
        output.append(value[cursor : match.start()])
        key = match.group("general") or match.group("equals_only")
        output.append(f"{key}=<redacted>")
        cursor = _credential_value_end(value, match.end())
    output.append(value[cursor:])
    return _URI_CREDENTIALS.sub(r"\1<redacted>\2", "".join(output))


def _credential_value_end(value: str, start: int) -> int:
    if start >= len(value):
        return start
    opening = value[start]
    if opening in {'"', "'"}:
        index = start + 1
        while index < len(value):
            if value[index] == opening:
                if index + 1 < len(value) and value[index + 1] == opening:
                    index += 2
                    continue
                return index + 1
            index += 1
        return len(value)
    if opening == "{":
        index = start + 1
        while index < len(value):
            if value[index] == "}":
                if index + 1 < len(value) and value[index + 1] == "}":
                    index += 2
                    continue
                return index + 1
            index += 1
        return len(value)
    index = start
    while index < len(value) and value[index] not in ";\r\n,":
        index += 1
    return index
