import json
from pathlib import Path

import pytest

from portfolio_analyzer.access.libraries import (
    LIBRARY_MANIFEST_SCHEMA,
    load_approved_libraries,
    resolve_approved_libraries,
)
from portfolio_analyzer.config import AnalyzerSettings
from portfolio_analyzer.library_identity import ApprovedLibraryReference
from portfolio_analyzer.staging.hashing import sha256_file
from portfolio_analyzer.v2.identity import canonical_json_bytes


def _settings(tmp_path: Path) -> AnalyzerSettings:
    settings = AnalyzerSettings(workspace=tmp_path / "workspace")
    settings.ensure_workspace()
    return settings


def _write_manifest(
    settings: AnalyzerSettings,
    application_id: str,
    entries: list[dict[str, str]],
) -> None:
    manifest = {
        "schema_version": LIBRARY_MANIFEST_SCHEMA,
        "applications": {application_id: entries},
    }
    (settings.source_inventory_dir / "access_libraries.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )


def test_library_selection_defaults_to_none_without_manifest(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    unapproved = settings.shared_libraries_dir / "unapproved.accdb"
    unapproved.write_bytes(b"present but not approved")

    selected = load_approved_libraries(
        settings, "app-1", primary_filename="main.accdb"
    )

    assert selected == []


def test_library_manifest_requires_the_pinned_hash(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    library = settings.shared_libraries_dir / "approved" / "common.accdb"
    library.parent.mkdir()
    library.write_bytes(b"reviewed library")
    _write_manifest(
        settings,
        "app-1",
        [
            {
                "path": "approved/common.accdb",
                "sha256": sha256_file(library),
                "reason": "Required VBA reference",
            }
        ],
    )

    selected = load_approved_libraries(
        settings, "app-1", primary_filename="main.accdb"
    )

    assert selected == [library.resolve()]
    resolved = resolve_approved_libraries(
        settings, "app-1", primary_filename="main.accdb"
    )
    assert len(resolved) == 1
    assert resolved[0].reference.filename == "common.accdb"
    assert resolved[0].reference.sha256 == sha256_file(library)
    assert str(settings.workspace) not in resolved[0].reference.model_dump_json()

    library.write_bytes(b"changed after approval")
    with pytest.raises(ValueError, match="approved library hash mismatch"):
        load_approved_libraries(settings, "app-1", primary_filename="main.accdb")


def test_library_manifest_rejects_primary_filename_collision(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    library = settings.shared_libraries_dir / "approved" / "Main.ACCDB"
    library.parent.mkdir()
    library.write_bytes(b"not the primary")
    _write_manifest(
        settings,
        "app-1",
        [
            {
                "path": "approved/Main.ACCDB",
                "sha256": sha256_file(library),
                "reason": "Bad collision",
            }
        ],
    )

    with pytest.raises(ValueError, match="approved library filename collision"):
        load_approved_libraries(settings, "app-1", primary_filename="main.accdb")


def test_library_manifest_rejects_library_to_library_filename_collision(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    first = settings.shared_libraries_dir / "first" / "Common.accdb"
    second = settings.shared_libraries_dir / "second" / "common.ACCDB"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    _write_manifest(
        settings,
        "app-1",
        [
            {
                "path": "first/Common.accdb",
                "sha256": sha256_file(first),
                "reason": "First reference",
            },
            {
                "path": "second/common.ACCDB",
                "sha256": sha256_file(second),
                "reason": "Conflicting reference",
            },
        ],
    )

    with pytest.raises(ValueError, match="approved library filename collision"):
        load_approved_libraries(settings, "app-1", primary_filename="main.accdb")


def test_library_manifest_rejects_duplicate_application_mapping_keys(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    manifest = settings.source_inventory_dir / "access_libraries.json"
    manifest.write_text(
        '{"schema_version":"access-library-manifest-v1","applications":'
        '{"app-1":[],"app-1":[]}}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid"):
        load_approved_libraries(settings, "app-1", primary_filename="main.accdb")


def test_library_manifest_rejects_configured_symlink(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    target = settings.shared_libraries_dir / "real.accdb"
    target.write_bytes(b"reviewed")
    link = settings.shared_libraries_dir / "linked.accdb"
    link.symlink_to(target)
    _write_manifest(
        settings,
        "app-1",
        [
            {
                "path": link.name,
                "sha256": sha256_file(target),
                "reason": "Symlinks are not stable reviewed identities",
            }
        ],
    )

    with pytest.raises(ValueError, match="symbolic link"):
        resolve_approved_libraries(
            settings,
            "app-1",
            primary_filename="main.accdb",
        )


def test_library_reference_is_strict_and_canonical_json_round_trips() -> None:
    reference = ApprovedLibraryReference(
        filename="e\N{COMBINING ACUTE ACCENT}.accdb",
        sha256="A" * 64,
    )

    assert reference.filename == "\N{LATIN SMALL LETTER E WITH ACUTE}.accdb"
    assert reference.sha256 == "a" * 64
    assert ApprovedLibraryReference.model_validate_json(
        canonical_json_bytes(reference)
    ) == reference
    with pytest.raises(ValueError, match="credential-shaped"):
        ApprovedLibraryReference(filename="PWD=secret.accdb", sha256="a" * 64)
    with pytest.raises(ValueError):
        ApprovedLibraryReference.model_validate(
            {"filename": b"bytes.accdb", "sha256": "a" * 64}
        )
