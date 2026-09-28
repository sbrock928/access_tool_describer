"""Canonical, credential-safe identities for V2 state.

The V2 state store hashes only canonical JSON.  Text is sanitized before it
participates in an identifier or is written to disk, so accidental credential
values cannot become durable state merely because they were used as an ID
input.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence, Set
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from portfolio_analyzer.redaction import redact_sensitive_text

REDACTED = "<redacted>"

_SECRET_KEYS = frozenset(
    {
        "apikey",
        "accesstoken",
        "authorization",
        "account",
        "authentication",
        "clientid",
        "clientsecret",
        "credential",
        "credentials",
        "integratedsecurity",
        "password",
        "passwd",
        "persistsecurityinfo",
        "pwd",
        "secret",
        "token",
        "trustedconnection",
        "uid",
        "user",
        "userid",
        "username",
    }
)
_SAFE_PREFIX = re.compile(r"[a-z][a-z0-9_]*\Z")


def sanitize_text(value: str) -> str:
    """Redact common credential forms without removing useful endpoint identity."""

    normalized = unicodedata.normalize("NFC", value)
    return redact_sensitive_text(normalized)


def sanitize_value(value: Any) -> Any:
    """Return a JSON-compatible value with recursively sanitized strings."""

    if isinstance(value, BaseModel):
        return sanitize_value(value.model_dump(mode="json"))
    if isinstance(value, Enum):
        return sanitize_value(value.value)
    if isinstance(value, Path):
        return sanitize_text(value.as_posix())
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return sanitize_text(value)
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            normalized_key = re.sub(r"[\s_-]", "", key).casefold()
            output[key] = REDACTED if normalized_key in _SECRET_KEYS else sanitize_value(raw_value)
        return output
    if isinstance(value, Set):
        sanitized = [sanitize_value(item) for item in value]
        return sorted(sanitized, key=_canonical_sort_key)
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [sanitize_value(item) for item in value]
    raise TypeError(f"Unsupported canonical value: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize a value deterministically after credential sanitization."""

    return json.dumps(
        sanitize_value(value),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    """Return the SHA-256 of sanitized canonical JSON."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def stable_id(prefix: str, *parts: Any, length: int = 24) -> str:
    """Create a readable deterministic ID from sanitized canonical inputs."""

    if not _SAFE_PREFIX.fullmatch(prefix):
        raise ValueError("ID prefix must contain lowercase letters, digits, and underscores")
    if not 16 <= length <= 64:
        raise ValueError("ID digest length must be between 16 and 64")
    digest = canonical_sha256(list(parts))
    return f"{prefix}_{digest[:length]}"


def normalize_source_identity(value: str) -> str:
    """Normalize Windows or POSIX source locators for stable artifact identity."""

    normalized = unicodedata.normalize("NFKC", value.strip()).replace("\\", "/")
    prefix = "//" if normalized.startswith("//") else ""
    body = re.sub(r"/+", "/", normalized.removeprefix("//"))
    return f"{prefix}{body}".casefold()


def opaque_source_identity(value: str) -> str:
    """Return a stable one-way identity so post-staging state never contains source paths."""

    normalized = normalize_source_identity(value)
    return f"source_{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"


def _canonical_sort_key(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
