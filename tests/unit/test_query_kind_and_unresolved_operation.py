from __future__ import annotations

from datetime import UTC, datetime

import pytest

from portfolio_analyzer.access.windows_extractor import (
    _query_kind as extraction_query_kind,
)
from portfolio_analyzer.analysis.bundle import (
    _query_kind as evidence_query_kind,
)
from portfolio_analyzer.analysis.bundle import build_application_evidence_bundles
from portfolio_analyzer.parsing.dsn import (
    DsnResolution,
    RegistryBitness,
    RegistryDsnRecord,
    RegistryScope,
    resolve_windows_dsn,
)
from portfolio_analyzer.v2.models import (
    ApplicationEvidenceBundle,
    ConnectionKind,
    DataOperation,
    InteractionScope,
    QueryKind,
)
from portfolio_analyzer.v2.workflow import (
    ExtractedArtifactSnapshot,
    ExtractedObjectSnapshot,
    StagedApplication,
    StagedArtifactRecord,
    StageIndex,
)

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


class _EmptyRegistry:
    def read_dsn(
        self,
        _name: str,
        *,
        scope: RegistryScope,
        bitness: RegistryBitness,
    ) -> RegistryDsnRecord | None:
        del scope, bitness
        return None


@pytest.mark.parametrize(
    ("dao_type", "normalized", "expected"),
    (
        (144, "pass_through_bulk", QueryKind.PASS_THROUGH_BULK),
        (160, "compound", QueryKind.COMPOUND),
        (224, "procedure", QueryKind.PROCEDURE),
        (240, "action", QueryKind.ACTION),
    ),
)
def test_documented_extended_dao_query_types_are_normalized_consistently(
    dao_type: int,
    normalized: str,
    expected: QueryKind,
) -> None:
    assert extraction_query_kind(dao_type) == normalized
    assert evidence_query_kind(normalized, dao_type) == expected
    assert evidence_query_kind("unknown", dao_type) == expected


@pytest.mark.parametrize(
    ("sql", "expected"),
    (
        ("SELECT * FROM dbo.Deal", DataOperation.READ),
        ("INSERT INTO dbo.Deal (DealId) VALUES (1)", DataOperation.INSERT),
        ("UPDATE dbo.Deal SET Status = 'Closed'", DataOperation.UPDATE),
        ("DELETE FROM dbo.Deal WHERE DealId = 1", DataOperation.DELETE),
    ),
)
def test_unresolved_external_query_preserves_static_sql_operation(
    sql: str,
    expected: DataOperation,
) -> None:
    bundle = _unresolved_pass_through_bundle(sql)

    assert len(bundle.interactions) == 1
    interaction = bundle.interactions[0]
    assert interaction.scope == InteractionScope.UNRESOLVED
    assert interaction.operation == expected
    assert interaction.connection_id == bundle.connections[0].connection_id
    assert interaction.object_name == "dbo.Deal"
    evidence_by_id = {item.evidence_id: item for item in bundle.evidence}
    assert "sql_object_reference" in {
        evidence_by_id[evidence_id].fact_type for evidence_id in interaction.evidence_ids
    }


def test_bulk_pass_through_kind_retains_pass_through_connection_semantics() -> None:
    bundle = _unresolved_pass_through_bundle(
        "SELECT * FROM dbo.Deal",
        dao_type=144,
        normalized_kind="pass_through_bulk",
    )

    query = bundle.queries[0]
    assert query.query_kind == QueryKind.PASS_THROUGH_BULK
    assert query.connection_kind == ConnectionKind.PASS_THROUGH_QUERY
    assert query.connection_id == bundle.connections[0].connection_id


def _unresolved_pass_through_bundle(
    sql: str,
    *,
    dao_type: int = 112,
    normalized_kind: str = "pass_through",
) -> ApplicationEvidenceBundle:
    artifact = StagedArtifactRecord(
        application_id="app-unresolved-operation",
        application_name="Unresolved Operation",
        source_locator=r"C:\Inventory\Unresolved.accdb",
        staged_relative_path="applications/app-unresolved-operation/Unresolved.accdb",
        filename="Unresolved.accdb",
        access_format="accdb",
        size_bytes=123,
        sha256="a" * 64,
    )
    stage = StageIndex(
        generated_at=NOW,
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
        extractor_version="fixture-static-v2",
        coverage_status="complete",
        objects=(
            ExtractedObjectSnapshot(
                object_type="query",
                name="qryUnresolved",
                sanitized_definition=sql,
                sanitized_properties={
                    "query_kind": normalized_kind,
                    "dao_type_code": str(dao_type),
                    "connect": "ODBC;DSN=DefinitelyMissingForStaticTest",
                    "returns_records": "true",
                    "odbc_timeout": "30",
                    "parameters": "[]",
                    "attributes": "0",
                    "hidden": "false",
                    "system": "false",
                },
            ),
        ),
        querydef_enumerated_count=1,
        querydef_succeeded_count=1,
    )

    def resolve_missing(value: str) -> DsnResolution:
        return resolve_windows_dsn(
            value,
            registry_reader=_EmptyRegistry(),
            target_bitness=RegistryBitness.X64,
        )

    return build_application_evidence_bundles(
        stage,
        (snapshot,),
        dsn_resolver=resolve_missing,
    )[0]
