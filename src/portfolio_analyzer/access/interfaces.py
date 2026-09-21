"""Contracts that make unsafe bare-path extraction impossible."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from portfolio_analyzer.models import ExtractedApplication, StagedArtifact


class AccessExtractor(Protocol):
    version: str

    def extract(self, artifact: StagedArtifact, destination: Path) -> ExtractedApplication:
        """Extract static definitions from a verified locally staged artifact."""
