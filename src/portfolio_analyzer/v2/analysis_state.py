"""Persistable V2 audit envelopes for application and portfolio Qwen runs."""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import Field, model_validator

from portfolio_analyzer.v2.identity import canonical_json_bytes
from portfolio_analyzer.v2.models import (
    ApplicationInterpretation,
    LogicalUnitInterpretation,
    NonEmptyString,
    PortfolioAnalysis,
    PortfolioCandidate,
    Sha256,
    StrictModel,
)


class InferenceCacheRecord(StrictModel):
    content_sha256: Sha256
    schema_sha256: Sha256
    prompt_sha256: Sha256
    model_sha256: Sha256
    cache_key: Sha256


class LogicalUnitRunRecord(StrictModel):
    logical_unit_id: NonEmptyString
    status: Literal["complete", "partial", "failed", "abstained", "skipped"]
    reason: str | None = None
    chunk_count: int = Field(ge=0)
    interpretation: LogicalUnitInterpretation | None = None
    cache_keys: tuple[InferenceCacheRecord, ...] = ()

    @model_validator(mode="after")
    def validate_disposition(self) -> Self:
        if self.status in {"complete", "abstained"} and self.interpretation is None:
            raise ValueError(
                f"{self.status} logical unit must contain its interpretation"
            )
        if self.status in {"failed", "skipped"} and not self.reason:
            raise ValueError(f"{self.status} logical unit must contain a reason")
        if self.status == "skipped" and self.interpretation is not None:
            raise ValueError("skipped logical unit cannot contain an interpretation")
        return self


class ApplicationAnalysisPayload(StrictModel):
    schema_version: Literal["application-analysis-audit-v2"] = (
        "application-analysis-audit-v2"
    )
    application_id: NonEmptyString
    source_bundle_sha256: Sha256
    status: Literal["complete", "partial", "failed"]
    unit_results: tuple[LogicalUnitRunRecord, ...]
    interpretation: ApplicationInterpretation | None = None
    cache_key: InferenceCacheRecord | None = None
    cache_keys: tuple[InferenceCacheRecord, ...] = ()
    reason: str | None = None

    @model_validator(mode="after")
    def validate_analysis(self) -> Self:
        unit_ids = [item.logical_unit_id for item in self.unit_results]
        if len(set(unit_ids)) != len(unit_ids):
            raise ValueError("application analysis contains duplicate logical units")
        if self.status == "complete" and self.interpretation is None:
            raise ValueError("complete application analysis requires an interpretation")
        if self.status == "complete" and any(
            item.status not in {"complete", "abstained"} for item in self.unit_results
        ):
            raise ValueError("complete application analysis cannot contain incomplete units")
        if self.status == "complete" and any(
            item.status == "skipped" for item in self.unit_results
        ):
            raise ValueError("an application with skipped units must be partial")
        if self.status == "failed" and not self.reason:
            raise ValueError("failed application analysis must contain a reason")
        if self.interpretation is not None:
            if self.interpretation.application_id != self.application_id:
                raise ValueError("application interpretation belongs to another application")
            if self.interpretation.source_bundle_sha256 != self.source_bundle_sha256:
                raise ValueError("application interpretation cites another evidence bundle")
        object.__setattr__(
            self,
            "unit_results",
            tuple(sorted(self.unit_results, key=lambda item: item.logical_unit_id)),
        )
        cache_records = {item.cache_key: item for item in self.cache_keys}
        object.__setattr__(
            self,
            "cache_keys",
            tuple(cache_records[key] for key in sorted(cache_records)),
        )
        return self


class PortfolioAnalysisPayload(StrictModel):
    schema_version: Literal["portfolio-analysis-audit-v2"] = "portfolio-analysis-audit-v2"
    status: Literal["complete", "incomplete"]
    candidates: tuple[PortfolioCandidate, ...]
    application_profile_sha256s: tuple[Sha256, ...]
    analysis: PortfolioAnalysis | None = None
    batch_count: int = Field(ge=0)
    cache_keys: tuple[InferenceCacheRecord, ...] = ()
    reason: str | None = None

    @model_validator(mode="after")
    def validate_portfolio(self) -> Self:
        candidate_ids = [item.candidate_id for item in self.candidates]
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("portfolio audit contains duplicate candidates")
        profile_hashes = tuple(sorted(set(self.application_profile_sha256s)))
        object.__setattr__(self, "application_profile_sha256s", profile_hashes)
        object.__setattr__(
            self,
            "candidates",
            tuple(sorted(self.candidates, key=lambda item: item.candidate_id)),
        )
        if self.status == "complete":
            if self.analysis is None:
                raise ValueError("complete portfolio analysis requires an interpretation")
            if tuple(self.analysis.application_interpretation_sha256s) != profile_hashes:
                raise ValueError("portfolio interpretation profile hashes do not match its audit")
        elif not self.reason:
            raise ValueError("incomplete portfolio analysis must contain a reason")
        return self


def application_payload_from_result(result: Any) -> ApplicationAnalysisPayload:
    """Convert the persistence-free pipeline result without weakening the strict contract."""

    value = result.to_payload()
    return ApplicationAnalysisPayload(
        application_id=str(value["application_id"]),
        source_bundle_sha256=str(value["source_bundle_sha256"]),
        status=value["status"],
        unit_results=tuple(
            LogicalUnitRunRecord(
                logical_unit_id=str(item["logical_unit_id"]),
                status=item["status"],
                reason=item["reason"],
                chunk_count=int(item["chunk_count"]),
                interpretation=(
                    LogicalUnitInterpretation.model_validate_json(
                        canonical_json_bytes(item["interpretation"])
                    )
                    if item["interpretation"] is not None
                    else None
                ),
                cache_keys=tuple(
                    InferenceCacheRecord.model_validate(cache)
                    for cache in item["cache_keys"]
                ),
            )
            for item in value["unit_results"]
        ),
        interpretation=(
            ApplicationInterpretation.model_validate_json(
                canonical_json_bytes(value["interpretation"])
            )
            if value["interpretation"] is not None
            else None
        ),
        cache_key=(
            InferenceCacheRecord.model_validate(value["cache_key"])
            if value["cache_key"] is not None
            else None
        ),
        cache_keys=tuple(
            InferenceCacheRecord.model_validate(item)
            for item in value.get("cache_keys", ())
        ),
        reason=value["reason"],
    )


def portfolio_payload_from_result(
    result: Any,
    *,
    candidates: tuple[PortfolioCandidate, ...],
    application_profile_sha256s: tuple[str, ...],
) -> PortfolioAnalysisPayload:
    value = result.to_payload()
    return PortfolioAnalysisPayload(
        status=value["status"],
        candidates=candidates,
        application_profile_sha256s=application_profile_sha256s,
        analysis=(
            PortfolioAnalysis.model_validate_json(canonical_json_bytes(value["analysis"]))
            if value["analysis"] is not None
            else None
        ),
        batch_count=int(value["batch_count"]),
        cache_keys=tuple(
            InferenceCacheRecord.model_validate(item) for item in value["cache_keys"]
        ),
        reason=value["reason"],
    )
