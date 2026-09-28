from datetime import UTC, datetime, timedelta

from portfolio_analyzer.library_identity import (
    ApprovedLibraryReference,
    LibraryInjectionStatus,
)
from portfolio_analyzer.v2.fingerprints import extraction_fingerprint, stage_fingerprint
from portfolio_analyzer.v2.workflow import (
    EXTRACTION_POLICY_VERSION,
    ExtractedArtifactSnapshot,
    StagedApplication,
    StagedArtifactRecord,
    StageIndex,
)


def _index(*, generated_at: datetime, source: str, staged_path: str) -> StageIndex:
    artifact = StagedArtifactRecord(
        application_id="app-1",
        application_name="Example",
        source_locator=source,
        staged_relative_path=staged_path,
        filename="main.accdb",
        access_format="accdb",
        size_bytes=4,
        sha256="a" * 64,
    )
    return StageIndex(
        generated_at=generated_at,
        inventory_sha256="b" * 64,
        applications=(
            StagedApplication(
                application_id="app-1",
                application_name="Example",
                artifact_ids=(artifact.artifact_id,),
            ),
        ),
        artifacts=(artifact,),
    )


def test_stage_fingerprint_excludes_time_and_path_text_but_retains_artifact_identity() -> None:
    now = datetime(2026, 9, 28, tzinfo=UTC)
    first = _index(
        generated_at=now,
        source="C:/inventory/main.accdb",
        staged_path="staged_tools/a/main.accdb",
    )
    second = first.model_copy(
        update={
            "generated_at": now + timedelta(days=1),
            "artifacts": (
                first.artifacts[0].model_copy(
                    update={
                        "source_locator": "D:/other/main.accdb",
                        "staged_relative_path": "staged_tools/b/main.accdb",
                    }
                ),
            ),
        }
    )

    assert stage_fingerprint(first) == stage_fingerprint(second)

    changed = first.model_copy(
        update={
            "artifacts": (
                first.artifacts[0].model_copy(update={"sha256": "c" * 64}),
            ),
        }
    )
    assert stage_fingerprint(first) != stage_fingerprint(changed)


def test_extraction_fingerprint_chains_library_identity_and_injection_outcome() -> None:
    library = ApprovedLibraryReference(filename="reviewed.accdb", sha256="b" * 64)
    skipped = ExtractedArtifactSnapshot(
        application_id="app-1",
        artifact_id="artifact-1",
        artifact_sha256="a" * 64,
        extractor_version="test-extractor-v1",
        extraction_policy_version=EXTRACTION_POLICY_VERSION,
        approved_libraries=(library,),
        library_injection_status=LibraryInjectionStatus.SKIPPED_SAFETY_GATE,
        extracted_at=datetime(2026, 9, 28, tzinfo=UTC),
        coverage_status="partial",
    )
    injected = ExtractedArtifactSnapshot(
        application_id="app-1",
        artifact_id="artifact-1",
        artifact_sha256="a" * 64,
        extractor_version="test-extractor-v1",
        extraction_policy_version=EXTRACTION_POLICY_VERSION,
        approved_libraries=(library,),
        injected_libraries=(library,),
        library_injection_status=LibraryInjectionStatus.INJECTED,
        extracted_at=skipped.extracted_at + timedelta(hours=1),
        coverage_status="partial",
    )
    changed_library = ApprovedLibraryReference(
        filename="reviewed.accdb",
        sha256="c" * 64,
    )
    changed = ExtractedArtifactSnapshot(
        application_id="app-1",
        artifact_id="artifact-1",
        artifact_sha256="a" * 64,
        extractor_version="test-extractor-v1",
        extraction_policy_version=EXTRACTION_POLICY_VERSION,
        approved_libraries=(changed_library,),
        library_injection_status=LibraryInjectionStatus.SKIPPED_SAFETY_GATE,
        extracted_at=skipped.extracted_at,
        coverage_status="partial",
    )

    assert extraction_fingerprint(skipped) != extraction_fingerprint(injected)
    assert extraction_fingerprint(skipped) != extraction_fingerprint(changed)
