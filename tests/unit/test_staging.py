from pathlib import Path

import pytest

from portfolio_analyzer.config import AnalyzerSettings
from portfolio_analyzer.models import ArtifactStatus, InventoryRecord
from portfolio_analyzer.staging.copying import ArtifactStager, _fsync_directory
from portfolio_analyzer.staging.validation import (
    UnsafeArtifactError,
    assert_trusted_staged_artifact,
)


def record(source: Path) -> InventoryRecord:
    return InventoryRecord(
        tool_inventory_id="1",
        tool_name="Sample",
        inventory_filename=source.name,
        filepath=source,
    )


def test_stager_copies_and_hashes_without_using_source_for_analysis(tmp_path: Path) -> None:
    source = tmp_path / "network" / "sample.accdb"
    source.parent.mkdir()
    source.write_bytes(b"synthetic access fixture")
    settings = AnalyzerSettings(workspace=tmp_path / "workspace")
    settings.ensure_workspace()

    artifact = ArtifactStager(settings).stage_primary(record(source))

    assert artifact.status == ArtifactStatus.STAGED
    assert artifact.local_staged_path is not None
    assert artifact.local_staged_path != source
    assert artifact.local_staged_path.relative_to(settings.staged_tools_dir).parts[0] == "Sample"
    assert assert_trusted_staged_artifact(artifact, settings).read_bytes() == source.read_bytes()


def test_unsafe_source_artifact_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source.accdb"
    source.write_bytes(b"source")
    settings = AnalyzerSettings(workspace=tmp_path / "workspace")
    settings.ensure_workspace()
    artifact = ArtifactStager(settings).stage_primary(record(source))
    assert artifact.local_staged_path is not None
    artifact.local_staged_path = source

    with pytest.raises(UnsafeArtifactError, match="outside"):
        assert_trusted_staged_artifact(artifact, settings)


def test_changed_staged_artifact_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source.accdb"
    source.write_bytes(b"source")
    settings = AnalyzerSettings(workspace=tmp_path / "workspace")
    settings.ensure_workspace()
    artifact = ArtifactStager(settings).stage_primary(record(source))
    assert artifact.local_staged_path is not None
    artifact.local_staged_path.write_bytes(b"changed")

    with pytest.raises(UnsafeArtifactError, match="hash mismatch"):
        assert_trusted_staged_artifact(artifact, settings)


def test_restaging_changed_source_preserves_published_binary_version(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.accdb"
    source.write_bytes(b"version-one")
    settings = AnalyzerSettings(workspace=tmp_path / "workspace")
    settings.ensure_workspace()
    stager = ArtifactStager(settings)

    first = stager.stage_primary(record(source))
    assert first.local_staged_path is not None
    first_path = first.local_staged_path

    source.write_bytes(b"version-two")
    second = stager.stage_primary(record(source))

    assert second.local_staged_path is not None
    assert second.artifact_id == first.artifact_id
    assert second.sha256 != first.sha256
    assert second.local_staged_path != first_path
    assert first_path.read_bytes() == b"version-one"
    assert second.local_staged_path.read_bytes() == b"version-two"


def test_application_bundle_copies_only_inventory_primary(
    tmp_path: Path,
) -> None:
    source = tmp_path / "network" / "prod" / "main.accdb"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"primary")
    (source.parent / "shared.accdb").write_bytes(b"library")
    (source.parent / "templates").mkdir()
    (source.parent / "templates" / "report.xlsx").write_bytes(b"template")
    (source.parent / "main.laccdb").write_bytes(b"transient lock")
    settings = AnalyzerSettings(workspace=tmp_path / "workspace")
    settings.ensure_workspace()

    artifacts = ArtifactStager(settings).stage_application_bundle(record(source))

    assert len(artifacts) == 1
    primary = artifacts[0]
    assert primary.local_staged_path is not None
    assert primary.local_staged_path.name == "main.accdb"
    assert not any(settings.staged_tools_dir.rglob("shared.accdb"))
    assert not any(settings.staged_tools_dir.rglob("report.xlsx"))
    assert not (primary.local_staged_path.parent / "main.laccdb").exists()


def test_euc_folder_name_is_windows_safe_and_readable(tmp_path: Path) -> None:
    source = tmp_path / "source.accdb"
    source.write_bytes(b"source")
    settings = AnalyzerSettings(workspace=tmp_path / "workspace")
    settings.ensure_workspace()
    unsafe_name = InventoryRecord(
        tool_inventory_id="27",
        tool_name="Finance: Month/End",
        inventory_filename=source.name,
        filepath=source,
    )

    artifact = ArtifactStager(settings).stage_primary(unsafe_name)

    assert artifact.local_staged_path is not None
    assert artifact.local_staged_path.relative_to(settings.staged_tools_dir).parts[0] == (
        "Finance_ Month_End"
    )


def test_unsupported_directory_fsync_does_not_fail_staging_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[int] = []
    monkeypatch.setattr("portfolio_analyzer.staging.copying.os.open", lambda *_args: 42)

    def unsupported_fsync(_descriptor: int) -> None:
        raise OSError("directory fsync is unsupported")

    monkeypatch.setattr("portfolio_analyzer.staging.copying.os.fsync", unsupported_fsync)
    monkeypatch.setattr(
        "portfolio_analyzer.staging.copying.os.close", lambda descriptor: closed.append(descriptor)
    )

    _fsync_directory(tmp_path)

    assert closed == [42]
