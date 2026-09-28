"""Stable V2 cache and lineage fingerprints, excluding volatile host/run metadata."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel

from portfolio_analyzer.v2.identity import canonical_sha256, sanitize_value
from portfolio_analyzer.v2.models import (
    ApplicationEvidenceBundle,
    ApplicationInterpretation,
    PortfolioAnalysis,
    ReportModel,
)
from portfolio_analyzer.v2.workflow import ExtractedArtifactSnapshot, StageIndex

_VOLATILE_KEYS = frozenset(
    {
        "completed_at",
        "created_at",
        "extracted_at",
        "generated_at",
        "imported_at",
        "reviewed_at",
        "started_at",
    }
)
_MACHINE_PATH_KEYS = frozenset({"source_locator", "staged_relative_path"})


def stage_fingerprint(index: StageIndex) -> str:
    """Fingerprint staged identities and bytes without time or machine/source path text."""

    return stable_payload_sha256(index)


def extraction_fingerprint(snapshot: ExtractedArtifactSnapshot) -> str:
    return stable_payload_sha256(snapshot)


def evidence_bundle_fingerprint(bundle: ApplicationEvidenceBundle) -> str:
    return stable_payload_sha256(bundle)


def interpretation_fingerprint(
    interpretation: ApplicationInterpretation | PortfolioAnalysis,
) -> str:
    return stable_payload_sha256(interpretation)


def report_fingerprint(report: ReportModel) -> str:
    return stable_payload_sha256(report)


def analysis_fingerprint(
    bundles: Iterable[ApplicationEvidenceBundle],
    interpretations: Iterable[ApplicationInterpretation],
    portfolio: PortfolioAnalysis | None,
) -> str:
    """Stable origin identity used by review workbooks and overlays."""

    payload = {
        "bundle_fingerprints": sorted(evidence_bundle_fingerprint(item) for item in bundles),
        "interpretation_fingerprints": sorted(
            interpretation_fingerprint(item) for item in interpretations
        ),
        "portfolio_fingerprint": (
            interpretation_fingerprint(portfolio) if portfolio is not None else None
        ),
    }
    return canonical_sha256(payload)


def stable_payload_sha256(value: BaseModel | dict[str, Any]) -> str:
    """Hash canonical sanitized content after removing only declared volatile fields."""

    raw: Any = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return canonical_sha256(_without_volatile_values(sanitize_value(raw)))


def _without_volatile_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _without_volatile_values(item)
            for key, item in value.items()
            if str(key) not in _VOLATILE_KEYS | _MACHINE_PATH_KEYS
        }
    if isinstance(value, list):
        return [_without_volatile_values(item) for item in value]
    return value
