"""Path-free identity contract for reviewed Access libraries."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from portfolio_analyzer.redaction import redact_sensitive_text


class LibraryInjectionStatus(StrEnum):
    """Whether reviewed libraries were actually copied into the disposable lane."""

    NOT_CONFIGURED = "not_configured"
    SKIPPED_SAFETY_GATE = "skipped_safety_gate"
    INJECTED = "injected"
    FAILED = "failed"


class ApprovedLibraryReference(BaseModel):
    """Filename and digest safe to retain in extraction and evidence state."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )

    filename: str = Field(min_length=1)
    sha256: str
    library_id: str = ""

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        normalized = unicodedata.normalize("NFC", value)
        if (
            Path(normalized).name != normalized
            or normalized in {".", ".."}
            or any(character in normalized for character in ("/", "\\", "\x00"))
            or any(ord(character) < 32 for character in normalized)
            or any(character in normalized for character in '<>:"|?*')
            or normalized.endswith((" ", "."))
        ):
            raise ValueError("approved library filename must be a basename")
        if Path(normalized).suffix.casefold() not in {".accdb", ".mdb"}:
            raise ValueError("approved library reference must identify an Access file")
        if redact_sensitive_text(normalized) != normalized:
            raise ValueError(
                "approved library filename must not contain credential-shaped text"
            )
        return normalized

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        normalized = value.casefold()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("library sha256 must be a 64-character hexadecimal digest")
        return normalized

    @model_validator(mode="after")
    def assign_library_id(self) -> Self:
        payload = json.dumps(
            ["approved_library", self.filename.casefold(), self.sha256],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        expected = f"approved_library_{hashlib.sha256(payload).hexdigest()[:24]}"
        if self.library_id and self.library_id != expected:
            raise ValueError("library_id does not match the approved library identity")
        object.__setattr__(self, "library_id", expected)
        return self
