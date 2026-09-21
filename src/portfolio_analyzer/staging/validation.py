"""Safety checks that prevent direct or altered source analysis."""

from __future__ import annotations

from pathlib import Path

from portfolio_analyzer.config import AnalyzerSettings
from portfolio_analyzer.models import ArtifactStatus, StagedArtifact
from portfolio_analyzer.staging.hashing import sha256_file


class UnsafeArtifactError(ValueError):
    pass


def assert_trusted_staged_artifact(artifact: StagedArtifact, settings: AnalyzerSettings) -> Path:
    """Return a validated local path or reject it before an extractor can open it."""
    if artifact.status != ArtifactStatus.STAGED or artifact.local_staged_path is None:
        raise UnsafeArtifactError("Extraction requires a successfully staged artifact")
    path = artifact.local_staged_path.resolve()
    stage_root = settings.staged_tools_dir.resolve()
    try:
        path.relative_to(stage_root)
    except ValueError as exc:
        raise UnsafeArtifactError("Artifact is outside the local staging workspace") from exc
    if path == artifact.original_source_path.resolve():
        raise UnsafeArtifactError("Source artifact cannot be used for extraction")
    if not path.is_file() or artifact.sha256 is None:
        raise UnsafeArtifactError("Staged artifact is missing or lacks a recorded hash")
    if sha256_file(path) != artifact.sha256:
        raise UnsafeArtifactError("Staged artifact hash mismatch; restage before extraction")
    return path
