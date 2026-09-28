"""Strict V2 contracts between extraction, interpretation, portfolio, and reporting.

These models deliberately represent parsed connection identity rather than raw connection
strings.  Every free-text field is credential-sanitized before validation and serialization.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum, StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from portfolio_analyzer.library_identity import (
    ApprovedLibraryReference,
    LibraryInjectionStatus,
)
from portfolio_analyzer.v2.identity import normalize_source_identity, sanitize_text, stable_id

APPLICATION_EVIDENCE_SCHEMA_VERSION = "application-evidence-v2"
LOGICAL_INTERPRETATION_SCHEMA_VERSION = "logical-interpretation-v2"
APPLICATION_INTERPRETATION_SCHEMA_VERSION = "application-interpretation-v2"
PORTFOLIO_ANALYSIS_SCHEMA_VERSION = "portfolio-analysis-v2"
REPORT_MODEL_SCHEMA_VERSION = "report-model-v2"
PARTIAL_REPORT_WATERMARK: Literal[
    "PARTIAL REPORT - coverage omissions present; portfolio-wide absence claims are suppressed."
] = (
    "PARTIAL REPORT - coverage omissions present; portfolio-wide absence claims are suppressed."
)
QWEN_REPO_ID = "Qwen/Qwen2.5-0.5B-Instruct"
QWEN_REVISION = "7ae557604adf67be50417f59c2c2f167def9a775"

_RAW_CONNECTION_ASSIGNMENT = re.compile(
    r"(?i)(?:^|[;\"'])\s*(?:(?:ODBC|OLEDB)\s*[:;]\s*)?"
    r"(?:ODBC|PROVIDER|DRIVER|DSN|FILE\s*DSN|DBQ|SERVER|DATA\s*SOURCE|DATABASE|"
    r"INITIAL\s*CATALOG|TRUSTED_CONNECTION|UID|USER\s*ID|PWD|PASSWORD)\s*="
)


def _reject_raw_connection_value(value: str) -> str:
    if _RAW_CONNECTION_ASSIGNMENT.search(value):
        raise ValueError("value must not contain a connection string")
    return value


NonEmptyString = Annotated[str, Field(min_length=1)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class StrictModel(BaseModel):
    """Immutable model that rejects unknown fields and sanitizes all nested text."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
        validate_default=True,
    )

    @field_validator("*", mode="after", check_fields=False)
    @classmethod
    def sanitize_strings(cls, value: Any) -> Any:
        return _sanitize_runtime_value(value)


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ExtractionStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class EvidenceOrigin(StrEnum):
    OBSERVED = "observed_fact"
    OWNER_CLAIM = "owner_claim"
    UNRESOLVED = "unresolved"


class AccessObjectType(StrEnum):
    TABLE = "table"
    LINKED_TABLE = "linked_table"
    TABLE_LINK_STATUS_UNKNOWN = "table_link_status_unknown"
    QUERY = "query"
    FORM = "form"
    REPORT = "report"
    MACRO = "macro"
    MODULE = "module"
    PROCEDURE = "procedure"
    REFERENCE = "reference"


class QueryKind(StrEnum):
    SELECT = "select"
    ACTION = "action"
    CROSSTAB = "crosstab"
    PASS_THROUGH = "pass_through"
    PASS_THROUGH_BULK = "pass_through_bulk"
    COMPOUND = "compound"
    PROCEDURE = "procedure"
    DATA_DEFINITION = "data_definition"
    UNION = "union"
    UNKNOWN = "unknown"


class DataOperation(StrEnum):
    READ = "READ"
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    CREATE = "CREATE"
    EXECUTE = "EXECUTE"
    UNKNOWN = "UNKNOWN"


class ConnectionKind(StrEnum):
    LOCAL_ACCESS = "local_access"
    LINKED_TABLE = "linked_table"
    PASS_THROUGH_QUERY = "pass_through_query"
    VBA_CONNECTION = "vba_connection"
    FILE_DEPENDENCY = "file_dependency"
    UNKNOWN = "unknown"


class ConnectionProvenance(StrEnum):
    LOCAL_ACCESS = "Access local object"
    TABLEDEF_CONNECT = "TableDef.Connect"
    QUERYDEF_CONNECT = "QueryDef.Connect"
    VBA_CONNECTION_STRING = "VBA connection string"
    FILESYSTEM_REFERENCE = "filesystem reference"
    UNRESOLVED_DYNAMIC = "unresolved dynamic construction"


class ResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"
    WRONG_BITNESS = "wrong_bitness"
    PERMISSION_DENIED = "permission_denied"
    UNSUPPORTED_FILE_DSN = "unsupported_file_dsn"
    MALFORMED = "malformed"
    NOT_APPLICABLE = "not_applicable"


class InteractionScope(StrEnum):
    LOCAL = "local"
    EXTERNAL = "external"
    UNRESOLVED = "unresolved"


class DependencyNodeKind(StrEnum):
    APPLICATION = "application"
    ACCESS_OBJECT = "access_object"
    DATASOURCE = "datasource"
    DATABASE_OBJECT = "database_object"
    FILE = "file"
    EXTERNAL_SYSTEM = "external_system"


class DependencyRelation(StrEnum):
    REFERENCES = "references"
    CALLS = "calls"
    BINDS_TO = "binds_to"
    READS = "reads"
    WRITES = "writes"
    PRODUCES = "produces"
    DEPENDS_ON = "depends_on"


class InterpretationKind(StrEnum):
    PURPOSE = "purpose"
    WORKFLOW = "workflow"
    BUSINESS_CAPABILITY = "business_capability"
    MODERNIZATION_CONCERN = "modernization_concern"
    PORTFOLIO_OPPORTUNITY = "portfolio_opportunity"
    MIGRATION_CONSIDERATION = "migration_consideration"


class ReportStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    EDITED = "edited"
    REJECTED = "rejected"


class ReportOmissionStage(StrEnum):
    STAGE = "stage"
    EXTRACT = "extract"
    ANALYZE = "analyze"
    REPORT = "report"


class PortfolioCandidateType(StrEnum):
    SHARED_ENDPOINT = "shared_endpoint"
    SHARED_OBJECT = "shared_object"
    SHARED_FILE = "shared_file"
    EXACT_CODE = "exact_code"
    SEMANTIC_OVERLAP = "semantic_overlap"


class KeyValueFact(StrictModel):
    """A sanitized extracted property; connection blobs are intentionally forbidden."""

    name: NonEmptyString
    value: NonEmptyString
    evidence_ids: tuple[NonEmptyString, ...] = ()

    @field_validator("name")
    @classmethod
    def reject_connection_blob_names(cls, value: str) -> str:
        normalized = re.sub(r"[\s_-]", "", value).casefold()
        if normalized in {"connect", "connection", "connectionstring", "odbcconnect"}:
            raise ValueError("raw connection properties are not part of the V2 evidence contract")
        return value

    @field_validator("value")
    @classmethod
    def reject_connection_blob_values(cls, value: str) -> str:
        return _reject_raw_connection_value(value)

    @model_validator(mode="after")
    def normalize_references(self) -> Self:
        object.__setattr__(self, "evidence_ids", _sorted_unique(self.evidence_ids))
        return self


class ArtifactEvidence(StrictModel):
    application_id: NonEmptyString
    source_locator: NonEmptyString
    staged_relative_path: NonEmptyString
    filename: NonEmptyString
    access_format: Literal["accdb", "mdb"]
    sha256: Sha256
    size_bytes: Annotated[int, Field(ge=0)]
    extractor_version: NonEmptyString
    approved_libraries: tuple[ApprovedLibraryReference, ...] = ()
    injected_libraries: tuple[ApprovedLibraryReference, ...] = ()
    library_injection_status: LibraryInjectionStatus = (
        LibraryInjectionStatus.NOT_CONFIGURED
    )
    extraction_status: ExtractionStatus
    extracted_at: datetime
    warnings: tuple[NonEmptyString, ...] = ()
    artifact_id: str = ""

    @field_validator("sha256")
    @classmethod
    def normalize_digest(cls, value: str) -> str:
        return value.casefold()

    @field_validator("extracted_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        return _aware_timestamp(value)

    @field_validator("staged_relative_path")
    @classmethod
    def require_safe_relative_path(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        candidate = PurePosixPath(normalized)
        if candidate.is_absolute() or ".." in candidate.parts or normalized in {"", "."}:
            raise ValueError("staged_relative_path must be a contained relative path")
        return candidate.as_posix()

    @model_validator(mode="after")
    def assign_artifact_id(self) -> Self:
        expected = stable_id(
            "artifact", self.application_id, normalize_source_identity(self.source_locator)
        )
        _set_or_validate_id(self, "artifact_id", expected)
        expected_suffix = f".{self.access_format}"
        if not self.filename.casefold().endswith(expected_suffix):
            raise ValueError(f"filename must end with {expected_suffix}")
        object.__setattr__(self, "warnings", _sorted_unique(self.warnings))
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
        return self


class EvidenceRecord(StrictModel):
    application_id: NonEmptyString
    artifact_id: NonEmptyString
    artifact_sha256: Sha256
    object_id: str | None = None
    origin: EvidenceOrigin = EvidenceOrigin.OBSERVED
    fact_type: NonEmptyString
    location: str | None = None
    observation: NonEmptyString
    confidence: Confidence = Confidence.HIGH
    evidence_id: str = ""

    @model_validator(mode="after")
    def assign_evidence_id(self) -> Self:
        expected = stable_id(
            "evidence",
            self.application_id,
            self.artifact_id,
            self.artifact_sha256,
            self.object_id,
            self.origin,
            self.fact_type,
            self.location,
            self.observation,
        )
        _set_or_validate_id(self, "evidence_id", expected)
        return self


class OwnerClaim(StrictModel):
    application_id: NonEmptyString
    field: NonEmptyString
    value: NonEmptyString
    source: NonEmptyString
    claim_id: str = ""

    @model_validator(mode="after")
    def assign_claim_id(self) -> Self:
        expected = stable_id(
            "claim", self.application_id, self.field, self.value, self.source
        )
        _set_or_validate_id(self, "claim_id", expected)
        return self


class AccessObjectEvidence(StrictModel):
    artifact_id: NonEmptyString
    object_type: AccessObjectType
    name: NonEmptyString
    sanitized_definition: str | None = None
    attributes: tuple[KeyValueFact, ...] = ()
    evidence_ids: tuple[NonEmptyString, ...] = ()
    object_id: str = ""

    @model_validator(mode="after")
    def assign_object_id(self) -> Self:
        expected = stable_id(
            "object", self.artifact_id, self.object_type.value, self.name.casefold()
        )
        _set_or_validate_id(self, "object_id", expected)
        object.__setattr__(
            self,
            "attributes",
            tuple(sorted(self.attributes, key=lambda item: item.name)),
        )
        object.__setattr__(self, "evidence_ids", _sorted_unique(self.evidence_ids))
        return self


class ConnectionEvidence(StrictModel):
    """One observed connection instance; never the raw connection string."""

    application_id: NonEmptyString
    artifact_id: NonEmptyString
    artifact_sha256: Sha256
    object_id: NonEmptyString
    connection_kind: ConnectionKind
    sanitized_summary: NonEmptyString
    direct_provenance: ConnectionProvenance
    resolution_status: ResolutionStatus
    resolution_provenance: tuple[NonEmptyString, ...] = ()
    resolution_warnings: tuple[NonEmptyString, ...] = ()
    datasource_id: str | None = None
    evidence_ids: tuple[NonEmptyString, ...] = ()
    connection_id: str = ""

    @field_validator("sanitized_summary")
    @classmethod
    def reject_raw_connection_shape(cls, value: str) -> str:
        return _reject_raw_connection_value(value)

    @model_validator(mode="after")
    def assign_connection_id(self) -> Self:
        if self.connection_kind == ConnectionKind.LOCAL_ACCESS:
            if self.resolution_status != ResolutionStatus.NOT_APPLICABLE:
                raise ValueError("local connection resolution must be not_applicable")
            if self.direct_provenance != ConnectionProvenance.LOCAL_ACCESS:
                raise ValueError("local connection must use local provenance")
            if self.datasource_id is not None:
                raise ValueError("local connection cannot claim an external datasource")
        elif self.resolution_status == ResolutionStatus.RESOLVED and self.datasource_id is None:
            raise ValueError("resolved external connection must reference a datasource")
        for field_name in (
            "resolution_provenance",
            "resolution_warnings",
            "evidence_ids",
        ):
            object.__setattr__(self, field_name, _sorted_unique(getattr(self, field_name)))
        expected = stable_id(
            "connection",
            self.application_id,
            self.artifact_id,
            self.artifact_sha256,
            self.object_id,
            self.connection_kind,
            self.direct_provenance,
            self.sanitized_summary,
        )
        _set_or_validate_id(self, "connection_id", expected)
        return self


class AccessTableEvidence(StrictModel):
    """TableDef metadata with only parsed datasource identity references."""

    object_id: NonEmptyString
    is_linked: bool | None
    connect_metadata_status: Literal["available", "unavailable"] = "available"
    is_hidden: bool = False
    is_system: bool = False
    source_table_name: str | None = None
    connection_id: str | None = None
    evidence_ids: tuple[NonEmptyString, ...] = ()

    @model_validator(mode="after")
    def validate_linkage(self) -> Self:
        if self.connect_metadata_status == "unavailable":
            if self.is_linked is not None:
                raise ValueError(
                    "table linkage must remain unknown when Connect metadata is unavailable"
                )
            if self.connection_id is not None:
                raise ValueError(
                    "table with unavailable Connect metadata cannot reference connection evidence"
                )
            object.__setattr__(self, "evidence_ids", _sorted_unique(self.evidence_ids))
            return self
        if self.is_linked is None:
            raise ValueError("available Connect metadata must determine table linkage")
        if self.is_linked and self.connection_id is None:
            raise ValueError("linked table metadata must reference observed connection evidence")
        if not self.is_linked and (
            self.connection_id is not None or self.source_table_name is not None
        ):
            raise ValueError("local table metadata cannot declare external linkage")
        object.__setattr__(self, "evidence_ids", _sorted_unique(self.evidence_ids))
        return self


class QueryParameterEvidence(StrictModel):
    """One ordered DAO QueryDef parameter without a runtime value."""

    ordinal: Annotated[int, Field(ge=0)]
    name: NonEmptyString
    dao_type: int | None = None
    direction: int | None = None


class QueryEvidence(StrictModel):
    object_id: NonEmptyString
    query_kind: QueryKind
    dao_type: int | None = None
    attributes: int | None = None
    sanitized_sql: str | None = None
    returns_records: bool | None = None
    odbc_timeout_seconds: Annotated[int, Field(ge=0)] | None = None
    is_hidden: bool = False
    is_system: bool = False
    connect_metadata_status: Literal["available", "unavailable"] = "available"
    connection_kind: ConnectionKind
    connection_id: str | None = None
    parameters: tuple[QueryParameterEvidence, ...] = ()
    parameter_names: tuple[NonEmptyString, ...] = ()
    evidence_ids: tuple[NonEmptyString, ...] = ()

    @model_validator(mode="after")
    def normalize_collections(self) -> Self:
        if self.connect_metadata_status == "unavailable":
            if self.connection_kind == ConnectionKind.LOCAL_ACCESS:
                raise ValueError(
                    "query cannot be classified as local when Connect metadata is unavailable"
                )
            if self.connection_id is None:
                raise ValueError(
                    "query with unavailable Connect metadata must retain unresolved "
                    "connection evidence"
                )
        if self.query_kind in {
            QueryKind.PASS_THROUGH,
            QueryKind.PASS_THROUGH_BULK,
        }:
            if self.connection_kind != ConnectionKind.PASS_THROUGH_QUERY:
                raise ValueError("pass-through query must use pass-through connection linkage")
            if self.connection_id is None:
                raise ValueError("pass-through query must reference observed connection evidence")
        elif self.connection_kind == ConnectionKind.PASS_THROUGH_QUERY:
            raise ValueError("pass-through connection linkage requires a pass-through query")
        if self.connection_kind != ConnectionKind.LOCAL_ACCESS and self.connection_id is None:
            raise ValueError("external query connection must reference connection evidence")
        ordinals = [item.ordinal for item in self.parameters]
        names = [item.name.casefold() for item in self.parameters]
        if len(ordinals) != len(set(ordinals)):
            raise ValueError("query parameter ordinals must be unique")
        if len(names) != len(set(names)):
            raise ValueError("query parameter names must be unique")
        parameters = tuple(sorted(self.parameters, key=lambda item: item.ordinal))
        object.__setattr__(self, "parameters", parameters)
        derived_names = tuple(item.name for item in parameters)
        supplied_names = _sorted_unique(self.parameter_names)
        if parameters and supplied_names and set(supplied_names) != set(derived_names):
            raise ValueError("query parameter_names must agree with typed parameters")
        object.__setattr__(
            self,
            "parameter_names",
            derived_names if parameters else supplied_names,
        )
        object.__setattr__(self, "evidence_ids", _sorted_unique(self.evidence_ids))
        return self


class DatasourceIdentity(StrictModel):
    """Canonical parsed endpoint, independent of any observed connection instance."""

    platform: NonEmptyString
    driver: str | None = None
    dsn: str | None = None
    server: str | None = None
    database: str | None = None
    resource: str | None = None
    datasource_id: str = ""

    @field_validator("platform", "driver", "dsn", "server", "database", "resource")
    @classmethod
    def reject_raw_connection_fields(cls, value: str | None) -> str | None:
        return None if value is None else _reject_raw_connection_value(value)

    @model_validator(mode="after")
    def assign_datasource_id(self) -> Self:
        if not any((self.driver, self.dsn, self.server, self.database, self.resource)):
            raise ValueError("datasource identity needs at least one parsed endpoint field")
        expected = stable_id(
            "datasource",
            self.platform.casefold(),
            _casefold_optional(self.driver),
            _casefold_optional(self.dsn),
            _casefold_optional(self.server),
            _casefold_optional(self.database),
            _casefold_optional(self.resource),
        )
        _set_or_validate_id(self, "datasource_id", expected)
        return self


class DatasourceInteraction(StrictModel):
    source_object_id: NonEmptyString
    operation: DataOperation
    scope: InteractionScope
    connection_id: str | None = None
    datasource_id: str | None = None
    local_target_object_id: str | None = None
    catalog: str | None = None
    database: str | None = None
    schema_name: str | None = None
    object_name: str | None = None
    evidence_ids: tuple[NonEmptyString, ...] = ()
    confidence: Confidence = Confidence.HIGH
    interaction_id: str = ""

    @field_validator("catalog", "database", "schema_name", "object_name")
    @classmethod
    def reject_raw_connection_fields(cls, value: str | None) -> str | None:
        return None if value is None else _reject_raw_connection_value(value)

    @model_validator(mode="after")
    def assign_interaction_id(self) -> Self:
        if self.scope == InteractionScope.LOCAL:
            if (
                self.local_target_object_id is None
                or self.connection_id is not None
                or self.datasource_id is not None
            ):
                raise ValueError("local interaction must target one local Access object")
            if any((self.catalog, self.database, self.schema_name, self.object_name)):
                raise ValueError("local interaction cannot carry external object identity")
        elif self.scope == InteractionScope.EXTERNAL:
            if (
                self.connection_id is None
                or self.datasource_id is None
                or self.local_target_object_id is not None
            ):
                raise ValueError(
                    "external interaction must reference connection evidence and a datasource"
                )
        elif (
            self.connection_id is None
            or self.datasource_id is not None
            or self.local_target_object_id is not None
        ):
            raise ValueError(
                "unresolved interaction must reference only observed connection evidence"
            )
        expected = stable_id(
            "interaction",
            self.source_object_id,
            self.operation,
            self.scope,
            self.connection_id,
            self.datasource_id,
            self.local_target_object_id,
            _casefold_optional(self.catalog),
            _casefold_optional(self.database),
            _casefold_optional(self.schema_name),
            _casefold_optional(self.object_name),
        )
        _set_or_validate_id(self, "interaction_id", expected)
        object.__setattr__(self, "evidence_ids", _sorted_unique(self.evidence_ids))
        return self


class DependencyNode(StrictModel):
    application_id: NonEmptyString
    kind: DependencyNodeKind
    label: NonEmptyString
    artifact_id: str | None = None
    object_id: str | None = None
    datasource_id: str | None = None
    node_id: str = ""

    @model_validator(mode="after")
    def assign_node_id(self) -> Self:
        expected = stable_id(
            "node",
            self.application_id,
            self.kind,
            self.artifact_id,
            self.object_id,
            self.datasource_id,
            self.label.casefold(),
        )
        _set_or_validate_id(self, "node_id", expected)
        return self


class DependencyEdge(StrictModel):
    source_node_id: NonEmptyString
    target_node_id: NonEmptyString
    relationship: DependencyRelation
    operation: DataOperation = DataOperation.UNKNOWN
    evidence_ids: tuple[NonEmptyString, ...] = ()
    confidence: Confidence = Confidence.HIGH
    edge_id: str = ""

    @model_validator(mode="after")
    def assign_edge_id(self) -> Self:
        if self.source_node_id == self.target_node_id:
            raise ValueError("dependency edge cannot be self-referential")
        expected = stable_id(
            "edge",
            self.source_node_id,
            self.target_node_id,
            self.relationship,
            self.operation,
        )
        _set_or_validate_id(self, "edge_id", expected)
        object.__setattr__(self, "evidence_ids", _sorted_unique(self.evidence_ids))
        return self


class UnresolvedReference(StrictModel):
    source_object_id: NonEmptyString
    reference: NonEmptyString
    reason: NonEmptyString
    evidence_ids: tuple[NonEmptyString, ...] = ()
    unresolved_id: str = ""

    @model_validator(mode="after")
    def assign_unresolved_id(self) -> Self:
        expected = stable_id(
            "unresolved", self.source_object_id, self.reference, self.reason
        )
        _set_or_validate_id(self, "unresolved_id", expected)
        object.__setattr__(self, "evidence_ids", _sorted_unique(self.evidence_ids))
        return self


class ExtractionCoverage(StrictModel):
    primary_artifact_count: Annotated[int, Field(ge=1)]
    complete_artifact_count: Annotated[int, Field(ge=0)]
    partial_artifact_count: Annotated[int, Field(ge=0)]
    failed_artifact_count: Annotated[int, Field(ge=0)]
    discovered_object_count: Annotated[int, Field(ge=0)]
    extracted_object_count: Annotated[int, Field(ge=0)]
    warning_count: Annotated[int, Field(ge=0)]
    unresolved_reference_count: Annotated[int, Field(ge=0)]
    tabledef_enumerated_count: Annotated[int, Field(ge=0)] = 0
    tabledef_succeeded_count: Annotated[int, Field(ge=0)] = 0
    tabledef_failed_count: Annotated[int, Field(ge=0)] = 0
    querydef_enumerated_count: Annotated[int, Field(ge=0)] = 0
    querydef_succeeded_count: Annotated[int, Field(ge=0)] = 0
    querydef_failed_count: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def counts_are_consistent(self) -> Self:
        processed = (
            self.complete_artifact_count
            + self.partial_artifact_count
            + self.failed_artifact_count
        )
        if processed != self.primary_artifact_count:
            raise ValueError("artifact status counts must equal primary_artifact_count")
        if self.extracted_object_count > self.discovered_object_count:
            raise ValueError("extracted objects cannot exceed discovered objects")
        if self.tabledef_enumerated_count != (
            self.tabledef_succeeded_count + self.tabledef_failed_count
        ):
            raise ValueError("TableDef coverage counts are inconsistent")
        if self.querydef_enumerated_count != (
            self.querydef_succeeded_count + self.querydef_failed_count
        ):
            raise ValueError("QueryDef coverage counts are inconsistent")
        return self


class ApplicationEvidenceBundle(StrictModel):
    schema_version: Literal["application-evidence-v2"] = "application-evidence-v2"
    application_id: NonEmptyString
    application_name: NonEmptyString
    inventory_record_ids: tuple[NonEmptyString, ...]
    generated_at: datetime
    artifacts: tuple[ArtifactEvidence, ...]
    objects: tuple[AccessObjectEvidence, ...] = ()
    connections: tuple[ConnectionEvidence, ...] = ()
    tables: tuple[AccessTableEvidence, ...] = ()
    queries: tuple[QueryEvidence, ...] = ()
    datasources: tuple[DatasourceIdentity, ...] = ()
    interactions: tuple[DatasourceInteraction, ...] = ()
    dependency_nodes: tuple[DependencyNode, ...] = ()
    dependency_edges: tuple[DependencyEdge, ...] = ()
    evidence: tuple[EvidenceRecord, ...] = ()
    owner_claims: tuple[OwnerClaim, ...] = ()
    unresolved_references: tuple[UnresolvedReference, ...] = ()
    coverage: ExtractionCoverage

    @field_validator("generated_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        return _aware_timestamp(value)

    @model_validator(mode="after")
    def validate_graph_and_references(self) -> Self:
        artifacts = _unique_by(self.artifacts, "artifact_id")
        objects = _unique_by(self.objects, "object_id")
        datasources = _unique_by(self.datasources, "datasource_id")
        connections = _unique_by(self.connections, "connection_id")
        evidence = _unique_by(self.evidence, "evidence_id")
        nodes = _unique_by(self.dependency_nodes, "node_id")
        _unique_by(self.tables, "object_id")
        _unique_by(self.queries, "object_id")
        _unique_by(self.interactions, "interaction_id")
        _unique_by(self.dependency_edges, "edge_id")
        _unique_by(self.owner_claims, "claim_id")
        _unique_by(self.unresolved_references, "unresolved_id")

        if len(artifacts) != self.coverage.primary_artifact_count:
            raise ValueError("bundle artifacts must match primary_artifact_count")
        actual_status_counts = {
            ExtractionStatus.COMPLETE: sum(
                item.extraction_status == ExtractionStatus.COMPLETE
                for item in artifacts.values()
            ),
            ExtractionStatus.PARTIAL: sum(
                item.extraction_status == ExtractionStatus.PARTIAL
                for item in artifacts.values()
            ),
            ExtractionStatus.FAILED: sum(
                item.extraction_status == ExtractionStatus.FAILED
                for item in artifacts.values()
            ),
        }
        if actual_status_counts != {
            ExtractionStatus.COMPLETE: self.coverage.complete_artifact_count,
            ExtractionStatus.PARTIAL: self.coverage.partial_artifact_count,
            ExtractionStatus.FAILED: self.coverage.failed_artifact_count,
        }:
            raise ValueError("bundle artifact statuses must match coverage counts")

        for artifact in artifacts.values():
            if artifact.application_id != self.application_id:
                raise ValueError("artifact belongs to a different application")
        for item in evidence.values():
            if item.application_id != self.application_id or item.artifact_id not in artifacts:
                raise ValueError("evidence references an unknown artifact or application")
            if item.artifact_sha256 != artifacts[item.artifact_id].sha256:
                raise ValueError("evidence artifact SHA-256 does not match its artifact")
            if item.object_id is not None:
                source_object = objects.get(item.object_id)
                if source_object is None or source_object.artifact_id != item.artifact_id:
                    raise ValueError("evidence object does not belong to its artifact")
        for item in objects.values():
            if item.artifact_id not in artifacts:
                raise ValueError("Access object references an unknown artifact")
            _require_known(item.evidence_ids, evidence, "object evidence")
            for attribute in item.attributes:
                _require_known(attribute.evidence_ids, evidence, "attribute evidence")
        for table in self.tables:
            source_object = objects.get(table.object_id)
            if source_object is None or source_object.object_type not in {
                AccessObjectType.TABLE,
                AccessObjectType.LINKED_TABLE,
                AccessObjectType.TABLE_LINK_STATUS_UNKNOWN,
            }:
                raise ValueError("table metadata must reference a table Access object")
            expected_linkage = {
                AccessObjectType.TABLE: False,
                AccessObjectType.LINKED_TABLE: True,
                AccessObjectType.TABLE_LINK_STATUS_UNKNOWN: None,
            }[source_object.object_type]
            if table.is_linked is not expected_linkage:
                raise ValueError("table linkage must agree with its Access object type")
            expected_connect_status = (
                "unavailable"
                if source_object.object_type == AccessObjectType.TABLE_LINK_STATUS_UNKNOWN
                else "available"
            )
            if table.connect_metadata_status != expected_connect_status:
                raise ValueError(
                    "table Connect metadata status must agree with its Access object type"
                )
            if table.connection_id is not None:
                connection = connections.get(table.connection_id)
                if connection is None or connection.object_id != table.object_id:
                    raise ValueError("table metadata references an unknown connection")
            _require_known(table.evidence_ids, evidence, "table evidence")
        for query in self.queries:
            source_object = objects.get(query.object_id)
            if source_object is None or source_object.object_type != AccessObjectType.QUERY:
                raise ValueError("query metadata must reference a query Access object")
            if query.connection_id is not None:
                connection = connections.get(query.connection_id)
                if connection is None or connection.object_id != query.object_id:
                    raise ValueError("query metadata references an unknown connection")
            _require_known(query.evidence_ids, evidence, "query evidence")
        for connection in connections.values():
            if connection.application_id != self.application_id:
                raise ValueError("connection evidence belongs to a different application")
            artifact = artifacts.get(connection.artifact_id)
            source_object = objects.get(connection.object_id)
            if artifact is None or connection.artifact_sha256 != artifact.sha256:
                raise ValueError("connection evidence references an unknown artifact version")
            if source_object is None or source_object.artifact_id != connection.artifact_id:
                raise ValueError("connection evidence object does not belong to its artifact")
            if (
                connection.datasource_id is not None
                and connection.datasource_id not in datasources
            ):
                raise ValueError("connection evidence references an unknown datasource")
            _require_known(connection.evidence_ids, evidence, "connection evidence")
        for interaction in self.interactions:
            if interaction.source_object_id not in objects:
                raise ValueError("interaction references an unknown source object")
            if (
                interaction.connection_id is not None
                and interaction.connection_id not in connections
            ):
                raise ValueError("interaction references unknown connection evidence")
            if (
                interaction.datasource_id is not None
                and interaction.datasource_id not in datasources
            ):
                raise ValueError("interaction references an unknown datasource")
            if (
                interaction.local_target_object_id is not None
                and interaction.local_target_object_id not in objects
            ):
                raise ValueError("interaction references an unknown local target")
            if (
                interaction.connection_id is not None
                and interaction.datasource_id is not None
                and connections[interaction.connection_id].datasource_id
                != interaction.datasource_id
            ):
                raise ValueError("interaction datasource disagrees with connection evidence")
            _require_known(interaction.evidence_ids, evidence, "interaction evidence")
        for node in nodes.values():
            if node.application_id != self.application_id:
                raise ValueError("dependency node belongs to a different application")
            if node.artifact_id is not None and node.artifact_id not in artifacts:
                raise ValueError("dependency node references an unknown artifact")
            if node.object_id is not None and node.object_id not in objects:
                raise ValueError("dependency node references an unknown object")
            if node.datasource_id is not None and node.datasource_id not in datasources:
                raise ValueError("dependency node references an unknown datasource")
        for edge in self.dependency_edges:
            if edge.source_node_id not in nodes or edge.target_node_id not in nodes:
                raise ValueError("dependency edge references an unknown node")
            _require_known(edge.evidence_ids, evidence, "dependency edge evidence")
        for claim in self.owner_claims:
            if claim.application_id != self.application_id:
                raise ValueError("owner claim belongs to a different application")
        for unresolved in self.unresolved_references:
            if unresolved.source_object_id not in objects:
                raise ValueError("unresolved reference has an unknown source object")
            _require_known(unresolved.evidence_ids, evidence, "unresolved evidence")

        object.__setattr__(self, "inventory_record_ids", _sorted_unique(self.inventory_record_ids))
        _sort_model_tuple(self, "artifacts", "artifact_id")
        _sort_model_tuple(self, "objects", "object_id")
        _sort_model_tuple(self, "connections", "connection_id")
        _sort_model_tuple(self, "tables", "object_id")
        _sort_model_tuple(self, "queries", "object_id")
        _sort_model_tuple(self, "datasources", "datasource_id")
        _sort_model_tuple(self, "interactions", "interaction_id")
        _sort_model_tuple(self, "dependency_nodes", "node_id")
        _sort_model_tuple(self, "dependency_edges", "edge_id")
        _sort_model_tuple(self, "evidence", "evidence_id")
        _sort_model_tuple(self, "owner_claims", "claim_id")
        _sort_model_tuple(self, "unresolved_references", "unresolved_id")
        return self


class ModelProvenance(StrictModel):
    # Historical provenance must remain readable so incompatible payloads can be
    # rejected cleanly instead of making a workspace unreadable. Runtime model
    # authorization remains fixed in semantic.model_store and qwen.provider.
    model_repo_id: NonEmptyString = QWEN_REPO_ID
    model_revision: NonEmptyString = QWEN_REVISION
    model_manifest_sha256: Sha256
    prompt_version: NonEmptyString
    output_schema_version: NonEmptyString
    inference_library_version: NonEmptyString
    generation_parameters: tuple[KeyValueFact, ...] = ()

    @model_validator(mode="after")
    def normalize_parameters(self) -> Self:
        names = [item.name for item in self.generation_parameters]
        if len(set(names)) != len(names):
            raise ValueError("generation parameter names must be unique")
        object.__setattr__(
            self,
            "generation_parameters",
            tuple(sorted(self.generation_parameters, key=lambda item: item.name)),
        )
        return self


def is_current_model_provenance(value: ModelProvenance) -> bool:
    """Return whether persisted provenance belongs to the sole approved runtime model."""
    return (
        value.model_repo_id == QWEN_REPO_ID
        and value.model_revision == QWEN_REVISION
    )


class InterpretiveFinding(StrictModel):
    application_id: NonEmptyString
    kind: InterpretationKind
    title: NonEmptyString
    explanation: NonEmptyString
    confidence: Confidence
    evidence_ids: tuple[NonEmptyString, ...]
    claim_ids: tuple[NonEmptyString, ...] = ()
    uncertainties: tuple[NonEmptyString, ...] = ()
    finding_id: str = ""

    @model_validator(mode="after")
    def assign_finding_id(self) -> Self:
        object.__setattr__(self, "evidence_ids", _sorted_unique(self.evidence_ids))
        object.__setattr__(self, "claim_ids", _sorted_unique(self.claim_ids))
        object.__setattr__(self, "uncertainties", _sorted_unique(self.uncertainties))
        if not self.evidence_ids and not self.claim_ids:
            raise ValueError("interpretive finding must cite evidence or an owner claim")
        if self.confidence == Confidence.HIGH:
            raise ValueError(
                "model interpretation confidence cannot be high before human review"
            )
        expected = stable_id(
            "finding",
            self.application_id,
            self.kind,
            self.title.casefold(),
            self.evidence_ids,
            self.claim_ids,
        )
        _set_or_validate_id(self, "finding_id", expected)
        return self


class LogicalUnitInterpretation(StrictModel):
    schema_version: Literal["logical-interpretation-v2"] = "logical-interpretation-v2"
    application_id: NonEmptyString
    logical_unit_id: NonEmptyString
    source_bundle_sha256: Sha256
    purpose: NonEmptyString
    business_entities: tuple[NonEmptyString, ...] = ()
    workflow_actions: tuple[NonEmptyString, ...] = ()
    datasource_interaction_ids: tuple[NonEmptyString, ...] = ()
    called_object_ids: tuple[NonEmptyString, ...] = ()
    generated_outputs: tuple[NonEmptyString, ...] = ()
    user_interactions: tuple[NonEmptyString, ...] = ()
    important_business_terms: tuple[NonEmptyString, ...] = ()
    uncertainties: tuple[NonEmptyString, ...] = ()
    evidence_ids: tuple[NonEmptyString, ...]
    provenance: ModelProvenance
    interpretation_id: str = ""

    @model_validator(mode="after")
    def assign_interpretation_id(self) -> Self:
        for field_name in (
            "business_entities",
            "workflow_actions",
            "datasource_interaction_ids",
            "called_object_ids",
            "generated_outputs",
            "user_interactions",
            "important_business_terms",
            "uncertainties",
            "evidence_ids",
        ):
            object.__setattr__(self, field_name, _sorted_unique(getattr(self, field_name)))
        if not self.evidence_ids:
            raise ValueError("logical-unit interpretation must cite observed evidence")
        expected = stable_id(
            "unit_interpretation",
            self.application_id,
            self.logical_unit_id,
            self.source_bundle_sha256,
            self.provenance.model_manifest_sha256,
            self.provenance.prompt_version,
            self.provenance.output_schema_version,
        )
        _set_or_validate_id(self, "interpretation_id", expected)
        return self


class ApplicationInterpretation(StrictModel):
    schema_version: Literal["application-interpretation-v2"] = (
        "application-interpretation-v2"
    )
    application_id: NonEmptyString
    source_bundle_sha256: Sha256
    logical_unit_interpretation_ids: tuple[NonEmptyString, ...]
    summary: NonEmptyString
    business_purpose: NonEmptyString
    major_workflows: tuple[NonEmptyString, ...] = ()
    capabilities: tuple[NonEmptyString, ...] = ()
    modernization_concerns: tuple[NonEmptyString, ...] = ()
    uncertainties: tuple[NonEmptyString, ...] = ()
    findings: tuple[InterpretiveFinding, ...] = ()
    evidence_ids: tuple[NonEmptyString, ...]
    claim_ids: tuple[NonEmptyString, ...] = ()
    provenance: ModelProvenance
    interpretation_id: str = ""

    @model_validator(mode="after")
    def assign_interpretation_id(self) -> Self:
        for finding in self.findings:
            if finding.application_id != self.application_id:
                raise ValueError("application finding belongs to a different application")
        for field_name in (
            "logical_unit_interpretation_ids",
            "major_workflows",
            "capabilities",
            "modernization_concerns",
            "uncertainties",
            "evidence_ids",
            "claim_ids",
        ):
            object.__setattr__(self, field_name, _sorted_unique(getattr(self, field_name)))
        if not self.evidence_ids and not self.claim_ids:
            raise ValueError("application interpretation must cite evidence or an owner claim")
        _sort_model_tuple(self, "findings", "finding_id")
        expected = stable_id(
            "application_interpretation",
            self.application_id,
            self.source_bundle_sha256,
            self.logical_unit_interpretation_ids,
            self.provenance.model_manifest_sha256,
            self.provenance.prompt_version,
            self.provenance.output_schema_version,
        )
        _set_or_validate_id(self, "interpretation_id", expected)
        return self


class ApplicationSimilarity(StrictModel):
    source_application_id: NonEmptyString
    target_application_id: NonEmptyString
    score: Annotated[float, Field(ge=0.0, le=1.0)]
    shared_features: tuple[NonEmptyString, ...]
    evidence_ids: tuple[NonEmptyString, ...]

    @model_validator(mode="after")
    def normalize_similarity(self) -> Self:
        if self.source_application_id >= self.target_application_id:
            raise ValueError("similarity application IDs must be unique and sorted")
        object.__setattr__(self, "shared_features", _sorted_unique(self.shared_features))
        object.__setattr__(self, "evidence_ids", _sorted_unique(self.evidence_ids))
        return self


class PortfolioCandidate(StrictModel):
    """Deterministic comparison candidate, separate from Qwen interpretation."""

    candidate_type: PortfolioCandidateType
    application_ids: tuple[NonEmptyString, ...]
    evidence_ids: tuple[NonEmptyString, ...]
    basis_ids: tuple[NonEmptyString, ...] = ()
    score: Annotated[float, Field(ge=0.0, le=1.0)]
    policy_version: NonEmptyString
    candidate_id: str = ""

    @model_validator(mode="after")
    def assign_candidate_id(self) -> Self:
        object.__setattr__(self, "application_ids", _sorted_unique(self.application_ids))
        object.__setattr__(self, "evidence_ids", _sorted_unique(self.evidence_ids))
        object.__setattr__(self, "basis_ids", _sorted_unique(self.basis_ids))
        if len(self.application_ids) < 2:
            raise ValueError("portfolio candidate must compare at least two applications")
        if not self.evidence_ids:
            raise ValueError("portfolio candidate must cite observed evidence")
        expected = stable_id(
            "portfolio_candidate",
            self.candidate_type,
            self.application_ids,
            self.evidence_ids,
            self.basis_ids,
            self.score,
            self.policy_version,
        )
        _set_or_validate_id(self, "candidate_id", expected)
        return self


class PortfolioFinding(StrictModel):
    kind: InterpretationKind
    title: NonEmptyString
    narrative: NonEmptyString
    application_ids: tuple[NonEmptyString, ...]
    evidence_ids: tuple[NonEmptyString, ...]
    confidence: Confidence
    uncertainties: tuple[NonEmptyString, ...] = ()
    finding_id: str = ""

    @model_validator(mode="after")
    def assign_finding_id(self) -> Self:
        object.__setattr__(self, "application_ids", _sorted_unique(self.application_ids))
        object.__setattr__(self, "evidence_ids", _sorted_unique(self.evidence_ids))
        object.__setattr__(self, "uncertainties", _sorted_unique(self.uncertainties))
        if not self.application_ids or not self.evidence_ids:
            raise ValueError("portfolio finding must cite applications and observed evidence")
        if self.confidence == Confidence.HIGH:
            raise ValueError(
                "model interpretation confidence cannot be high before human review"
            )
        expected = stable_id(
            "portfolio_finding",
            self.kind,
            self.title.casefold(),
            self.application_ids,
            self.evidence_ids,
        )
        _set_or_validate_id(self, "finding_id", expected)
        return self


class PortfolioAnalysis(StrictModel):
    schema_version: Literal["portfolio-analysis-v2"] = "portfolio-analysis-v2"
    application_interpretation_sha256s: tuple[Sha256, ...]
    similarities: tuple[ApplicationSimilarity, ...] = ()
    findings: tuple[PortfolioFinding, ...] = ()
    uncertainties: tuple[NonEmptyString, ...] = ()
    provenance: ModelProvenance
    portfolio_analysis_id: str = ""

    @model_validator(mode="after")
    def assign_portfolio_analysis_id(self) -> Self:
        object.__setattr__(
            self,
            "application_interpretation_sha256s",
            _sorted_unique(self.application_interpretation_sha256s),
        )
        object.__setattr__(self, "uncertainties", _sorted_unique(self.uncertainties))
        object.__setattr__(
            self,
            "similarities",
            tuple(
                sorted(
                    self.similarities,
                    key=lambda item: (
                        item.source_application_id,
                        item.target_application_id,
                    ),
                )
            ),
        )
        _sort_model_tuple(self, "findings", "finding_id")
        expected = stable_id(
            "portfolio_analysis",
            self.application_interpretation_sha256s,
            self.provenance.model_manifest_sha256,
            self.provenance.prompt_version,
            self.provenance.output_schema_version,
        )
        _set_or_validate_id(self, "portfolio_analysis_id", expected)
        return self


class ReportApplicationEntry(StrictModel):
    application_id: NonEmptyString
    application_name: NonEmptyString
    evidence_bundle_sha256: Sha256
    interpretation_sha256: Sha256 | None
    status: ExtractionStatus
    summary: str | None = None
    business_purpose: str | None = None
    major_workflows: tuple[NonEmptyString, ...] = ()
    capabilities: tuple[NonEmptyString, ...] = ()
    artifact_count: Annotated[int, Field(ge=0)] = 0
    object_count: Annotated[int, Field(ge=0)] = 0
    table_count: Annotated[int, Field(ge=0)] = 0
    query_count: Annotated[int, Field(ge=0)] = 0
    datasource_count: Annotated[int, Field(ge=0)] = 0
    connection_count: Annotated[int, Field(ge=0)] = 0
    interaction_count: Annotated[int, Field(ge=0)] = 0
    evidence_count: Annotated[int, Field(ge=0)] = 0
    unresolved_reference_count: Annotated[int, Field(ge=0)] = 0
    coverage_notes: tuple[NonEmptyString, ...] = ()

    @model_validator(mode="after")
    def normalize_notes(self) -> Self:
        object.__setattr__(self, "major_workflows", _sorted_unique(self.major_workflows))
        object.__setattr__(self, "capabilities", _sorted_unique(self.capabilities))
        object.__setattr__(self, "coverage_notes", _sorted_unique(self.coverage_notes))
        return self


class ReportReviewRecord(StrictModel):
    application_id: NonEmptyString
    subject_id: NonEmptyString
    status: ReviewStatus
    edited_value: str | None = None
    reviewer: str | None = None
    notes: str | None = None
    evidence_ids: tuple[NonEmptyString, ...] = ()
    claim_ids: tuple[NonEmptyString, ...] = ()
    review_id: str = ""

    @model_validator(mode="after")
    def assign_review_id(self) -> Self:
        if self.status == ReviewStatus.EDITED and not self.edited_value:
            raise ValueError("edited review requires an edited value")
        if self.status != ReviewStatus.EDITED and self.edited_value is not None:
            raise ValueError("only edited review may contain an edited value")
        object.__setattr__(self, "evidence_ids", _sorted_unique(self.evidence_ids))
        object.__setattr__(self, "claim_ids", _sorted_unique(self.claim_ids))
        expected = stable_id(
            "review",
            self.application_id,
            self.subject_id,
            self.status,
            self.edited_value,
            self.notes,
            self.evidence_ids,
            self.claim_ids,
        )
        _set_or_validate_id(self, "review_id", expected)
        return self


class ReportUnresolvedEntry(StrictModel):
    application_id: NonEmptyString
    unresolved: UnresolvedReference
    entry_id: str = ""

    @model_validator(mode="after")
    def assign_entry_id(self) -> Self:
        expected = stable_id(
            "report_unresolved", self.application_id, self.unresolved.unresolved_id
        )
        _set_or_validate_id(self, "entry_id", expected)
        return self


class ReportCoverageEntry(StrictModel):
    application_id: NonEmptyString
    coverage: ExtractionCoverage
    coverage_id: str = ""

    @model_validator(mode="after")
    def assign_coverage_id(self) -> Self:
        expected = stable_id("report_coverage", self.application_id, self.coverage)
        _set_or_validate_id(self, "coverage_id", expected)
        return self


class ReportLineageTarget(StrictModel):
    application_id: NonEmptyString
    artifact_id: NonEmptyString
    source_object_id: NonEmptyString
    interaction_id: str | None = None
    operation: DataOperation
    scope: InteractionScope
    local_target_object_id: str | None = None
    catalog: str | None = None
    database: str | None = None
    schema_name: str | None = None
    object_name: str | None = None
    evidence_ids: tuple[NonEmptyString, ...]
    target_id: str = ""

    @field_validator("catalog", "database", "schema_name", "object_name")
    @classmethod
    def reject_raw_connection_fields(cls, value: str | None) -> str | None:
        return None if value is None else _reject_raw_connection_value(value)

    @model_validator(mode="after")
    def assign_target_id(self) -> Self:
        object.__setattr__(self, "evidence_ids", _sorted_unique(self.evidence_ids))
        if not self.evidence_ids:
            raise ValueError("report lineage target must cite evidence")
        expected = stable_id(
            "report_lineage_target",
            self.application_id,
            self.artifact_id,
            self.source_object_id,
            self.interaction_id,
            self.operation,
            self.scope,
            self.local_target_object_id,
            _casefold_optional(self.catalog),
            _casefold_optional(self.database),
            _casefold_optional(self.schema_name),
            _casefold_optional(self.object_name),
            self.evidence_ids,
        )
        _set_or_validate_id(self, "target_id", expected)
        return self


class PassThroughQueryView(StrictModel):
    application_id: NonEmptyString
    artifact_id: NonEmptyString
    query_object_id: NonEmptyString
    query_name: NonEmptyString
    query_kind: QueryKind
    connection_id: NonEmptyString
    connection_kind: ConnectionKind
    direct_provenance: ConnectionProvenance
    datasource_id: str | None = None
    resolution_status: ResolutionStatus
    resolution_provenance: tuple[NonEmptyString, ...] = ()
    resolution_warnings: tuple[NonEmptyString, ...] = ()
    platform: str | None = None
    driver: str | None = None
    dsn: str | None = None
    server: str | None = None
    database: str | None = None
    operations: tuple[DataOperation, ...] = ()
    catalogs: tuple[NonEmptyString, ...] = ()
    schemas: tuple[NonEmptyString, ...] = ()
    object_names: tuple[NonEmptyString, ...] = ()
    targets: tuple[ReportLineageTarget, ...] = ()
    evidence_ids: tuple[NonEmptyString, ...]
    view_id: str = ""

    @field_validator("platform", "driver", "dsn", "server", "database")
    @classmethod
    def reject_raw_connection_fields(cls, value: str | None) -> str | None:
        return None if value is None else _reject_raw_connection_value(value)

    @model_validator(mode="after")
    def assign_view_id(self) -> Self:
        object.__setattr__(
            self,
            "operations",
            tuple(sorted(set(self.operations), key=lambda item: item.value)),
        )
        for field_name in (
            "resolution_provenance",
            "resolution_warnings",
            "catalogs",
            "schemas",
            "object_names",
            "evidence_ids",
        ):
            object.__setattr__(self, field_name, _sorted_unique(getattr(self, field_name)))
        _sort_model_tuple(self, "targets", "target_id")
        if not self.evidence_ids:
            raise ValueError("pass-through report view must cite evidence")
        for target in self.targets:
            if (
                target.application_id != self.application_id
                or target.artifact_id != self.artifact_id
                or target.source_object_id != self.query_object_id
            ):
                raise ValueError("pass-through lineage target belongs to another object")
            if not set(target.evidence_ids).issubset(self.evidence_ids):
                raise ValueError("pass-through target evidence is absent from its view")
        expected = stable_id(
            "pass_through_view",
            self.application_id,
            self.artifact_id,
            self.query_object_id,
            self.query_kind,
            self.connection_id,
            self.connection_kind,
            self.direct_provenance,
            self.datasource_id,
            self.resolution_status,
            self.resolution_provenance,
            self.resolution_warnings,
            self.operations,
            self.catalogs,
            self.schemas,
            self.object_names,
            self.targets,
            self.evidence_ids,
        )
        _set_or_validate_id(self, "view_id", expected)
        return self


class LinkedTableView(StrictModel):
    application_id: NonEmptyString
    artifact_id: NonEmptyString
    table_object_id: NonEmptyString
    table_name: NonEmptyString
    object_kind: AccessObjectType
    source_table_name: str | None = None
    connection_id: NonEmptyString
    connection_kind: ConnectionKind
    direct_provenance: ConnectionProvenance
    datasource_id: str | None = None
    resolution_status: ResolutionStatus
    resolution_provenance: tuple[NonEmptyString, ...] = ()
    resolution_warnings: tuple[NonEmptyString, ...] = ()
    platform: str | None = None
    driver: str | None = None
    dsn: str | None = None
    server: str | None = None
    database: str | None = None
    operations: tuple[DataOperation, ...] = ()
    catalogs: tuple[NonEmptyString, ...] = ()
    schemas: tuple[NonEmptyString, ...] = ()
    object_names: tuple[NonEmptyString, ...] = ()
    targets: tuple[ReportLineageTarget, ...] = ()
    evidence_ids: tuple[NonEmptyString, ...]
    view_id: str = ""

    @field_validator("platform", "driver", "dsn", "server", "database")
    @classmethod
    def reject_raw_connection_fields(cls, value: str | None) -> str | None:
        return None if value is None else _reject_raw_connection_value(value)

    @model_validator(mode="after")
    def assign_view_id(self) -> Self:
        if self.object_kind != AccessObjectType.LINKED_TABLE:
            raise ValueError("linked-table view must identify a linked-table object")
        object.__setattr__(
            self,
            "operations",
            tuple(sorted(set(self.operations), key=lambda item: item.value)),
        )
        for field_name in (
            "resolution_provenance",
            "resolution_warnings",
            "catalogs",
            "schemas",
            "object_names",
            "evidence_ids",
        ):
            object.__setattr__(self, field_name, _sorted_unique(getattr(self, field_name)))
        _sort_model_tuple(self, "targets", "target_id")
        if not self.evidence_ids:
            raise ValueError("linked-table report view must cite evidence")
        for target in self.targets:
            if (
                target.application_id != self.application_id
                or target.artifact_id != self.artifact_id
                or target.source_object_id != self.table_object_id
            ):
                raise ValueError("linked-table lineage target belongs to another object")
            if not set(target.evidence_ids).issubset(self.evidence_ids):
                raise ValueError("linked-table target evidence is absent from its view")
        expected = stable_id(
            "linked_table_view",
            self.application_id,
            self.artifact_id,
            self.table_object_id,
            self.object_kind,
            self.connection_id,
            self.connection_kind,
            self.direct_provenance,
            self.datasource_id,
            self.resolution_status,
            self.resolution_provenance,
            self.resolution_warnings,
            self.source_table_name,
            self.operations,
            self.catalogs,
            self.schemas,
            self.object_names,
            self.targets,
            self.evidence_ids,
        )
        _set_or_validate_id(self, "view_id", expected)
        return self


class ReportObjectEntry(StrictModel):
    application_id: NonEmptyString
    access_object: AccessObjectEvidence
    entry_id: str = ""

    @model_validator(mode="after")
    def assign_entry_id(self) -> Self:
        expected = stable_id(
            "report_object", self.application_id, self.access_object.object_id
        )
        _set_or_validate_id(self, "entry_id", expected)
        return self


class ReportTableEntry(StrictModel):
    application_id: NonEmptyString
    table: AccessTableEvidence
    entry_id: str = ""

    @model_validator(mode="after")
    def assign_entry_id(self) -> Self:
        expected = stable_id("report_table", self.application_id, self.table.object_id)
        _set_or_validate_id(self, "entry_id", expected)
        return self


class ReportQueryEntry(StrictModel):
    application_id: NonEmptyString
    query: QueryEvidence
    entry_id: str = ""

    @model_validator(mode="after")
    def assign_entry_id(self) -> Self:
        expected = stable_id("report_query", self.application_id, self.query.object_id)
        _set_or_validate_id(self, "entry_id", expected)
        return self


class ReportDataAccessEntry(StrictModel):
    application_id: NonEmptyString
    data_access: DatasourceInteraction
    entry_id: str = ""

    @model_validator(mode="after")
    def assign_entry_id(self) -> Self:
        expected = stable_id(
            "report_data_access", self.application_id, self.data_access.interaction_id
        )
        _set_or_validate_id(self, "entry_id", expected)
        return self


class ReportDependencyEdgeEntry(StrictModel):
    application_id: NonEmptyString
    dependency_edge: DependencyEdge
    entry_id: str = ""

    @model_validator(mode="after")
    def assign_entry_id(self) -> Self:
        expected = stable_id(
            "report_dependency", self.application_id, self.dependency_edge.edge_id
        )
        _set_or_validate_id(self, "entry_id", expected)
        return self


class ReportOmission(StrictModel):
    application_id: str | None = None
    logical_unit_id: str | None = None
    stage: ReportOmissionStage
    reason: NonEmptyString
    omission_id: str = ""

    @model_validator(mode="after")
    def assign_omission_id(self) -> Self:
        if self.logical_unit_id is not None and self.application_id is None:
            raise ValueError("logical-unit omission must identify its application")
        expected = stable_id(
            "report_omission",
            self.application_id,
            self.logical_unit_id,
            self.stage,
            self.reason,
        )
        _set_or_validate_id(self, "omission_id", expected)
        return self


class ReportInventoryExclusion(StrictModel):
    application_id: NonEmptyString
    application_name: NonEmptyString
    filename: NonEmptyString
    source_locator: NonEmptyString
    status: Literal["skipped_unsupported_format"] = "skipped_unsupported_format"
    reason: NonEmptyString
    exclusion_id: str = ""

    @model_validator(mode="after")
    def assign_exclusion_id(self) -> Self:
        expected = stable_id(
            "inventory_exclusion",
            self.application_id,
            normalize_source_identity(self.source_locator),
            self.filename.casefold(),
            self.status,
            self.reason,
        )
        _set_or_validate_id(self, "exclusion_id", expected)
        return self


class ReportModel(StrictModel):
    schema_version: Literal["report-model-v2"] = "report-model-v2"
    status: ReportStatus
    generated_at: datetime
    analysis_fingerprint: Sha256
    applications: tuple[ReportApplicationEntry, ...]
    observed_facts: tuple[EvidenceRecord, ...]
    owner_claims: tuple[OwnerClaim, ...]
    application_profiles: tuple[ApplicationInterpretation, ...]
    review_records: tuple[ReportReviewRecord, ...]
    unresolved_references: tuple[ReportUnresolvedEntry, ...]
    coverage: tuple[ReportCoverageEntry, ...]
    pass_through_queries: tuple[PassThroughQueryView, ...]
    linked_tables: tuple[LinkedTableView, ...]
    object_registry: tuple[ReportObjectEntry, ...]
    table_registry: tuple[ReportTableEntry, ...]
    query_registry: tuple[ReportQueryEntry, ...]
    connection_registry: tuple[ConnectionEvidence, ...]
    datasource_registry: tuple[DatasourceIdentity, ...]
    data_access: tuple[ReportDataAccessEntry, ...]
    dependency_nodes: tuple[DependencyNode, ...]
    dependency_edges: tuple[ReportDependencyEdgeEntry, ...]
    inventory_exclusions: tuple[ReportInventoryExclusion, ...] = ()
    omissions: tuple[ReportOmission, ...]
    candidates: tuple[PortfolioCandidate, ...]
    portfolio_findings: tuple[PortfolioFinding, ...]
    portfolio_analysis_sha256: Sha256 | None = None
    excluded_application_ids: tuple[NonEmptyString, ...] = ()
    warnings: tuple[NonEmptyString, ...] = ()
    partial_watermark: Literal[
        "PARTIAL REPORT - coverage omissions present; portfolio-wide absence claims are suppressed."
    ] | None = None
    absence_claims_suppressed: bool = False
    report_id: str = ""

    @field_validator("generated_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        return _aware_timestamp(value)

    @model_validator(mode="after")
    def assign_report_id(self) -> Self:
        application_ids = [item.application_id for item in self.applications]
        if len(set(application_ids)) != len(application_ids):
            raise ValueError("report application IDs must be unique")
        if self.status == ReportStatus.COMPLETE and self.excluded_application_ids:
            raise ValueError("complete report cannot exclude applications")
        if self.status == ReportStatus.COMPLETE and (
            self.omissions or self.partial_watermark is not None
        ):
            raise ValueError("complete report cannot contain partial-report controls")
        if self.status == ReportStatus.PARTIAL and (
            not self.omissions
            or self.partial_watermark != PARTIAL_REPORT_WATERMARK
            or not self.absence_claims_suppressed
        ):
            raise ValueError(
                "partial report requires omissions, watermark, and absence-claim suppression"
            )
        candidate_ids = [item.candidate_id for item in self.candidates]
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("report candidate IDs must be unique")
        finding_ids = [item.finding_id for item in self.portfolio_findings]
        if len(set(finding_ids)) != len(finding_ids):
            raise ValueError("report portfolio finding IDs must be unique")
        known_applications = set(application_ids)
        proposals = {
            **{item.candidate_id: item.evidence_ids for item in self.candidates},
            **{item.finding_id: item.evidence_ids for item in self.portfolio_findings},
        }
        evidence = _unique_by(self.observed_facts, "evidence_id")
        claims = _unique_by(self.owner_claims, "claim_id")
        profiles = _unique_by(self.application_profiles, "application_id")
        _unique_by(self.review_records, "review_id")
        _unique_by(self.unresolved_references, "entry_id")
        coverage = _unique_by(self.coverage, "application_id")
        _unique_by(self.pass_through_queries, "view_id")
        _unique_by(self.linked_tables, "view_id")
        objects = _unique_by(self.object_registry, "entry_id")
        tables = _unique_by(self.table_registry, "entry_id")
        queries = _unique_by(self.query_registry, "entry_id")
        connections = _unique_by(self.connection_registry, "connection_id")
        datasources = _unique_by(self.datasource_registry, "datasource_id")
        access_rows = _unique_by(self.data_access, "entry_id")
        nodes = _unique_by(self.dependency_nodes, "node_id")
        dependency_rows = _unique_by(self.dependency_edges, "entry_id")
        pass_through_targets = tuple(
            target
            for view in self.pass_through_queries
            for target in view.targets
        )
        linked_table_targets = tuple(
            target for view in self.linked_tables for target in view.targets
        )
        lineage_targets = (*pass_through_targets, *linked_table_targets)
        _unique_by(lineage_targets, "target_id")
        _unique_by(self.inventory_exclusions, "exclusion_id")
        _unique_by(self.omissions, "omission_id")
        if set(coverage) != known_applications:
            raise ValueError("report coverage must contain every included application exactly once")
        if self.status == ReportStatus.COMPLETE and set(profiles) != known_applications:
            raise ValueError(
                "complete report must contain every application interpretation"
            )
        if self.status == ReportStatus.COMPLETE and self.portfolio_analysis_sha256 is None:
            raise ValueError("complete report requires a portfolio interpretation")
        for fact in evidence.values():
            if fact.application_id not in known_applications:
                raise ValueError("report fact belongs to an unknown application")
        for claim in claims.values():
            if claim.application_id not in known_applications:
                raise ValueError("report claim belongs to an unknown application")
        for profile in profiles.values():
            if profile.application_id not in known_applications:
                raise ValueError("report profile belongs to an unknown application")
            _require_known(profile.evidence_ids, evidence, "report profile evidence")
            _require_known(profile.claim_ids, claims, "report profile claims")
        for review in self.review_records:
            if review.application_id not in known_applications:
                raise ValueError("report review belongs to an unknown application")
            _require_known(review.evidence_ids, evidence, "report review evidence")
            _require_known(review.claim_ids, claims, "report review claims")
            proposal_evidence = proposals.get(review.subject_id)
            if proposal_evidence is None:
                raise ValueError("report review references an unknown proposal")
            if tuple(sorted(review.evidence_ids)) != tuple(sorted(proposal_evidence)):
                raise ValueError("report review evidence does not match its proposal")
        for unresolved in self.unresolved_references:
            if unresolved.application_id not in known_applications:
                raise ValueError("report unresolved item belongs to an unknown application")
            _require_known(
                unresolved.unresolved.evidence_ids,
                evidence,
                "report unresolved evidence",
            )
        object_by_application = {
            (item.application_id, item.access_object.object_id): item.access_object
            for item in objects.values()
        }
        query_by_application = {
            (item.application_id, item.query.object_id): item.query
            for item in queries.values()
        }
        table_by_application = {
            (item.application_id, item.table.object_id): item.table
            for item in tables.values()
        }
        access_by_interaction = {
            item.data_access.interaction_id: item for item in access_rows.values()
        }
        for view in self.pass_through_queries:
            if view.application_id not in known_applications:
                raise ValueError("report datasource view belongs to an unknown application")
            _require_known(view.evidence_ids, evidence, "report datasource-view evidence")
            source_object = object_by_application.get(
                (view.application_id, view.query_object_id)
            )
            query = query_by_application.get((view.application_id, view.query_object_id))
            connection = connections.get(view.connection_id)
            if (
                source_object is None
                or source_object.artifact_id != view.artifact_id
                or query is None
                or query.query_kind != view.query_kind
                or connection is None
                or connection.application_id != view.application_id
                or connection.object_id != view.query_object_id
                or connection.connection_kind != view.connection_kind
                or connection.direct_provenance != view.direct_provenance
                or connection.resolution_status != view.resolution_status
                or connection.resolution_provenance != view.resolution_provenance
                or connection.resolution_warnings != view.resolution_warnings
                or connection.datasource_id != view.datasource_id
            ):
                raise ValueError("pass-through view disagrees with normalized lineage")
            _validate_view_datasource(
                view.datasource_id,
                view.platform,
                view.driver,
                view.dsn,
                view.server,
                view.database,
                datasources,
            )
            _validate_view_targets(
                view.targets,
                evidence,
                access_by_interaction,
                view.application_id,
            )
        for linked_view in self.linked_tables:
            if linked_view.application_id not in known_applications:
                raise ValueError("report datasource view belongs to an unknown application")
            _require_known(
                linked_view.evidence_ids, evidence, "report datasource-view evidence"
            )
            source_object = object_by_application.get(
                (linked_view.application_id, linked_view.table_object_id)
            )
            table = table_by_application.get(
                (linked_view.application_id, linked_view.table_object_id)
            )
            connection = connections.get(linked_view.connection_id)
            if (
                source_object is None
                or source_object.artifact_id != linked_view.artifact_id
                or source_object.object_type != linked_view.object_kind
                or table is None
                or table.source_table_name != linked_view.source_table_name
                or connection is None
                or connection.application_id != linked_view.application_id
                or connection.object_id != linked_view.table_object_id
                or connection.connection_kind != linked_view.connection_kind
                or connection.direct_provenance != linked_view.direct_provenance
                or connection.resolution_status != linked_view.resolution_status
                or connection.resolution_provenance
                != linked_view.resolution_provenance
                or connection.resolution_warnings != linked_view.resolution_warnings
                or connection.datasource_id != linked_view.datasource_id
            ):
                raise ValueError("linked-table view disagrees with normalized lineage")
            _validate_view_datasource(
                linked_view.datasource_id,
                linked_view.platform,
                linked_view.driver,
                linked_view.dsn,
                linked_view.server,
                linked_view.database,
                datasources,
            )
            _validate_view_targets(
                linked_view.targets,
                evidence,
                access_by_interaction,
                linked_view.application_id,
            )
        object_keys = {
            (item.application_id, item.access_object.object_id)
            for item in objects.values()
        }
        for item in objects.values():
            if item.application_id not in known_applications:
                raise ValueError("report object belongs to an unknown application")
            _require_known(
                item.access_object.evidence_ids, evidence, "report object evidence"
            )
        for item in tables.values():
            if (item.application_id, item.table.object_id) not in object_keys:
                raise ValueError("report table references an unknown application object")
            if item.table.connection_id is not None:
                connection = connections.get(item.table.connection_id)
                if connection is None or connection.application_id != item.application_id:
                    raise ValueError("report table references an unknown application connection")
            _require_known(item.table.evidence_ids, evidence, "report table evidence")
        for item in queries.values():
            if (item.application_id, item.query.object_id) not in object_keys:
                raise ValueError("report query references an unknown application object")
            if item.query.connection_id is not None:
                connection = connections.get(item.query.connection_id)
                if connection is None or connection.application_id != item.application_id:
                    raise ValueError("report query references an unknown application connection")
            _require_known(item.query.evidence_ids, evidence, "report query evidence")
        for connection in connections.values():
            if connection.application_id not in known_applications:
                raise ValueError("report connection belongs to an unknown application")
            if (connection.application_id, connection.object_id) not in object_keys:
                raise ValueError("report connection references an unknown object")
            if (
                connection.datasource_id is not None
                and connection.datasource_id not in datasources
            ):
                raise ValueError("report connection references an unknown datasource")
            _require_known(
                connection.evidence_ids, evidence, "report connection evidence"
            )
        for item in access_rows.values():
            if item.application_id not in known_applications:
                raise ValueError("report data access belongs to an unknown application")
            if (item.application_id, item.data_access.source_object_id) not in object_keys:
                raise ValueError("report data access references an unknown source object")
            if (
                item.data_access.connection_id is not None
                and (
                    item.data_access.connection_id not in connections
                    or connections[item.data_access.connection_id].application_id
                    != item.application_id
                )
            ):
                raise ValueError("report data access references an unknown connection")
            if (
                item.data_access.datasource_id is not None
                and item.data_access.datasource_id not in datasources
            ):
                raise ValueError("report data access references an unknown datasource")
            _require_known(
                item.data_access.evidence_ids, evidence, "report data-access evidence"
            )
        for node in nodes.values():
            if node.application_id not in known_applications:
                raise ValueError("report dependency node belongs to an unknown application")
        for item in dependency_rows.values():
            if item.application_id not in known_applications:
                raise ValueError("report dependency belongs to an unknown application")
            edge = item.dependency_edge
            if edge.source_node_id not in nodes or edge.target_node_id not in nodes:
                raise ValueError("report dependency references an unknown node")
            if (
                nodes[edge.source_node_id].application_id != item.application_id
                or nodes[edge.target_node_id].application_id != item.application_id
            ):
                raise ValueError("report dependency crosses application boundaries")
            _require_known(edge.evidence_ids, evidence, "report dependency evidence")
        excluded_omissions = {
            item.application_id
            for item in self.omissions
            if item.application_id is not None and item.logical_unit_id is None
        }
        if not set(self.excluded_application_ids).issubset(excluded_omissions):
            raise ValueError("every excluded application requires a structured omission")
        for candidate in self.candidates:
            if not set(candidate.application_ids).issubset(known_applications):
                raise ValueError("report candidate references an unknown application")
            _require_known(candidate.evidence_ids, evidence, "report candidate evidence")
            if any(
                evidence[evidence_id].application_id not in candidate.application_ids
                for evidence_id in candidate.evidence_ids
            ):
                raise ValueError("report candidate evidence crosses its application membership")
            technical_basis = {
                *(item.access_object.object_id for item in objects.values()),
                *connections,
                *datasources,
                *(item.data_access.interaction_id for item in access_rows.values()),
                *nodes,
                *(item.dependency_edge.edge_id for item in dependency_rows.values()),
            }
            missing_basis = set(candidate.basis_ids) - technical_basis
            if missing_basis:
                raise ValueError("report candidate cites unknown technical basis")
        for finding in self.portfolio_findings:
            if not set(finding.application_ids).issubset(known_applications):
                raise ValueError("report finding references an unknown application")
            _require_known(finding.evidence_ids, evidence, "report finding evidence")
            if any(
                evidence[evidence_id].application_id not in finding.application_ids
                for evidence_id in finding.evidence_ids
            ):
                raise ValueError("report finding evidence crosses its application membership")
        for application in self.applications:
            fact_count = sum(
                fact.application_id == application.application_id
                for fact in self.observed_facts
            )
            if application.evidence_count != fact_count:
                raise ValueError("application evidence count does not match normalized facts")
            unresolved_count = sum(
                item.application_id == application.application_id
                for item in self.unresolved_references
            )
            if application.unresolved_reference_count != unresolved_count:
                raise ValueError(
                    "application unresolved count does not match normalized rows"
                )
            object_count = sum(
                item.application_id == application.application_id
                for item in self.object_registry
            )
            table_count = sum(
                item.application_id == application.application_id
                for item in self.table_registry
            )
            query_count = sum(
                item.application_id == application.application_id
                for item in self.query_registry
            )
            datasource_ids = {
                item.data_access.datasource_id
                for item in self.data_access
                if item.application_id == application.application_id
                and item.data_access.datasource_id is not None
            } | {
                item.datasource_id
                for item in self.connection_registry
                if item.application_id == application.application_id
                and item.datasource_id is not None
            }
            connection_count = sum(
                item.application_id == application.application_id
                for item in self.connection_registry
            )
            interaction_count = sum(
                item.application_id == application.application_id
                for item in self.data_access
            )
            if (
                application.object_count != object_count
                or application.table_count != table_count
                or application.query_count != query_count
                or application.datasource_count != len(datasource_ids)
                or application.connection_count != connection_count
                or application.interaction_count != interaction_count
            ):
                raise ValueError(
                    "application counts do not match normalized report registries"
                )
        object.__setattr__(
            self,
            "applications",
            tuple(sorted(self.applications, key=lambda item: item.application_id)),
        )
        object.__setattr__(
            self,
            "excluded_application_ids",
            _sorted_unique(self.excluded_application_ids),
        )
        _sort_model_tuple(self, "candidates", "candidate_id")
        _sort_model_tuple(self, "portfolio_findings", "finding_id")
        _sort_model_tuple(self, "observed_facts", "evidence_id")
        _sort_model_tuple(self, "owner_claims", "claim_id")
        _sort_model_tuple(self, "application_profiles", "application_id")
        _sort_model_tuple(self, "review_records", "review_id")
        _sort_model_tuple(self, "unresolved_references", "entry_id")
        _sort_model_tuple(self, "coverage", "coverage_id")
        _sort_model_tuple(self, "pass_through_queries", "view_id")
        _sort_model_tuple(self, "linked_tables", "view_id")
        _sort_model_tuple(self, "object_registry", "entry_id")
        _sort_model_tuple(self, "table_registry", "entry_id")
        _sort_model_tuple(self, "query_registry", "entry_id")
        _sort_model_tuple(self, "connection_registry", "connection_id")
        _sort_model_tuple(self, "datasource_registry", "datasource_id")
        _sort_model_tuple(self, "data_access", "entry_id")
        _sort_model_tuple(self, "dependency_nodes", "node_id")
        _sort_model_tuple(self, "dependency_edges", "entry_id")
        _sort_model_tuple(self, "inventory_exclusions", "exclusion_id")
        _sort_model_tuple(self, "omissions", "omission_id")
        object.__setattr__(self, "warnings", _sorted_unique(self.warnings))
        expected = stable_id(
            "report",
            self.status,
            self.analysis_fingerprint,
            self.applications,
            self.observed_facts,
            self.owner_claims,
            self.application_profiles,
            self.review_records,
            self.unresolved_references,
            self.coverage,
            self.pass_through_queries,
            self.linked_tables,
            self.object_registry,
            self.table_registry,
            self.query_registry,
            self.connection_registry,
            self.datasource_registry,
            self.data_access,
            self.dependency_nodes,
            self.dependency_edges,
            tuple(item.exclusion_id for item in self.inventory_exclusions),
            self.omissions,
            self.candidates,
            self.portfolio_findings,
            self.portfolio_analysis_sha256,
            self.excluded_application_ids,
            self.partial_watermark,
            self.absence_claims_suppressed,
        )
        _set_or_validate_id(self, "report_id", expected)
        return self


def _sanitize_runtime_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, tuple):
        return tuple(_sanitize_runtime_value(item) for item in value)
    if isinstance(value, list):
        return [_sanitize_runtime_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize_runtime_value(item) for key, item in value.items()}
    return value


def _set_or_validate_id(model: BaseModel, field_name: str, expected: str) -> None:
    current = getattr(model, field_name)
    if current and current != expected:
        raise ValueError(f"{field_name} does not match the canonical identity")
    object.__setattr__(model, field_name, expected)


def _sorted_unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(set(values), key=lambda value: (value.casefold(), value)))


def _unique_by(items: tuple[Any, ...], field_name: str) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for item in items:
        identifier = getattr(item, field_name)
        if identifier in output:
            raise ValueError(f"duplicate {field_name}: {identifier}")
        output[identifier] = item
    return output


def _require_known(
    identifiers: tuple[str, ...], known: dict[str, Any], description: str
) -> None:
    missing = set(identifiers) - set(known)
    if missing:
        raise ValueError(f"{description} references unknown IDs: {', '.join(sorted(missing))}")


def _validate_view_targets(
    targets: tuple[ReportLineageTarget, ...],
    evidence: dict[str, Any],
    access_by_interaction: dict[str, ReportDataAccessEntry],
    application_id: str,
) -> None:
    for target in targets:
        if target.application_id != application_id:
            raise ValueError("report lineage target belongs to another application")
        _require_known(target.evidence_ids, evidence, "report lineage-target evidence")
        if target.interaction_id is None:
            continue
        access_entry = access_by_interaction.get(target.interaction_id)
        if access_entry is None or access_entry.application_id != application_id:
            raise ValueError("report lineage target references an unknown interaction")
        access = access_entry.data_access
        if (
            access.source_object_id != target.source_object_id
            or access.operation != target.operation
            or access.scope != target.scope
            or access.local_target_object_id != target.local_target_object_id
            or access.catalog != target.catalog
            or access.database != target.database
            or access.schema_name != target.schema_name
            or access.object_name != target.object_name
        ):
            raise ValueError("report lineage target disagrees with normalized data access")


def _validate_view_datasource(
    datasource_id: str | None,
    platform: str | None,
    driver: str | None,
    dsn: str | None,
    server: str | None,
    database: str | None,
    datasources: dict[str, Any],
) -> None:
    datasource = datasources.get(datasource_id) if datasource_id is not None else None
    if datasource_id is not None and datasource is None:
        raise ValueError("report datasource view references an unknown datasource")
    expected = (
        (None, None, None, None, None)
        if datasource is None
        else (
            datasource.platform,
            datasource.driver,
            datasource.dsn,
            datasource.server,
            datasource.database,
        )
    )
    if (platform, driver, dsn, server, database) != expected:
        raise ValueError("report datasource view disagrees with datasource identity")


def _sort_model_tuple(model: BaseModel, field_name: str, identifier_name: str) -> None:
    values = getattr(model, field_name)
    object.__setattr__(
        model,
        field_name,
        tuple(sorted(values, key=lambda item: getattr(item, identifier_name))),
    )


def _casefold_optional(value: str | None) -> str | None:
    return value.casefold() if value is not None else None


def _aware_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value


# Plan-facing vocabulary.  The concrete classes retain descriptive implementation names while
# these aliases keep the public contract aligned with the architecture specification.
AccessQueryEvidence = QueryEvidence
DataAccess = DatasourceInteraction
Coverage = ExtractionCoverage
InterpretationClaim = InterpretiveFinding
UnitInterpretation = LogicalUnitInterpretation
ApplicationProfile = ApplicationInterpretation
PortfolioInterpretation = PortfolioAnalysis
PortfolioReportModel = ReportModel
