"""Strict V2 workflow payloads that connect staging and extraction to evidence building."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from portfolio_analyzer.library_identity import (
    ApprovedLibraryReference,
    LibraryInjectionStatus,
)
from portfolio_analyzer.v2.identity import normalize_source_identity, stable_id
from portfolio_analyzer.v2.models import NonEmptyString, OwnerClaim, Sha256, StrictModel

if TYPE_CHECKING:
    from portfolio_analyzer.models import AccessExtractionResult

STAGE_INDEX_SCHEMA_VERSION = "stage-index-v2"
EXTRACTION_SNAPSHOT_SCHEMA_VERSION = "extraction-snapshot-v2"
EXTRACTION_POLICY_VERSION = "access-static-extraction-policy-v3"


class InventoryExclusion(StrictModel):
    application_id: NonEmptyString
    application_name: NonEmptyString
    filename: NonEmptyString
    source_locator: NonEmptyString
    status: Literal["skipped_unsupported_format"] = "skipped_unsupported_format"
    reason: NonEmptyString


class StagingFailure(StrictModel):
    application_id: NonEmptyString
    application_name: NonEmptyString
    filename: NonEmptyString
    source_locator: NonEmptyString
    reason: NonEmptyString


class StagedArtifactRecord(StrictModel):
    application_id: NonEmptyString
    application_name: NonEmptyString
    source_locator: NonEmptyString
    staged_relative_path: NonEmptyString
    filename: NonEmptyString
    access_format: Literal["accdb", "mdb"]
    size_bytes: Annotated[int, Field(ge=0)]
    sha256: Sha256
    artifact_id: str = ""

    @model_validator(mode="after")
    def assign_artifact_id(self) -> Self:
        expected = stable_id(
            "artifact", self.application_id, normalize_source_identity(self.source_locator)
        )
        if self.artifact_id and self.artifact_id != expected:
            raise ValueError("artifact_id does not match canonical source identity")
        object.__setattr__(self, "artifact_id", expected)
        if self.filename.casefold().rsplit(".", 1)[-1] != self.access_format:
            raise ValueError("filename and Access format do not agree")
        return self


class StagedApplication(StrictModel):
    application_id: NonEmptyString
    application_name: NonEmptyString
    inventory_descriptions: tuple[NonEmptyString, ...] = ()
    owner_claims: tuple[OwnerClaim, ...] = ()
    artifact_ids: tuple[NonEmptyString, ...]

    @model_validator(mode="after")
    def normalize_values(self) -> Self:
        object.__setattr__(self, "artifact_ids", tuple(sorted(set(self.artifact_ids))))
        object.__setattr__(
            self,
            "inventory_descriptions",
            tuple(sorted(set(self.inventory_descriptions), key=str.casefold)),
        )
        if any(item.application_id != self.application_id for item in self.owner_claims):
            raise ValueError("staged owner claim belongs to another application")
        claims = {item.claim_id: item for item in self.owner_claims}
        if len(claims) != len(self.owner_claims):
            raise ValueError("staged application contains duplicate owner claims")
        object.__setattr__(
            self,
            "owner_claims",
            tuple(claims[key] for key in sorted(claims)),
        )
        if not self.artifact_ids:
            raise ValueError("staged application must contain at least one Access artifact")
        return self


class StageIndex(StrictModel):
    schema_version: Literal["stage-index-v2"] = "stage-index-v2"
    generated_at: datetime
    inventory_sha256: Sha256
    applications: tuple[StagedApplication, ...]
    artifacts: tuple[StagedArtifactRecord, ...]
    exclusions: tuple[InventoryExclusion, ...] = ()
    failures: tuple[StagingFailure, ...] = ()

    @field_validator("generated_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_index(self) -> Self:
        artifact_by_id = {item.artifact_id: item for item in self.artifacts}
        if len(artifact_by_id) != len(self.artifacts):
            raise ValueError("stage index contains duplicate artifact IDs")
        application_ids = {item.application_id for item in self.applications}
        if len(application_ids) != len(self.applications):
            raise ValueError("stage index contains duplicate application IDs")
        referenced: set[str] = set()
        for application in self.applications:
            for artifact_id in application.artifact_ids:
                artifact = artifact_by_id.get(artifact_id)
                if artifact is None or artifact.application_id != application.application_id:
                    raise ValueError("application references an unknown or foreign artifact")
                referenced.add(artifact_id)
        if referenced != set(artifact_by_id):
            raise ValueError("every staged artifact must belong to exactly one application")
        object.__setattr__(
            self,
            "applications",
            tuple(sorted(self.applications, key=lambda item: item.application_id)),
        )
        object.__setattr__(
            self, "artifacts", tuple(sorted(self.artifacts, key=lambda item: item.artifact_id))
        )
        object.__setattr__(
            self,
            "exclusions",
            tuple(
                sorted(
                    self.exclusions,
                    key=lambda item: (
                        item.application_id,
                        item.source_locator.casefold(),
                    ),
                )
            ),
        )
        object.__setattr__(
            self,
            "failures",
            tuple(
                sorted(
                    self.failures,
                    key=lambda item: (
                        item.application_id,
                        item.source_locator.casefold(),
                    ),
                )
            ),
        )
        return self


class ExtractedObjectSnapshot(StrictModel):
    object_type: NonEmptyString
    name: NonEmptyString
    sanitized_definition: str | None = None
    sanitized_properties: dict[str, str] = Field(default_factory=dict)


class ExtractedArtifactSnapshot(StrictModel):
    schema_version: Literal["extraction-snapshot-v2"] = "extraction-snapshot-v2"
    application_id: NonEmptyString
    artifact_id: NonEmptyString
    artifact_sha256: Sha256
    extractor_version: NonEmptyString
    extraction_policy_version: NonEmptyString | None = None
    approved_libraries: tuple[ApprovedLibraryReference, ...] = ()
    injected_libraries: tuple[ApprovedLibraryReference, ...] = ()
    library_injection_status: LibraryInjectionStatus = (
        LibraryInjectionStatus.NOT_CONFIGURED
    )
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    coverage_status: Literal["complete", "partial"]
    warnings: tuple[NonEmptyString, ...] = ()
    objects: tuple[ExtractedObjectSnapshot, ...] = ()
    derived_copy_sha256: Sha256 | None = None
    tabledef_enumerated_count: Annotated[int, Field(ge=0)] = 0
    tabledef_succeeded_count: Annotated[int, Field(ge=0)] = 0
    tabledef_failed_count: Annotated[int, Field(ge=0)] = 0
    querydef_enumerated_count: Annotated[int, Field(ge=0)] = 0
    querydef_succeeded_count: Annotated[int, Field(ge=0)] = 0
    querydef_failed_count: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def normalize_snapshot(self) -> Self:
        if self.extracted_at.tzinfo is None or self.extracted_at.utcoffset() is None:
            raise ValueError("extraction timestamp must include a timezone")
        if self.tabledef_enumerated_count != (
            self.tabledef_succeeded_count + self.tabledef_failed_count
        ):
            raise ValueError("TableDef coverage counts are inconsistent")
        if self.querydef_enumerated_count != (
            self.querydef_succeeded_count + self.querydef_failed_count
        ):
            raise ValueError("QueryDef coverage counts are inconsistent")
        object.__setattr__(self, "warnings", tuple(sorted(set(self.warnings))))
        libraries = {item.library_id: item for item in self.approved_libraries}
        if len(libraries) != len(self.approved_libraries):
            raise ValueError("approved library identities must be unique")
        object.__setattr__(
            self,
            "approved_libraries",
            tuple(libraries[key] for key in sorted(libraries)),
        )
        injected = {item.library_id: item for item in self.injected_libraries}
        if len(injected) != len(self.injected_libraries):
            raise ValueError("injected library identities must be unique")
        if not set(injected).issubset(libraries):
            raise ValueError("injected libraries must be configured approved libraries")
        object.__setattr__(
            self,
            "injected_libraries",
            tuple(injected[key] for key in sorted(injected)),
        )
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
        object.__setattr__(
            self,
            "objects",
            tuple(
                sorted(
                    self.objects,
                    key=lambda item: (item.object_type.casefold(), item.name.casefold()),
                )
            ),
        )
        return self


def snapshot_from_extraction_result(
    *,
    application_id: str,
    artifact_id: str,
    artifact_sha256: str,
    staged_path: Path,
    extracted: AccessExtractionResult,
) -> ExtractedArtifactSnapshot:
    """Promote one sanitized worker result into the strict V2 snapshot contract."""
    if extracted.tool_inventory_id != application_id:
        raise ValueError("extraction result belongs to a different application")
    if extracted.artifact_id != artifact_id:
        raise ValueError("extraction result belongs to a different artifact")
    if extracted.staged_path.resolve() != staged_path.resolve():
        raise ValueError("extraction result references a different staged path")
    return ExtractedArtifactSnapshot(
        application_id=application_id,
        artifact_id=artifact_id,
        artifact_sha256=artifact_sha256,
        extractor_version=str(extracted.extractor_version),
        extraction_policy_version=EXTRACTION_POLICY_VERSION,
        approved_libraries=tuple(extracted.approved_libraries),
        injected_libraries=tuple(extracted.injected_libraries),
        library_injection_status=extracted.library_injection_status,
        coverage_status=extracted.coverage_status,
        warnings=tuple(str(item) for item in extracted.extraction_errors),
        objects=tuple(
            ExtractedObjectSnapshot(
                object_type=str(item.object_type),
                name=str(item.name),
                sanitized_definition=item.definition,
                sanitized_properties={
                    str(key): str(value) for key, value in item.properties.items()
                },
            )
            for item in extracted.objects
        ),
        derived_copy_sha256=extracted.derived_copy_sha256,
        tabledef_enumerated_count=int(extracted.tabledef_enumerated_count),
        tabledef_succeeded_count=int(extracted.tabledef_succeeded_count),
        tabledef_failed_count=int(extracted.tabledef_failed_count),
        querydef_enumerated_count=int(extracted.querydef_enumerated_count),
        querydef_succeeded_count=int(extracted.querydef_succeeded_count),
        querydef_failed_count=int(extracted.querydef_failed_count),
    )
