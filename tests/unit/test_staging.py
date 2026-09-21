from pathlib import Path

import pytest

from portfolio_analyzer.config import AnalyzerSettings
from portfolio_analyzer.models import ArtifactStatus, InventoryRecord
from portfolio_analyzer.staging.copying import ArtifactStager
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
