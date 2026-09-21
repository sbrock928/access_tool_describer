"""Explicit, reviewable Access automation guardrails."""

from __future__ import annotations

import platform
from pathlib import Path

from portfolio_analyzer.config import AnalyzerSettings
from portfolio_analyzer.models import StagedArtifact
from portfolio_analyzer.staging.validation import assert_trusted_staged_artifact


class WindowsAccessUnavailable(RuntimeError):
    pass


def validate_access_extraction_request(
    artifact: StagedArtifact, destination: Path, settings: AnalyzerSettings
) -> Path:
    if platform.system() != "Windows":
        raise WindowsAccessUnavailable("Access extraction requires Windows with Microsoft Access")
    local_path = assert_trusted_staged_artifact(artifact, settings)
    destination.resolve().relative_to(settings.extracted_dir.resolve())
    return local_path
