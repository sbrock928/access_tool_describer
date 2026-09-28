"""Typed configuration and workspace layout."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict


class AnalyzerSettings(BaseModel):
    model_config = ConfigDict(frozen=True)
    workspace: Path

    @property
    def source_inventory_dir(self) -> Path:
        return self.workspace / "source_inventory"

    @property
    def staged_tools_dir(self) -> Path:
        return self.workspace / "staged_tools"

    @property
    def shared_libraries_dir(self) -> Path:
        """Curated local Access libraries available to disposable extraction bundles."""
        return self.staged_tools_dir / "shared_libraries"

    @property
    def extracted_dir(self) -> Path:
        return self.workspace / "extracted"

    @property
    def reports_dir(self) -> Path:
        return self.workspace / "reports"

    def ensure_workspace(self) -> None:
        for directory in (
            self.source_inventory_dir,
            self.staged_tools_dir,
            self.shared_libraries_dir,
            self.extracted_dir,
            self.reports_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
