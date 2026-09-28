from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from portfolio_analyzer.v2.models import PortfolioCandidateType
from portfolio_analyzer.v2.quality import (
    CALIBRATION_TIE_BREAK,
    SemanticPairScore,
    _calibrate_threshold,
    evaluate_quality,
)


def test_quality_check_scores_frozen_capabilities_and_semantic_pairs(
    tmp_path: Path,
) -> None:
    gold = tmp_path / "gold.csv"
    gold.write_text(
        "application_id,expected_capabilities,expected_related_application_ids\n"
        "app-a,Payments,app-b\n"
        "app-b,Payments,app-a\n"
        "app-c,Archive,\n",
        encoding="utf-8",
    )
    bundles = cast(
        Any,
        (
            SimpleNamespace(application_id="app-a"),
            SimpleNamespace(application_id="app-b"),
            SimpleNamespace(application_id="app-c"),
        ),
    )
    profiles = cast(
        Any,
        (
            SimpleNamespace(
                application_id="app-a",
                capabilities=("Payments",),
                major_workflows=("Settle payment",),
            ),
            SimpleNamespace(
                application_id="app-b",
                capabilities=("Payments",),
                major_workflows=("Settle payment",),
            ),
            SimpleNamespace(
                application_id="app-c",
                capabilities=("Archive",),
                major_workflows=(),
            ),
        ),
    )
    candidates = cast(
        Any,
        (
            SimpleNamespace(
                candidate_type=PortfolioCandidateType.SEMANTIC_OVERLAP,
                application_ids=("app-a", "app-b"),
            ),
        ),
    )

    result = evaluate_quality(gold, bundles, profiles, candidates)

    assert result.passed
    assert result.capability_recall == 1.0
    assert result.related_pair_f1 == 1.0
    assert result.frozen_semantic_threshold == 0.5
    assert result.calibrated_semantic_threshold == 1.0
    assert result.frozen_threshold_validated
    assert result.calibration_tie_break == CALIBRATION_TIE_BREAK
    assert result.reviewed_semantic_pairs == 3
    assert tuple(item.score for item in result.semantic_pair_scores) == (1.0, 0.0, 0.0)


def test_quality_check_fails_when_gold_does_not_validate_frozen_threshold(
    tmp_path: Path,
) -> None:
    gold = tmp_path / "gold.csv"
    gold.write_text(
        "application_id,expected_capabilities,expected_related_application_ids\n"
        "app-a,Alpha,app-b\n"
        "app-b,Beta,app-a\n"
        "app-c,Gamma,\n",
        encoding="utf-8",
    )
    bundles = cast(
        Any,
        tuple(SimpleNamespace(application_id=value) for value in ("app-a", "app-b", "app-c")),
    )
    profiles = cast(
        Any,
        (
            SimpleNamespace(
                application_id="app-a",
                capabilities=("Alpha", "Shared"),
                major_workflows=(),
            ),
            SimpleNamespace(
                application_id="app-b",
                capabilities=("Beta", "Shared"),
                major_workflows=(),
            ),
            SimpleNamespace(
                application_id="app-c",
                capabilities=("Gamma",),
                major_workflows=(),
            ),
        ),
    )

    result = evaluate_quality(gold, bundles, profiles, ())

    assert not result.passed
    assert result.related_pair_f1 == 0.0
    assert result.calibrated_pair_f1 == 1.0
    assert result.calibrated_semantic_threshold == 0.333333
    assert not result.frozen_threshold_validated
    assert any("does not validate frozen semantic threshold" in item for item in result.reasons)


def test_calibration_tie_break_prefers_precision_then_conservative_threshold() -> None:
    evaluation = _calibrate_threshold(
        (
            SemanticPairScore(("app-a", "app-b"), 0.8, True),
            SemanticPairScore(("app-a", "app-c"), 0.7, False),
            SemanticPairScore(("app-a", "app-d"), 0.6, True),
            SemanticPairScore(("app-a", "app-e"), 0.6, False),
        )
    )

    assert evaluation is not None
    assert evaluation.threshold == 0.8
    assert evaluation.precision == 1.0
    assert evaluation.recall == 0.5


def test_quality_check_requires_positive_and_negative_reviewed_pairs(tmp_path: Path) -> None:
    gold = tmp_path / "gold.csv"
    gold.write_text(
        "application_id,expected_capabilities,expected_related_application_ids\n"
        "app-a,Shared,app-b\n"
        "app-b,Shared,app-a\n",
        encoding="utf-8",
    )
    bundles = cast(
        Any,
        (
            SimpleNamespace(application_id="app-a"),
            SimpleNamespace(application_id="app-b"),
        ),
    )
    profiles = cast(
        Any,
        (
            SimpleNamespace(
                application_id="app-a",
                capabilities=("Shared",),
                major_workflows=(),
            ),
            SimpleNamespace(
                application_id="app-b",
                capabilities=("Shared",),
                major_workflows=(),
            ),
        ),
    )
    candidates = cast(
        Any,
        (
            SimpleNamespace(
                candidate_type=PortfolioCandidateType.SEMANTIC_OVERLAP,
                application_ids=("app-a", "app-b"),
            ),
        ),
    )

    result = evaluate_quality(gold, bundles, profiles, candidates)

    assert not result.passed
    assert not result.frozen_threshold_validated
    assert "gold set contains no reviewed non-related application pair" in result.reasons


def test_quality_check_requires_related_application_to_have_reviewed_row(
    tmp_path: Path,
) -> None:
    gold = tmp_path / "gold.csv"
    gold.write_text(
        "application_id,expected_capabilities,expected_related_application_ids\n"
        "app-a,Shared,app-b\n",
        encoding="utf-8",
    )
    bundles = cast(
        Any,
        (
            SimpleNamespace(application_id="app-a"),
            SimpleNamespace(application_id="app-b"),
        ),
    )
    profiles = cast(
        Any,
        (
            SimpleNamespace(
                application_id="app-a",
                capabilities=("Shared",),
                major_workflows=(),
            ),
            SimpleNamespace(
                application_id="app-b",
                capabilities=("Shared",),
                major_workflows=(),
            ),
        ),
    )

    with pytest.raises(ValueError, match="require their own reviewed rows"):
        evaluate_quality(gold, bundles, profiles, ())


def test_quality_check_rejects_unknown_gold_application(tmp_path: Path) -> None:
    gold = tmp_path / "gold.csv"
    gold.write_text(
        "application_id,expected_capabilities,expected_related_application_ids\n"
        "missing,Payments,\n",
        encoding="utf-8",
    )
    bundles = cast(Any, (SimpleNamespace(application_id="app-a"),))
    profiles = cast(
        Any,
        (SimpleNamespace(application_id="app-a", capabilities=("Payments",)),),
    )

    with pytest.raises(ValueError, match="unknown application"):
        evaluate_quality(gold, bundles, profiles, ())
