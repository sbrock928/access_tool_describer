from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from portfolio_analyzer.v2.analysis_state import application_payload_from_result
from portfolio_analyzer.v2.models import (
    ApplicationInterpretation,
    LogicalUnitInterpretation,
    ModelProvenance,
)


def _provenance() -> ModelProvenance:
    return ModelProvenance(
        model_manifest_sha256="a" * 64,
        prompt_version="application-v2",
        output_schema_version="application-interpretation-v2",
        inference_library_version="fixture",
    )


def _profile() -> ApplicationInterpretation:
    return ApplicationInterpretation(
        application_id="app-1",
        source_bundle_sha256="b" * 64,
        logical_unit_interpretation_ids=(),
        summary="Unknown because no semantic definitions were available.",
        business_purpose="Unknown",
        uncertainties=("No semantic-bearing definitions were extracted.",),
        evidence_ids=("ev-1",),
        provenance=_provenance(),
    )


def _unit(logical_unit_id: str) -> LogicalUnitInterpretation:
    return LogicalUnitInterpretation(
        application_id="app-1",
        logical_unit_id=logical_unit_id,
        source_bundle_sha256="b" * 64,
        purpose="Insufficient evidence for a more specific interpretation.",
        uncertainties=("The unit abstained from a stronger claim.",),
        evidence_ids=("ev-1",),
        provenance=_provenance(),
    )


def test_application_audit_persists_zero_unit_synthesis_and_cache_dimensions() -> None:
    profile = _profile()
    cache = {
        "content_sha256": "1" * 64,
        "schema_sha256": "2" * 64,
        "prompt_sha256": "3" * 64,
        "model_sha256": "4" * 64,
        "cache_key": "5" * 64,
    }
    payload: dict[str, Any] = {
        "application_id": "app-1",
        "source_bundle_sha256": "b" * 64,
        "status": "complete",
        "reason": None,
        "unit_results": [],
        "interpretation": profile.model_dump(mode="json"),
        "cache_key": cache,
        "cache_keys": [cache],
    }
    result = SimpleNamespace(to_payload=lambda: payload)

    stored = application_payload_from_result(result)

    assert stored.interpretation == profile
    assert stored.unit_results == ()
    assert stored.cache_key is not None
    assert stored.cache_key.cache_key == "5" * 64
    assert tuple(item.cache_key for item in stored.cache_keys) == ("5" * 64,)


def test_complete_application_audit_rejects_missing_profile() -> None:
    with pytest.raises(ValidationError, match="requires an interpretation"):
        application_payload_from_result(
            SimpleNamespace(
                to_payload=lambda: {
                    "application_id": "app-1",
                    "source_bundle_sha256": "b" * 64,
                    "status": "complete",
                    "reason": None,
                    "unit_results": [],
                    "interpretation": None,
                    "cache_key": None,
                    "cache_keys": [],
                }
            )
        )


def test_abstained_and_skipped_unit_dispositions_round_trip_losslessly() -> None:
    profile = _profile()
    payload: dict[str, Any] = {
        "application_id": "app-1",
        "source_bundle_sha256": "b" * 64,
        "status": "partial",
        "reason": "One logical unit was skipped.",
        "unit_results": [
            {
                "logical_unit_id": "unit-abstained",
                "status": "abstained",
                "reason": "Evidence was insufficient for a stronger claim.",
                "chunk_count": 1,
                "interpretation": _unit("unit-abstained").model_dump(mode="json"),
                "cache_keys": [],
            },
            {
                "logical_unit_id": "unit-skipped",
                "status": "skipped",
                "reason": "No semantic text was available.",
                "chunk_count": 0,
                "interpretation": None,
                "cache_keys": [],
            },
        ],
        "interpretation": profile.model_dump(mode="json"),
        "cache_key": None,
        "cache_keys": [],
    }

    stored = application_payload_from_result(SimpleNamespace(to_payload=lambda: payload))

    assert tuple(item.status for item in stored.unit_results) == (
        "abstained",
        "skipped",
    )


def test_complete_application_accepts_abstained_but_not_skipped_units() -> None:
    profile = _profile()
    common: dict[str, Any] = {
        "application_id": "app-1",
        "source_bundle_sha256": "b" * 64,
        "status": "complete",
        "reason": None,
        "interpretation": profile.model_dump(mode="json"),
        "cache_key": None,
        "cache_keys": [],
    }
    abstained = {
        "logical_unit_id": "unit-abstained",
        "status": "abstained",
        "reason": "Evidence was insufficient for a stronger claim.",
        "chunk_count": 1,
        "interpretation": _unit("unit-abstained").model_dump(mode="json"),
        "cache_keys": [],
    }
    complete = application_payload_from_result(
        SimpleNamespace(to_payload=lambda: {**common, "unit_results": [abstained]})
    )
    assert complete.unit_results[0].status == "abstained"

    skipped = {
        **abstained,
        "status": "skipped",
        "reason": "No semantic text was available.",
        "interpretation": None,
    }
    with pytest.raises(ValidationError, match="incomplete units"):
        application_payload_from_result(
            SimpleNamespace(to_payload=lambda: {**common, "unit_results": [skipped]})
        )
