from pathlib import Path

from portfolio_analyzer.access.windows_extractor import WindowsAccessExtractor
from portfolio_analyzer.config import AnalyzerSettings


def test_working_bundle_adds_unique_shared_access_library(tmp_path: Path) -> None:
    settings = AnalyzerSettings(workspace=tmp_path / "workspace")
    settings.ensure_workspace()
    primary = settings.staged_tools_dir / "tool-1" / "bundle" / "app" / "main.accdb"
    primary.parent.mkdir(parents=True)
    primary.write_bytes(b"primary")
    library = settings.staged_tools_dir / "library" / "bundle" / "EUC_AL.accdb"
    library.parent.mkdir(parents=True)
    library.write_bytes(b"shared library")

    working_database, working_bundle, additions, warnings = WindowsAccessExtractor(
        settings
    )._prepare_working_bundle(primary, settings.extracted_dir / "tool-1")

    assert working_database.read_bytes() == b"primary"
    assert (working_database.parent / "euc_al.accdb").read_bytes() == b"shared library"
    assert library.read_bytes() == b"shared library"
    assert working_bundle.parent == settings.extracted_dir / "tool-1"
    assert additions == ["Added shared workspace library 'euc_al.accdb' to temporary bundle."]
    assert warnings == []


def test_curated_shared_library_takes_precedence_over_bundle_copy(tmp_path: Path) -> None:
    settings = AnalyzerSettings(workspace=tmp_path / "workspace")
    settings.ensure_workspace()
    primary = settings.staged_tools_dir / "tool-1" / "bundle" / "main.accdb"
    primary.parent.mkdir(parents=True)
    primary.write_bytes(b"primary")
    bundle_library = settings.staged_tools_dir / "tool-2" / "bundle" / "EUC_AL.accdb"
    bundle_library.parent.mkdir(parents=True)
    bundle_library.write_bytes(b"other library")
    curated_library = settings.shared_libraries_dir / "EUC_AL.accdb"
    curated_library.write_bytes(b"approved library")

    extractor = WindowsAccessExtractor(settings)
    working_database, _, additions, warnings = extractor._prepare_working_bundle(
        primary, settings.extracted_dir / "tool-1"
    )

    assert (working_database.parent / "euc_al.accdb").read_bytes() == b"approved library"
    assert additions == ["Added curated shared library 'euc_al.accdb' to temporary bundle."]
    assert warnings == []
