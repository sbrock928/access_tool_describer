from pathlib import Path

from portfolio_analyzer.access.windows_extractor import WindowsAccessExtractor
from portfolio_analyzer.config import AnalyzerSettings


def test_working_bundle_contains_only_primary_and_curated_shared_libraries(tmp_path: Path) -> None:
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
    )._prepare_working_bundle(primary, settings.extracted_dir / "tool-1")

    assert working_database.read_bytes() == b"primary"
    assert (working_bundle / "euc_al.accdb").read_bytes() == b"shared library"
    assert not (working_bundle / "unrelated.accdb").exists()
    assert library.read_bytes() == b"shared library"
    assert working_bundle.parent == settings.extracted_dir / "tool-1"
    assert additions == ["Added curated shared library 'euc_al.accdb' to temporary bundle."]
    assert warnings == []


def test_duplicate_curated_shared_library_is_not_guessed(tmp_path: Path) -> None:
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
        primary, settings.extracted_dir / "tool-1"
    )

    assert working_database.read_bytes() == b"primary"
    assert not (working_database.parent / "euc_al.accdb").exists()
    assert additions == []
    assert warnings == [
        "Curated shared library 'euc_al.accdb' was not added: 2 different versions were found."
    ]
