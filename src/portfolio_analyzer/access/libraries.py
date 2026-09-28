"""Explicit, hash-pinned Access library selection for disposable export copies."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator

from portfolio_analyzer.config import AnalyzerSettings
from portfolio_analyzer.library_identity import ApprovedLibraryReference
from portfolio_analyzer.staging.hashing import sha256_file

LIBRARY_MANIFEST_SCHEMA = "access-library-manifest-v1"


class ApprovedLibrary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    sha256: str
    reason: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        normalized = PurePosixPath(value.replace("\\", "/"))
        if normalized.is_absolute() or ".." in normalized.parts or not normalized.name:
            raise ValueError("library path must be a normalized relative path")
        if normalized.suffix.casefold() not in {".accdb", ".mdb"}:
            raise ValueError("approved libraries must be .accdb or .mdb files")
        return normalized.as_posix()

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        normalized = value.casefold()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("library sha256 must be a 64-character hexadecimal digest")
        return normalized


class ApprovedLibraryManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = LIBRARY_MANIFEST_SCHEMA
    applications: dict[str, list[ApprovedLibrary]] = Field(default_factory=dict)

    @field_validator("schema_version")
    @classmethod
    def validate_schema(cls, value: str) -> str:
        if value != LIBRARY_MANIFEST_SCHEMA:
            raise ValueError(f"unsupported Access library manifest schema: {value}")
        return value


@dataclass(frozen=True, slots=True)
class VerifiedApprovedLibrary:
    """Verified local path plus the path-free identity safe for durable state."""

    path: Path
    reference: ApprovedLibraryReference


def load_approved_libraries(
    settings: AnalyzerSettings,
    application_id: str,
    *,
    primary_filename: str,
) -> list[Path]:
    """Return verified staged libraries for one application; absence means no libraries."""
    return [
        item.path
        for item in resolve_approved_libraries(
            settings,
            application_id,
            primary_filename=primary_filename,
        )
    ]


def resolve_approved_libraries(
    settings: AnalyzerSettings,
    application_id: str,
    *,
    primary_filename: str,
) -> tuple[VerifiedApprovedLibrary, ...]:
    """Resolve and verify an application's non-transitive, hash-pinned libraries."""

    manifest_path = settings.source_inventory_dir / "access_libraries.json"
    if not manifest_path.exists():
        return ()
    if manifest_path.is_symlink():
        raise ValueError("Access library manifest must not be a symbolic link")
    try:
        raw = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
        )
        manifest = ApprovedLibraryManifest.model_validate(raw)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"Access library manifest is invalid: {manifest_path.name}") from exc

    root = settings.shared_libraries_dir.resolve()
    filenames = {unicodedata.normalize("NFC", primary_filename).casefold()}
    selected: list[VerifiedApprovedLibrary] = []
    for entry in manifest.applications.get(application_id, []):
        configured_candidate = root / entry.path
        if _path_contains_symlink(root, configured_candidate):
            raise ValueError(f"approved library is a symbolic link: {entry.path}")
        candidate = configured_candidate.resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("approved library resolves outside shared_libraries") from exc
        if not candidate.is_file():
            raise ValueError(f"approved library is missing or unsafe: {entry.path}")
        reference = ApprovedLibraryReference(
            filename=candidate.name,
            sha256=entry.sha256,
        )
        filename = reference.filename.casefold()
        if filename in filenames:
            raise ValueError(f"approved library filename collision: {candidate.name}")
        filenames.add(filename)
        if sha256_file(candidate) != entry.sha256:
            raise ValueError(f"approved library hash mismatch: {entry.path}")
        selected.append(
            VerifiedApprovedLibrary(
                path=candidate,
                reference=reference,
            )
        )
    return tuple(sorted(selected, key=lambda item: item.reference.library_id))


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("Access library manifest contains a duplicate mapping key")
        output[key] = value
    return output


def _path_contains_symlink(root: Path, candidate: Path) -> bool:
    """Reject a symlink or junction-like lexical component before resolving it."""

    current = root
    for part in candidate.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            return True
    return False
