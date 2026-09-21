"""Typed configuration and workspace layout."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class AnalyzerSettings(BaseModel):
    model_config = ConfigDict(frozen=True)
    workspace: Path
    supporting_file_depth: int = Field(default=1, ge=0, le=3)
    supporting_extensions: frozenset[str] = frozenset(
        {
            ".ini",
            ".cfg",
            ".config",
            ".vbs",
            ".bat",
            ".cmd",
            ".sql",
            ".csv",
            ".xlsx",
            ".xlsm",
            ".dll",
            ".txt",
        }
    )

    @property
    def source_inventory_dir(self) -> Path:
        return self.workspace / "source_inventory"

    @property
    def staged_tools_dir(self) -> Path:
        return self.workspace / "staged_tools"

    @property
    def extracted_dir(self) -> Path:
        return self.workspace / "extracted"

    @property
    def analysis_dir(self) -> Path:
        return self.workspace / "analysis"

    @property
    def reports_dir(self) -> Path:
        return self.workspace / "reports"

    @property
    def logs_dir(self) -> Path:
        return self.workspace / "logs"

    def ensure_workspace(self) -> None:
        for directory in (
            self.source_inventory_dir,
            self.staged_tools_dir,
            self.extracted_dir,
            self.analysis_dir,
            self.reports_dir,
            self.logs_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
