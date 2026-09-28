"""The only component permitted to read source locations for copying."""

from __future__ import annotations

import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from portfolio_analyzer.config import AnalyzerSettings
from portfolio_analyzer.models import ArtifactStatus, InventoryRecord, StagedArtifact
from portfolio_analyzer.naming import euc_directory_name
from portfolio_analyzer.staging.hashing import sha256_file
from portfolio_analyzer.v2.identity import opaque_source_identity, stable_id

ACCESS_PRIMARY_EXTENSIONS = frozenset({".accdb", ".mdb"})


class ArtifactStager:
    """Copies source files into an owned local workspace; never analyzes sources."""

    def __init__(self, settings: AnalyzerSettings) -> None:
        self.settings = settings

    def stage_primary(self, record: InventoryRecord) -> StagedArtifact:
        extension = Path(record.inventory_filename).suffix.casefold()
        if extension not in ACCESS_PRIMARY_EXTENSIONS:
            return StagedArtifact(
                tool_inventory_id=record.tool_inventory_id,
                original_source_path=record.filepath,
                filename=record.inventory_filename,
                extension=extension,
                status=ArtifactStatus.SKIPPED_UNSUPPORTED_FORMAT,
                error=(
                    f"Unsupported primary format '{extension or '<none>'}'; "
                    "only .accdb and .mdb are eligible"
                ),
                is_primary=True,
            )
        return self._stage(record, record.filepath, is_primary=True)

    def stage_application_bundle(self, record: InventoryRecord) -> list[StagedArtifact]:
        """Stage only the inventory-listed Access primary.

        Dependencies are discovered from static Access evidence. Recursively mirroring the source
        directory would copy unrelated or sensitive files and blur the source/staging boundary.
        """
        return [self.stage_primary(record)]

    def stage_supporting(self, record: InventoryRecord) -> list[StagedArtifact]:
        """Return non-primary artifacts from the full application bundle."""
        return []

    def _stage(
        self,
        record: InventoryRecord,
        source: Path,
        *,
        is_primary: bool,
        relative_path: Path | None = None,
    ) -> StagedArtifact:
        filename = source.name
        artifact_id = stable_id(
            "artifact",
            record.tool_inventory_id,
            opaque_source_identity(str(source)),
        )
        target_dir = self.settings.staged_tools_dir / euc_directory_name(record.tool_name)
        temporary: Path | None = None
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            if source.is_symlink():
                raise StagingValidationError("Symbolic-link sources are not allowed")
            if not source.is_file():
                raise StagingValidationError("Source file is missing or is not a regular file")
            resolved_source = source.resolve()
            resolved_workspace = self.settings.workspace.resolve()
            if resolved_source.is_relative_to(resolved_workspace):
                raise StagingValidationError(
                    "The analysis workspace cannot be used as an inventory source"
                )
            if source.suffix.casefold() not in ACCESS_PRIMARY_EXTENSIONS:
                raise StagingValidationError(
                    "Only .accdb and .mdb primary artifacts may be staged"
                )
            source_stat_before = source.stat()
            source_sha256 = sha256_file(source)
            target = (
                target_dir
                / "bundle"
                / artifact_id
                / source_sha256
                / (relative_path or Path(filename))
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            stage_root = self.settings.staged_tools_dir.resolve()
            if not target.parent.resolve().is_relative_to(stage_root):
                raise StagingValidationError("Unsafe staged destination")
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{filename}.", suffix=".tmp", dir=target.parent
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            shutil.copy2(source, temporary)
            source_stat = source.stat()
            target_stat = temporary.stat()
            if (
                source_stat.st_size != source_stat_before.st_size
                or source_stat.st_mtime_ns != source_stat_before.st_mtime_ns
            ):
                raise StagingValidationError("Source file changed while it was being staged")
            if target_stat.st_size != source_stat.st_size:
                raise StagingValidationError("Staged file size differs from source")
            target_sha256 = sha256_file(temporary)
            if target_sha256 != source_sha256:
                raise StagingValidationError("Staged file hash differs from source")
            with temporary.open("rb") as staged_file:
                os.fsync(staged_file.fileno())
            os.replace(temporary, target)
            temporary = None
            _fsync_directory(target.parent)
            return StagedArtifact(
                artifact_id=artifact_id,
                tool_inventory_id=record.tool_inventory_id,
                original_source_path=source,
                local_staged_path=target.resolve(),
                filename=filename,
                extension=source.suffix.lower(),
                size_bytes=target_stat.st_size,
                source_modified_at=datetime.fromtimestamp(source_stat.st_mtime, tz=UTC),
                sha256=target_sha256,
                status=ArtifactStatus.STAGED,
                is_primary=is_primary,
            )
        except StagingValidationError as exc:
            return StagedArtifact(
                artifact_id=artifact_id,
                tool_inventory_id=record.tool_inventory_id,
                original_source_path=source,
                filename=filename,
                extension=source.suffix.lower(),
                status=ArtifactStatus.FAILED,
                error=str(exc),
                is_primary=is_primary,
            )
        except OSError as exc:
            return StagedArtifact(
                artifact_id=artifact_id,
                tool_inventory_id=record.tool_inventory_id,
                original_source_path=source,
                filename=filename,
                extension=source.suffix.lower(),
                status=ArtifactStatus.FAILED,
                error=f"Staging copy failed safely ({type(exc).__name__})",
                is_primary=is_primary,
            )
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class StagingValidationError(OSError):
    """A safe staging failure message that contains no source or workspace path."""


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
