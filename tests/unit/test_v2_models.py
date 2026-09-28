from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from portfolio_analyzer.v2.identity import canonical_json_bytes, canonical_sha256, stable_id
from portfolio_analyzer.v2.models import (
    AccessObjectEvidence,
    AccessObjectType,
    AccessTableEvidence,
    ApplicationEvidenceBundle,
    ApplicationInterpretation,
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
    EvidenceRecord,
    ExtractionCoverage,
    ExtractionStatus,
    InteractionScope,
    InterpretationKind,
    InterpretiveFinding,
    KeyValueFact,
    LogicalUnitInterpretation,
    ModelProvenance,
    PortfolioAnalysis,
    PortfolioCandidate,
    PortfolioCandidateType,
    PortfolioFinding,
    QueryEvidence,
    QueryKind,
    QueryParameterEvidence,
    ReportApplicationEntry,
    ReportCoverageEntry,
    ReportModel,
    ReportStatus,
    ResolutionStatus,
    is_current_model_provenance,
)

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64


def test_canonical_hashing_is_order_independent_and_redacts_credentials() -> None:
    first = {
        "server": "SQL01",
        "nested": {"Password": "supersecret", "database": "Warehouse"},
        "connect": "UID=reader;PWD=othersecret;SERVER=SQL01;DATABASE=Warehouse",
    }
    second = {
        "connect": "UID=someone;PWD=different;SERVER=SQL01;DATABASE=Warehouse",
        "nested": {"database": "Warehouse", "Password": "not-the-same"},
        "server": "SQL01",
    }

    serialized = canonical_json_bytes(first)

    assert b"supersecret" not in serialized
    assert b"othersecret" not in serialized
    assert b"reader" not in serialized
    assert b"SQL01" in serialized
    assert canonical_sha256(first) == canonical_sha256(second)
    assert stable_id("example", first) == stable_id("example", second)


def test_artifact_and_object_ids_are_stable_and_validate_supplied_ids() -> None:
    first = _artifact(source=r"\\Server\Share\Tool.accdb")
    second = _artifact(source="//server/share/tool.accdb")
    obj = AccessObjectEvidence(
        artifact_id=first.artifact_id,
        object_type=AccessObjectType.QUERY,
        name="qryDeals",
    )

    assert first.artifact_id == second.artifact_id
    assert obj.object_id.startswith("object_")

    with pytest.raises(ValidationError, match="canonical identity"):
        AccessObjectEvidence(
            artifact_id=first.artifact_id,
            object_type=AccessObjectType.QUERY,
            name="qryDeals",
            object_id="object_wrong",
        )


def test_query_evidence_canonicalizes_typed_parameters_without_losing_ordinal_order() -> None:
    query = QueryEvidence(
        object_id="object-query",
        query_kind=QueryKind.SELECT,
        dao_type=0,
        attributes=-2147483645,
        is_hidden=True,
        is_system=True,
        connection_kind=ConnectionKind.LOCAL_ACCESS,
        parameters=(
            QueryParameterEvidence(
                ordinal=1,
                name="pAlpha",
                dao_type=8,
                direction=1,
            ),
            QueryParameterEvidence(
                ordinal=0,
                name="pZulu",
                dao_type=10,
                direction=0,
            ),
        ),
    )

    assert tuple(item.ordinal for item in query.parameters) == (0, 1)
    assert query.parameter_names == ("pZulu", "pAlpha")
    assert query.attributes == -2147483645
    assert query.is_hidden is True
    assert query.is_system is True

    with pytest.raises(ValidationError, match="parameter ordinals must be unique"):
        QueryEvidence(
            object_id="object-query",
            query_kind=QueryKind.SELECT,
            connection_kind=ConnectionKind.LOCAL_ACCESS,
            parameters=(
                QueryParameterEvidence(ordinal=0, name="first"),
                QueryParameterEvidence(ordinal=0, name="second"),
            ),
        )


def test_datasource_contract_forbids_raw_connections_and_keeps_endpoint_identity() -> None:
    datasource = DatasourceIdentity(
        platform="Microsoft SQL Server",
        driver="ODBC Driver 17 for SQL Server",
        server="SQL01",
        database="CorporateTrust",
    )

    assert datasource.server == "SQL01"
    assert datasource.database == "CorporateTrust"
    assert "connect" not in datasource.model_dump()
    assert "connection_string" not in datasource.model_dump()

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        DatasourceIdentity.model_validate(
            {
                **datasource.model_dump(mode="python"),
                "connect": "ODBC;SERVER=SQL01;PWD=SECRET",
            }
        )
    with pytest.raises(ValidationError, match="must not contain a connection string"):
        ConnectionEvidence(
            application_id="app-1",
            artifact_id="artifact-1",
            artifact_sha256=DIGEST_A,
            object_id="query-1",
            connection_kind=ConnectionKind.PASS_THROUGH_QUERY,
            sanitized_summary="ODBC;SERVER=SQL01;DATABASE=CorporateTrust",
            direct_provenance=ConnectionProvenance.QUERYDEF_CONNECT,
            resolution_status=ResolutionStatus.RESOLVED,
            datasource_id=datasource.datasource_id,
        )
    with pytest.raises(ValidationError, match="raw connection properties"):
        KeyValueFact(name="Connect", value="ODBC;SERVER=SQL01")

    assert {item.value for item in ResolutionStatus} == {
        "resolved",
        "unresolved",
        "ambiguous",
        "wrong_bitness",
        "permission_denied",
        "unsupported_file_dsn",
        "malformed",
        "not_applicable",
    }


@pytest.mark.parametrize(
    "raw_connection",
    (
        r"DBQ=C:\private\finance.accdb",
        r"FILEDSN=C:\private\finance.dsn",
        "OLEDB:Provider=SQLOLEDB;Data Source=SQL01",
    ),
)
def test_strict_connection_models_reject_additional_raw_connection_forms(
    raw_connection: str,
) -> None:
    with pytest.raises(ValidationError, match="must not contain a connection string"):
        ConnectionEvidence(
            application_id="app-1",
            artifact_id="artifact-1",
            artifact_sha256=DIGEST_A,
            object_id="query-1",
            connection_kind=ConnectionKind.PASS_THROUGH_QUERY,
            sanitized_summary=raw_connection,
            direct_provenance=ConnectionProvenance.QUERYDEF_CONNECT,
            resolution_status=ResolutionStatus.UNRESOLVED,
        )
    with pytest.raises(ValidationError, match="must not contain a connection string"):
        DatasourceIdentity(
            platform="Microsoft Access",
            resource=raw_connection,
        )
    with pytest.raises(ValidationError, match="must not contain a connection string"):
        KeyValueFact(name="description", value=raw_connection)


def test_application_bundle_closes_references_and_sanitizes_serialized_evidence() -> None:
    bundle = _bundle()
    serialized = canonical_json_bytes(bundle)

    assert bundle.schema_version == "application-evidence-v2"
    assert b"topsecret" not in serialized
    assert b"analyst" not in serialized
    assert b"SQL01" in serialized
    assert bundle.connections[0].direct_provenance == ConnectionProvenance.QUERYDEF_CONNECT
    assert bundle.queries[0].is_hidden is False
    assert bundle.tables[0].is_linked is False
    assert bundle.interactions[0].operation == DataOperation.READ

    broken_object = AccessObjectEvidence(
        artifact_id=bundle.artifacts[0].artifact_id,
        object_type=AccessObjectType.QUERY,
        name="Broken",
        evidence_ids=("evidence_missing",),
    )
    payload = bundle.model_dump(mode="python")
    payload["objects"] = (*bundle.objects, broken_object)
    with pytest.raises(ValidationError, match="object evidence references unknown IDs"):
        ApplicationEvidenceBundle.model_validate(payload)


def test_bundle_hash_is_independent_of_unordered_input_collections() -> None:
    bundle = _bundle()
    extra = EvidenceRecord(
        application_id="app-1",
        artifact_id=bundle.artifacts[0].artifact_id,
        artifact_sha256=bundle.artifacts[0].sha256,
        origin=bundle.evidence[0].origin,
        fact_type="query_type",
        observation="DAO query type is pass-through",
    )
    first = ApplicationEvidenceBundle(
        **{
            **bundle.model_dump(mode="python"),
            "evidence": (*bundle.evidence, extra),
        }
    )
    second = ApplicationEvidenceBundle(
        **{
            **bundle.model_dump(mode="python"),
            "evidence": (extra, *bundle.evidence),
        }
    )

    assert canonical_sha256(first) == canonical_sha256(second)


def test_identical_bytes_at_distinct_sources_remain_distinct_artifacts() -> None:
    first = _artifact(source=r"C:\Source\one.accdb")
    second = _artifact(source=r"C:\Source\two.accdb")

    assert first.sha256 == second.sha256
    assert first.artifact_id != second.artifact_id


def test_evidence_identity_is_content_versioned_by_artifact_sha256() -> None:
    artifact = _artifact(source=r"C:\Source\tool.accdb")
    first = EvidenceRecord(
        application_id=artifact.application_id,
        artifact_id=artifact.artifact_id,
        artifact_sha256=DIGEST_A,
        fact_type="object_count",
        observation="Found 3 objects",
    )
    second = EvidenceRecord(
        application_id=artifact.application_id,
        artifact_id=artifact.artifact_id,
        artifact_sha256=DIGEST_B,
        fact_type="object_count",
        observation="Found 3 objects",
    )

    assert first.evidence_id != second.evidence_id


def test_portfolio_candidate_is_deterministic_and_requires_observed_support() -> None:
    candidate = PortfolioCandidate(
        candidate_type=PortfolioCandidateType.SHARED_ENDPOINT,
        application_ids=("app-2", "app-1"),
        evidence_ids=("evidence-2", "evidence-1"),
        score=0.9,
        policy_version="candidate-policy-v2",
    )
    reordered = PortfolioCandidate(
        candidate_type=PortfolioCandidateType.SHARED_ENDPOINT,
        application_ids=("app-1", "app-2"),
        evidence_ids=("evidence-1", "evidence-2"),
        score=0.9,
        policy_version="candidate-policy-v2",
    )

    assert candidate.candidate_id == reordered.candidate_id
    with pytest.raises(ValidationError, match="at least two applications"):
        PortfolioCandidate(
            candidate_type=PortfolioCandidateType.EXACT_CODE,
            application_ids=("app-1",),
            evidence_ids=("evidence-1",),
            score=1.0,
            policy_version="candidate-policy-v2",
        )


def test_interpretation_portfolio_and_report_contracts_are_cited_and_qwen_only() -> None:
    bundle = _bundle()
    provenance = _provenance()
    evidence_id = bundle.evidence[0].evidence_id
    logical = LogicalUnitInterpretation(
        application_id=bundle.application_id,
        logical_unit_id=bundle.objects[0].object_id,
        source_bundle_sha256=DIGEST_B,
        purpose="Retrieves deal records for an investor report.",
        business_entities=("Deal",),
        workflow_actions=("Prepare investor report",),
        datasource_interaction_ids=(bundle.interactions[0].interaction_id,),
        evidence_ids=(evidence_id,),
        provenance=provenance,
    )
    finding = InterpretiveFinding(
        application_id=bundle.application_id,
        kind=InterpretationKind.BUSINESS_CAPABILITY,
        title="Investor reporting",
        explanation="The query retrieves deal data used by an investor report.",
        confidence=Confidence.MEDIUM,
        evidence_ids=(evidence_id,),
    )
    application = ApplicationInterpretation(
        application_id=bundle.application_id,
        source_bundle_sha256=DIGEST_B,
        logical_unit_interpretation_ids=(logical.interpretation_id,),
        summary="Produces investor-facing reporting from CorporateTrust deal data.",
        business_purpose="Prepare investor reporting.",
        major_workflows=("Prepare investor report",),
        capabilities=("Investor reporting",),
        findings=(finding,),
        evidence_ids=(evidence_id,),
        provenance=provenance,
    )
    portfolio_finding = PortfolioFinding(
        kind=InterpretationKind.PORTFOLIO_OPPORTUNITY,
        title="Shared reporting datasource",
        narrative="Applications using the same endpoint warrant a shared-contract review.",
        application_ids=(bundle.application_id,),
        evidence_ids=(evidence_id,),
        confidence=Confidence.MEDIUM,
    )
    portfolio = PortfolioAnalysis(
        application_interpretation_sha256s=(DIGEST_C,),
        findings=(portfolio_finding,),
        provenance=provenance,
    )
    report = ReportModel(
        status=ReportStatus.COMPLETE,
        generated_at=NOW,
        analysis_fingerprint=DIGEST_A,
        applications=(
            ReportApplicationEntry(
                application_id=bundle.application_id,
                application_name=bundle.application_name,
                evidence_bundle_sha256=DIGEST_B,
                interpretation_sha256=DIGEST_C,
                status=ExtractionStatus.COMPLETE,
                evidence_count=len(bundle.evidence),
            ),
        ),
        observed_facts=bundle.evidence,
        owner_claims=(),
        application_profiles=(application,),
        review_records=(),
        unresolved_references=(),
        coverage=(
            ReportCoverageEntry(
                application_id=bundle.application_id,
                coverage=bundle.coverage,
            ),
        ),
        pass_through_queries=(),
        linked_tables=(),
        object_registry=(),
        table_registry=(),
        query_registry=(),
        connection_registry=(),
        datasource_registry=(),
        data_access=(),
        dependency_nodes=(),
        dependency_edges=(),
        omissions=(),
        candidates=(),
        portfolio_findings=(portfolio_finding,),
        portfolio_analysis_sha256=canonical_sha256(portfolio),
    )

    assert provenance.model_repo_id == "Qwen/Qwen2.5-0.5B-Instruct"
    assert logical.interpretation_id.startswith("unit_interpretation_")
    assert application.interpretation_id.startswith("application_interpretation_")
    assert portfolio.portfolio_analysis_id.startswith("portfolio_analysis_")
    assert report.report_id.startswith("report_")

    with pytest.raises(ValidationError, match="must cite.*evidence"):
        LogicalUnitInterpretation(
            application_id="app-1",
            logical_unit_id="object-1",
            source_bundle_sha256=DIGEST_A,
            purpose="Unsupported assertion",
            evidence_ids=(),
            provenance=provenance,
        )
    with pytest.raises(ValidationError, match="complete report cannot exclude"):
        ReportModel.model_validate(
            {
                **report.model_dump(mode="python"),
                "excluded_application_ids": ("app-2",),
                "report_id": "",
            }
        )


def test_historical_model_provenance_is_readable_but_not_current() -> None:
    historical = ModelProvenance(
        model_repo_id="Qwen/Qwen2.5-1.5B-Instruct",
        model_revision="989aa7980e4cf806f80c7fef2b1adb7bc71aa306",
        model_manifest_sha256=DIGEST_A,
        prompt_version="historical-prompts",
        output_schema_version="historical-contracts",
        inference_library_version="transformers-historical",
    )

    assert not is_current_model_provenance(historical)
    assert is_current_model_provenance(_provenance())


def _artifact(*, source: str) -> ArtifactEvidence:
    return ArtifactEvidence(
        application_id="app-1",
        source_locator=source,
        staged_relative_path="applications/app-1/tool.accdb",
        filename="tool.accdb",
        access_format="accdb",
        sha256=DIGEST_A,
        size_bytes=42,
        extractor_version="windows-com-v2",
        extraction_status=ExtractionStatus.COMPLETE,
        extracted_at=NOW,
    )


def _bundle() -> ApplicationEvidenceBundle:
    artifact = _artifact(source=r"\\fileserver\Access\tool.accdb")
    query_object = AccessObjectEvidence(
        artifact_id=artifact.artifact_id,
        object_type=AccessObjectType.QUERY,
        name="qryInvestorReport",
        sanitized_definition="SELECT * FROM dbo.Deal",
    )
    evidence = EvidenceRecord(
        application_id="app-1",
        artifact_id=artifact.artifact_id,
        artifact_sha256=artifact.sha256,
        object_id=query_object.object_id,
        fact_type="query_definition",
        location="QueryDef.SQL",
        observation="SELECT * FROM dbo.Deal; credential PWD=topsecret",
    )
    query_object = AccessObjectEvidence(
        artifact_id=artifact.artifact_id,
        object_type=AccessObjectType.QUERY,
        name="qryInvestorReport",
        sanitized_definition="SELECT * FROM dbo.Deal",
        evidence_ids=(evidence.evidence_id,),
    )
    datasource = DatasourceIdentity(
        platform="Microsoft SQL Server",
        driver="ODBC Driver 17 for SQL Server",
        server="SQL01",
        database="CorporateTrust",
    )
    connection = ConnectionEvidence(
        application_id="app-1",
        artifact_id=artifact.artifact_id,
        artifact_sha256=artifact.sha256,
        object_id=query_object.object_id,
        connection_kind=ConnectionKind.PASS_THROUGH_QUERY,
        sanitized_summary="SQL Server SQL01 / CorporateTrust via ODBC Driver 17",
        direct_provenance=ConnectionProvenance.QUERYDEF_CONNECT,
        resolution_status=ResolutionStatus.RESOLVED,
        resolution_provenance=("direct QueryDef fields",),
        datasource_id=datasource.datasource_id,
        evidence_ids=(evidence.evidence_id,),
    )
    query = QueryEvidence(
        object_id=query_object.object_id,
        query_kind=QueryKind.PASS_THROUGH,
        dao_type=112,
        sanitized_sql="SELECT * FROM dbo.Deal",
        returns_records=True,
        odbc_timeout_seconds=60,
        is_hidden=False,
        is_system=False,
        connection_kind=ConnectionKind.PASS_THROUGH_QUERY,
        connection_id=connection.connection_id,
        evidence_ids=(evidence.evidence_id,),
    )
    table_object = AccessObjectEvidence(
        artifact_id=artifact.artifact_id,
        object_type=AccessObjectType.TABLE,
        name="LocalSettings",
    )
    table = AccessTableEvidence(
        object_id=table_object.object_id,
        is_linked=False,
    )
    interaction = DatasourceInteraction(
        source_object_id=query_object.object_id,
        operation=DataOperation.READ,
        scope=InteractionScope.EXTERNAL,
        connection_id=connection.connection_id,
        datasource_id=datasource.datasource_id,
        catalog="CorporateTrust",
        database="CorporateTrust",
        schema_name="dbo",
        object_name="Deal",
        evidence_ids=(evidence.evidence_id,),
    )
    source_node = DependencyNode(
        application_id="app-1",
        kind=DependencyNodeKind.ACCESS_OBJECT,
        label="qryInvestorReport",
        artifact_id=artifact.artifact_id,
        object_id=query_object.object_id,
    )
    target_node = DependencyNode(
        application_id="app-1",
        kind=DependencyNodeKind.DATABASE_OBJECT,
        label="SQL01/CorporateTrust/dbo/Deal",
        datasource_id=datasource.datasource_id,
    )
    edge = DependencyEdge(
        source_node_id=source_node.node_id,
        target_node_id=target_node.node_id,
        relationship=DependencyRelation.READS,
        operation=DataOperation.READ,
        evidence_ids=(evidence.evidence_id,),
    )
    return ApplicationEvidenceBundle(
        application_id="app-1",
        application_name="Investor Reporting",
        inventory_record_ids=("inventory-1",),
        generated_at=NOW,
        artifacts=(artifact,),
        objects=(query_object, table_object),
        connections=(connection,),
        tables=(table,),
        queries=(query,),
        datasources=(datasource,),
        interactions=(interaction,),
        dependency_nodes=(target_node, source_node),
        dependency_edges=(edge,),
        evidence=(evidence,),
        coverage=ExtractionCoverage(
            primary_artifact_count=1,
            complete_artifact_count=1,
            partial_artifact_count=0,
            failed_artifact_count=0,
            discovered_object_count=2,
            extracted_object_count=2,
            warning_count=0,
            unresolved_reference_count=0,
        ),
    )


def _provenance() -> ModelProvenance:
    return ModelProvenance(
        model_manifest_sha256=DIGEST_A,
        prompt_version="qwen-prompts-v2",
        output_schema_version="qwen-output-v2",
        inference_library_version="5.17.0",
        generation_parameters=(KeyValueFact(name="temperature", value="0"),),
    )
