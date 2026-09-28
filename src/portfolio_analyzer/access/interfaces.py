"""Contracts that make unsafe bare-path extraction impossible."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from portfolio_analyzer.models import AccessExtractionResult, VerifiedStagedArtifact


class AccessExtractor(Protocol):
    version: str

    def extract(
        self, artifact: VerifiedStagedArtifact, destination: Path
    ) -> AccessExtractionResult:
        """Extract static definitions from a verified locally staged artifact."""
