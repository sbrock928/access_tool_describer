from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

import portfolio_analyzer.v2.state as state_module
from portfolio_analyzer.qwen.pipeline import inference_cache_key
from portfolio_analyzer.v2.inference_cache import (
    InferenceCacheIntegrityError,
    PersistentInferenceOutputCache,
)
from portfolio_analyzer.v2.models import EvidenceRecord, StrictModel
from portfolio_analyzer.v2.state import (
    STATE_DIRECTORY_NAME,
    ApplicationRunRecord,
    ContentReference,
    RunManifest,
    RunPhase,
    RunStatus,
    StateIntegrityError,
    StatePublicationError,
    V2StateStore,
    WorkspaceLockedError,
    WorkspaceLockRequiredError,
    WorkspaceSchemaError,
    initialize_workspace,
    preflight_workspace,
)
from portfolio_analyzer.v2.workflow import (
    EXTRACTION_POLICY_VERSION,
    ExtractedArtifactSnapshot,
)

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
DIGEST_A = "a" * 64


class _VersionedPayload(StrictModel):
    schema_version: str
    value: str


def test_initialize_allows_only_declared_operator_inputs(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    inventory = workspace / "source_inventory"
    inventory.mkdir(parents=True)
    (workspace / "analyzer.toml").write_text("[analysis]\n", encoding="utf-8")
    owner_context = inventory / "owner_context.csv"
    owner_context.write_text("application_id,owner\napp-1,Alice\n", encoding="utf-8")
    access_libraries = inventory / "access_libraries.json"
    access_libraries.write_text("[]\n", encoding="utf-8")

    metadata = initialize_workspace(workspace, created_at=NOW, workspace_id="workspace-1")

    assert metadata == preflight_workspace(workspace)
    assert owner_context.read_text(encoding="utf-8").endswith("Alice\n")
    assert access_libraries.read_text(encoding="utf-8") == "[]\n"
    assert (workspace / "analyzer.toml").is_file()


def test_initialize_refuses_unmarked_legacy_or_generated_content(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    legacy = workspace / "staged"
    legacy.mkdir(parents=True)
    (legacy / "old.json").write_text("{}", encoding="utf-8")

    with pytest.raises(WorkspaceSchemaError, match="legacy|unsupported"):
        initialize_workspace(workspace, created_at=NOW)

    assert not (workspace / STATE_DIRECTORY_NAME).exists()


def test_content_store_is_canonical_sanitized_and_hash_verified(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _record()

    first = store.put(record)
    second = store.put(record)
    stored_path = store.state_root / first.relative_path

    assert first == second
    assert b"topsecret" not in stored_path.read_bytes()
    assert store.load(first, EvidenceRecord) == record

    stored_path.write_bytes(b"{}")
    with pytest.raises(StateIntegrityError, match="size mismatch|SHA-256 mismatch"):
        store.verify(first)


def test_content_store_binds_schema_labels_to_encoded_and_decoded_models(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    snapshot_value = _snapshot()

    with pytest.raises(StateIntegrityError, match="schema label"):
        store.put(snapshot_value, schema_name="forged-snapshot-v2")

    snapshot = store.put(snapshot_value)
    forged_snapshot = ContentReference(
        sha256=snapshot.sha256,
        relative_path=snapshot.relative_path,
        size_bytes=snapshot.size_bytes,
        schema_name="forged-snapshot-v2",
    )
    with pytest.raises(StateIntegrityError, match="encoded schema version"):
        store.load(forged_snapshot, ExtractedArtifactSnapshot)
    with store.exclusive_lock(), pytest.raises(
        StateIntegrityError, match="encoded schema version"
    ):
        store.publish_run(_manifest(forged_snapshot))

    record = store.put(_record())
    forged_record = ContentReference(
        sha256=record.sha256,
        relative_path=record.relative_path,
        size_bytes=record.size_bytes,
        schema_name="forged-evidence-v2",
    )
    with pytest.raises(StateIntegrityError, match="decoded model type"):
        store.load(forged_record, EvidenceRecord)


def test_publish_run_requires_lock_and_updates_current_pointer_last(tmp_path: Path) -> None:
    store = _store(tmp_path)
    snapshot = store.put(_snapshot())
    manifest = _manifest(snapshot)

    with pytest.raises(WorkspaceLockRequiredError):
        store.publish_run(manifest)
    with pytest.raises(StateIntegrityError, match="no valid current run"):
        store.load_current_run(RunPhase.EXTRACT)

    with store.exclusive_lock():
        published = store.publish_run(manifest)

    current_path = store.state_root / "current" / "extract.json"
    assert current_path.is_file()
    assert published.manifest_relative_path == "runs/extract/run-1/manifest.json"
    assert store.load_run(RunPhase.EXTRACT, "run-1") == manifest
    assert store.load_current_run(RunPhase.EXTRACT) == manifest


def test_extraction_snapshot_is_verified_before_manifest_or_pointer_publication(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    snapshot = store.put(_snapshot())
    manifest = _manifest(snapshot)
    (store.state_root / snapshot.relative_path).write_bytes(b"corrupt")

    with store.exclusive_lock(), pytest.raises(StateIntegrityError):
        store.publish_run(manifest)

    assert not (store.state_root / "runs" / "extract" / "run-1" / "manifest.json").exists()
    assert not (store.state_root / "current" / "extract.json").exists()


def test_report_publication_is_required_and_hash_verified_before_run_publication(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    report_model = store.put(
        _VersionedPayload(schema_version="report-model-v2", value="normalized report")
    )
    publication = store.put(
        _VersionedPayload(
            schema_version="report-publication-v2",
            value="verified report artifact hashes",
        )
    )

    with pytest.raises(ValidationError, match="publication manifest"):
        RunManifest(
            run_id="report-run-1",
            phase=RunPhase.REPORT,
            status=RunStatus.COMPLETE,
            started_at=NOW,
            completed_at=NOW,
            report_model=report_model,
        )

    manifest = RunManifest(
        run_id="report-run-1",
        phase=RunPhase.REPORT,
        status=RunStatus.COMPLETE,
        started_at=NOW,
        completed_at=NOW,
        report_model=report_model,
        report_publication=publication,
    )
    (store.state_root / publication.relative_path).write_bytes(b"corrupt")
    with store.exclusive_lock(), pytest.raises(StateIntegrityError):
        store.publish_run(manifest)

    assert not (
        store.state_root / "runs" / "report" / "report-run-1" / "manifest.json"
    ).exists()
    assert not (store.state_root / "current" / "report.json").exists()


def test_workspace_writer_lock_is_exclusive_and_releases(tmp_path: Path) -> None:
    first = _store(tmp_path)
    second = V2StateStore(first.root)

    with (
        first.exclusive_lock(),
        pytest.raises(WorkspaceLockedError, match="workspace is locked"),
        second.exclusive_lock(),
    ):
        pytest.fail("a second writer acquired the workspace lock")

    with second.exclusive_lock() as lock:
        assert lock.acquired


def test_conflicting_immutable_run_does_not_replace_current_pointer(tmp_path: Path) -> None:
    store = _store(tmp_path)
    snapshot = store.put(_snapshot())
    original = _manifest(snapshot)
    conflicting = _manifest(snapshot, warnings=("different content",))

    with store.exclusive_lock():
        store.publish_run(original)
    with store.exclusive_lock(), pytest.raises(StatePublicationError, match="already published"):
        store.publish_run(conflicting)

    assert store.load_current_run(RunPhase.EXTRACT) == original


def test_payload_write_failure_publishes_no_partial_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)

    def fail_write(_path: Path, _payload: bytes) -> None:
        raise OSError("simulated payload publication crash")

    monkeypatch.setattr(state_module, "_atomic_write_bytes", fail_write)

    with pytest.raises(OSError, match="publication crash"):
        store.put(_record())

    assert not (store.state_root / "objects").exists()


@pytest.mark.parametrize("boundary", ["manifest", "current"])
def test_crash_at_manifest_or_current_boundary_preserves_previous_current_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    store = _store(tmp_path)
    snapshot = store.put(_snapshot())
    original = _manifest(snapshot)
    replacement = original.model_copy(
        update={"run_id": "run-2", "warnings": ("replacement",)}
    )
    with store.exclusive_lock():
        store.publish_run(original)
    atomic_write = state_module._atomic_write_bytes

    def crash_at_boundary(path: Path, payload: bytes) -> None:
        is_target = (
            boundary == "manifest"
            and path.as_posix().endswith("runs/extract/run-2/manifest.json")
        ) or (
            boundary == "current"
            and path.as_posix().endswith("current/extract.json")
        )
        if is_target:
            raise OSError(f"simulated {boundary} publication crash")
        atomic_write(path, payload)

    monkeypatch.setattr(state_module, "_atomic_write_bytes", crash_at_boundary)

    with store.exclusive_lock(), pytest.raises(OSError, match="publication crash"):
        store.publish_run(replacement)

    assert store.load_current_run(RunPhase.EXTRACT) == original
    replacement_manifest = (
        store.state_root / "runs" / "extract" / "run-2" / "manifest.json"
    )
    assert replacement_manifest.exists() is (boundary == "current")


def test_reference_path_must_match_its_content_address() -> None:
    with pytest.raises(ValidationError, match="does not match its SHA-256"):
        ContentReference(
            sha256=DIGEST_A,
            relative_path="../../outside.json",
            size_bytes=2,
            schema_name="test-v2",
        )


def test_inference_cache_is_atomic_input_addressed_and_prompt_free(tmp_path: Path) -> None:
    store = _store(tmp_path)
    cache = PersistentInferenceOutputCache(store.state_root)
    key = inference_cache_key(
        content_sha256="1" * 64,
        schema_sha256="2" * 64,
        prompt_sha256="3" * 64,
        model_sha256="4" * 64,
    )
    output = {"purpose": "Grounded result", "evidence_ids": ["evidence-1"]}

    cache.put(key, output)
    cache.put(key, output)

    path = (
        store.state_root
        / "inference-cache"
        / key.cache_key[:2]
        / f"{key.cache_key}.json"
    )
    assert cache.get(key) == output
    raw = path.read_bytes()
    assert b'"prompt_sha256"' in raw
    assert b'"system_prompt"' not in raw
    assert b'"user_prompt"' not in raw

    path.write_bytes(b"{}")
    with pytest.raises(InferenceCacheIntegrityError, match="invalid"):
        cache.get(key)


def test_phase_contracts_keep_outputs_at_their_declared_boundary(tmp_path: Path) -> None:
    store = _store(tmp_path)
    snapshot = store.put(_snapshot())

    with pytest.raises(ValidationError, match="artifact IDs only"):
        _run_manifest(
            RunPhase.STAGE,
            ApplicationRunRecord(
                application_id="app-1",
                artifact_ids=("artifact-1",),
                extraction_snapshots=(snapshot,),
                status=RunStatus.COMPLETE,
            ),
        )
    with pytest.raises(ValidationError, match="extraction snapshot"):
        _run_manifest(
            RunPhase.EXTRACT,
            ApplicationRunRecord(
                application_id="app-1",
                artifact_ids=("artifact-1",),
                status=RunStatus.COMPLETE,
            ),
        )
    with pytest.raises(ValidationError, match="extraction lineage"):
        _run_manifest(
            RunPhase.ANALYZE,
            ApplicationRunRecord(
                application_id="app-1",
                artifact_ids=("artifact-1",),
                extraction_snapshots=(snapshot,),
                status=RunStatus.COMPLETE,
            ),
        )
    with pytest.raises(ValidationError, match="report model"):
        _run_manifest(RunPhase.REPORT)


def test_complete_run_rejects_partial_application_records() -> None:
    with pytest.raises(ValidationError, match="incomplete applications"):
        RunManifest(
            run_id="stage-run",
            phase=RunPhase.STAGE,
            status=RunStatus.COMPLETE,
            started_at=NOW,
            completed_at=NOW,
            applications=(
                ApplicationRunRecord(
                    application_id="app-1",
                    artifact_ids=("artifact-1",),
                    status=RunStatus.PARTIAL,
                    warnings=("staging incomplete",),
                ),
            ),
        )


def _store(tmp_path: Path) -> V2StateStore:
    workspace = tmp_path / "workspace"
    initialize_workspace(workspace, created_at=NOW, workspace_id="workspace-1")
    return V2StateStore(workspace)


def _record() -> EvidenceRecord:
    return EvidenceRecord(
        application_id="app-1",
        artifact_id="artifact-1",
        artifact_sha256=DIGEST_A,
        fact_type="query_definition",
        observation="SELECT * FROM dbo.Deal; credential PWD=topsecret",
    )


def _snapshot() -> ExtractedArtifactSnapshot:
    return ExtractedArtifactSnapshot(
        application_id="app-1",
        artifact_id="artifact-1",
        artifact_sha256=DIGEST_A,
        extractor_version="test-extractor-v1",
        extraction_policy_version=EXTRACTION_POLICY_VERSION,
        extracted_at=NOW,
        coverage_status="complete",
    )


def _manifest(
    snapshot: ContentReference,
    *,
    warnings: tuple[str, ...] = (),
) -> RunManifest:
    return RunManifest(
        run_id="run-1",
        phase=RunPhase.EXTRACT,
        status=RunStatus.COMPLETE,
        started_at=NOW,
        completed_at=NOW,
        applications=(
            ApplicationRunRecord(
                application_id="app-1",
                artifact_ids=("artifact-1",),
                extraction_snapshots=(snapshot,),
                status=RunStatus.COMPLETE,
            ),
        ),
        warnings=warnings,
    )


def _run_manifest(
    phase: RunPhase,
    application: ApplicationRunRecord | None = None,
) -> RunManifest:
    return RunManifest(
        run_id=f"{phase.value}-run",
        phase=phase,
        status=RunStatus.COMPLETE,
        started_at=NOW,
        completed_at=NOW,
        applications=() if application is None else (application,),
    )
