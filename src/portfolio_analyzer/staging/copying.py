"""The only component permitted to read source locations for copying."""

from __future__ import annotations

import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

from portfolio_analyzer.config import AnalyzerSettings
from portfolio_analyzer.models import ArtifactStatus, InventoryRecord, StagedArtifact
from portfolio_analyzer.staging.hashing import sha256_file


def safe_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "unnamed"


class ArtifactStager:
    """Copies source files into an owned local workspace; never analyzes sources."""

    def __init__(self, settings: AnalyzerSettings) -> None:
        self.settings = settings

    def stage_primary(self, record: InventoryRecord) -> StagedArtifact:
        return self._stage(record, record.filepath, is_primary=True)

    def stage_supporting(self, record: InventoryRecord) -> list[StagedArtifact]:
        """Stage same-directory, same-stem supporting files within configured rules."""
        try:
            candidates = sorted(record.filepath.parent.iterdir())
        except OSError:
            return []
        artifacts: list[StagedArtifact] = []
        for candidate in candidates:
            if (
                candidate == record.filepath
                or not candidate.is_file()
                or candidate.suffix.lower() not in self.settings.supporting_extensions
                or candidate.stem.casefold() != record.filepath.stem.casefold()
            ):
                continue
            artifacts.append(self._stage(record, candidate, is_primary=False))
        return artifacts

    def _stage(self, record: InventoryRecord, source: Path, *, is_primary: bool) -> StagedArtifact:
        filename = source.name
        target_dir = self.settings.staged_tools_dir / safe_component(record.tool_inventory_id)
        target = target_dir / ("primary" if is_primary else "supporting") / filename
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
