from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from portfolio_analyzer.v2.candidates import (
    FROZEN_CANDIDATE_POLICY,
    generate_portfolio_candidates,
)
from portfolio_analyzer.v2.fingerprints import evidence_bundle_fingerprint
from portfolio_analyzer.v2.models import (
    AccessObjectEvidence,
    AccessObjectType,
    ApplicationEvidenceBundle,
    ApplicationProfile,
    ArtifactEvidence,
    ConnectionEvidence,
    ConnectionKind,
    ConnectionProvenance,
    DataAccess,
    DataOperation,
    DatasourceIdentity,
    EvidenceRecord,
    ExtractionCoverage,
    ExtractionStatus,
    InteractionScope,
    ModelProvenance,
    PortfolioCandidateType,
    ResolutionStatus,
)

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
DIGEST_A = "a" * 64


def test_candidates_are_deterministic_overlapping_and_evidence_backed() -> None:
    first = _bundle("app-1", "First", "1")
    second = _bundle("app-2", "Second", "2")
    first_profile = _profile(first)
    second_profile = _profile(second)

    candidates = generate_portfolio_candidates(
        (second, first), (second_profile, first_profile)
    )
    reordered = generate_portfolio_candidates(
        (first, second), (first_profile, second_profile)
    )

    assert tuple(item.candidate_id for item in candidates) == tuple(
        item.candidate_id for item in reordered
    )
    assert {item.candidate_type for item in candidates} == (
        set(PortfolioCandidateType) - {PortfolioCandidateType.SEMANTIC_OVERLAP}
    )
    assert len(candidates) == 4
    assert all(item.application_ids == ("app-1", "app-2") for item in candidates)
    assert all(item.evidence_ids for item in candidates)
    assert all(item.basis_ids for item in candidates)


def test_semantic_threshold_and_profile_scope_are_strict() -> None:
    first = _bundle("app-1", "First", "1")
    second = _bundle("app-2", "Second", "2")
    different = _profile(second).model_copy(
        update={"capabilities": ("Unrelated",), "major_workflows": ()}
    )

    candidates = generate_portfolio_candidates((first, second), (_profile(first), different))

    assert PortfolioCandidateType.SEMANTIC_OVERLAP not in {
        item.candidate_type for item in candidates
    }
    with pytest.raises(ValueError, match="without evidence bundles"):
        generate_portfolio_candidates(
            (first,),
            (_profile(first).model_copy(update={"application_id": "app-unknown"}),),
        )
    assert FROZEN_CANDIDATE_POLICY.version == "portfolio-candidates-v3"
    assert FROZEN_CANDIDATE_POLICY.semantic_overlap_min_score is None
    with pytest.raises(FrozenInstanceError):
        FROZEN_CANDIDATE_POLICY.semantic_overlap_min_score = 0.1  # type: ignore[misc]


def test_unresolved_connections_do_not_form_endpoint_or_file_candidates() -> None:
    first = _bundle("app-1", "First", "1")
    second = _bundle("app-2", "Second", "2")
    ambiguous_connection = ConnectionEvidence.model_validate(
        {
            **second.connections[0].model_dump(mode="python"),
            "resolution_status": ResolutionStatus.AMBIGUOUS,
        }
    )
    second = ApplicationEvidenceBundle.model_validate(
        {
            **second.model_dump(mode="python"),
            "connections": (ambiguous_connection,),
        }
    )

    types = {
        item.candidate_type for item in generate_portfolio_candidates((first, second))
    }

    assert PortfolioCandidateType.SHARED_ENDPOINT not in types
    assert PortfolioCandidateType.SHARED_FILE not in types


def _bundle(application_id: str, name: str, source_suffix: str) -> ApplicationEvidenceBundle:
    artifact = ArtifactEvidence(
        application_id=application_id,
        source_locator=rf"\\fileserver\apps\{source_suffix}.accdb",
        staged_relative_path=f"applications/{application_id}/tool.accdb",
        filename="tool.accdb",
        access_format="accdb",
        sha256=source_suffix * 64,
        size_bytes=42,
        extractor_version="windows-com-v2",
        extraction_status=ExtractionStatus.COMPLETE,
        extracted_at=NOW,
    )
    query = AccessObjectEvidence(
        artifact_id=artifact.artifact_id,
        object_type=AccessObjectType.QUERY,
        name="qryShared",
        sanitized_definition="SELECT * FROM dbo.Deal",
    )
    evidence = EvidenceRecord(
        application_id=application_id,
        artifact_id=artifact.artifact_id,
        artifact_sha256=artifact.sha256,
        object_id=query.object_id,
        fact_type="query_definition",
        observation="Reads dbo.Deal from the approved warehouse endpoint",
    )
    query = query.model_copy(update={"evidence_ids": (evidence.evidence_id,)})
    datasource = DatasourceIdentity(
        platform="Microsoft SQL Server",
        server="SQL01",
        database="CorporateTrust",
        resource=r"\\fileserver\shared\mapping.csv",
    )
    connection = ConnectionEvidence(
        application_id=application_id,
        artifact_id=artifact.artifact_id,
        artifact_sha256=artifact.sha256,
        object_id=query.object_id,
        connection_kind=ConnectionKind.PASS_THROUGH_QUERY,
        sanitized_summary="SQL Server SQL01 / CorporateTrust",
        direct_provenance=ConnectionProvenance.QUERYDEF_CONNECT,
        resolution_status=ResolutionStatus.RESOLVED,
        datasource_id=datasource.datasource_id,
        evidence_ids=(evidence.evidence_id,),
    )
    access = DataAccess(
        source_object_id=query.object_id,
        operation=DataOperation.READ,
        scope=InteractionScope.EXTERNAL,
        connection_id=connection.connection_id,
        datasource_id=datasource.datasource_id,
        database="CorporateTrust",
        schema_name="dbo",
        object_name="Deal",
        evidence_ids=(evidence.evidence_id,),
    )
    return ApplicationEvidenceBundle(
        application_id=application_id,
        application_name=name,
        inventory_record_ids=(f"inventory-{source_suffix}",),
        generated_at=NOW,
        artifacts=(artifact,),
        objects=(query,),
        connections=(connection,),
        datasources=(datasource,),
        interactions=(access,),
        evidence=(evidence,),
        coverage=ExtractionCoverage(
            primary_artifact_count=1,
            complete_artifact_count=1,
            partial_artifact_count=0,
            failed_artifact_count=0,
            discovered_object_count=1,
            extracted_object_count=1,
            warning_count=0,
            unresolved_reference_count=0,
        ),
    )


def _profile(bundle: ApplicationEvidenceBundle) -> ApplicationProfile:
    return ApplicationProfile(
        application_id=bundle.application_id,
        source_bundle_sha256=evidence_bundle_fingerprint(bundle),
        logical_unit_interpretation_ids=(),
        summary="Produces investor reports.",
        business_purpose="Investor reporting",
        major_workflows=("Prepare report",),
        capabilities=("Investor reporting",),
        evidence_ids=(bundle.evidence[0].evidence_id,),
        provenance=ModelProvenance(
            model_manifest_sha256=DIGEST_A,
            prompt_version="qwen-prompts-v2",
            output_schema_version="qwen-output-v2",
            inference_library_version="5.17.0",
        ),
    )
