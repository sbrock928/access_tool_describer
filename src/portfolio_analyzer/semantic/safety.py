"""Prompt-boundary redaction and normalization for untrusted application text."""

from __future__ import annotations

import re

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|pwd|passphrase|secret|api[_ -]?key|access[_ -]?token)\b"
    r"(\s*[:=]\s*)([^;\s\r\n]+|\"[^\"]*\"|'[^']*')"
)
_UNC_PATH = re.compile(r"\\\\[^\\\s\"']+\\[^\s\"']+")
_DRIVE_PATH = re.compile(r"(?i)\b[A-Z]:\\(?:[^\s\"']+\\)*[^\s\"']*")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f]")


def redact_semantic_text(value: str, *, redact_paths: bool, limit: int) -> str:
    text = _CONTROL_CHARACTERS.sub("", value)
    text = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text)
    if redact_paths:
        text = _UNC_PATH.sub("[NETWORK_PATH]", text)
        text = _DRIVE_PATH.sub("[LOCAL_PATH]", text)
    text = text[:limit]
    return text


def prompt_data(value: object) -> str:
    """Delimit serialized source data so it cannot be confused with instructions."""
    import json

    return (
        "<UNTRUSTED_SOURCE_DATA>\n"
        + json.dumps(value, ensure_ascii=True)
        + "\n</UNTRUSTED_SOURCE_DATA>"
    )
