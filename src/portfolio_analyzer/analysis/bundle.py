"""Build canonical V2 application evidence bundles from staged extraction snapshots.

This module is deliberately deterministic and static. It never opens a database, follows a file
reference, or establishes an ODBC connection. Raw connection declarations are used only as
ephemeral input to the safe parser and injected registry-only DSN resolver.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PureWindowsPath
from typing import Literal, cast

from portfolio_analyzer.library_identity import LibraryInjectionStatus
from portfolio_analyzer.parsing.connections import (
    normalize_connection_identity,
    parse_connection_string_details,
)
from portfolio_analyzer.parsing.dsn import DsnResolution, resolve_windows_dsn
from portfolio_analyzer.parsing.sql import SqlObjectReference, classify_sql
from portfolio_analyzer.parsing.vba import extract_procedures
from portfolio_analyzer.v2.identity import normalize_source_identity, sanitize_text
from portfolio_analyzer.v2.models import (
    AccessObjectEvidence,
    AccessObjectType,
    AccessTableEvidence,
    ApplicationEvidenceBundle,
    ArtifactEvidence,
    Confidence,
    ConnectionEvidence,
    ConnectionKind,
    ConnectionProvenance,
    DataOperation,
    DatasourceIdentity,
    DatasourceInteraction,
    DependencyEdge,
    DependencyNode,
    DependencyNodeKind,
    DependencyRelation,
    EvidenceOrigin,
    EvidenceRecord,
    ExtractionCoverage,
    ExtractionStatus,
    InteractionScope,
    KeyValueFact,
    OwnerClaim,
    QueryEvidence,
    QueryKind,
    QueryParameterEvidence,
    ResolutionStatus,
    UnresolvedReference,
)
from portfolio_analyzer.v2.workflow import (
    ExtractedArtifactSnapshot,
    ExtractedObjectSnapshot,
    StagedApplication,
    StagedArtifactRecord,
    StageIndex,
)

DsnResolver = Callable[[str], DsnResolution]

_OBJECT_TYPES = {
    "table": AccessObjectType.TABLE,
    "linked_table": AccessObjectType.LINKED_TABLE,
    "table_link_status_unknown": AccessObjectType.TABLE_LINK_STATUS_UNKNOWN,
    "query": AccessObjectType.QUERY,
    "form": AccessObjectType.FORM,
    "report": AccessObjectType.REPORT,
    "macro": AccessObjectType.MACRO,
    "module": AccessObjectType.MODULE,
    "procedure": AccessObjectType.PROCEDURE,
    "reference": AccessObjectType.REFERENCE,
}
_QUERY_KINDS = {
    "select": QueryKind.SELECT,
    "crosstab": QueryKind.CROSSTAB,
    "pass_through": QueryKind.PASS_THROUGH,
    "pass_through_bulk": QueryKind.PASS_THROUGH_BULK,
    "compound": QueryKind.COMPOUND,
    "procedure": QueryKind.PROCEDURE,
    "data_definition": QueryKind.DATA_DEFINITION,
    "union": QueryKind.UNION,
    "delete": QueryKind.ACTION,
    "update": QueryKind.ACTION,
    "append": QueryKind.ACTION,
    "make_table": QueryKind.ACTION,
    "action": QueryKind.ACTION,
    "unknown": QueryKind.UNKNOWN,
}
_DAO_QUERY_KINDS = {
    0: QueryKind.SELECT,
    16: QueryKind.CROSSTAB,
    32: QueryKind.ACTION,
    48: QueryKind.ACTION,
    64: QueryKind.ACTION,
    80: QueryKind.ACTION,
    96: QueryKind.DATA_DEFINITION,
    112: QueryKind.PASS_THROUGH,
    128: QueryKind.UNION,
    144: QueryKind.PASS_THROUGH_BULK,
    160: QueryKind.COMPOUND,
    224: QueryKind.PROCEDURE,
    240: QueryKind.ACTION,
}

_PASS_THROUGH_QUERY_KINDS = frozenset(
    {
        QueryKind.PASS_THROUGH,
        QueryKind.PASS_THROUGH_BULK,
    }
)
_SAFE_ATTRIBUTE_KEYS = {
    AccessObjectType.TABLE: frozenset(
        {"attributes", "connect_metadata_status", "fields", "hidden", "system"}
    ),
    AccessObjectType.LINKED_TABLE: frozenset(
        {"attributes", "connect_metadata_status", "hidden", "system"}
    ),
    AccessObjectType.TABLE_LINK_STATUS_UNKNOWN: frozenset(
        {"attributes", "connect_metadata_status", "hidden", "system"}
    ),
    AccessObjectType.QUERY: frozenset(
        {"attributes", "connect_metadata_status", "hidden", "max_records", "system"}
    ),
    AccessObjectType.REFERENCE: frozenset({"full_path", "guid", "is_broken"}),
}

_VBA_STRING = re.compile(r'"(?P<value>(?:""|[^"\r\n])*)"')
_SQL_PREFIX = re.compile(
    r"^\s*(?:PARAMETERS\b.*?;\s*)?(?:SELECT|TRANSFORM|INSERT|UPDATE|DELETE|"
    r"MERGE|EXEC(?:UTE)?|CREATE|ALTER|DROP)\b",
    re.I | re.S,
)
_CONNECTION_LITERAL = re.compile(
    r"(?i)(?:^|;)\s*(?:odbc|driver|dsn|server|data\s+source|database|"
    r"initial\s+catalog|provider|filedsn|file\s+dsn)\s*="
)
_CODE_BEHIND_MARKER = re.compile(r"(?im)^\s*CodeBehind(?:Form|Report)\b[^\r\n]*$")
_WINDOWS_FILE_LITERAL = re.compile(r"^(?:\\\\|[A-Za-z]:\\)")


@dataclass(frozen=True, slots=True)
class _ArtifactContext:
    staged: StagedArtifactRecord
    snapshot: ExtractedArtifactSnapshot | None


@dataclass(frozen=True, slots=True)
class _TableWork:
    object_id: str
    artifact_id: str
    connection_id: str | None
    datasource_id: str | None
    source_table_name: str | None
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _QueryWork:
    object_id: str
    artifact_id: str
    sql: str
    query_kind: QueryKind
    connection_id: str | None
    datasource_id: str | None
    connection_provenance: ConnectionProvenance
    connection_evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ObjectWork:
    object_id: str
    artifact_id: str
    object_type: AccessObjectType
    definition: str
    properties: dict[str, str]


def build_application_evidence_bundles(
    stage_index: StageIndex,
    snapshots: Iterable[ExtractedArtifactSnapshot],
    *,
    dsn_resolver: DsnResolver | None = None,
    generated_at: datetime | None = None,
) -> tuple[ApplicationEvidenceBundle, ...]:
    """Build exactly one bundle for every application in a stage index."""
    snapshot_by_id = _validated_snapshots(stage_index, snapshots)
    resolver = dsn_resolver or _default_dsn_resolver
    timestamp = generated_at or stage_index.generated_at
    artifact_by_id = {item.artifact_id: item for item in stage_index.artifacts}
    return tuple(
        _BundleBuilder(
            application=application,
            artifacts=tuple(artifact_by_id[item] for item in application.artifact_ids),
            snapshots=snapshot_by_id,
            generated_at=timestamp,
            dsn_resolver=resolver,
        ).build()
        for application in stage_index.applications
    )


def build_application_evidence_bundle(
    stage_index: StageIndex,
    application_id: str,
    snapshots: Iterable[ExtractedArtifactSnapshot],
    *,
    dsn_resolver: DsnResolver | None = None,
    generated_at: datetime | None = None,
) -> ApplicationEvidenceBundle:
    """Build one named application bundle while rejecting foreign snapshots."""
    application = next(
        (item for item in stage_index.applications if item.application_id == application_id),
        None,
    )
    if application is None:
        raise ValueError("application_id is not present in the stage index")
    snapshot_by_id = _validated_snapshots(stage_index, snapshots)
    foreign = set(snapshot_by_id) - set(application.artifact_ids)
    if foreign:
        raise ValueError("snapshots include artifacts from another application")
    artifact_by_id = {item.artifact_id: item for item in stage_index.artifacts}
    return _BundleBuilder(
        application=application,
        artifacts=tuple(artifact_by_id[item] for item in application.artifact_ids),
        snapshots=snapshot_by_id,
        generated_at=generated_at or stage_index.generated_at,
        dsn_resolver=dsn_resolver or _default_dsn_resolver,
    ).build()


def _validated_snapshots(
    stage_index: StageIndex,
    snapshots: Iterable[ExtractedArtifactSnapshot],
) -> dict[str, ExtractedArtifactSnapshot]:
    staged_by_id = {item.artifact_id: item for item in stage_index.artifacts}
    output: dict[str, ExtractedArtifactSnapshot] = {}
    for snapshot in snapshots:
        if snapshot.artifact_id in output:
            raise ValueError("duplicate extraction snapshot artifact_id")
        staged = staged_by_id.get(snapshot.artifact_id)
        if staged is None:
            raise ValueError("extraction snapshot references an unknown staged artifact")
        if snapshot.application_id != staged.application_id:
            raise ValueError("extraction snapshot belongs to a different application")
        if snapshot.artifact_sha256 != staged.sha256:
            raise ValueError("extraction snapshot SHA-256 does not match staged artifact")
        output[snapshot.artifact_id] = snapshot
    return output


def _default_dsn_resolver(value: str) -> DsnResolution:
    return resolve_windows_dsn(value)


@dataclass(slots=True)
class _BundleBuilder:
    application: StagedApplication
    artifacts: tuple[StagedArtifactRecord, ...]
    snapshots: dict[str, ExtractedArtifactSnapshot]
    generated_at: datetime
    dsn_resolver: DsnResolver
    evidence: dict[str, EvidenceRecord] = field(default_factory=dict)
    objects: dict[str, AccessObjectEvidence] = field(default_factory=dict)
    connections: dict[str, ConnectionEvidence] = field(default_factory=dict)
    tables: dict[str, AccessTableEvidence] = field(default_factory=dict)
    queries: dict[str, QueryEvidence] = field(default_factory=dict)
    datasources: dict[str, DatasourceIdentity] = field(default_factory=dict)
    interactions: dict[str, DatasourceInteraction] = field(default_factory=dict)
    nodes: dict[str, DependencyNode] = field(default_factory=dict)
    edges: dict[str, DependencyEdge] = field(default_factory=dict)
    unresolved: dict[str, UnresolvedReference] = field(default_factory=dict)
    table_work: dict[str, _TableWork] = field(default_factory=dict)
    query_work: dict[str, _QueryWork] = field(default_factory=dict)
    object_work: dict[str, _ObjectWork] = field(default_factory=dict)
    object_lookup: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    artifact_warnings: dict[str, list[str]] = field(default_factory=dict)
    artifact_status: dict[str, ExtractionStatus] = field(default_factory=dict)
    discovered_object_count: int = 0

    def build(self) -> ApplicationEvidenceBundle:
        contexts = self._contexts()
        for context in contexts:
            if context.snapshot is not None:
                for extracted_object in context.snapshot.objects:
                    self._add_object(context, extracted_object)

        self._add_nodes()
        self._analyze_non_query_objects()
        self._add_linked_table_edges()
        for work in sorted(self.query_work.values(), key=lambda item: item.object_id):
            self._analyze_query(work)
        return self._finalize(contexts)

    def _contexts(self) -> tuple[_ArtifactContext, ...]:
        contexts: list[_ArtifactContext] = []
        for staged in self.artifacts:
            snapshot = self.snapshots.get(staged.artifact_id)
            self.artifact_warnings[staged.artifact_id] = []
            if snapshot is None:
                self.artifact_status[staged.artifact_id] = ExtractionStatus.FAILED
                self.artifact_warnings[staged.artifact_id].append(
                    "extraction_snapshot_missing"
                )
            else:
                status = (
                    ExtractionStatus.COMPLETE
                    if snapshot.coverage_status == "complete" and not snapshot.warnings
                    else ExtractionStatus.PARTIAL
                )
                self.artifact_status[staged.artifact_id] = status
                self.artifact_warnings[staged.artifact_id].extend(snapshot.warnings)
                self.discovered_object_count += len(snapshot.objects)
            contexts.append(_ArtifactContext(staged=staged, snapshot=snapshot))
        return tuple(contexts)

    def _add_object(
        self,
        context: _ArtifactContext,
        extracted: ExtractedObjectSnapshot,
    ) -> None:
        object_type = _OBJECT_TYPES.get(extracted.object_type.strip().casefold())
        if object_type is None:
            self._warn(context.staged.artifact_id, "unsupported_extracted_object_type")
            return
        base_object = AccessObjectEvidence(
            artifact_id=context.staged.artifact_id,
            object_type=object_type,
            name=extracted.name,
            sanitized_definition=_safe_definition(extracted.sanitized_definition),
        )
        if base_object.object_id in self.objects:
            raise ValueError("duplicate artifact-scoped Access object identity")

        inventory_evidence = self._new_evidence(
            context.staged,
            object_id=base_object.object_id,
            fact_type="access_object_inventory",
            location="DAO or SaveAsText inventory",
            observation="Access object inventoried",
        )
        evidence_ids = [inventory_evidence.evidence_id]
        attributes = self._safe_attributes(
            object_type,
            extracted.sanitized_properties,
            inventory_evidence.evidence_id,
        )

        if object_type in {
            AccessObjectType.TABLE,
            AccessObjectType.LINKED_TABLE,
            AccessObjectType.TABLE_LINK_STATUS_UNKNOWN,
        }:
            evidence_ids.extend(
                self._add_table(context, extracted, base_object, inventory_evidence.evidence_id)
            )
        elif object_type == AccessObjectType.QUERY:
            evidence_ids.extend(
                self._add_query(context, extracted, base_object, inventory_evidence.evidence_id)
            )

        completed = AccessObjectEvidence(
            artifact_id=base_object.artifact_id,
            object_type=base_object.object_type,
            name=base_object.name,
            sanitized_definition=base_object.sanitized_definition,
            attributes=attributes,
            evidence_ids=tuple(evidence_ids),
            object_id=base_object.object_id,
        )
        self.objects[completed.object_id] = completed
        self.object_work[completed.object_id] = _ObjectWork(
            object_id=completed.object_id,
            artifact_id=completed.artifact_id,
            object_type=completed.object_type,
            definition=extracted.sanitized_definition or "",
            properties=dict(extracted.sanitized_properties),
        )
        lookup_key = (completed.artifact_id, completed.name.casefold())
        self.object_lookup.setdefault(lookup_key, []).append(completed.object_id)

    def _safe_attributes(
        self,
        object_type: AccessObjectType,
        properties: dict[str, str],
        evidence_id: str,
    ) -> tuple[KeyValueFact, ...]:
        allowed = _SAFE_ATTRIBUTE_KEYS.get(object_type, frozenset())
        return tuple(
            KeyValueFact(name=key, value=value, evidence_ids=(evidence_id,))
            for key, value in sorted(properties.items(), key=lambda item: item[0].casefold())
            if key.casefold() in allowed and value.strip()
        )

    def _add_table(
        self,
        context: _ArtifactContext,
        extracted: ExtractedObjectSnapshot,
        source_object: AccessObjectEvidence,
        inventory_evidence_id: str,
    ) -> tuple[str, ...]:
        properties = extracted.sanitized_properties
        raw_connect_metadata_status = properties.get(
            "connect_metadata_status", "available"
        ).strip().casefold()
        if raw_connect_metadata_status not in {"available", "unavailable"}:
            self._warn(
                context.staged.artifact_id,
                f"table_connect_metadata_status_invalid:{source_object.object_id}",
            )
            raw_connect_metadata_status = "unavailable"
        connect_metadata_status = cast(
            Literal["available", "unavailable"], raw_connect_metadata_status
        )
        link_status_unknown = (
            source_object.object_type == AccessObjectType.TABLE_LINK_STATUS_UNKNOWN
            or connect_metadata_status == "unavailable"
        )
        linked: bool | None = (
            None
            if link_status_unknown
            else source_object.object_type == AccessObjectType.LINKED_TABLE
        )
        source_table_name = properties.get("source_table_name", "").strip() or None
        evidence_ids = [inventory_evidence_id]
        connection_id: str | None = None
        datasource_id: str | None = None

        if linked is True:
            connection_evidence = self._new_evidence(
                context.staged,
                object_id=source_object.object_id,
                fact_type="linked_table_connection",
                location="TableDef.Connect",
                observation="Sanitized linked-table connection declaration parsed",
            )
            evidence_ids.append(connection_evidence.evidence_id)
            connection_record = self._connection_evidence(
                context.staged,
                object_id=source_object.object_id,
                connection=properties.get("connect", ""),
                connection_kind=ConnectionKind.LINKED_TABLE,
                provenance=ConnectionProvenance.TABLEDEF_CONNECT,
                evidence_id=connection_evidence.evidence_id,
            )
            connection_id = connection_record.connection_id
            datasource_id = connection_record.datasource_id
            if source_table_name is not None:
                source_evidence = self._new_evidence(
                    context.staged,
                    object_id=source_object.object_id,
                    fact_type="linked_table_source",
                    location="TableDef.SourceTableName",
                    observation="Linked-table external object identity extracted",
                )
                evidence_ids.append(source_evidence.evidence_id)
            else:
                unresolved_evidence = self._add_unresolved(
                    context.staged,
                    source_object.object_id,
                    reference=source_object.name,
                    reason="linked_table_source_name_unavailable",
                    connection_id=connection_id,
                    location=ConnectionProvenance.TABLEDEF_CONNECT.value,
                )
                evidence_ids.append(unresolved_evidence)
        elif linked is None:
            if source_table_name is not None:
                source_evidence = self._new_evidence(
                    context.staged,
                    object_id=source_object.object_id,
                    fact_type="table_source_name",
                    location="TableDef.SourceTableName",
                    observation="TableDef source name extracted while link status is unknown",
                )
                evidence_ids.append(source_evidence.evidence_id)
            unresolved_evidence = self._add_unresolved(
                context.staged,
                source_object.object_id,
                reference=source_table_name or source_object.name,
                reason="tabledef_connect_metadata_unavailable",
                connection_id=None,
                location=ConnectionProvenance.TABLEDEF_CONNECT.value,
                supporting_evidence_ids=tuple(evidence_ids),
            )
            evidence_ids.append(unresolved_evidence)
        elif properties.get("connect", "").strip():
            self._warn(context.staged.artifact_id, "local_table_has_connection_metadata")

        is_system = _property_bool(
            properties, "system"
        ) or source_object.name.casefold().startswith("msys")
        is_hidden = _property_bool(properties, "hidden") or source_object.name.startswith("~")
        table = AccessTableEvidence(
            object_id=source_object.object_id,
            is_linked=linked,
            connect_metadata_status=connect_metadata_status,
            is_hidden=is_hidden,
            is_system=is_system,
            source_table_name=source_table_name if linked is not False else None,
            connection_id=connection_id,
            evidence_ids=tuple(evidence_ids),
        )
        self.tables[table.object_id] = table
        self.table_work[table.object_id] = _TableWork(
            object_id=table.object_id,
            artifact_id=context.staged.artifact_id,
            connection_id=connection_id,
            datasource_id=datasource_id,
            source_table_name=source_table_name,
            evidence_ids=tuple(evidence_ids),
        )
        return tuple(evidence_ids[1:])

    def _add_query(
        self,
        context: _ArtifactContext,
        extracted: ExtractedObjectSnapshot,
        source_object: AccessObjectEvidence,
        inventory_evidence_id: str,
    ) -> tuple[str, ...]:
        properties = extracted.sanitized_properties
        dao_type = self._optional_int(
            context.staged.artifact_id,
            source_object.object_id,
            "dao_type_code",
            properties.get("dao_type_code", ""),
        )
        query_kind = _query_kind(properties.get("query_kind", ""), dao_type)
        returns_records = self._optional_bool(
            context.staged.artifact_id,
            source_object.object_id,
            "returns_records",
            properties.get("returns_records", ""),
        )
        odbc_timeout = self._optional_int(
            context.staged.artifact_id,
            source_object.object_id,
            "odbc_timeout",
            properties.get("odbc_timeout", ""),
            minimum=0,
        )
        parameters = self._parameters(
            context.staged.artifact_id,
            source_object.object_id,
            properties.get("parameters", ""),
        )
        attributes = self._optional_int(
            context.staged.artifact_id,
            source_object.object_id,
            "attributes",
            properties.get("attributes", ""),
        )
        raw_connect_metadata_status = properties.get(
            "connect_metadata_status", "available"
        ).strip().casefold()
        if raw_connect_metadata_status not in {"available", "unavailable"}:
            self._warn(
                context.staged.artifact_id,
                f"query_connect_metadata_status_invalid:{source_object.object_id}",
            )
            raw_connect_metadata_status = "unavailable"
        connect_metadata_status = cast(
            Literal["available", "unavailable"], raw_connect_metadata_status
        )
        sql = extracted.sanitized_definition or ""
        sql_evidence = self._new_evidence(
            context.staged,
            object_id=source_object.object_id,
            fact_type="query_sql",
            location="QueryDef.SQL",
            observation="Sanitized QueryDef SQL extracted",
        )
        metadata_evidence = self._new_evidence(
            context.staged,
            object_id=source_object.object_id,
            fact_type="query_metadata",
            location="QueryDef metadata",
            observation=(
                f"QueryDef metadata extracted (kind={query_kind.value};"
                f"dao_type={dao_type if dao_type is not None else 'unknown'})"
            ),
        )
        evidence_ids = [
            inventory_evidence_id,
            sql_evidence.evidence_id,
            metadata_evidence.evidence_id,
        ]

        connection = properties.get("connect", "").strip()
        is_pass_through = query_kind in _PASS_THROUGH_QUERY_KINDS
        connection_id: str | None = None
        datasource_id: str | None = None
        connection_evidence_ids: tuple[str, ...] = ()
        if is_pass_through or connection or connect_metadata_status == "unavailable":
            connection_evidence = self._new_evidence(
                context.staged,
                object_id=source_object.object_id,
                fact_type="query_connection",
                location="QueryDef.Connect",
                observation="Sanitized QueryDef connection declaration parsed",
            )
            evidence_ids.append(connection_evidence.evidence_id)
            kind = (
                ConnectionKind.PASS_THROUGH_QUERY
                if is_pass_through
                else ConnectionKind.UNKNOWN
            )
            connection_record = self._connection_evidence(
                context.staged,
                object_id=source_object.object_id,
                connection=connection,
                connection_kind=kind,
                provenance=ConnectionProvenance.QUERYDEF_CONNECT,
                evidence_id=connection_evidence.evidence_id,
            )
            connection_id = connection_record.connection_id
            datasource_id = connection_record.datasource_id
            connection_kind = kind
            connection_provenance = ConnectionProvenance.QUERYDEF_CONNECT
            connection_evidence_ids = (connection_evidence.evidence_id,)
            if connect_metadata_status == "unavailable":
                unresolved_evidence = self._add_unresolved(
                    context.staged,
                    source_object.object_id,
                    reference=source_object.name,
                    reason="querydef_connect_metadata_unavailable",
                    connection_id=connection_id,
                    location=ConnectionProvenance.QUERYDEF_CONNECT.value,
                    supporting_evidence_ids=connection_evidence_ids,
                )
                evidence_ids.append(unresolved_evidence)
        else:
            connection_kind = ConnectionKind.LOCAL_ACCESS
            connection_provenance = ConnectionProvenance.LOCAL_ACCESS

        query = QueryEvidence(
            object_id=source_object.object_id,
            query_kind=query_kind,
            dao_type=dao_type,
            attributes=attributes,
            sanitized_sql=sql or None,
            returns_records=returns_records,
            odbc_timeout_seconds=odbc_timeout,
            is_hidden=_property_bool(properties, "hidden") or source_object.name.startswith("~"),
            is_system=(
                _property_bool(properties, "system")
                or source_object.name.casefold().startswith("~sq_")
            ),
            connect_metadata_status=connect_metadata_status,
            connection_kind=connection_kind,
            connection_id=connection_id,
            parameters=parameters,
            evidence_ids=tuple(evidence_ids),
        )
        self.queries[query.object_id] = query
        self.query_work[query.object_id] = _QueryWork(
            object_id=query.object_id,
            artifact_id=context.staged.artifact_id,
            sql=sql,
            query_kind=query_kind,
            connection_id=connection_id,
            datasource_id=datasource_id,
            connection_provenance=connection_provenance,
            connection_evidence_ids=connection_evidence_ids,
        )
        return tuple(evidence_ids[1:])

    def _connection_evidence(
        self,
        artifact: StagedArtifactRecord,
        *,
        object_id: str,
        connection: str,
        connection_kind: ConnectionKind,
        provenance: ConnectionProvenance,
        evidence_id: str,
        instance_label: str | None = None,
    ) -> ConnectionEvidence:
        parsed = parse_connection_string_details(connection)
        declared_identity = normalize_connection_identity(parsed)
        uses_dsn = bool(
            parsed.values_for("dsn")
            or parsed.values_for("filedsn")
            or parsed.values_for("file dsn")
        )
        resolution_provenance: tuple[str, ...] = ("declared_connection_metadata",)
        resolution_warnings: tuple[str, ...] = ()

        if uses_dsn:
            try:
                resolution = self.dsn_resolver(connection)
            except Exception:
                self._warn(artifact.artifact_id, f"dsn_resolver_failed:{object_id}")
                identity = declared_identity
                status = ResolutionStatus.UNRESOLVED
                resolution_warnings = ("dsn_resolver_failed_safely",)
            else:
                identity = (
                    resolution.identity
                    or resolution.declared_identity
                    or declared_identity
                )
                status = ResolutionStatus(resolution.status.value)
                resolution_provenance = tuple(
                    ["declared_connection_metadata"]
                    + [
                        (
                            "analysis_host_windows_registry:"
                            f"{item.scope.value}:{item.bitness.value}:{item.outcome.value}:"
                            f"{','.join(item.lineage_keys) or 'none'}"
                        )
                        for item in resolution.provenance
                    ]
                )
                resolution_warnings = (
                    f"dsn_resolution_reason:{resolution.reason}",
                    f"dsn_registry_candidate_count:{resolution.candidate_count}",
                )
                if status != ResolutionStatus.RESOLVED:
                    resolution_warnings = (
                        *resolution_warnings,
                        f"dsn_resolution_{status.value}",
                    )
        else:
            identity = declared_identity
            if parsed.malformed or identity.conflicts:
                status = ResolutionStatus.MALFORMED
                resolution_warnings = ("connection_metadata_malformed",)
            elif any((identity.server, identity.database, identity.file)):
                status = ResolutionStatus.RESOLVED
            else:
                status = ResolutionStatus.UNRESOLVED
                resolution_warnings = ("connection_endpoint_unresolved",)

        datasource_id: str | None = None
        if any(
            (
                identity.driver,
                identity.dsn,
                identity.server,
                identity.database,
                identity.file,
            )
        ):
            datasource = DatasourceIdentity(
                platform=identity.platform,
                driver=identity.driver,
                dsn=identity.dsn,
                server=identity.server,
                database=identity.database,
                resource=identity.file,
            )
            self.datasources.setdefault(datasource.datasource_id, datasource)
            datasource_id = datasource.datasource_id
            self._store_datasource_node(datasource)
        elif status == ResolutionStatus.RESOLVED:
            status = ResolutionStatus.UNRESOLVED
            resolution_warnings = (*resolution_warnings, "resolved_endpoint_missing")

        connection_record = ConnectionEvidence(
            application_id=self.application.application_id,
            artifact_id=artifact.artifact_id,
            artifact_sha256=artifact.sha256,
            object_id=object_id,
            connection_kind=connection_kind,
            sanitized_summary=(
                f"{connection_kind.value} connection metadata"
                + (f" ({instance_label})" if instance_label else "")
            ),
            direct_provenance=provenance,
            resolution_status=status,
            resolution_provenance=resolution_provenance,
            resolution_warnings=resolution_warnings,
            datasource_id=datasource_id,
            evidence_ids=(evidence_id,),
        )
        if connection_record.connection_id in self.connections:
            raise ValueError("duplicate object-scoped connection evidence")
        self.connections[connection_record.connection_id] = connection_record
        return connection_record

    def _add_nodes(self) -> None:
        self._store_node(
            DependencyNode(
                application_id=self.application.application_id,
                kind=DependencyNodeKind.APPLICATION,
                label=self.application.application_name,
            )
        )
        for source_object in self.objects.values():
            self._store_node(
                DependencyNode(
                    application_id=self.application.application_id,
                    kind=DependencyNodeKind.ACCESS_OBJECT,
                    label=f"{source_object.object_type.value}:{source_object.name}",
                    artifact_id=source_object.artifact_id,
                    object_id=source_object.object_id,
                )
            )
        for datasource in self.datasources.values():
            self._store_datasource_node(datasource)

    def _store_datasource_node(self, datasource: DatasourceIdentity) -> DependencyNode:
        return self._store_node(
            DependencyNode(
                application_id=self.application.application_id,
                kind=(
                    DependencyNodeKind.FILE
                    if datasource.resource is not None
                    else DependencyNodeKind.DATASOURCE
                ),
                label=_datasource_label(datasource),
                datasource_id=datasource.datasource_id,
            )
        )

    def _application_node(self) -> DependencyNode:
        return next(
            item for item in self.nodes.values() if item.kind == DependencyNodeKind.APPLICATION
        )

    def _analyze_non_query_objects(self) -> None:
        for work in sorted(self.object_work.values(), key=lambda item: item.object_id):
            if work.object_type in {
                AccessObjectType.TABLE,
                AccessObjectType.LINKED_TABLE,
                AccessObjectType.TABLE_LINK_STATUS_UNKNOWN,
                AccessObjectType.QUERY,
            }:
                continue
            if work.object_type == AccessObjectType.REFERENCE:
                path = work.properties.get("full_path", "").strip()
                if path:
                    self._add_file_dependency(
                        work,
                        path,
                        DataOperation.UNKNOWN,
                        "reference file metadata",
                        1,
                    )
            if work.object_type in {AccessObjectType.FORM, AccessObjectType.REPORT}:
                self._analyze_ui_object(work)
            if work.object_type == AccessObjectType.MACRO:
                self._analyze_macro(work)
            if work.object_type in {
                AccessObjectType.MODULE,
                AccessObjectType.PROCEDURE,
                AccessObjectType.FORM,
                AccessObjectType.REPORT,
            }:
                self._analyze_vba(work)

    def _analyze_ui_object(self, work: _ObjectWork) -> None:
        properties = _root_ui_properties(work.definition)
        record_source = properties.get("recordsource", "").strip()
        if record_source:
            evidence = self._new_evidence(
                self._staged_artifact(work.artifact_id),
                object_id=work.object_id,
                fact_type="record_source",
                location="SaveAsText.RecordSource",
                observation="Form or report RecordSource extracted",
            )
            self._extend_source_evidence(work.object_id, evidence.evidence_id)
            if _SQL_PREFIX.match(record_source):
                self._analyze_embedded_sql(
                    work,
                    record_source,
                    location="SaveAsText.RecordSource",
                    supporting_evidence=(evidence.evidence_id,),
                )
            elif record_source.startswith("="):
                self._record_unresolved(
                    work,
                    "<dynamic RecordSource>",
                    "record_source_constructed_dynamically",
                    "SaveAsText.RecordSource",
                )
            else:
                self._add_local_object_reference(
                    work,
                    record_source.strip("[]"),
                    operation=DataOperation.READ,
                    relationship=DependencyRelation.BINDS_TO,
                    evidence_ids=(evidence.evidence_id,),
                    expected_types={
                        AccessObjectType.TABLE,
                        AccessObjectType.LINKED_TABLE,
                        AccessObjectType.TABLE_LINK_STATUS_UNKNOWN,
                        AccessObjectType.QUERY,
                    },
                    missing_reason="record_source_not_found_in_source_artifact",
                )

        code_match = _CODE_BEHIND_MARKER.search(work.definition)
        code_available = bool(
            code_match and work.definition[code_match.end() :].strip()
        )
        if code_available:
            evidence = self._new_evidence(
                self._staged_artifact(work.artifact_id),
                object_id=work.object_id,
                fact_type="code_behind",
                location="SaveAsText.CodeBehind",
                observation="Form or report code-behind extracted",
            )
            self._extend_source_evidence(work.object_id, evidence.evidence_id)

        for event_name, event_target in sorted(properties.items()):
            if not event_name.startswith("on") or not event_target:
                continue
            evidence = self._new_evidence(
                self._staged_artifact(work.artifact_id),
                object_id=work.object_id,
                fact_type="ui_event_binding",
                location=f"SaveAsText.{event_name}",
                observation="Form or report event binding extracted",
            )
            self._extend_source_evidence(work.object_id, evidence.evidence_id)
            normalized = event_target.strip().strip('"')
            if normalized.casefold() == "[event procedure]":
                if not code_available:
                    self._record_unresolved(
                        work,
                        "<event procedure>",
                        "event_procedure_code_not_available",
                        "SaveAsText event binding",
                    )
                continue
            if normalized.startswith("="):
                self._record_unresolved(
                    work,
                    "<dynamic event expression>",
                    "event_expression_target_dynamic",
                    "SaveAsText event binding",
                )
                continue
            self._add_local_object_reference(
                work,
                normalized,
                operation=DataOperation.EXECUTE,
                relationship=DependencyRelation.CALLS,
                evidence_ids=(evidence.evidence_id,),
                expected_types={AccessObjectType.MACRO},
                missing_reason="event_macro_not_found_in_source_artifact",
                include_interaction=False,
            )

    def _analyze_macro(self, work: _ObjectWork) -> None:
        for ordinal, (action, properties) in enumerate(
            _macro_actions(work.definition), start=1
        ):
            normalized = action.casefold()
            evidence = self._new_evidence(
                self._staged_artifact(work.artifact_id),
                object_id=work.object_id,
                fact_type="macro_action",
                location=f"SaveAsText.MacroAction#{ordinal}",
                observation=f"Access macro action extracted ({normalized})",
            )
            self._extend_source_evidence(work.object_id, evidence.evidence_id)
            target_types = {
                "openquery": {AccessObjectType.QUERY},
                "openform": {AccessObjectType.FORM},
                "openreport": {AccessObjectType.REPORT},
                "runmacro": {AccessObjectType.MACRO},
            }
            if normalized in target_types:
                target = _first_property(
                    properties,
                    {
                        "openquery": ("queryname", "objectname", "argument"),
                        "openform": ("formname", "objectname", "argument"),
                        "openreport": ("reportname", "objectname", "argument"),
                        "runmacro": ("macroname", "objectname", "argument"),
                    }[normalized],
                )
                if not target:
                    self._record_unresolved(
                        work,
                        "<dynamic macro target>",
                        f"macro_{normalized}_target_unavailable",
                        "SaveAsText macro action",
                    )
                    continue
                self._add_local_object_reference(
                    work,
                    target,
                    operation=DataOperation.EXECUTE,
                    relationship=DependencyRelation.CALLS,
                    evidence_ids=(evidence.evidence_id,),
                    expected_types=target_types[normalized],
                    missing_reason=f"macro_{normalized}_target_not_found",
                    include_interaction=normalized == "openquery",
                )
            elif normalized in {"runsql", "openquery"}:
                sql = _first_property(
                    properties, ("sqlstatement", "sql", "argument")
                )
                if sql and _SQL_PREFIX.match(sql):
                    self._analyze_embedded_sql(
                        work,
                        sql,
                        location="SaveAsText macro action",
                        supporting_evidence=(evidence.evidence_id,),
                    )
                else:
                    self._record_unresolved(
                        work,
                        "<dynamic macro SQL>",
                        "macro_sql_target_dynamic_or_unavailable",
                        "SaveAsText macro action",
                    )

            for path in sorted(
                {
                    value
                    for value in properties.values()
                    if _looks_like_file_reference(value)
                },
                key=str.casefold,
            ):
                operation = _macro_file_operation(normalized, properties)
                self._add_file_dependency(
                    work,
                    path,
                    operation,
                    "macro file argument",
                    ordinal,
                )

    def _analyze_vba(self, work: _ObjectWork) -> None:
        code = _vba_code(work)
        if not code.strip():
            return
        artifact = self._staged_artifact(work.artifact_id)
        for ordinal, procedure in enumerate(
            sorted(set(extract_procedures(code)), key=str.casefold), start=1
        ):
            evidence = self._new_evidence(
                artifact,
                object_id=work.object_id,
                fact_type="vba_procedure",
                location=f"VBA procedure #{ordinal}",
                observation=f"VBA procedure inventoried ({procedure})",
            )
            self._extend_source_evidence(work.object_id, evidence.evidence_id)

        connection_by_variable: dict[str, str] = {}
        connection_matches = [
            match
            for match in _VBA_STRING.finditer(code)
            if _looks_like_connection_literal(_decode_vba_string(match.group("value")))
        ]
        dynamic_connection_sources: dict[str, tuple[int, str | None]] = {}
        static_connection_matches: list[re.Match[str]] = []
        for match in connection_matches:
            statement_start, statement_end = _vba_statement_bounds(code, match.start())
            receiver = _connection_statement_receiver(code, statement_start, statement_end)
            if receiver is None:
                # A connection-shaped literal is not itself a connection declaration. Indirect
                # assignments are conservatively handled by the non-literal connection sink below.
                continue
            if not _vba_connection_expression_is_dynamic(code, match):
                static_connection_matches.append(match)
                continue
            receiver_variable = receiver.casefold()
            key = f"variable:{receiver_variable}"
            dynamic_connection_sources.setdefault(
                key, (statement_start, receiver_variable)
            )

        for match in re.finditer(
            r"(?im)\b(?P<variable>[A-Za-z_]\w*)\."
            r"(?:ConnectionString[ \t]*=[ \t]*|Open\b[ \t]*(?:\([ \t]*)?)"
            r"(?!\")(?=\S)",
            code,
        ):
            variable = match.group("variable").casefold()
            dynamic_connection_sources.setdefault(
                f"variable:{variable}", (match.start(), variable)
            )

        dynamic_connection_variables = {
            variable
            for _position, variable in dynamic_connection_sources.values()
            if variable is not None
        }
        static_connection_matches = [
            match
            for match in static_connection_matches
            if (receiver := _connection_receiver(code, match.start())) is None
            or receiver.casefold() not in dynamic_connection_variables
        ]
        for ordinal, match in enumerate(static_connection_matches, start=1):
            declaration = _decode_vba_string(match.group("value"))
            evidence = self._new_evidence(
                artifact,
                object_id=work.object_id,
                fact_type="vba_connection",
                location=f"VBA connection metadata #{ordinal}",
                observation="Static VBA connection metadata parsed",
            )
            self._extend_source_evidence(work.object_id, evidence.evidence_id)
            connection = self._connection_evidence(
                artifact,
                object_id=work.object_id,
                connection=declaration,
                connection_kind=ConnectionKind.VBA_CONNECTION,
                provenance=ConnectionProvenance.VBA_CONNECTION_STRING,
                evidence_id=evidence.evidence_id,
                instance_label=f"static #{ordinal}",
            )
            variable = _connection_receiver(code, match.start())
            if variable:
                connection_by_variable[variable.casefold()] = connection.connection_id
            if connection.datasource_id is not None:
                self._add_edge(
                    self._object_node(work.object_id).node_id,
                    self._datasource_node(connection.datasource_id).node_id,
                    DependencyRelation.DEPENDS_ON,
                    DataOperation.UNKNOWN,
                    (evidence.evidence_id,),
                )

        for ordinal, (_position, variable) in enumerate(
            sorted(dynamic_connection_sources.values(), key=lambda item: (item[0], item[1] or "")),
            start=1,
        ):
            evidence = self._new_evidence(
                artifact,
                object_id=work.object_id,
                origin=EvidenceOrigin.UNRESOLVED,
                fact_type="vba_dynamic_connection",
                location=f"VBA dynamic connection #{ordinal}",
                observation="VBA connection metadata is constructed dynamically",
                confidence=Confidence.MEDIUM,
            )
            self._extend_source_evidence(work.object_id, evidence.evidence_id)
            connection = self._connection_evidence(
                artifact,
                object_id=work.object_id,
                connection="",
                connection_kind=ConnectionKind.VBA_CONNECTION,
                provenance=ConnectionProvenance.UNRESOLVED_DYNAMIC,
                evidence_id=evidence.evidence_id,
                instance_label=f"dynamic #{ordinal}",
            )
            if variable is not None:
                connection_by_variable[variable] = connection.connection_id
            self._record_unresolved(
                work,
                "<dynamic connection>",
                "vba_connection_constructed_dynamically",
                ConnectionProvenance.UNRESOLVED_DYNAMIC.value,
                connection_id=connection.connection_id,
                supporting_evidence_ids=(evidence.evidence_id,),
            )

        file_connections = self._analyze_vba_files(work, code)
        database_connection_by_variable = _database_variable_connections(
            code, file_connections
        )
        local_database_variables = _local_database_variables(code)
        active_connection = _active_connection_variables(code)

        for match in re.finditer(
            r"(?im)\bDoCmd\.(?P<action>OpenQuery|OpenForm|OpenReport|RunMacro)\b"
            r"(?P<tail>[^\r\n]*)",
            code,
        ):
            action = match.group("action").casefold()
            target = _first_vba_literal_argument(match.group("tail"))
            evidence = self._new_evidence(
                artifact,
                object_id=work.object_id,
                fact_type="docmd_action",
                location=f"VBA DoCmd.{match.group('action')}",
                observation=f"Explicit DoCmd action extracted ({action})",
            )
            self._extend_source_evidence(work.object_id, evidence.evidence_id)
            if target is None:
                self._record_unresolved(
                    work,
                    "<dynamic DoCmd target>",
                    f"docmd_{action}_target_dynamic",
                    f"VBA DoCmd.{match.group('action')}",
                )
                continue
            expected_types = {
                "openquery": {AccessObjectType.QUERY},
                "openform": {AccessObjectType.FORM},
                "openreport": {AccessObjectType.REPORT},
                "runmacro": {AccessObjectType.MACRO},
            }[action]
            self._add_local_object_reference(
                work,
                target,
                operation=DataOperation.EXECUTE,
                relationship=DependencyRelation.CALLS,
                evidence_ids=(evidence.evidence_id,),
                expected_types=expected_types,
                missing_reason=f"docmd_{action}_target_not_found",
                include_interaction=action == "openquery",
            )

        for match in re.finditer(
            r"(?im)\bDoCmd\.RunSQL\b(?P<tail>[^\r\n]*)", code
        ):
            evidence = self._new_evidence(
                artifact,
                object_id=work.object_id,
                fact_type="docmd_action",
                location="VBA DoCmd.RunSQL",
                observation="Explicit DoCmd action extracted (runsql)",
            )
            self._extend_source_evidence(work.object_id, evidence.evidence_id)
            sql = _first_vba_literal_argument(match.group("tail"))
            if sql is None:
                self._record_unresolved(
                    work,
                    "<dynamic SQL>",
                    "docmd_runsql_constructed_dynamically",
                    "VBA DoCmd.RunSQL",
                )
            else:
                self._analyze_embedded_sql(
                    work,
                    sql,
                    location="VBA DoCmd.RunSQL",
                    supporting_evidence=(evidence.evidence_id,),
                )

        for match in re.finditer(
            r"(?im)\bDoCmd\.(?P<action>Transfer[A-Za-z0-9_]*|OutputTo|SendObject)\b"
            r"(?P<tail>[^\r\n]*)",
            code,
        ):
            action = match.group("action").casefold()
            evidence = self._new_evidence(
                artifact,
                object_id=work.object_id,
                fact_type="docmd_action",
                location=f"VBA DoCmd.{match.group('action')}",
                observation=f"Explicit DoCmd action extracted ({action})",
            )
            self._extend_source_evidence(work.object_id, evidence.evidence_id)
            arguments = _vba_arguments(match.group("tail"))
            if action in {"transferspreadsheet", "transfertext"}:
                direction = arguments[0].casefold() if arguments else ""
                operation = (
                    DataOperation.READ
                    if direction in {"acexport", "acexportdelim", "acexportfixed", "1"}
                    else DataOperation.INSERT
                    if direction
                    in {"acimport", "acimportdelim", "acimportfixed", "acimporthtml", "0"}
                    else DataOperation.UNKNOWN
                )
                target = (
                    _quoted_vba_value(arguments[2])
                    if len(arguments) > 2
                    else None
                )
                if target is None:
                    self._record_unresolved(
                        work,
                        "<dynamic transfer object>",
                        f"docmd_{action}_object_dynamic",
                        f"VBA DoCmd.{match.group('action')}",
                    )
                else:
                    self._add_local_object_reference(
                        work,
                        target,
                        operation=operation,
                        relationship=_dependency_relation(operation),
                        evidence_ids=(evidence.evidence_id,),
                        expected_types={
                            AccessObjectType.TABLE,
                            AccessObjectType.LINKED_TABLE,
                            AccessObjectType.TABLE_LINK_STATUS_UNKNOWN,
                            AccessObjectType.QUERY,
                        },
                        missing_reason=f"docmd_{action}_object_not_found",
                    )
            elif action in {"outputto", "sendobject"} and len(arguments) > 1:
                target = _quoted_vba_value(arguments[1])
                if target:
                    self._add_local_object_reference(
                        work,
                        target,
                        operation=DataOperation.READ,
                        relationship=DependencyRelation.PRODUCES,
                        evidence_ids=(evidence.evidence_id,),
                        expected_types={
                            AccessObjectType.TABLE,
                            AccessObjectType.TABLE_LINK_STATUS_UNKNOWN,
                            AccessObjectType.QUERY,
                            AccessObjectType.FORM,
                            AccessObjectType.REPORT,
                        },
                        missing_reason=f"docmd_{action}_object_not_found",
                        include_interaction=False,
                    )

        for match in re.finditer(
            r"(?im)\b(?P<receiver>CurrentDb(?:\(\))?|[A-Za-z_]\w*)\.Execute\b"
            r"(?P<tail>[^\r\n]*)",
            code,
        ):
            sql = _first_vba_literal_argument(match.group("tail"))
            receiver = match.group("receiver").casefold().replace("()", "")
            connection_id = connection_by_variable.get(receiver)
            connection_id = database_connection_by_variable.get(receiver, connection_id)
            is_local = receiver == "currentdb" or receiver in local_database_variables
            if sql is None:
                self._record_unresolved(
                    work,
                    "<dynamic SQL>",
                    "vba_execute_sql_constructed_dynamically",
                    "VBA Execute",
                    connection_id=connection_id,
                )
            elif connection_id is not None:
                self._analyze_embedded_sql(
                    work,
                    sql,
                    location="VBA Execute",
                    connection_id=connection_id,
                )
            elif is_local:
                self._analyze_embedded_sql(work, sql, location="VBA Execute")
            else:
                self._analyze_embedded_sql(
                    work,
                    sql,
                    location="VBA Execute",
                    unknown_scope_reason="vba_execute_connection_not_resolved",
                )

        for match in re.finditer(
            r"(?im)\b(?P<command>[A-Za-z_]\w*)\.CommandText\s*=\s*"
            r"(?P<value>\"(?:\"\"|[^\"\r\n])*\"|[^\r\n]+)",
            code,
        ):
            sql = _quoted_vba_value(match.group("value"))
            command = match.group("command").casefold()
            variable = active_connection.get(command)
            connection_id = connection_by_variable.get(variable or "")
            if sql is None:
                self._record_unresolved(
                    work,
                    "<dynamic command text>",
                    "ado_command_text_constructed_dynamically",
                    "VBA ADODB.CommandText",
                    connection_id=connection_id,
                )
            elif connection_id is not None:
                self._analyze_embedded_sql(
                    work,
                    sql,
                    location="VBA ADODB.CommandText",
                    connection_id=connection_id,
                )
            else:
                self._analyze_embedded_sql(
                    work,
                    sql,
                    location="VBA ADODB.CommandText",
                    unknown_scope_reason="ado_command_connection_not_resolved",
                )

        for match in re.finditer(
            r"(?im)\bQueryDefs\s*(?:\(\s*\"(?P<quoted>(?:\"\"|[^\"\r\n])+)\""
            r"\s*\)|!\[?(?P<bang>[A-Za-z_][\w $-]*)\]?)",
            code,
        ):
            target = _decode_vba_string(match.group("quoted") or match.group("bang"))
            evidence = self._new_evidence(
                artifact,
                object_id=work.object_id,
                fact_type="dao_saved_query_reference",
                location="VBA DAO.QueryDefs",
                observation="DAO saved-query reference extracted",
            )
            self._extend_source_evidence(work.object_id, evidence.evidence_id)
            self._add_local_object_reference(
                work,
                target,
                operation=DataOperation.EXECUTE,
                relationship=DependencyRelation.CALLS,
                evidence_ids=(evidence.evidence_id,),
                expected_types={AccessObjectType.QUERY},
                missing_reason="dao_saved_query_not_found",
            )

        for match in re.finditer(
            r"(?im)\b(?:(?P<receiver>[A-Za-z_]\w*)\.)?OpenRecordset\s*"
            r"\((?P<tail>[^\r\n]*)",
            code,
        ):
            value = _first_vba_literal_argument(match.group("tail"))
            receiver = (match.group("receiver") or "currentdb").casefold()
            connection_id = connection_by_variable.get(receiver)
            connection_id = database_connection_by_variable.get(receiver, connection_id)
            if value is None:
                self._record_unresolved(
                    work,
                    "<dynamic recordset source>",
                    "dao_or_ado_recordset_source_dynamic",
                    "VBA OpenRecordset",
                    connection_id=connection_id,
                )
            elif _SQL_PREFIX.match(value):
                self._analyze_embedded_sql(
                    work,
                    value,
                    location="VBA OpenRecordset",
                    connection_id=connection_id,
                )
            else:
                evidence = self._new_evidence(
                    artifact,
                    object_id=work.object_id,
                    fact_type="recordset_source",
                    location="VBA OpenRecordset",
                    observation="DAO or ADO recordset source extracted",
                )
                self._extend_source_evidence(work.object_id, evidence.evidence_id)
                if connection_id is None:
                    self._add_local_object_reference(
                        work,
                        value,
                        operation=DataOperation.READ,
                        relationship=DependencyRelation.READS,
                        evidence_ids=(evidence.evidence_id,),
                        expected_types={
                            AccessObjectType.TABLE,
                            AccessObjectType.LINKED_TABLE,
                            AccessObjectType.TABLE_LINK_STATUS_UNKNOWN,
                            AccessObjectType.QUERY,
                        },
                        missing_reason="recordset_source_not_found",
                    )
                else:
                    self._add_external_reference_for_connection(
                        work,
                        connection_id,
                        value,
                        DataOperation.READ,
                        (evidence.evidence_id,),
                        "VBA OpenRecordset",
                    )

    def _analyze_vba_files(
        self, work: _ObjectWork, code: str
    ) -> dict[str, tuple[str, str]]:
        found: dict[str, tuple[str, DataOperation]] = {}
        for match in _VBA_STRING.finditer(code):
            value = _decode_vba_string(match.group("value"))
            if not _looks_like_file_reference(value):
                continue
            line_start = code.rfind("\n", 0, match.start()) + 1
            line_end = code.find("\n", match.end())
            line_end = len(code) if line_end < 0 else line_end
            operation = _vba_file_operation(code[line_start:line_end], match.start() - line_start)
            normalized = normalize_source_identity(value)
            previous = found.get(normalized)
            if previous is None or previous[1] == DataOperation.UNKNOWN:
                found[normalized] = (value, operation)
        output: dict[str, tuple[str, str]] = {}
        for ordinal, (normalized, (path, operation)) in enumerate(
            sorted(found.items()), start=1
        ):
            connection_id, datasource_id = self._add_file_dependency(
                work,
                path,
                operation,
                "VBA file reference",
                ordinal,
            )
            output[normalized] = (connection_id, datasource_id)
        return output

    def _add_file_dependency(
        self,
        work: _ObjectWork,
        path: str,
        operation: DataOperation,
        location: str,
        ordinal: int,
    ) -> tuple[str, str]:
        artifact = self._staged_artifact(work.artifact_id)
        normalized_path = normalize_source_identity(path)
        evidence = self._new_evidence(
            artifact,
            object_id=work.object_id,
            fact_type="file_dependency",
            location=f"{location} #{ordinal}",
            observation="Static external file dependency extracted",
        )
        self._extend_source_evidence(work.object_id, evidence.evidence_id)
        datasource = DatasourceIdentity(
            platform=_file_platform(normalized_path),
            resource=normalized_path,
        )
        self.datasources.setdefault(datasource.datasource_id, datasource)
        datasource_node = self._store_datasource_node(datasource)
        connection = ConnectionEvidence(
            application_id=self.application.application_id,
            artifact_id=artifact.artifact_id,
            artifact_sha256=artifact.sha256,
            object_id=work.object_id,
            connection_kind=ConnectionKind.FILE_DEPENDENCY,
            sanitized_summary=f"file dependency metadata (static #{ordinal})",
            direct_provenance=ConnectionProvenance.FILESYSTEM_REFERENCE,
            resolution_status=ResolutionStatus.RESOLVED,
            resolution_provenance=("static_file_literal",),
            datasource_id=datasource.datasource_id,
            evidence_ids=(evidence.evidence_id,),
        )
        existing = self.connections.get(connection.connection_id)
        if existing is not None and existing.datasource_id != datasource.datasource_id:
            raise ValueError("file connection identity collision")
        self.connections.setdefault(connection.connection_id, connection)
        interaction = DatasourceInteraction(
            source_object_id=work.object_id,
            operation=operation,
            scope=InteractionScope.EXTERNAL,
            connection_id=connection.connection_id,
            datasource_id=datasource.datasource_id,
            evidence_ids=(evidence.evidence_id,),
        )
        self._add_interaction(interaction)
        relation = _dependency_relation(operation)
        self._add_edge(
            self._object_node(work.object_id).node_id,
            datasource_node.node_id,
            relation,
            operation,
            (evidence.evidence_id,),
        )
        self._add_edge(
            self._application_node().node_id,
            datasource_node.node_id,
            relation,
            operation,
            (evidence.evidence_id,),
        )
        return connection.connection_id, datasource.datasource_id

    def _analyze_embedded_sql(
        self,
        work: _ObjectWork,
        sql: str,
        *,
        location: str,
        supporting_evidence: tuple[str, ...] = (),
        connection_id: str | None = None,
        unknown_scope_reason: str | None = None,
    ) -> None:
        finding = classify_sql(sql)
        if not finding.references:
            if finding.operation not in {"READ", "UNKNOWN"}:
                self._record_unresolved(
                    work,
                    "<SQL target unavailable>",
                    "static_sql_target_not_parsed",
                    location,
                    connection_id=connection_id,
                    operation=_data_operation(finding.operation),
                    supporting_evidence_ids=supporting_evidence,
                )
            return
        for reference in finding.references:
            operation = _data_operation(reference.operation)
            evidence = self._new_evidence(
                self._staged_artifact(work.artifact_id),
                object_id=work.object_id,
                fact_type="embedded_sql_reference",
                location=location,
                observation=f"Embedded SQL {operation.value} object reference extracted",
            )
            self._extend_source_evidence(work.object_id, evidence.evidence_id)
            evidence_ids = (evidence.evidence_id, *supporting_evidence)
            if connection_id is not None:
                self._add_external_reference_for_connection(
                    work,
                    connection_id,
                    reference.name,
                    operation,
                    evidence_ids,
                    location,
                )
            elif unknown_scope_reason is not None:
                self._record_unresolved(
                    work,
                    reference.name,
                    unknown_scope_reason,
                    location,
                )
            else:
                self._add_local_object_reference(
                    work,
                    reference.name,
                    operation=operation,
                    relationship=_dependency_relation(operation),
                    evidence_ids=evidence_ids,
                    expected_types={
                        AccessObjectType.TABLE,
                        AccessObjectType.LINKED_TABLE,
                        AccessObjectType.TABLE_LINK_STATUS_UNKNOWN,
                        AccessObjectType.QUERY,
                    },
                    missing_reason="embedded_sql_reference_not_found",
                )

    def _add_local_object_reference(
        self,
        work: _ObjectWork,
        target_name: str,
        *,
        operation: DataOperation,
        relationship: DependencyRelation,
        evidence_ids: tuple[str, ...],
        expected_types: set[AccessObjectType] | None,
        missing_reason: str,
        include_interaction: bool = True,
    ) -> None:
        candidates = self.object_lookup.get(
            (work.artifact_id, target_name.strip().casefold()), []
        )
        if expected_types is not None:
            candidates = [
                object_id
                for object_id in candidates
                if self.objects[object_id].object_type in expected_types
            ]
        if len(candidates) != 1:
            reason = missing_reason if not candidates else f"{missing_reason}_ambiguous"
            self._record_unresolved(
                work,
                target_name,
                reason,
                ConnectionProvenance.LOCAL_ACCESS.value,
            )
            return
        target_id = candidates[0]
        if target_id == work.object_id:
            self._record_unresolved(
                work,
                target_name,
                "self_reference_not_materialized_as_dependency",
                ConnectionProvenance.LOCAL_ACCESS.value,
            )
            return
        self._add_edge(
            self._object_node(work.object_id).node_id,
            self._object_node(target_id).node_id,
            relationship,
            operation,
            evidence_ids,
        )
        if (
            self.objects[target_id].object_type
            == AccessObjectType.TABLE_LINK_STATUS_UNKNOWN
        ):
            unknown_table = self.table_work[target_id]
            self._record_unresolved(
                work,
                target_name,
                "tabledef_connect_metadata_unavailable",
                ConnectionProvenance.TABLEDEF_CONNECT.value,
                operation=operation,
                supporting_evidence_ids=(*evidence_ids, *unknown_table.evidence_ids),
            )
            return
        linked = self.table_work.get(target_id)
        if linked is not None and linked.connection_id is not None:
            if (
                linked.source_table_name is None
                or linked.datasource_id is None
                or self.connections[linked.connection_id].resolution_status
                != ResolutionStatus.RESOLVED
            ):
                self._record_unresolved(
                    work,
                    target_name,
                    "linked_table_external_target_unavailable",
                    ConnectionProvenance.TABLEDEF_CONNECT.value,
                    connection_id=linked.connection_id,
                    operation=operation,
                    supporting_evidence_ids=(*evidence_ids, *linked.evidence_ids),
                )
            elif include_interaction:
                self._add_external_interaction_values(
                    source_object_id=work.object_id,
                    connection_id=linked.connection_id,
                    datasource_id=linked.datasource_id,
                    reference=linked.source_table_name,
                    operation=operation,
                    evidence_ids=(*evidence_ids, *linked.evidence_ids),
                )
            return
        if include_interaction and self.objects[target_id].object_type in {
            AccessObjectType.TABLE,
            AccessObjectType.LINKED_TABLE,
            AccessObjectType.QUERY,
        }:
            self._add_interaction(
                DatasourceInteraction(
                    source_object_id=work.object_id,
                    operation=operation,
                    scope=InteractionScope.LOCAL,
                    local_target_object_id=target_id,
                    evidence_ids=evidence_ids,
                )
            )

    def _add_external_reference_for_connection(
        self,
        work: _ObjectWork,
        connection_id: str,
        reference: str,
        operation: DataOperation,
        evidence_ids: tuple[str, ...],
        location: str,
    ) -> None:
        connection = self.connections[connection_id]
        if (
            connection.resolution_status != ResolutionStatus.RESOLVED
            or connection.datasource_id is None
        ):
            self._record_unresolved(
                work,
                reference,
                "external_connection_not_resolved",
                location,
                connection_id=connection_id,
                operation=operation,
                supporting_evidence_ids=evidence_ids,
            )
            return
        self._add_external_interaction_values(
            source_object_id=work.object_id,
            connection_id=connection_id,
            datasource_id=connection.datasource_id,
            reference=reference,
            operation=operation,
            evidence_ids=evidence_ids,
        )

    def _record_unresolved(
        self,
        work: _ObjectWork,
        reference: str,
        reason: str,
        location: str,
        *,
        connection_id: str | None = None,
        operation: DataOperation = DataOperation.UNKNOWN,
        supporting_evidence_ids: tuple[str, ...] = (),
    ) -> None:
        evidence_id = self._add_unresolved(
            self._staged_artifact(work.artifact_id),
            work.object_id,
            reference=reference,
            reason=reason,
            connection_id=connection_id,
            location=location,
            operation=operation,
            supporting_evidence_ids=supporting_evidence_ids,
        )
        self._extend_source_evidence(work.object_id, evidence_id)

    def _add_linked_table_edges(self) -> None:
        for work in self.table_work.values():
            if work.datasource_id is None:
                continue
            source_node = self._object_node(work.object_id)
            datasource_node = self._datasource_node(work.datasource_id)
            self._add_edge(
                source_node.node_id,
                datasource_node.node_id,
                DependencyRelation.DEPENDS_ON,
                DataOperation.UNKNOWN,
                work.evidence_ids,
            )
            connection = self.connections[work.connection_id] if work.connection_id else None
            if (
                work.source_table_name is not None
                and connection is not None
                and connection.resolution_status == ResolutionStatus.RESOLVED
            ):
                target_node, _parts = self._external_node(
                    work.datasource_id, work.source_table_name
                )
                self._add_edge(
                    source_node.node_id,
                    target_node.node_id,
                    DependencyRelation.REFERENCES,
                    DataOperation.UNKNOWN,
                    work.evidence_ids,
                )

    def _analyze_query(self, work: _QueryWork) -> None:
        source_node = self._object_node(work.object_id)
        if work.datasource_id is not None:
            datasource_node = self._datasource_node(work.datasource_id)
            self._add_edge(
                source_node.node_id,
                datasource_node.node_id,
                DependencyRelation.DEPENDS_ON,
                DataOperation.UNKNOWN,
                work.connection_evidence_ids,
            )

        sql_finding = classify_sql(work.sql)
        if not sql_finding.references:
            if work.query_kind in _PASS_THROUGH_QUERY_KINDS:
                evidence_id = self._add_unresolved(
                    self._staged_artifact(work.artifact_id),
                    work.object_id,
                    reference="<pass-through target unavailable>",
                    reason="pass_through_sql_target_not_parsed",
                    connection_id=work.connection_id,
                    location=work.connection_provenance.value,
                    operation=_data_operation(sql_finding.operation),
                )
                self._extend_source_evidence(work.object_id, evidence_id)
            return

        for reference in sql_finding.references:
            self._add_query_reference(work, reference)

    def _add_query_reference(self, work: _QueryWork, reference: SqlObjectReference) -> None:
        artifact = self._staged_artifact(work.artifact_id)
        operation = _data_operation(reference.operation)
        reference_evidence = self._new_evidence(
            artifact,
            object_id=work.object_id,
            fact_type="sql_object_reference",
            location="QueryDef.SQL",
            observation=f"SQL {operation.value} object reference extracted",
        )
        evidence_ids = (reference_evidence.evidence_id, *work.connection_evidence_ids)
        self._extend_source_evidence(work.object_id, reference_evidence.evidence_id)

        if work.connection_id is not None:
            connection = self.connections[work.connection_id]
            if (
                connection.resolution_status == ResolutionStatus.RESOLVED
                and work.datasource_id is not None
            ):
                self._add_external_interaction(
                    work,
                    reference.name,
                    operation,
                    evidence_ids,
                )
            else:
                unresolved_evidence = self._add_unresolved(
                    artifact,
                    work.object_id,
                    reference=reference.name,
                    reason="external_connection_not_resolved",
                    connection_id=work.connection_id,
                    location=work.connection_provenance.value,
                    operation=operation,
                    supporting_evidence_ids=evidence_ids,
                )
                self._extend_source_evidence(work.object_id, unresolved_evidence)
            return

        matches = self.object_lookup.get((work.artifact_id, reference.name.casefold()), [])
        if len(matches) != 1:
            reason = (
                "local_reference_not_found_in_source_artifact"
                if not matches
                else "local_reference_is_ambiguous_in_source_artifact"
            )
            unresolved_evidence = self._add_unresolved(
                artifact,
                work.object_id,
                reference=reference.name,
                reason=reason,
                connection_id=None,
                location=ConnectionProvenance.LOCAL_ACCESS.value,
            )
            self._extend_source_evidence(work.object_id, unresolved_evidence)
            return

        target_id = matches[0]
        if target_id == work.object_id:
            unresolved_evidence = self._add_unresolved(
                artifact,
                work.object_id,
                reference=reference.name,
                reason="self_reference_not_materialized_as_dependency",
                connection_id=None,
                location=ConnectionProvenance.LOCAL_ACCESS.value,
            )
            self._extend_source_evidence(work.object_id, unresolved_evidence)
            return

        if (
            self.objects[target_id].object_type
            == AccessObjectType.TABLE_LINK_STATUS_UNKNOWN
        ):
            unknown_table = self.table_work[target_id]
            self._add_edge(
                self._object_node(work.object_id).node_id,
                self._object_node(target_id).node_id,
                _dependency_relation(operation),
                operation,
                evidence_ids,
            )
            unresolved_evidence = self._add_unresolved(
                artifact,
                work.object_id,
                reference=reference.name,
                reason="tabledef_connect_metadata_unavailable",
                connection_id=None,
                location=ConnectionProvenance.TABLEDEF_CONNECT.value,
                operation=operation,
                supporting_evidence_ids=(*evidence_ids, *unknown_table.evidence_ids),
            )
            self._extend_source_evidence(work.object_id, unresolved_evidence)
            return

        linked = self.table_work.get(target_id)
        if linked is not None and linked.connection_id is not None:
            target_node = self._object_node(target_id)
            self._add_edge(
                self._object_node(work.object_id).node_id,
                target_node.node_id,
                _dependency_relation(operation),
                operation,
                evidence_ids,
            )
            linked_connection = self.connections[linked.connection_id]
            if (
                linked.source_table_name is None
                or linked.datasource_id is None
                or linked_connection.resolution_status != ResolutionStatus.RESOLVED
            ):
                unresolved_evidence = self._add_unresolved(
                    artifact,
                    work.object_id,
                    reference=reference.name,
                    reason="linked_table_external_target_unavailable",
                    connection_id=linked.connection_id,
                    location=ConnectionProvenance.TABLEDEF_CONNECT.value,
                    operation=operation,
                    supporting_evidence_ids=(*evidence_ids, *linked.evidence_ids),
                )
                self._extend_source_evidence(work.object_id, unresolved_evidence)
            else:
                inherited = _QueryWork(
                    object_id=work.object_id,
                    artifact_id=work.artifact_id,
                    sql=work.sql,
                    query_kind=work.query_kind,
                    connection_id=linked.connection_id,
                    datasource_id=linked.datasource_id,
                    connection_provenance=ConnectionProvenance.TABLEDEF_CONNECT,
                    connection_evidence_ids=linked.evidence_ids,
                )
                self._add_external_interaction(
                    inherited,
                    linked.source_table_name,
                    operation,
                    (*evidence_ids, *linked.evidence_ids),
                )
            return

        interaction = DatasourceInteraction(
            source_object_id=work.object_id,
            operation=operation,
            scope=InteractionScope.LOCAL,
            local_target_object_id=target_id,
            evidence_ids=evidence_ids,
        )
        self._add_interaction(interaction)
        self._add_edge(
            self._object_node(work.object_id).node_id,
            self._object_node(target_id).node_id,
            _dependency_relation(operation),
            operation,
            evidence_ids,
        )

    def _add_external_interaction(
        self,
        work: _QueryWork,
        reference: str,
        operation: DataOperation,
        evidence_ids: tuple[str, ...],
    ) -> None:
        if work.connection_id is None or work.datasource_id is None:
            raise ValueError("external interaction requires connection evidence and a datasource")
        self._add_external_interaction_values(
            source_object_id=work.object_id,
            connection_id=work.connection_id,
            datasource_id=work.datasource_id,
            reference=reference,
            operation=operation,
            evidence_ids=evidence_ids,
        )

    def _add_external_interaction_values(
        self,
        *,
        source_object_id: str,
        connection_id: str,
        datasource_id: str,
        reference: str,
        operation: DataOperation,
        evidence_ids: tuple[str, ...],
    ) -> None:
        target_node, parts = self._external_node(datasource_id, reference)
        datasource = self.datasources[datasource_id]
        interaction = DatasourceInteraction(
            source_object_id=source_object_id,
            operation=operation,
            scope=InteractionScope.EXTERNAL,
            connection_id=connection_id,
            datasource_id=datasource_id,
            catalog=parts[0],
            database=parts[1] or datasource.database,
            schema_name=parts[2],
            object_name=parts[3],
            evidence_ids=evidence_ids,
        )
        self._add_interaction(interaction)
        self._add_edge(
            self._object_node(source_object_id).node_id,
            target_node.node_id,
            _dependency_relation(operation),
            operation,
            evidence_ids,
        )

    def _external_node(
        self, datasource_id: str, reference: str
    ) -> tuple[DependencyNode, tuple[str | None, str | None, str | None, str]]:
        datasource = self.datasources[datasource_id]
        parts = _external_name_parts(reference)
        label = ".".join(
            item for item in (parts[0], parts[1] or datasource.database, parts[2], parts[3]) if item
        )
        node = DependencyNode(
            application_id=self.application.application_id,
            kind=DependencyNodeKind.DATABASE_OBJECT,
            label=label,
            datasource_id=datasource_id,
        )
        return self._store_node(node), parts

    def _add_unresolved(
        self,
        artifact: StagedArtifactRecord,
        object_id: str,
        *,
        reference: str,
        reason: str,
        connection_id: str | None,
        location: str,
        operation: DataOperation = DataOperation.UNKNOWN,
        supporting_evidence_ids: tuple[str, ...] = (),
    ) -> str:
        evidence = self._new_evidence(
            artifact,
            object_id=object_id,
            origin=EvidenceOrigin.UNRESOLVED,
            fact_type="unresolved_reference",
            location=location,
            observation="A static reference could not be resolved",
            confidence=Confidence.MEDIUM,
        )
        evidence_ids = (evidence.evidence_id, *supporting_evidence_ids)
        unresolved = UnresolvedReference(
            source_object_id=object_id,
            reference=reference,
            reason=reason,
            evidence_ids=evidence_ids,
        )
        existing = self.unresolved.get(unresolved.unresolved_id)
        if existing is None:
            self.unresolved[unresolved.unresolved_id] = unresolved
        else:
            self.unresolved[unresolved.unresolved_id] = UnresolvedReference(
                source_object_id=existing.source_object_id,
                reference=existing.reference,
                reason=existing.reason,
                evidence_ids=(*existing.evidence_ids, *evidence_ids),
                unresolved_id=existing.unresolved_id,
            )
        if connection_id is not None:
            self._add_interaction(
                DatasourceInteraction(
                    source_object_id=object_id,
                    operation=operation,
                    scope=InteractionScope.UNRESOLVED,
                    connection_id=connection_id,
                    object_name=reference,
                    evidence_ids=evidence_ids,
                    confidence=Confidence.MEDIUM,
                )
            )
        return evidence.evidence_id

    def _add_interaction(self, candidate: DatasourceInteraction) -> None:
        existing = self.interactions.get(candidate.interaction_id)
        if existing is None:
            self.interactions[candidate.interaction_id] = candidate
            return
        self.interactions[candidate.interaction_id] = DatasourceInteraction(
            source_object_id=existing.source_object_id,
            operation=existing.operation,
            scope=existing.scope,
            connection_id=existing.connection_id,
            datasource_id=existing.datasource_id,
            local_target_object_id=existing.local_target_object_id,
            catalog=existing.catalog,
            database=existing.database,
            schema_name=existing.schema_name,
            object_name=existing.object_name,
            evidence_ids=(*existing.evidence_ids, *candidate.evidence_ids),
            confidence=(
                Confidence.MEDIUM
                if Confidence.MEDIUM in {existing.confidence, candidate.confidence}
                else existing.confidence
            ),
            interaction_id=existing.interaction_id,
        )

    def _add_edge(
        self,
        source_node_id: str,
        target_node_id: str,
        relationship: DependencyRelation,
        operation: DataOperation,
        evidence_ids: tuple[str, ...],
    ) -> None:
        if source_node_id == target_node_id:
            return
        candidate = DependencyEdge(
            source_node_id=source_node_id,
            target_node_id=target_node_id,
            relationship=relationship,
            operation=operation,
            evidence_ids=evidence_ids,
        )
        existing = self.edges.get(candidate.edge_id)
        if existing is None:
            self.edges[candidate.edge_id] = candidate
            return
        self.edges[candidate.edge_id] = DependencyEdge(
            source_node_id=existing.source_node_id,
            target_node_id=existing.target_node_id,
            relationship=existing.relationship,
            operation=existing.operation,
            evidence_ids=(*existing.evidence_ids, *candidate.evidence_ids),
            confidence=existing.confidence,
            edge_id=existing.edge_id,
        )

    def _new_evidence(
        self,
        artifact: StagedArtifactRecord,
        *,
        object_id: str,
        fact_type: str,
        location: str,
        observation: str,
        origin: EvidenceOrigin = EvidenceOrigin.OBSERVED,
        confidence: Confidence = Confidence.HIGH,
    ) -> EvidenceRecord:
        candidate = EvidenceRecord(
            application_id=self.application.application_id,
            artifact_id=artifact.artifact_id,
            artifact_sha256=artifact.sha256,
            object_id=object_id,
            origin=origin,
            fact_type=fact_type,
            location=location,
            observation=observation,
            confidence=confidence,
        )
        self.evidence.setdefault(candidate.evidence_id, candidate)
        return candidate

    def _store_node(self, candidate: DependencyNode) -> DependencyNode:
        self.nodes.setdefault(candidate.node_id, candidate)
        return self.nodes[candidate.node_id]

    def _object_node(self, object_id: str) -> DependencyNode:
        return next(item for item in self.nodes.values() if item.object_id == object_id)

    def _datasource_node(self, datasource_id: str) -> DependencyNode:
        return next(
            item
            for item in self.nodes.values()
            if item.datasource_id == datasource_id
            and item.kind in {DependencyNodeKind.DATASOURCE, DependencyNodeKind.FILE}
        )

    def _extend_source_evidence(self, object_id: str, evidence_id: str) -> None:
        source = self.objects[object_id]
        self.objects[object_id] = AccessObjectEvidence(
            artifact_id=source.artifact_id,
            object_type=source.object_type,
            name=source.name,
            sanitized_definition=source.sanitized_definition,
            attributes=source.attributes,
            evidence_ids=(*source.evidence_ids, evidence_id),
            object_id=source.object_id,
        )
        query = self.queries.get(object_id)
        if query is not None:
            self.queries[object_id] = QueryEvidence(
                object_id=query.object_id,
                query_kind=query.query_kind,
                dao_type=query.dao_type,
                attributes=query.attributes,
                sanitized_sql=query.sanitized_sql,
                returns_records=query.returns_records,
                odbc_timeout_seconds=query.odbc_timeout_seconds,
                is_hidden=query.is_hidden,
                is_system=query.is_system,
                connect_metadata_status=query.connect_metadata_status,
                connection_kind=query.connection_kind,
                connection_id=query.connection_id,
                parameters=query.parameters,
                parameter_names=query.parameter_names,
                evidence_ids=(*query.evidence_ids, evidence_id),
            )

    def _optional_int(
        self,
        artifact_id: str,
        object_id: str,
        field_name: str,
        value: str,
        *,
        minimum: int | None = None,
    ) -> int | None:
        if not value.strip():
            return None
        try:
            parsed = int(value)
        except ValueError:
            self._warn(artifact_id, f"query_metadata_invalid:{object_id}:{field_name}")
            return None
        if minimum is not None and parsed < minimum:
            self._warn(artifact_id, f"query_metadata_invalid:{object_id}:{field_name}")
            return None
        return parsed

    def _optional_bool(
        self,
        artifact_id: str,
        object_id: str,
        field_name: str,
        value: str,
    ) -> bool | None:
        if not value.strip():
            return None
        normalized = value.strip().casefold()
        if normalized in {"true", "yes", "1", "-1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
        self._warn(artifact_id, f"query_metadata_invalid:{object_id}:{field_name}")
        return None

    def _parameters(
        self, artifact_id: str, object_id: str, value: str
    ) -> tuple[QueryParameterEvidence, ...]:
        if not value.strip():
            return ()
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            self._warn(artifact_id, f"query_metadata_invalid:{object_id}:parameters")
            return ()
        if not isinstance(decoded, list):
            self._warn(artifact_id, f"query_metadata_invalid:{object_id}:parameters")
            return ()
        parameters: list[QueryParameterEvidence] = []
        names: set[str] = set()
        ordinals: set[int] = set()
        for list_index, item in enumerate(decoded):
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                self._warn(artifact_id, f"query_metadata_invalid:{object_id}:parameters")
                continue
            name = item["name"].strip()
            if not name or name.casefold() in names:
                self._warn(artifact_id, f"query_metadata_invalid:{object_id}:parameters")
                continue
            ordinal = self._parameter_int(
                artifact_id,
                object_id,
                list_index,
                "ordinal",
                item.get("ordinal", list_index),
            )
            if ordinal is None or ordinal < 0 or ordinal in ordinals:
                self._warn(artifact_id, f"query_metadata_invalid:{object_id}:parameters")
                continue
            names.add(name.casefold())
            ordinals.add(ordinal)
            parameters.append(
                QueryParameterEvidence(
                    ordinal=ordinal,
                    name=name,
                    dao_type=self._parameter_int(
                        artifact_id,
                        object_id,
                        list_index,
                        "type",
                        item.get("type"),
                    ),
                    direction=self._parameter_int(
                        artifact_id,
                        object_id,
                        list_index,
                        "direction",
                        item.get("direction"),
                    ),
                )
            )
        return tuple(parameters)

    def _parameter_int(
        self,
        artifact_id: str,
        object_id: str,
        ordinal: int,
        field_name: str,
        value: object,
    ) -> int | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        try:
            return int(str(value))
        except (TypeError, ValueError):
            self._warn(
                artifact_id,
                f"query_metadata_invalid:{object_id}:parameters[{ordinal}].{field_name}",
            )
            return None

    def _warn(self, artifact_id: str, warning: str) -> None:
        self.artifact_warnings[artifact_id].append(warning)
        if self.artifact_status[artifact_id] != ExtractionStatus.FAILED:
            self.artifact_status[artifact_id] = ExtractionStatus.PARTIAL

    def _staged_artifact(self, artifact_id: str) -> StagedArtifactRecord:
        return next(item for item in self.artifacts if item.artifact_id == artifact_id)

    def _finalize(
        self, contexts: tuple[_ArtifactContext, ...]
    ) -> ApplicationEvidenceBundle:
        artifact_evidence = tuple(
            ArtifactEvidence(
                application_id=self.application.application_id,
                source_locator=context.staged.source_locator,
                staged_relative_path=context.staged.staged_relative_path,
                filename=context.staged.filename,
                access_format=context.staged.access_format,
                sha256=context.staged.sha256,
                size_bytes=context.staged.size_bytes,
                extractor_version=(
                    context.snapshot.extractor_version
                    if context.snapshot is not None
                    else "not_extracted"
                ),
                approved_libraries=(
                    context.snapshot.approved_libraries
                    if context.snapshot is not None
                    else ()
                ),
                injected_libraries=(
                    context.snapshot.injected_libraries
                    if context.snapshot is not None
                    else ()
                ),
                library_injection_status=(
                    context.snapshot.library_injection_status
                    if context.snapshot is not None
                    else LibraryInjectionStatus.NOT_CONFIGURED
                ),
                extraction_status=self.artifact_status[context.staged.artifact_id],
                extracted_at=self.generated_at,
                warnings=tuple(self.artifact_warnings[context.staged.artifact_id]),
                artifact_id=context.staged.artifact_id,
            )
            for context in contexts
        )
        status_counts = {
            status: sum(item.extraction_status == status for item in artifact_evidence)
            for status in ExtractionStatus
        }
        owner_claims_by_id = {
            item.claim_id: item for item in self.application.owner_claims
        }
        for description in self.application.inventory_descriptions:
            claim = OwnerClaim(
                application_id=self.application.application_id,
                field="description",
                value=description,
                source="inventory workbook",
            )
            owner_claims_by_id[claim.claim_id] = claim
        owner_claims = tuple(
            owner_claims_by_id[key] for key in sorted(owner_claims_by_id)
        )
        coverage = ExtractionCoverage(
            primary_artifact_count=len(artifact_evidence),
            complete_artifact_count=status_counts[ExtractionStatus.COMPLETE],
            partial_artifact_count=status_counts[ExtractionStatus.PARTIAL],
            failed_artifact_count=status_counts[ExtractionStatus.FAILED],
            discovered_object_count=self.discovered_object_count,
            extracted_object_count=len(self.objects),
            warning_count=sum(len(set(items)) for items in self.artifact_warnings.values()),
            unresolved_reference_count=len(self.unresolved),
            tabledef_enumerated_count=sum(
                context.snapshot.tabledef_enumerated_count
                for context in contexts
                if context.snapshot is not None
            ),
            tabledef_succeeded_count=sum(
                context.snapshot.tabledef_succeeded_count
                for context in contexts
                if context.snapshot is not None
            ),
            tabledef_failed_count=sum(
                context.snapshot.tabledef_failed_count
                for context in contexts
                if context.snapshot is not None
            ),
            querydef_enumerated_count=sum(
                context.snapshot.querydef_enumerated_count
                for context in contexts
                if context.snapshot is not None
            ),
            querydef_succeeded_count=sum(
                context.snapshot.querydef_succeeded_count
                for context in contexts
                if context.snapshot is not None
            ),
            querydef_failed_count=sum(
                context.snapshot.querydef_failed_count
                for context in contexts
                if context.snapshot is not None
            ),
        )
        return ApplicationEvidenceBundle(
            application_id=self.application.application_id,
            application_name=self.application.application_name,
            inventory_record_ids=(self.application.application_id,),
            generated_at=self.generated_at,
            artifacts=artifact_evidence,
            objects=tuple(self.objects.values()),
            connections=tuple(self.connections.values()),
            tables=tuple(self.tables.values()),
            queries=tuple(self.queries.values()),
            datasources=tuple(self.datasources.values()),
            interactions=tuple(self.interactions.values()),
            dependency_nodes=tuple(self.nodes.values()),
            dependency_edges=tuple(self.edges.values()),
            evidence=tuple(self.evidence.values()),
            owner_claims=owner_claims,
            unresolved_references=tuple(self.unresolved.values()),
            coverage=coverage,
        )


def _safe_definition(value: str | None) -> str | None:
    if value is None:
        return None

    def replace_connection(match: re.Match[str]) -> str:
        literal = _decode_vba_string(match.group("value"))
        if _looks_like_connection_literal(literal):
            return '"<connection metadata redacted>"'
        return match.group(0)

    return sanitize_text(_VBA_STRING.sub(replace_connection, value))


def _root_ui_properties(definition: str) -> dict[str, str]:
    output: dict[str, str] = {}
    depth = 0
    active: str | None = None
    for raw_line in definition.splitlines():
        line = raw_line.strip()
        if _CODE_BEHIND_MARKER.fullmatch(line):
            break
        if re.fullmatch(r"Begin(?:\s+\w+)?", line, re.I):
            depth += 1
            active = None
            continue
        if line.casefold() == "end":
            depth -= 1
            active = None
            continue
        if depth > 1:
            continue
        assignment = re.fullmatch(r"(?P<key>\w+)\s*=\s*(?P<value>.*)", line)
        if assignment:
            key = assignment.group("key").casefold()
            if key == "recordsource" or key.startswith("on"):
                active = key
                output[key] = _unquote_access(assignment.group("value"))
            else:
                active = None
        elif active and line.startswith('"'):
            output[active] += _unquote_access(line)
        else:
            active = None
    return output


def _macro_actions(definition: str) -> tuple[tuple[str, dict[str, str]], ...]:
    assignments: list[tuple[str, str]] = []
    for raw_line in definition.splitlines():
        match = re.match(r"^\s*(?P<key>\w+)\s*=\s*(?P<value>.*?)\s*$", raw_line)
        if not match:
            continue
        assignments.append(
            (
                match.group("key").casefold(),
                _unquote_access(match.group("value")),
            )
        )
    output: list[tuple[str, dict[str, str]]] = []
    for index, (key, action) in enumerate(assignments):
        if key != "action" or not action:
            continue
        properties: dict[str, str] = {}
        for property_name, value in assignments[index + 1 :]:
            if property_name == "action":
                break
            properties.setdefault(property_name, value)
        output.append((action, properties))
    return tuple(output)


def _first_property(properties: dict[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = properties.get(key, "").strip()
        if value:
            return value.strip("[]")
    return None


def _macro_file_operation(
    action: str, properties: dict[str, str]
) -> DataOperation:
    context = " ".join((action, *properties.values())).casefold()
    if "acimport" in context or action.startswith("import"):
        return DataOperation.READ
    if "acexport" in context or action.startswith("export") or action == "outputto":
        return DataOperation.CREATE
    return DataOperation.UNKNOWN


def _vba_code(work: _ObjectWork) -> str:
    definition = work.definition
    if work.object_type in {AccessObjectType.FORM, AccessObjectType.REPORT}:
        marker = _CODE_BEHIND_MARKER.search(definition)
        if marker:
            definition = definition[marker.end() :]
    uncommented = "\n".join(_without_vba_comment(line) for line in definition.splitlines())
    return re.sub(r"\s+_\s*\n\s*", " ", uncommented)


def _without_vba_comment(line: str) -> str:
    in_string = False
    index = 0
    while index < len(line):
        character = line[index]
        if character == '"':
            if in_string and index + 1 < len(line) and line[index + 1] == '"':
                index += 2
                continue
            in_string = not in_string
        elif character == "'" and not in_string:
            return line[:index]
        index += 1
    if re.match(r"^\s*Rem(?:\s|$)", line, re.I):
        return ""
    return line


def _decode_vba_string(value: str) -> str:
    return value.replace('""', '"').strip()


def _looks_like_connection_literal(value: str) -> bool:
    return bool(_CONNECTION_LITERAL.search(value.strip()))


def _vba_connection_expression_is_dynamic(code: str, match: re.Match[str]) -> bool:
    statement_start, statement_end = _vba_statement_bounds(code, match.start())
    statement = code[statement_start:statement_end]
    in_string = False
    index = 0
    while index < len(statement):
        character = statement[index]
        if character == '"':
            if in_string and index + 1 < len(statement) and statement[index + 1] == '"':
                index += 2
                continue
            in_string = not in_string
        elif not in_string and character in {"&", "+"}:
            return True
        index += 1
    return False


def _vba_statement_bounds(code: str, offset: int) -> tuple[int, int]:
    """Return the colon/newline-delimited VBA statement containing ``offset``."""

    line_start = code.rfind("\n", 0, offset) + 1
    line_end = code.find("\n", offset)
    if line_end < 0:
        line_end = len(code)
    statement_start = line_start
    in_string = False
    index = line_start
    while index < line_end:
        character = code[index]
        if character == '"':
            if in_string and index + 1 < line_end and code[index + 1] == '"':
                index += 2
                continue
            in_string = not in_string
        elif character == ":" and not in_string:
            if index < offset:
                statement_start = index + 1
            else:
                return statement_start, index
        index += 1
    return statement_start, line_end


def _connection_receiver(code: str, literal_start: int) -> str | None:
    line_start = code.rfind("\n", 0, literal_start) + 1
    prefix = code[line_start:literal_start]
    match = re.search(
        r"(?i)\b(?P<variable>[A-Za-z_]\w*)\."
        r"(?:ConnectionString\s*=\s*|Open\s*(?:\(\s*)?)$",
        prefix,
    )
    return match.group("variable") if match else None


def _connection_statement_receiver(code: str, start: int, end: int) -> str | None:
    match = re.search(
        r"(?i)\b(?P<variable>[A-Za-z_]\w*)\."
        r"(?:ConnectionString[ \t]*=|Open\b)",
        code[start:end],
    )
    return match.group("variable") if match else None


def _database_variable_connections(
    code: str,
    file_connections: dict[str, tuple[str, str]],
) -> dict[str, str]:
    output: dict[str, str] = {}
    pattern = re.compile(
        r"(?im)\bSet\s+(?P<variable>[A-Za-z_]\w*)\s*=\s*[^\r\n]*?"
        r"OpenDatabase\s*\(\s*\"(?P<path>(?:\"\"|[^\"\r\n])+)\""
    )
    for match in pattern.finditer(code):
        normalized = normalize_source_identity(
            _decode_vba_string(match.group("path"))
        )
        connection = file_connections.get(normalized)
        if connection is not None:
            output[match.group("variable").casefold()] = connection[0]
    return output


def _local_database_variables(code: str) -> set[str]:
    return {
        match.group("variable").casefold()
        for match in re.finditer(
            r"(?im)\bSet\s+(?P<variable>[A-Za-z_]\w*)\s*=\s*CurrentDb(?:\(\))?\b",
            code,
        )
    }


def _active_connection_variables(code: str) -> dict[str, str]:
    return {
        match.group("command").casefold(): match.group("connection").casefold()
        for match in re.finditer(
            r"(?im)\b(?:Set\s+)?(?P<command>[A-Za-z_]\w*)\.ActiveConnection\s*=\s*"
            r"(?P<connection>[A-Za-z_]\w*)",
            code,
        )
    }


def _first_vba_literal_argument(value: str) -> str | None:
    candidate = value.strip()
    if candidate.startswith("("):
        candidate = candidate[1:].lstrip()
    named = re.match(r"(?i)^[A-Za-z_]\w*\s*:=\s*", candidate)
    if named:
        candidate = candidate[named.end() :].lstrip()
    match = _VBA_STRING.match(candidate)
    if match is None or candidate[match.end() :].lstrip().startswith("&"):
        return None
    return _decode_vba_string(match.group("value"))


def _quoted_vba_value(value: str) -> str | None:
    candidate = value.strip()
    match = _VBA_STRING.match(candidate)
    if match is None or candidate[match.end() :].lstrip().startswith("&"):
        return None
    return _decode_vba_string(match.group("value"))


def _vba_arguments(value: str) -> list[str]:
    candidate = value.strip()
    if candidate.startswith("("):
        candidate = candidate[1:]
    output: list[str] = []
    start = 0
    depth = 0
    in_string = False
    index = 0
    while index < len(candidate):
        character = candidate[index]
        if character == '"':
            if in_string and index + 1 < len(candidate) and candidate[index + 1] == '"':
                index += 2
                continue
            in_string = not in_string
        elif not in_string:
            if character == "(":
                depth += 1
            elif character == ")" and depth:
                depth -= 1
            elif character == "," and depth == 0:
                output.append(candidate[start:index].strip())
                start = index + 1
        index += 1
    output.append(candidate[start:].strip().removesuffix(")").strip())
    return output


def _looks_like_file_reference(value: str) -> bool:
    candidate = value.strip()
    if not _WINDOWS_FILE_LITERAL.match(candidate):
        return False
    suffix = PureWindowsPath(candidate).suffix.casefold()
    return bool(suffix) or candidate.startswith("\\\\")


def _vba_file_operation(statement: str, literal_offset: int) -> DataOperation:
    normalized = statement.casefold()
    if "filecopy" in normalized:
        return (
            DataOperation.READ
            if statement[:literal_offset].count(",") == 0
            else DataOperation.CREATE
        )
    if any(marker in normalized for marker in ("acimport", " for input", "dir(")):
        return DataOperation.READ
    if any(
        marker in normalized
        for marker in ("acexport", " for output", " for append", "outputto")
    ):
        return DataOperation.CREATE
    return DataOperation.UNKNOWN


def _file_platform(value: str) -> str:
    suffix = PureWindowsPath(value).suffix.casefold()
    if suffix in {".xls", ".xlsx", ".xlsm", ".xlsb"}:
        return "excel"
    if suffix in {".accdb", ".mdb"}:
        return "access"
    if suffix in {".csv", ".tsv", ".txt", ".text"}:
        return "flat_file"
    return "filesystem"


def _unquote_access(value: str) -> str:
    candidate = value.strip()
    if len(candidate) >= 2 and candidate.startswith('"') and candidate.endswith('"'):
        return candidate[1:-1].replace('""', '"')
    return candidate


def _query_kind(value: str, dao_type: int | None) -> QueryKind:
    normalized = value.strip().casefold().replace("-", "_").replace(" ", "_")
    if dao_type is not None and dao_type in _DAO_QUERY_KINDS:
        return _DAO_QUERY_KINDS[dao_type]
    return _QUERY_KINDS.get(normalized, QueryKind.UNKNOWN)


def _property_bool(properties: dict[str, str], key: str) -> bool:
    return properties.get(key, "").strip().casefold() in {"true", "yes", "1", "-1"}


def _data_operation(value: str) -> DataOperation:
    return {
        "READ": DataOperation.READ,
        "INSERT": DataOperation.INSERT,
        "UPDATE": DataOperation.UPDATE,
        "DELETE": DataOperation.DELETE,
        "CREATE": DataOperation.CREATE,
        "EXECUTE": DataOperation.EXECUTE,
    }.get(value.upper(), DataOperation.UNKNOWN)


def _dependency_relation(operation: DataOperation) -> DependencyRelation:
    if operation == DataOperation.READ:
        return DependencyRelation.READS
    if operation == DataOperation.CREATE:
        return DependencyRelation.PRODUCES
    if operation == DataOperation.EXECUTE:
        return DependencyRelation.CALLS
    if operation in {DataOperation.INSERT, DataOperation.UPDATE, DataOperation.DELETE}:
        return DependencyRelation.WRITES
    return DependencyRelation.REFERENCES


def _external_name_parts(
    value: str,
) -> tuple[str | None, str | None, str | None, str]:
    parts = [item.strip() for item in value.split(".") if item.strip()]
    if not parts:
        return None, None, None, "<unknown object>"
    if len(parts) == 1:
        return None, None, None, parts[0]
    if len(parts) == 2:
        return None, None, parts[0], parts[1]
    if len(parts) == 3:
        return None, parts[0], parts[1], parts[2]
    return ".".join(parts[:-3]), parts[-3], parts[-2], parts[-1]


def _datasource_label(datasource: DatasourceIdentity) -> str:
    identity = datasource.resource or datasource.server or datasource.dsn or "unresolved"
    database = f"/{datasource.database}" if datasource.database else ""
    return f"{datasource.platform}:{identity}{database}"
