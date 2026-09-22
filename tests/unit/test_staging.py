from pathlib import Path

import pytest

from portfolio_analyzer.config import AnalyzerSettings
from portfolio_analyzer.models import ArtifactStatus, InventoryRecord
from portfolio_analyzer.staging.copying import (
    ArtifactStager,
    migrate_legacy_application_directories,
)
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


def test_application_bundle_preserves_relative_layout_but_marks_only_primary(
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

    assert len(artifacts) == 3
    primary = next(artifact for artifact in artifacts if artifact.is_primary)
    assert primary.local_staged_path is not None
    assert primary.local_staged_path.name == "main.accdb"
    assert (primary.local_staged_path.parent / "shared.accdb").exists()
    assert (primary.local_staged_path.parent / "templates" / "report.xlsx").exists()
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


def test_legacy_inventory_id_folders_are_renamed_to_euc_name(tmp_path: Path) -> None:
    settings = AnalyzerSettings(workspace=tmp_path / "workspace")
    settings.ensure_workspace()
    (settings.staged_tools_dir / "42").mkdir()
    (settings.extracted_dir / "42").mkdir()
    application = InventoryRecord(
        tool_inventory_id="42",
        tool_name="Payments EUC",
        inventory_filename="payments.accdb",
        filepath=Path("/source/payments.accdb"),
    )

    messages = migrate_legacy_application_directories(settings, [application])

    assert (settings.staged_tools_dir / "Payments EUC").is_dir()
    assert (settings.extracted_dir / "Payments EUC").is_dir()
    assert len(messages) == 2
