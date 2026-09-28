import hashlib
from pathlib import Path

import pytest

from portfolio_analyzer.access.libraries import VerifiedApprovedLibrary
from portfolio_analyzer.access.windows_extractor import (
    AccessExtractionError,
    WindowsAccessExtractor,
)
from portfolio_analyzer.config import AnalyzerSettings
from portfolio_analyzer.library_identity import ApprovedLibraryReference
from portfolio_analyzer.staging.hashing import sha256_file


def test_working_bundle_defaults_to_primary_only(tmp_path: Path) -> None:
    settings = AnalyzerSettings(workspace=tmp_path / "workspace")
    settings.ensure_workspace()
    primary = settings.staged_tools_dir / "tool-1" / "bundle" / "app" / "main.accdb"
    primary.parent.mkdir(parents=True)
    primary.write_bytes(b"primary")
    (primary.parent / "unrelated.accdb").write_bytes(b"unrelated tool")
    library = settings.shared_libraries_dir / "EUC_AL.accdb"
    library.parent.mkdir(parents=True, exist_ok=True)
    library.write_bytes(b"shared library")

    working_database, working_bundle, additions, warnings = WindowsAccessExtractor(
        settings
    )._prepare_working_bundle(
        primary,
        settings.extracted_dir / "tool-1",
        expected_primary_sha256=sha256_file(primary),
        expected_primary_size_bytes=primary.stat().st_size,
    )

    assert working_database.read_bytes() == b"primary"
    assert not (working_bundle / "euc_al.accdb").exists()
    assert not (working_bundle / "unrelated.accdb").exists()
    assert library.read_bytes() == b"shared library"
    assert working_bundle.parent == settings.extracted_dir / "tool-1"
    assert additions == []
    assert warnings == []


def test_unmanifested_curated_shared_libraries_are_not_guessed(tmp_path: Path) -> None:
    settings = AnalyzerSettings(workspace=tmp_path / "workspace")
    settings.ensure_workspace()
    primary = settings.staged_tools_dir / "tool-1" / "bundle" / "main.accdb"
    primary.parent.mkdir(parents=True)
    primary.write_bytes(b"primary")
    curated_library = settings.shared_libraries_dir / "first" / "EUC_AL.accdb"
    curated_library.parent.mkdir(parents=True)
    curated_library.write_bytes(b"approved library")
    alternate_library = settings.shared_libraries_dir / "second" / "EUC_AL.accdb"
    alternate_library.parent.mkdir(parents=True)
    alternate_library.write_bytes(b"other library")

    extractor = WindowsAccessExtractor(settings)
    working_database, _, additions, warnings = extractor._prepare_working_bundle(
        primary,
        settings.extracted_dir / "tool-1",
        expected_primary_sha256=sha256_file(primary),
        expected_primary_size_bytes=primary.stat().st_size,
    )

    assert working_database.read_bytes() == b"primary"
    assert not (working_database.parent / "euc_al.accdb").exists()
    assert additions == []
    assert warnings == []


def test_working_bundle_verifies_primary_against_pinned_identity_and_cleans(
    tmp_path: Path,
) -> None:
    settings = AnalyzerSettings(workspace=tmp_path / "workspace")
    settings.ensure_workspace()
    primary = settings.staged_tools_dir / "tool-1" / "main.accdb"
    primary.parent.mkdir(parents=True)
    primary.write_bytes(b"changed!")
    destination = settings.extracted_dir / "tool-1"

    with pytest.raises(AccessExtractionError, match="pinned size/hash"):
        WindowsAccessExtractor(settings)._prepare_working_bundle(
            primary,
            destination,
            expected_primary_sha256=hashlib.sha256(b"original").hexdigest(),
            expected_primary_size_bytes=len(b"original"),
        )

    assert list(destination.glob("_working_bundle-*")) == []


def test_working_bundle_failure_while_copying_library_removes_raw_primary(
    tmp_path: Path,
) -> None:
    settings = AnalyzerSettings(workspace=tmp_path / "workspace")
    settings.ensure_workspace()
    primary = settings.staged_tools_dir / "tool-1" / "main.accdb"
    primary.parent.mkdir(parents=True)
    primary.write_bytes(b"primary")
    library = settings.shared_libraries_dir / "reviewed.accdb"
    library.write_bytes(b"changed library")
    approved = VerifiedApprovedLibrary(
        path=library,
        reference=ApprovedLibraryReference(
            filename=library.name,
            sha256="a" * 64,
        ),
    )
    destination = settings.extracted_dir / "tool-1"

    with pytest.raises(AccessExtractionError, match="library copy failed"):
        WindowsAccessExtractor(settings)._prepare_working_bundle(
            primary,
            destination,
            approved_libraries=(approved,),
            expected_primary_sha256=sha256_file(primary),
            expected_primary_size_bytes=primary.stat().st_size,
        )

    assert list(destination.glob("_working_bundle-*")) == []
