"""The only component permitted to read source locations for copying."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

from portfolio_analyzer.config import AnalyzerSettings
from portfolio_analyzer.models import ArtifactStatus, InventoryRecord, StagedArtifact
from portfolio_analyzer.naming import euc_directory_name, legacy_inventory_directory_name
from portfolio_analyzer.staging.hashing import sha256_file


class ArtifactStager:
    """Copies source files into an owned local workspace; never analyzes sources."""

    _TRANSIENT_LOCK_EXTENSIONS = frozenset({".laccdb", ".ldb"})

    def __init__(self, settings: AnalyzerSettings) -> None:
        self.settings = settings

    def stage_primary(self, record: InventoryRecord) -> StagedArtifact:
        return self._stage(record, record.filepath, is_primary=True)

    def stage_application_bundle(self, record: InventoryRecord) -> list[StagedArtifact]:
        """Mirror all regular source-folder files so locally resolved references remain available.

        The inventory-listed file is the sole primary artifact. Sibling databases are copied only as
        supporting artifacts and are never selected as a primary database by extraction.
        """
        root = record.filepath.parent
        primary = record.filepath.resolve()
        try:
            candidates = sorted(path for path in root.rglob("*") if self._should_copy(path))
        except OSError:
            return [self.stage_primary(record)]
        if not any(path.resolve() == primary for path in candidates):
            candidates.append(record.filepath)
        return [
            self._stage(
                record,
                candidate,
                is_primary=candidate.resolve() == primary,
                relative_path=candidate.relative_to(root),
            )
            for candidate in candidates
        ]

    def stage_supporting(self, record: InventoryRecord) -> list[StagedArtifact]:
        """Return non-primary artifacts from the full application bundle."""
        return [
            artifact
            for artifact in self.stage_application_bundle(record)
            if not artifact.is_primary
        ]

    def _stage(
        self,
        record: InventoryRecord,
        source: Path,
        *,
        is_primary: bool,
        relative_path: Path | None = None,
    ) -> StagedArtifact:
        filename = source.name
        target_dir = self.settings.staged_tools_dir / euc_directory_name(record.tool_name)
        target = target_dir / "bundle" / (relative_path or Path(filename))
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            if not source.is_file():
                raise FileNotFoundError(f"Source file does not exist: {source}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            source_stat = source.stat()
            target_stat = target.stat()
            if target_stat.st_size != source_stat.st_size:
                raise OSError("Staged file size differs from source")
            return StagedArtifact(
                tool_inventory_id=record.tool_inventory_id,
                original_source_path=source,
                local_staged_path=target.resolve(),
                filename=filename,
                extension=source.suffix.lower(),
                size_bytes=target_stat.st_size,
                source_modified_at=datetime.fromtimestamp(source_stat.st_mtime, tz=UTC),
                sha256=sha256_file(target),
                status=ArtifactStatus.STAGED,
                is_primary=is_primary,
            )
        except OSError as exc:
            return StagedArtifact(
                tool_inventory_id=record.tool_inventory_id,
                original_source_path=source,
                filename=filename,
                extension=source.suffix.lower(),
                status=ArtifactStatus.FAILED,
                error=str(exc),
                is_primary=is_primary,
            )

    def _should_copy(self, candidate: Path) -> bool:
        """Copy source artifacts, not active Access/Office lock files or symbolic links."""
        return (
            candidate.is_file()
            and not candidate.is_symlink()
            and candidate.suffix.casefold() not in self._TRANSIENT_LOCK_EXTENSIONS
            and not candidate.name.startswith("~$")
        )


def migrate_legacy_application_directories(
    settings: AnalyzerSettings, records: list[InventoryRecord]
) -> list[str]:
    """Rename unambiguous inventory-ID directories from earlier analyzer versions."""
    messages: list[str] = []
    for record in records:
        legacy_name = legacy_inventory_directory_name(record.tool_inventory_id)
        euc_name = euc_directory_name(record.tool_name)
        if legacy_name.casefold() == euc_name.casefold():
            continue
        for root in (settings.staged_tools_dir, settings.extracted_dir):
            legacy = root / legacy_name
            destination = root / euc_name
            if not legacy.is_dir() or legacy.is_symlink():
                continue
            if destination.exists():
                messages.append(
                    f"Could not migrate legacy folder '{legacy}': destination already exists."
                )
                continue
            legacy.rename(destination)
            messages.append(f"Migrated legacy folder '{legacy.name}' to '{destination.name}'.")
    return messages
