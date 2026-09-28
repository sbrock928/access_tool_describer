from pathlib import Path

import pytest
from openpyxl import Workbook

from portfolio_analyzer.config import AnalyzerSettings
from portfolio_analyzer.inventory.loader import InventoryValidationError, load_inventory
from portfolio_analyzer.models import ArtifactStatus, InventoryRecord
from portfolio_analyzer.staging.copying import ArtifactStager
from portfolio_analyzer.staging.validation import (
    UnsafeArtifactError,
    assert_trusted_staged_artifact,
)


def _record(
    source: Path,
    *,
    tool_id: str = "access-1",
    tool_name: str = "Sample Access Application",
) -> InventoryRecord:
    return InventoryRecord(
        tool_inventory_id=tool_id,
        tool_name=tool_name,
        inventory_filename=source.name,
        filepath=source,
    )


def _settings(tmp_path: Path) -> AnalyzerSettings:
    settings = AnalyzerSettings(workspace=tmp_path / "workspace")
    settings.ensure_workspace()
    return settings


def test_inventory_rejects_filename_and_fullpath_basename_mismatch(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["INVENTORY_ID", "EUCTNAME", "FILE_NAME", "FULLPATH", "DESCRIPTION"])
    sheet.append(
        [
            "41",
            "Mismatch",
            "expected.accdb",
            str(tmp_path / "source" / "actual.accdb"),
            "Mismatch must be explicit",
        ]
    )
    inventory_path = tmp_path / "inventory.xlsx"
    workbook.save(inventory_path)

    with pytest.raises(InventoryValidationError, match="does not match the FULLPATH basename"):
        load_inventory(inventory_path)


def test_inventory_rejects_relative_and_traversing_source_paths(tmp_path: Path) -> None:
    for source in ("relative/main.accdb", "/reviewed/../other/main.accdb"):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(
            ["INVENTORY_ID", "EUCTNAME", "FILE_NAME", "FULLPATH", "DESCRIPTION"]
        )
        sheet.append(["41", "Unsafe", "main.accdb", source, "Unsafe path"])
        inventory_path = tmp_path / f"inventory-{len(source)}.xlsx"
        workbook.save(inventory_path)

        with pytest.raises(InventoryValidationError, match="absolute, traversal-free"):
            load_inventory(inventory_path)


def test_non_access_primary_is_explicitly_skipped_without_copying_bytes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source" / "workbook.xlsx"
    source.parent.mkdir()
    source.write_bytes(b"non-access-canary")
    settings = _settings(tmp_path)

    artifact = ArtifactStager(settings).stage_primary(_record(source))

    assert artifact.status == ArtifactStatus.SKIPPED_UNSUPPORTED_FORMAT
    assert artifact.local_staged_path is None
    assert artifact.sha256 is None
    assert artifact.error is not None
    assert "only .accdb and .mdb are eligible" in artifact.error
    assert list(settings.staged_tools_dir.rglob("*.xlsx")) == []
    assert source.read_bytes() == b"non-access-canary"


def test_application_bundle_copies_only_inventory_listed_primary(tmp_path: Path) -> None:
    source = tmp_path / "source" / "main.accdb"
    source.parent.mkdir()
    source.write_bytes(b"primary")
    (source.parent / "sibling.accdb").write_bytes(b"must-not-be-copied")
    (source.parent / "credentials.xlsx").write_bytes(b"must-not-be-copied")
    nested = source.parent / "nested"
    nested.mkdir()
    (nested / "script.sql").write_bytes(b"must-not-be-copied")
    settings = _settings(tmp_path)

    artifacts = ArtifactStager(settings).stage_application_bundle(_record(source))

    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.status == ArtifactStatus.STAGED
    assert artifact.local_staged_path is not None
    staged_files = sorted(
        path.resolve() for path in settings.staged_tools_dir.rglob("*") if path.is_file()
    )
    assert staged_files == [artifact.local_staged_path.resolve()]
    assert artifact.local_staged_path.read_bytes() == b"primary"


def test_artifact_ids_distinguish_same_name_sources_with_identical_bytes(
    tmp_path: Path,
) -> None:
    first_source = tmp_path / "source-a" / "main.accdb"
    second_source = tmp_path / "source-b" / "main.accdb"
    first_source.parent.mkdir()
    second_source.parent.mkdir()
    first_source.write_bytes(b"identical-content")
    second_source.write_bytes(b"identical-content")
    settings = _settings(tmp_path)
    stager = ArtifactStager(settings)

    first = stager.stage_primary(_record(first_source))
    second = stager.stage_primary(_record(second_source))

    assert first.status == second.status == ArtifactStatus.STAGED
    assert first.sha256 == second.sha256
    assert first.artifact_id != second.artifact_id
    assert first.local_staged_path is not None
    assert second.local_staged_path is not None
    assert first.local_staged_path != second.local_staged_path
    assert first.local_staged_path.parents[1].name == first.artifact_id
    assert second.local_staged_path.parents[1].name == second.artifact_id
    assert first.local_staged_path.parent.name == first.sha256
    assert second.local_staged_path.parent.name == second.sha256


def test_stager_rejects_workspace_sources_and_symbolic_links(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    workspace_source = settings.source_inventory_dir / "inside.accdb"
    workspace_source.write_bytes(b"workspace")

    workspace_artifact = ArtifactStager(settings).stage_primary(_record(workspace_source))

    assert workspace_artifact.status == ArtifactStatus.FAILED
    assert workspace_artifact.error is not None
    assert "workspace cannot be used as an inventory source" in workspace_artifact.error

    outside_source = tmp_path / "outside" / "real.accdb"
    outside_source.parent.mkdir()
    outside_source.write_bytes(b"real")
    source_link = tmp_path / "outside" / "alias.accdb"
    source_link.symlink_to(outside_source)

    symlink_artifact = ArtifactStager(settings).stage_primary(_record(source_link))

    assert symlink_artifact.status == ArtifactStatus.FAILED
    assert symlink_artifact.error is not None
    assert "Symbolic-link sources are not allowed" in symlink_artifact.error


def test_trusted_staging_rejects_contained_symbolic_link(tmp_path: Path) -> None:
    source = tmp_path / "source" / "main.accdb"
    source.parent.mkdir()
    source.write_bytes(b"primary")
    settings = _settings(tmp_path)
    artifact = ArtifactStager(settings).stage_primary(_record(source))
    assert artifact.local_staged_path is not None
    staged_path = artifact.local_staged_path
    replacement = staged_path.with_name("replacement.accdb")
    replacement.write_bytes(staged_path.read_bytes())
    staged_path.unlink()
    staged_path.symlink_to(replacement)

    with pytest.raises(UnsafeArtifactError, match="symbolic link"):
        assert_trusted_staged_artifact(artifact, settings)
