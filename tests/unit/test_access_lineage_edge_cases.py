from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from portfolio_analyzer.access.windows_extractor import WindowsAccessExtractor
from portfolio_analyzer.analysis.bundle import build_application_evidence_bundles
from portfolio_analyzer.config import AnalyzerSettings
from portfolio_analyzer.v2.models import (
    AccessObjectType,
    ApplicationEvidenceBundle,
    ConnectionKind,
    ConnectionProvenance,
    InteractionScope,
    ResolutionStatus,
)
from portfolio_analyzer.v2.workflow import (
    ExtractedArtifactSnapshot,
    ExtractedObjectSnapshot,
    StagedApplication,
    StagedArtifactRecord,
    StageIndex,
)


class _UnknownConnectTable:
    Name = "UnknownConnection"
    SourceTableName = "dbo.Unknown"
    Attributes = 0

    def __init__(self) -> None:
        self.field_reads = 0

    @property
    def Connect(self) -> str:  # noqa: N802 - mirrors COM
        raise RuntimeError("Connect unavailable")

    @property
    def Fields(self) -> list[object]:  # noqa: N802 - mirrors COM
        self.field_reads += 1
        raise AssertionError("Fields are unsafe when TableDef link status is unknown")


class _UnknownConnectQuery:
    Name = "qryUnknownConnection"
    SQL = "SELECT * FROM LocalOrders"
    Type = 0
    ReturnsRecords = True
    ODBCTimeout = 60
    MaxRecords = 0
    Attributes = 0
    Parameters = SimpleNamespace(Count=0)

    @property
    def Connect(self) -> str:  # noqa: N802 - mirrors COM
        raise RuntimeError("Connect unavailable")


def test_unavailable_tabledef_connect_stays_unknown_through_canonical_lineage(
    tmp_path: Path,
) -> None:
    extractor = WindowsAccessExtractor(AnalyzerSettings(workspace=tmp_path / "workspace"))
    table = _UnknownConnectTable()
    errors: list[str] = []

    extracted_table = extractor._table_object(table, 0, errors, None)

    assert extracted_table.object_type == "table_link_status_unknown"
    assert extracted_table.properties["connect_metadata_status"] == "unavailable"
    assert table.field_reads == 0
    assert any("TableDef[0].Connect unavailable" in error for error in errors)

    bundle = _bundle(
        (
            ExtractedObjectSnapshot(
                object_type=extracted_table.object_type,
                name=extracted_table.name,
                sanitized_properties=extracted_table.properties,
            ),
            ExtractedObjectSnapshot(
                object_type="query",
                name="qryUnknown",
                sanitized_definition="SELECT * FROM UnknownConnection",
                sanitized_properties={
                    "query_kind": "select",
                    "dao_type_code": "0",
                    "connect": "",
                    "returns_records": "True",
                    "odbc_timeout": "60",
                    "parameters": "[]",
                    "attributes": "0",
                    "hidden": "false",
                    "system": "false",
                },
            ),
        ),
        warnings=tuple(errors),
    )

    table_object = next(
        item
        for item in bundle.objects
        if item.object_type == AccessObjectType.TABLE_LINK_STATUS_UNKNOWN
    )
    table_evidence = next(
        item for item in bundle.tables if item.object_id == table_object.object_id
    )
    assert table_evidence.is_linked is None
    assert table_evidence.connect_metadata_status == "unavailable"
    assert table_evidence.source_table_name == "dbo.Unknown"
    assert not any(
        item.scope == InteractionScope.LOCAL
        and item.local_target_object_id == table_object.object_id
        for item in bundle.interactions
    )
    assert any(
        item.reason == "tabledef_connect_metadata_unavailable"
        for item in bundle.unresolved_references
    )


def test_mixed_literal_and_dynamic_vba_connection_is_not_treated_as_static() -> None:
    module = ExtractedObjectSnapshot(
        object_type="module",
        name="DynamicConnection",
        sanitized_definition=(
            "Public Sub Run()\n"
            "Dim cn As ADODB.Connection\n"
            "Set cn = New ADODB.Connection\n"
            'cn.ConnectionString = "Provider=SQLOLEDB;Data Source=" & ServerName & '
            '";Initial Catalog=Operations;"\n'
            "cn.Open\n"
            'cn.Execute "UPDATE dbo.RemoteOrders SET Status = 1"\n'
            "End Sub"
        ),
    )

    bundle = _bundle((module,))

    assert len(bundle.connections) == 1
    connection = bundle.connections[0]
    assert connection.direct_provenance == ConnectionProvenance.UNRESOLVED_DYNAMIC
    assert connection.resolution_status == ResolutionStatus.UNRESOLVED
    assert connection.datasource_id is None
    assert bundle.datasources == ()
    assert any(
        item.reason == "vba_connection_constructed_dynamically"
        for item in bundle.unresolved_references
    )


def test_indirect_dynamic_vba_connection_does_not_create_partial_static_datasource() -> None:
    module = ExtractedObjectSnapshot(
        object_type="module",
        name="IndirectDynamicConnection",
        sanitized_definition=(
            "Public Sub Run()\n"
            "Dim cs As String\n"
            'cs = "Provider=SQLOLEDB;Data Source="\n'
            "cs = cs & ServerName\n"
            "cn.ConnectionString = cs\n"
            "cn.Open\n"
            "End Sub"
        ),
    )

    bundle = _bundle((module,))

    assert len(bundle.connections) == 1
    connection = bundle.connections[0]
    assert connection.direct_provenance == ConnectionProvenance.UNRESOLVED_DYNAMIC
    assert connection.resolution_status == ResolutionStatus.UNRESOLVED
    assert connection.datasource_id is None
    assert bundle.datasources == ()


def test_unavailable_querydef_connect_never_becomes_local_lineage(tmp_path: Path) -> None:
    extractor = WindowsAccessExtractor(AnalyzerSettings(workspace=tmp_path / "workspace"))
    errors: list[str] = []

    extracted_query = extractor._query_object(_UnknownConnectQuery(), 0, errors, None)

    assert extracted_query.properties["connect_metadata_status"] == "unavailable"
    assert any("QueryDef[0].Connect unavailable" in error for error in errors)

    bundle = _bundle(
        (
            ExtractedObjectSnapshot(
                object_type="table",
                name="LocalOrders",
                sanitized_properties={
                    "connect": "",
                    "connect_metadata_status": "available",
                    "source_table_name": "",
                    "attributes": "0",
                    "hidden": "false",
                    "system": "false",
                    "fields": "[]",
                },
            ),
            ExtractedObjectSnapshot(
                object_type=extracted_query.object_type,
                name=extracted_query.name,
                sanitized_definition=extracted_query.definition,
                sanitized_properties=extracted_query.properties,
            ),
        ),
        warnings=tuple(errors),
    )

    query = bundle.queries[0]
    connection = bundle.connections[0]
    assert query.connect_metadata_status == "unavailable"
    assert query.connection_kind == ConnectionKind.UNKNOWN
    assert connection.resolution_status == ResolutionStatus.UNRESOLVED
    assert connection.datasource_id is None
    assert not any(item.scope == InteractionScope.LOCAL for item in bundle.interactions)
    assert any(
        item.reason == "querydef_connect_metadata_unavailable"
        for item in bundle.unresolved_references
    )


def _bundle(
    objects: tuple[ExtractedObjectSnapshot, ...],
    *,
    warnings: tuple[str, ...] = (),
) -> ApplicationEvidenceBundle:
    artifact = StagedArtifactRecord(
        application_id="app-edge",
        application_name="Edge Cases",
        source_locator=r"C:\Inventory\Edge.accdb",
        staged_relative_path="staged_tools/edge/Edge.accdb",
        filename="Edge.accdb",
        access_format="accdb",
        size_bytes=123,
        sha256="a" * 64,
    )
    stage = StageIndex(
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        inventory_sha256="b" * 64,
        applications=(
            StagedApplication(
                application_id=artifact.application_id,
                application_name=artifact.application_name,
                artifact_ids=(artifact.artifact_id,),
            ),
        ),
        artifacts=(artifact,),
    )
    snapshot = ExtractedArtifactSnapshot(
        application_id=artifact.application_id,
        artifact_id=artifact.artifact_id,
        artifact_sha256=artifact.sha256,
        extractor_version="test",
        coverage_status="partial" if warnings else "complete",
        warnings=warnings,
        objects=objects,
    )
    return build_application_evidence_bundles(stage, (snapshot,))[0]
