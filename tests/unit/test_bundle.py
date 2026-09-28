from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, cast

from portfolio_analyzer.analysis.bundle import build_application_evidence_bundles
from portfolio_analyzer.library_identity import (
    ApprovedLibraryReference,
    LibraryInjectionStatus,
)
from portfolio_analyzer.parsing.dsn import (
    DsnResolution,
    RegistryBitness,
    RegistryDsnRecord,
    RegistryScope,
    resolve_windows_dsn,
)
from portfolio_analyzer.v2.identity import canonical_json_bytes
from portfolio_analyzer.v2.models import (
    AccessObjectType,
    ConnectionKind,
    ConnectionProvenance,
    DataOperation,
    DependencyRelation,
    ExtractionStatus,
    InteractionScope,
    QueryKind,
    QueryParameterEvidence,
    ResolutionStatus,
)
from portfolio_analyzer.v2.workflow import (
    ExtractedArtifactSnapshot,
    ExtractedObjectSnapshot,
    StagedApplication,
    StagedArtifactRecord,
    StageIndex,
)

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
INVENTORY_DIGEST = "f" * 64


def test_library_configuration_and_actual_injection_reach_canonical_evidence() -> None:
    artifact = _artifact("app-1", "Main.accdb", "a" * 64)
    library = ApprovedLibraryReference(filename="reviewed.accdb", sha256="b" * 64)
    snapshot = ExtractedArtifactSnapshot(
        application_id=artifact.application_id,
        artifact_id=artifact.artifact_id,
        artifact_sha256=artifact.sha256,
        extractor_version="fixture-static-v2",
        approved_libraries=(library,),
        injected_libraries=(library,),
        library_injection_status=LibraryInjectionStatus.INJECTED,
        coverage_status="complete",
    )

    bundle = build_application_evidence_bundles(
        _stage("app-1", "Application", (artifact,)),
        (snapshot,),
    )[0]

    assert bundle.artifacts[0].approved_libraries == (library,)
    assert bundle.artifacts[0].injected_libraries == (library,)
    assert (
        bundle.artifacts[0].library_injection_status
        == LibraryInjectionStatus.INJECTED
    )


def test_local_references_are_artifact_scoped_across_multiple_primaries() -> None:
    first = _artifact("app-1", "First.accdb", "a" * 64)
    second = _artifact("app-1", "Second.accdb", "b" * 64)
    stage = _stage("app-1", "Portfolio App", (first, second), ("Owner supplied purpose",))
    snapshots = (
        _snapshot(
            second,
            (
                _table("Shared"),
                _query("qryUpdate", "UPDATE Shared SET Amount = 1", kind="update", dao=48),
            ),
        ),
        _snapshot(
            first,
            (
                _table("Shared"),
                _query("qryRead", "SELECT * FROM Shared", kind="select", dao=0),
            ),
        ),
    )

    bundle = build_application_evidence_bundles(stage, snapshots)[0]
    reversed_bundle = build_application_evidence_bundles(stage, reversed(snapshots))[0]

    assert canonical_json_bytes(bundle) == canonical_json_bytes(reversed_bundle)
    assert len(bundle.artifacts) == 2
    assert bundle.coverage.complete_artifact_count == 2
    assert bundle.coverage.partial_artifact_count == 0
    assert bundle.connections == ()
    assert bundle.datasources == ()
    assert bundle.owner_claims[0].value == "Owner supplied purpose"

    objects = {item.object_id: item for item in bundle.objects}
    interactions = {item.operation: item for item in bundle.interactions}
    assert set(interactions) == {DataOperation.READ, DataOperation.UPDATE}
    for interaction in interactions.values():
        assert interaction.scope == InteractionScope.LOCAL
        assert interaction.local_target_object_id is not None
        assert (
            objects[interaction.source_object_id].artifact_id
            == objects[interaction.local_target_object_id].artifact_id
        )
    assert {item.relationship for item in bundle.dependency_edges} == {
        DependencyRelation.READS,
        DependencyRelation.WRITES,
    }


def test_dao_collection_coverage_is_aggregated_without_losing_failures() -> None:
    first = _artifact("app-coverage", "First.accdb", "1" * 64)
    second = _artifact("app-coverage", "Second.accdb", "2" * 64)
    stage = _stage("app-coverage", "Coverage App", (first, second))
    snapshots = (
        _snapshot(
            first,
            (_table("LocalOne"),),
            coverage_status="partial",
            warnings=("one TableDef failed",),
            tabledef_counts=(2, 1, 1),
            querydef_counts=(3, 2, 1),
        ),
        _snapshot(
            second,
            (_table("LocalTwo"),),
            tabledef_counts=(4, 4, 0),
            querydef_counts=(5, 5, 0),
        ),
    )

    bundle = build_application_evidence_bundles(stage, snapshots)[0]

    assert bundle.coverage.tabledef_enumerated_count == 6
    assert bundle.coverage.tabledef_succeeded_count == 5
    assert bundle.coverage.tabledef_failed_count == 1
    assert bundle.coverage.querydef_enumerated_count == 8
    assert bundle.coverage.querydef_succeeded_count == 7
    assert bundle.coverage.querydef_failed_count == 1
    assert bundle.coverage.partial_artifact_count == 1


def test_querydef_metadata_retains_attributes_flags_and_ordered_typed_parameters() -> None:
    artifact = _artifact("app-query-metadata", "Metadata.accdb", "3" * 64)
    stage = _stage("app-query-metadata", "Metadata App", (artifact,))
    snapshot = _snapshot(
        artifact,
        (
            _query(
                "~sq_Internal",
                "PARAMETERS pAccount Text, pAsOf DateTime; SELECT 1",
                kind="select",
                dao=0,
                attributes="-2147483645",
                hidden="true",
                system="true",
                parameters=(
                    '[{"ordinal":"0","name":"pAccount","type":"10",'
                    '"direction":"0"},{"ordinal":"1","name":"pAsOf",'
                    '"type":"8","direction":"1"}]'
                ),
            ),
        ),
        querydef_counts=(1, 1, 0),
    )

    bundle = build_application_evidence_bundles(stage, (snapshot,))[0]
    query = bundle.queries[0]

    assert query.attributes == -2147483645
    assert query.is_hidden is True
    assert query.is_system is True
    assert query.parameters == (
        QueryParameterEvidence(
            ordinal=0,
            name="pAccount",
            dao_type=10,
            direction=0,
        ),
        QueryParameterEvidence(
            ordinal=1,
            name="pAsOf",
            dao_type=8,
            direction=1,
        ),
    )
    assert query.parameter_names == ("pAccount", "pAsOf")
    object_attributes = {
        item.name: item.value for item in bundle.objects[0].attributes
    }
    assert object_attributes["attributes"] == "-2147483645"
    assert object_attributes["hidden"] == "true"
    assert object_attributes["system"] == "true"


def test_local_query_to_query_reference_is_not_promoted_to_a_datasource() -> None:
    artifact = _artifact("app-query-chain", "Chain.accdb", "8" * 64)
    stage = _stage("app-query-chain", "Query Chain", (artifact,))
    snapshot = _snapshot(
        artifact,
        (
            _table("Orders"),
            _query("qryBase", "SELECT * FROM Orders", kind="select", dao=0),
            _query("qryTop", "SELECT * FROM qryBase", kind="select", dao=0),
        ),
    )

    bundle = build_application_evidence_bundles(stage, (snapshot,))[0]
    object_ids = {item.name: item.object_id for item in bundle.objects}
    interaction = next(
        item
        for item in bundle.interactions
        if item.source_object_id == object_ids["qryTop"]
    )

    assert interaction.scope == InteractionScope.LOCAL
    assert interaction.local_target_object_id == object_ids["qryBase"]
    assert interaction.connection_id is None
    assert interaction.datasource_id is None
    assert bundle.datasources == ()


def test_linked_table_connection_and_query_access_are_cited_without_secrets() -> None:
    artifact = _artifact("app-linked", "Linked.accdb", "c" * 64)
    stage = _stage("app-linked", "Linked App", (artifact,))
    connection = (
        "ODBC;Driver={ODBC Driver 18 for SQL Server};Server=SQL01;"
        "Database=Warehouse;UID=analyst;PWD=CONNECTION_SECRET;Custom=PRIVATE_CANARY"
    )
    snapshot = _snapshot(
        artifact,
        (
            ExtractedObjectSnapshot(
                object_type="linked_table",
                name="lnkCustomer",
                sanitized_properties={
                    "connect": connection,
                    "source_table_name": "dbo.Customer",
                    "attributes": "0",
                    "connect_metadata_status": "available",
                },
            ),
            _query("qryCustomer", "SELECT * FROM lnkCustomer", kind="select", dao=0),
        ),
    )

    bundle = build_application_evidence_bundles(stage, (snapshot,))[0]
    serialized = canonical_json_bytes(bundle).lower()

    assert len(bundle.connections) == 1
    observed_connection = bundle.connections[0]
    assert observed_connection.connection_kind == ConnectionKind.LINKED_TABLE
    assert observed_connection.direct_provenance == ConnectionProvenance.TABLEDEF_CONNECT
    assert observed_connection.resolution_status == ResolutionStatus.RESOLVED
    assert observed_connection.datasource_id == bundle.datasources[0].datasource_id
    assert bundle.datasources[0].platform == "sql_server"
    assert bundle.datasources[0].server == "sql01"
    assert bundle.datasources[0].database == "warehouse"
    assert bundle.tables[0].connection_id == observed_connection.connection_id

    interaction = bundle.interactions[0]
    assert interaction.scope == InteractionScope.EXTERNAL
    assert interaction.connection_id == observed_connection.connection_id
    assert interaction.operation == DataOperation.READ
    assert interaction.schema_name == "dbo"
    assert interaction.object_name == "Customer"
    assert any(
        edge.relationship == DependencyRelation.READS
        and edge.operation == DataOperation.READ
        for edge in bundle.dependency_edges
    )
    assert b"connection_secret" not in serialized
    assert b"private_canary" not in serialized
    assert b"analyst" not in serialized
    assert b"pwd=" not in serialized


def test_pass_through_query_keeps_metadata_and_injected_dsn_provenance() -> None:
    artifact = _artifact("app-pass", "Pass.accdb", "d" * 64)
    stage = _stage("app-pass", "Pass-through App", (artifact,))
    registry = _FakeRegistryReader(
        {
            ("finance", RegistryScope.USER, RegistryBitness.X64): RegistryDsnRecord(
                {
                    "Driver": "ODBC Driver 18 for SQL Server",
                    "Server": "SQL-PROD",
                    "Database": "FinanceMart",
                    "Password": "REGISTRY_SECRET",
                    "Unknown": "REGISTRY_PRIVATE",
                }
            ),
            ("finance", RegistryScope.SYSTEM, RegistryBitness.X64): RegistryDsnRecord(
                {
                    "Driver": "ODBC Driver 18 for SQL Server",
                    "Server": "SQL-SHADOWED",
                    "Database": "FinanceMart",
                }
            ),
        }
    )

    def resolve(value: str) -> DsnResolution:
        return resolve_windows_dsn(value, registry_reader=registry, target_bitness=64)

    snapshot = _snapshot(
        artifact,
        (
            _query(
                "qryDeals",
                "SELECT * FROM dbo.Deal",
                kind="pass_through",
                dao=112,
                connect="ODBC;DSN=Finance;UID=reader;PWD=QUERY_SECRET",
                returns_records="True",
                timeout="45",
                parameters='[{"name":"pAsOf","type":"8"}]',
            ),
        ),
    )

    bundle = build_application_evidence_bundles(
        stage, (snapshot,), dsn_resolver=resolve
    )[0]
    query = bundle.queries[0]
    connection = bundle.connections[0]
    interaction = bundle.interactions[0]
    serialized = canonical_json_bytes(bundle).lower()

    assert query.query_kind == QueryKind.PASS_THROUGH
    assert query.dao_type == 112
    assert query.returns_records is True
    assert query.odbc_timeout_seconds == 45
    assert query.attributes == 0
    assert query.is_hidden is False
    assert query.is_system is False
    assert query.parameters == (
        QueryParameterEvidence(
            ordinal=0,
            name="pAsOf",
            dao_type=8,
            direction=None,
        ),
    )
    assert query.parameter_names == ("pAsOf",)
    assert query.connection_id == connection.connection_id
    assert connection.resolution_status == ResolutionStatus.RESOLVED
    assert any("user:64:matched" in item for item in connection.resolution_provenance)
    assert any("system:64:matched" in item for item in connection.resolution_provenance)
    assert (
        "dsn_resolution_reason:registry_identity_resolved_user_precedence_shadowed_conflict"
        in connection.resolution_warnings
    )
    assert "dsn_registry_candidate_count:2" in connection.resolution_warnings
    assert interaction.scope == InteractionScope.EXTERNAL
    assert interaction.connection_id == connection.connection_id
    assert interaction.operation == DataOperation.READ
    assert interaction.database == "financemart"
    assert interaction.schema_name == "dbo"
    assert interaction.object_name == "Deal"
    assert registry.calls
    assert b"query_secret" not in serialized
    assert b"registry_secret" not in serialized
    assert b"registry_private" not in serialized
    assert b"reader" not in serialized


def test_dsnless_pass_through_exact_lineage_and_local_query_stays_local() -> None:
    artifact = _artifact("app-exact", "Exact.accdb", "7" * 64)
    stage = _stage("app-exact", "Exact App", (artifact,))
    snapshot = _snapshot(
        artifact,
        (
            _table("LocalTable"),
            _query(
                "qryDeal",
                "SELECT * FROM dbo.Deal",
                kind="pass_through",
                dao=112,
                connect=(
                    "ODBC;DRIVER={ODBC Driver 18 for SQL Server};SERVER=SQL01;"
                    "DATABASE=CorporateTrust;UID=reader;PWD=EXACT_SECRET"
                ),
                returns_records="True",
            ),
            _query(
                "qryLocal",
                "SELECT * FROM LocalTable",
                kind="select",
                dao=0,
            ),
        ),
    )

    bundle = build_application_evidence_bundles(stage, (snapshot,))[0]
    objects = {item.name: item.object_id for item in bundle.objects}
    queries = {item.object_id: item for item in bundle.queries}
    deal_query = queries[objects["qryDeal"]]
    local_query = queries[objects["qryLocal"]]
    deal_connection = next(
        item for item in bundle.connections if item.object_id == deal_query.object_id
    )
    datasource = next(
        item
        for item in bundle.datasources
        if item.datasource_id == deal_connection.datasource_id
    )
    deal_interaction = next(
        item for item in bundle.interactions if item.source_object_id == deal_query.object_id
    )
    local_interaction = next(
        item for item in bundle.interactions if item.source_object_id == local_query.object_id
    )

    assert deal_connection.direct_provenance == ConnectionProvenance.QUERYDEF_CONNECT
    assert datasource.platform == "sql_server"
    assert datasource.server == "sql01"
    assert datasource.database == "corporatetrust"
    assert deal_interaction.scope == InteractionScope.EXTERNAL
    assert deal_interaction.operation == DataOperation.READ
    assert deal_interaction.schema_name == "dbo"
    assert deal_interaction.object_name == "Deal"
    assert local_query.connection_kind == ConnectionKind.LOCAL_ACCESS
    assert local_query.connection_id is None
    assert local_interaction.scope == InteractionScope.LOCAL
    assert local_interaction.datasource_id is None
    assert local_interaction.connection_id is None
    serialized = canonical_json_bytes(bundle).lower()
    assert b"exact_secret" not in serialized
    assert b"pwd=" not in serialized


def test_missing_snapshot_and_unresolved_local_reference_have_explicit_coverage() -> None:
    extracted = _artifact("app-partial", "Extracted.accdb", "e" * 64)
    missing = _artifact("app-partial", "Missing.accdb", "1" * 64)
    stage = _stage("app-partial", "Partial App", (extracted, missing))
    snapshot = _snapshot(
        extracted,
        (_query("qryMissing", "SELECT * FROM DoesNotExist", kind="select", dao=0),),
        coverage_status="partial",
        warnings=("SaveAsText lane unavailable",),
    )

    bundle = build_application_evidence_bundles(stage, (snapshot,))[0]

    assert bundle.coverage.primary_artifact_count == 2
    assert bundle.coverage.complete_artifact_count == 0
    assert bundle.coverage.partial_artifact_count == 1
    assert bundle.coverage.failed_artifact_count == 1
    assert bundle.coverage.unresolved_reference_count == 1
    assert {item.extraction_status for item in bundle.artifacts} == {
        ExtractionStatus.PARTIAL,
        ExtractionStatus.FAILED,
    }
    assert bundle.unresolved_references[0].reference == "DoesNotExist"
    assert bundle.unresolved_references[0].reason == (
        "local_reference_not_found_in_source_artifact"
    )
    assert bundle.interactions == ()
    source_path = extracted.source_locator.casefold()
    assert all(source_path not in item.evidence_id.casefold() for item in bundle.evidence)


def test_plural_builder_returns_one_bundle_per_inventory_application() -> None:
    first = _artifact("app-a", "A.accdb", "2" * 64)
    second = _artifact("app-b", "B.mdb", "3" * 64)
    stage = StageIndex(
        generated_at=NOW,
        inventory_sha256=INVENTORY_DIGEST,
        applications=(
            StagedApplication(
                application_id="app-a",
                application_name="A",
                artifact_ids=(first.artifact_id,),
            ),
            StagedApplication(
                application_id="app-b",
                application_name="B",
                artifact_ids=(second.artifact_id,),
            ),
        ),
        artifacts=(first, second),
    )

    bundles = build_application_evidence_bundles(
        stage,
        (_snapshot(first, (_table("AData"),)), _snapshot(second, (_table("BData"),))),
    )

    assert [item.application_id for item in bundles] == ["app-a", "app-b"]
    assert all(len(item.artifacts) == 1 for item in bundles)
    assert all(item.objects[0].object_type == AccessObjectType.TABLE for item in bundles)


def test_form_report_record_sources_events_and_code_behind_create_cited_edges() -> None:
    artifact = _artifact("app-ui", "UI.accdb", "4" * 64)
    stage = _stage("app-ui", "UI App", (artifact,))
    snapshot = _snapshot(
        artifact,
        (
            _table("Orders"),
            _query("qryOrders", "SELECT * FROM Orders", kind="select", dao=0),
            ExtractedObjectSnapshot(
                object_type="report",
                name="rptOrders",
                sanitized_definition=(
                    'Begin Report\nRecordSource = "qryOrders"\nEnd\n'
                    "CodeBehindReport\nPrivate Sub Report_Open()\nEnd Sub"
                ),
            ),
            ExtractedObjectSnapshot(
                object_type="form",
                name="frmOrders",
                sanitized_definition=(
                    'Begin Form\nRecordSource = "qryOrders"\n'
                    'OnLoad = "[Event Procedure]"\nEnd\nCodeBehindForm\n'
                    "Private Sub Form_Load()\n"
                    'DoCmd.OpenQuery "qryOrders"\n'
                    'DoCmd.OpenReport "rptOrders"\nEnd Sub'
                ),
            ),
        ),
    )

    bundle = build_application_evidence_bundles(stage, (snapshot,))[0]
    objects = {item.name: item.object_id for item in bundle.objects}
    nodes = {item.object_id: item.node_id for item in bundle.dependency_nodes}
    edges = {
        (item.source_node_id, item.target_node_id, item.relationship)
        for item in bundle.dependency_edges
    }

    assert (
        nodes[objects["frmOrders"]],
        nodes[objects["qryOrders"]],
        DependencyRelation.BINDS_TO,
    ) in edges
    assert (
        nodes[objects["frmOrders"]],
        nodes[objects["qryOrders"]],
        DependencyRelation.CALLS,
    ) in edges
    assert (
        nodes[objects["frmOrders"]],
        nodes[objects["rptOrders"]],
        DependencyRelation.CALLS,
    ) in edges
    assert (
        nodes[objects["rptOrders"]],
        nodes[objects["qryOrders"]],
        DependencyRelation.BINDS_TO,
    ) in edges
    fact_types = {item.fact_type for item in bundle.evidence}
    assert {"record_source", "ui_event_binding", "code_behind", "vba_procedure"} <= fact_types


def test_vba_dao_ado_file_and_dynamic_references_are_grounded_without_secrets() -> None:
    artifact = _artifact("app-code", "Code.accdb", "5" * 64)
    stage = _stage("app-code", "Code App", (artifact,))
    module = ExtractedObjectSnapshot(
        object_type="module",
        name="Automation",
        sanitized_definition=(
            "Public Sub Run()\n"
            'CurrentDb.Execute "UPDATE Orders SET Status = 1"\n'
            'DoCmd.OpenQuery "qryRefresh"\n'
            "DoCmd.RunSQL sqlBuiltAtRuntime\n"
            'DoCmd.TransferSpreadsheet acExport, 10, "Orders", '
            '"C:\\Exports\\orders.xlsx"\n'
            "Dim cn As ADODB.Connection\n"
            "Set cn = New ADODB.Connection\n"
            'cn.ConnectionString = "Provider=SQLOLEDB;Data Source=SQL01;'
            "Initial Catalog=Operations;User ID=reader;Password=VBA_SECRET;"
            'Custom=PRIVATE_CANARY"\n'
            "cn.Open\n"
            'cn.Execute "UPDATE dbo.RemoteOrders SET Status = 1"\n'
            "End Sub"
        ),
    )
    snapshot = _snapshot(
        artifact,
        (
            _table("Orders"),
            _query("qryRefresh", "SELECT * FROM Orders", kind="select", dao=0),
            module,
        ),
    )

    bundle = build_application_evidence_bundles(stage, (snapshot,))[0]
    serialized = canonical_json_bytes(bundle).lower()
    scopes_and_operations = {
        (item.scope, item.operation) for item in bundle.interactions
    }

    assert (InteractionScope.LOCAL, DataOperation.UPDATE) in scopes_and_operations
    assert (InteractionScope.LOCAL, DataOperation.EXECUTE) in scopes_and_operations
    assert (InteractionScope.EXTERNAL, DataOperation.UPDATE) in scopes_and_operations
    assert (InteractionScope.EXTERNAL, DataOperation.CREATE) in scopes_and_operations
    assert any(item.platform == "sql_server" for item in bundle.datasources)
    assert any(item.platform == "excel" for item in bundle.datasources)
    assert any(
        item.reason == "docmd_runsql_constructed_dynamically"
        for item in bundle.unresolved_references
    )
    assert b"vba_secret" not in serialized
    assert b"private_canary" not in serialized
    assert b"reader" not in serialized
    assert b"<connection metadata redacted>" in serialized
    assert all("exports" not in item.evidence_id.casefold() for item in bundle.evidence)
    assert all("sql01" not in item.evidence_id.casefold() for item in bundle.evidence)


def test_macro_saved_query_file_and_unavailable_target_are_explicit() -> None:
    artifact = _artifact("app-macro", "Macro.accdb", "6" * 64)
    stage = _stage("app-macro", "Macro App", (artifact,))
    macro = ExtractedObjectSnapshot(
        object_type="macro",
        name="Nightly",
        sanitized_definition=(
            'Action = "OpenQuery"\nQueryName = "qryNightly"\n'
            'Action = "TransferSpreadsheet"\nTransferType = "acExport"\n'
            'FileName = "C:\\Exports\\nightly.xlsx"\n'
            'Action = "OpenReport"\n'
        ),
    )
    snapshot = _snapshot(
        artifact,
        (
            _table("Orders"),
            _query("qryNightly", "SELECT * FROM Orders", kind="select", dao=0),
            macro,
        ),
    )

    bundle = build_application_evidence_bundles(stage, (snapshot,))[0]

    assert any(
        item.operation == DataOperation.EXECUTE
        and item.scope == InteractionScope.LOCAL
        for item in bundle.interactions
    )
    assert any(
        item.operation == DataOperation.CREATE
        and item.scope == InteractionScope.EXTERNAL
        for item in bundle.interactions
    )
    assert any(
        item.reason == "macro_openreport_target_unavailable"
        for item in bundle.unresolved_references
    )


class _FakeRegistryReader:
    def __init__(
        self,
        records: dict[tuple[str, RegistryScope, RegistryBitness], RegistryDsnRecord],
    ) -> None:
        self.records = records
        self.calls: list[tuple[str, RegistryScope, RegistryBitness]] = []

    def read_dsn(
        self,
        name: str,
        *,
        scope: RegistryScope,
        bitness: RegistryBitness,
    ) -> RegistryDsnRecord | None:
        key = (name, scope, bitness)
        self.calls.append(key)
        return self.records.get(key)


def _artifact(application_id: str, filename: str, digest: str) -> StagedArtifactRecord:
    extension = filename.rsplit(".", 1)[-1].casefold()
    if extension not in {"accdb", "mdb"}:
        raise ValueError("fixture artifact must be an Access database")
    return StagedArtifactRecord(
        application_id=application_id,
        application_name=application_id,
        source_locator=rf"C:\Inventory\{application_id}\{filename}",
        staged_relative_path=f"applications/{application_id}/{filename}",
        filename=filename,
        access_format=cast(Literal["accdb", "mdb"], extension),
        size_bytes=123,
        sha256=digest,
    )


def _stage(
    application_id: str,
    application_name: str,
    artifacts: tuple[StagedArtifactRecord, ...],
    descriptions: tuple[str, ...] = (),
) -> StageIndex:
    return StageIndex(
        generated_at=NOW,
        inventory_sha256=INVENTORY_DIGEST,
        applications=(
            StagedApplication(
                application_id=application_id,
                application_name=application_name,
                inventory_descriptions=descriptions,
                artifact_ids=tuple(item.artifact_id for item in artifacts),
            ),
        ),
        artifacts=artifacts,
    )


def _snapshot(
    artifact: StagedArtifactRecord,
    objects: tuple[ExtractedObjectSnapshot, ...],
    *,
    coverage_status: Literal["complete", "partial"] = "complete",
    warnings: tuple[str, ...] = (),
    tabledef_counts: tuple[int, int, int] = (0, 0, 0),
    querydef_counts: tuple[int, int, int] = (0, 0, 0),
) -> ExtractedArtifactSnapshot:
    return ExtractedArtifactSnapshot(
        application_id=artifact.application_id,
        artifact_id=artifact.artifact_id,
        artifact_sha256=artifact.sha256,
        extractor_version="fixture-static-v2",
        coverage_status=coverage_status,
        warnings=warnings,
        objects=objects,
        tabledef_enumerated_count=tabledef_counts[0],
        tabledef_succeeded_count=tabledef_counts[1],
        tabledef_failed_count=tabledef_counts[2],
        querydef_enumerated_count=querydef_counts[0],
        querydef_succeeded_count=querydef_counts[1],
        querydef_failed_count=querydef_counts[2],
    )


def _table(name: str) -> ExtractedObjectSnapshot:
    return ExtractedObjectSnapshot(
        object_type="table",
        name=name,
        sanitized_properties={"attributes": "0", "fields": "[]"},
    )


def _query(
    name: str,
    sql: str,
    *,
    kind: str,
    dao: int,
    connect: str = "",
    returns_records: str = "",
    timeout: str = "",
    parameters: str = "[]",
    attributes: str = "0",
    hidden: str = "false",
    system: str = "false",
) -> ExtractedObjectSnapshot:
    return ExtractedObjectSnapshot(
        object_type="query",
        name=name,
        sanitized_definition=sql,
        sanitized_properties={
            "query_kind": kind,
            "dao_type_code": str(dao),
            "connect": connect,
            "returns_records": returns_records,
            "odbc_timeout": timeout,
            "parameters": parameters,
            "attributes": attributes,
            "hidden": hidden,
            "system": system,
        },
    )
