"""Small boundary models used before data enters the strict V2 contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from portfolio_analyzer.library_identity import (
    ApprovedLibraryReference,
    LibraryInjectionStatus,
)
from portfolio_analyzer.redaction import redact_sensitive_text
from portfolio_analyzer.v2.identity import normalize_source_identity, stable_id


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ArtifactStatus(StrEnum):
    STAGED = "staged"
    FAILED = "failed"
    SKIPPED = "skipped"
    SKIPPED_UNSUPPORTED_FORMAT = "skipped_unsupported_format"


class InventoryRecord(BaseModel):
    """One authoritative inventory row, retained only through staging."""

    model_config = ConfigDict(frozen=True)
    tool_inventory_id: str
    tool_name: str
    inventory_filename: str
    stated_description: str | None = None
    filepath: Path
    original_values: dict[str, Any] = Field(default_factory=dict)


class StagedArtifact(BaseModel):
    """Verified local capability token passed across the Access extraction boundary."""

    artifact_id: str = ""
    tool_inventory_id: str
    original_source_path: Path
    local_staged_path: Path | None = None
    filename: str
    extension: str
    size_bytes: int | None = None
    source_modified_at: datetime | None = None
    sha256: str | None = None
    status: ArtifactStatus
    error: str | None = None
    is_primary: bool = True

    @model_validator(mode="after")
    def assign_artifact_id(self) -> StagedArtifact:
        if not self.artifact_id:
            self.artifact_id = stable_id(
                "artifact",
                self.tool_inventory_id,
                normalize_source_identity(str(self.original_source_path)),
            )
        return self


class VerifiedStagedArtifact(BaseModel):
    """Source-free, hash-pinned capability accepted by the extraction worker."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    artifact_id: str
    tool_inventory_id: str
    local_staged_path: Path
    filename: str
    extension: Literal[".accdb", ".mdb"]
    size_bytes: int
    sha256: str
    status: Literal[ArtifactStatus.STAGED] = ArtifactStatus.STAGED

    @classmethod
    def from_staged(cls, artifact: StagedArtifact) -> Self:
        if (
            artifact.status != ArtifactStatus.STAGED
            or artifact.local_staged_path is None
            or artifact.size_bytes is None
            or artifact.sha256 is None
        ):
            raise ValueError("artifact is not a complete verified staging result")
        extension = artifact.extension.casefold()
        if extension not in {".accdb", ".mdb"}:
            raise ValueError("artifact is not an eligible Access primary")
        return cls(
            artifact_id=artifact.artifact_id,
            tool_inventory_id=artifact.tool_inventory_id,
            local_staged_path=artifact.local_staged_path,
            filename=artifact.filename,
            extension=cast(Literal[".accdb", ".mdb"], extension),
            size_bytes=artifact.size_bytes,
            sha256=artifact.sha256,
        )


class AccessExtractedObject(BaseModel):
    """Immediately sanitized object returned by the isolated Access worker."""

    model_config = ConfigDict(extra="forbid")
    object_type: str
    name: str
    definition: str | None = None
    properties: dict[str, str] = Field(default_factory=dict)

    @field_validator("name", "definition")
    @classmethod
    def sanitize_extracted_text(cls, value: str | None) -> str | None:
        return redact_sensitive_text(value) if value is not None else None

    @field_validator("properties")
    @classmethod
    def sanitize_extracted_properties(cls, value: dict[str, str]) -> dict[str, str]:
        output: dict[str, str] = {}
        for key, item in value.items():
            assignment = redact_sensitive_text(f"{key}={item}")
            output[key] = assignment.split("=", 1)[1]
        return output


class AccessExtractionResult(BaseModel):
    """Source-free, sanitized result crossing the isolated worker boundary."""

    model_config = ConfigDict(extra="forbid")
    tool_inventory_id: str
    artifact_id: str = ""
    staged_path: Path
    extractor_version: str
    approved_libraries: list[ApprovedLibraryReference] = Field(default_factory=list)
    injected_libraries: list[ApprovedLibraryReference] = Field(default_factory=list)
    library_injection_status: LibraryInjectionStatus = (
        LibraryInjectionStatus.NOT_CONFIGURED
    )
    objects: list[AccessExtractedObject] = Field(default_factory=list)
    extraction_errors: list[str] = Field(default_factory=list)
    coverage_status: Literal["complete", "partial"] = "complete"
    derived_copy_sha256: str | None = None
    tabledef_enumerated_count: int = Field(default=0, ge=0)
    tabledef_succeeded_count: int = Field(default=0, ge=0)
    tabledef_failed_count: int = Field(default=0, ge=0)
    querydef_enumerated_count: int = Field(default=0, ge=0)
    querydef_succeeded_count: int = Field(default=0, ge=0)
    querydef_failed_count: int = Field(default=0, ge=0)

    @field_validator("extraction_errors")
    @classmethod
    def sanitize_extraction_errors(cls, value: list[str]) -> list[str]:
        return [redact_sensitive_text(item) for item in value]

    @model_validator(mode="after")
    def validate_dao_coverage(self) -> AccessExtractionResult:
        if self.tabledef_enumerated_count != (
            self.tabledef_succeeded_count + self.tabledef_failed_count
        ):
            raise ValueError("TableDef coverage counts are inconsistent")
        if self.querydef_enumerated_count != (
            self.querydef_succeeded_count + self.querydef_failed_count
        ):
            raise ValueError("QueryDef coverage counts are inconsistent")
        libraries = {item.library_id: item for item in self.approved_libraries}
        if len(libraries) != len(self.approved_libraries):
            raise ValueError("approved library identities must be unique")
        self.approved_libraries = [libraries[key] for key in sorted(libraries)]
        injected = {item.library_id: item for item in self.injected_libraries}
        if len(injected) != len(self.injected_libraries):
            raise ValueError("injected library identities must be unique")
        if not set(injected).issubset(libraries):
            raise ValueError("injected libraries must be configured approved libraries")
        self.injected_libraries = [injected[key] for key in sorted(injected)]
        if not libraries:
            if self.library_injection_status != LibraryInjectionStatus.NOT_CONFIGURED:
                raise ValueError("library injection status requires configured libraries")
        elif self.library_injection_status == LibraryInjectionStatus.NOT_CONFIGURED:
            raise ValueError("configured libraries require an explicit injection status")
        if self.library_injection_status == LibraryInjectionStatus.INJECTED:
            if set(injected) != set(libraries):
                raise ValueError("injected status requires every configured library")
        elif injected:
            raise ValueError("non-injected status cannot contain injected libraries")
        return self
