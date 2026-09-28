"""Read-only, versioned quality checks for the fixed V2 semantic policy."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from itertools import combinations
from math import isclose
from pathlib import Path

from portfolio_analyzer.v2.candidates import (
    FROZEN_CANDIDATE_POLICY,
    semantic_profile_overlap_score,
)
from portfolio_analyzer.v2.models import (
    ApplicationEvidenceBundle,
    ApplicationInterpretation,
    PortfolioCandidate,
    PortfolioCandidateType,
)

QUALITY_POLICY_VERSION = "qwen-quality-v4"
GOLD_APPLICATION_ID = "application_id"
GOLD_CAPABILITIES = "expected_capabilities"
GOLD_RELATED_APPLICATIONS = "expected_related_application_ids"
GOLD_HEADERS = (
    GOLD_APPLICATION_ID,
    GOLD_CAPABILITIES,
    GOLD_RELATED_APPLICATIONS,
)
MIN_CAPABILITY_RECALL = 0.70
MIN_RELATED_PAIR_F1 = 0.70
CALIBRATION_TIE_BREAK = (
    "maximize_f1_then_precision_then_recall_then_highest_threshold"
)


@dataclass(frozen=True, slots=True)
class SemanticPairScore:
    application_ids: tuple[str, str]
    score: float
    expected_related: bool


@dataclass(frozen=True, slots=True)
class QualityResult:
    policy_version: str
    candidate_policy_version: str
    frozen_semantic_threshold: float | None
    calibrated_semantic_threshold: float | None
    frozen_threshold_validated: bool
    calibration_tie_break: str
    reviewed_applications: int
    reviewed_semantic_pairs: int
    semantic_pair_scores: tuple[SemanticPairScore, ...]
    expected_capabilities: int
    matched_capabilities: int
    expected_related_pairs: int
    predicted_related_pairs: int
    matched_related_pairs: int
    capability_recall: float | None
    related_pair_precision: float | None
    related_pair_recall: float | None
    related_pair_f1: float | None
    calibrated_pair_precision: float | None
    calibrated_pair_recall: float | None
    calibrated_pair_f1: float | None
    passed: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ThresholdEvaluation:
    threshold: float
    predicted_pairs: frozenset[tuple[str, str]]
    matched_pairs: frozenset[tuple[str, str]]
    precision: float | None
    recall: float | None
    f1: float | None


def evaluate_quality(
    gold_set: Path,
    bundles: tuple[ApplicationEvidenceBundle, ...],
    profiles: tuple[ApplicationInterpretation, ...],
    candidates: tuple[PortfolioCandidate, ...],
) -> QualityResult:
    """Evaluate and calibrate reviewed pairs without mutating the frozen generation policy."""

    rows = _read_gold_set(gold_set)
    bundle_ids = {item.application_id for item in bundles}
    profile_by_id = {item.application_id: item for item in profiles}
    if len(profile_by_id) != len(profiles):
        raise ValueError("quality inputs contain duplicate application profiles")
    if set(profile_by_id) != bundle_ids:
        raise ValueError("quality inputs require one profile per evidence bundle")

    expected_capabilities = 0
    matched_capabilities = 0
    expected_pairs: set[tuple[str, str]] = set()
    seen: set[str] = set()
    for row_number, row in rows:
        application_id = row[GOLD_APPLICATION_ID]
        if not application_id:
            raise ValueError(f"gold-set row {row_number} has no application_id")
        if application_id in seen:
            raise ValueError(f"gold set repeats application_id '{application_id}'")
        seen.add(application_id)
        if application_id not in profile_by_id:
            raise ValueError(
                f"gold-set row {row_number} references unknown application '{application_id}'"
            )
        expected = _terms(row[GOLD_CAPABILITIES])
        actual = {item.casefold() for item in profile_by_id[application_id].capabilities}
        expected_capabilities += len(expected)
        matched_capabilities += len(expected & actual)
        for related in _terms(row[GOLD_RELATED_APPLICATIONS]):
            related_id = next(
                (item for item in bundle_ids if item.casefold() == related),
                None,
            )
            if related_id is None:
                raise ValueError(
                    f"gold-set row {row_number} references unknown related application "
                    f"'{related}'"
                )
            if related_id == application_id:
                raise ValueError(f"gold-set row {row_number} relates an application to itself")
            first, second = sorted((application_id, related_id))
            expected_pairs.add((first, second))

    related_but_unreviewed = {
        application_id
        for pair in expected_pairs
        for application_id in pair
        if application_id not in seen
    }
    if related_but_unreviewed:
        raise ValueError(
            "gold-set related applications require their own reviewed rows: "
            + ", ".join(sorted(related_but_unreviewed))
        )

    pair_scores = tuple(
        SemanticPairScore(
            application_ids=(source_id, target_id),
            score=semantic_profile_overlap_score(
                profile_by_id[source_id],
                profile_by_id[target_id],
            ),
            expected_related=(source_id, target_id) in expected_pairs,
        )
        for source_id, target_id in combinations(sorted(seen), 2)
    )
    frozen_threshold = FROZEN_CANDIDATE_POLICY.semantic_overlap_min_score
    frozen_evaluation = (
        _evaluate_threshold(pair_scores, frozen_threshold)
        if frozen_threshold is not None
        else None
    )
    calibrated_evaluation = _calibrate_threshold(pair_scores)
    semantic_candidate_pairs = {
        _ordered_pair(source_id, target_id)
        for candidate in candidates
        if candidate.candidate_type == PortfolioCandidateType.SEMANTIC_OVERLAP
        for source_id, target_id in combinations(candidate.application_ids, 2)
        if source_id in seen and target_id in seen
    }
    capability_recall = _ratio(matched_capabilities, expected_capabilities)
    reviewed_negative_pairs = len(pair_scores) - len(expected_pairs)
    calibration_ready = bool(expected_pairs) and reviewed_negative_pairs > 0
    frozen_threshold_validated = bool(
        frozen_evaluation is not None
        and calibration_ready
        and calibrated_evaluation is not None
        and frozen_evaluation is not None
        and frozen_evaluation.f1 is not None
        and calibrated_evaluation.f1 is not None
        and isclose(
            frozen_evaluation.f1,
            calibrated_evaluation.f1,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
    )
    reasons: list[str] = []
    if frozen_threshold is None:
        reasons.append(
            "semantic similarity is disabled pending reviewed Qwen 0.5B "
            "gold-set calibration"
        )
    if not rows:
        reasons.append("gold set contains no reviewed applications")
    if rows and not pair_scores:
        reasons.append(
            "gold set requires at least two reviewed applications for semantic calibration"
        )
    if pair_scores and not expected_pairs:
        reasons.append("gold set contains no reviewed related-application pair")
    if pair_scores and reviewed_negative_pairs == 0:
        reasons.append("gold set contains no reviewed non-related application pair")
    if capability_recall is not None and capability_recall < MIN_CAPABILITY_RECALL:
        reasons.append(
            f"capability recall {capability_recall:.3f} is below "
            f"{MIN_CAPABILITY_RECALL:.3f}"
        )
    if (
        frozen_evaluation is not None
        and frozen_evaluation.f1 is not None
        and frozen_evaluation.f1 < MIN_RELATED_PAIR_F1
    ):
        reasons.append(
            f"semantic-pair F1 {frozen_evaluation.f1:.3f} is below "
            f"{MIN_RELATED_PAIR_F1:.3f}"
        )
    expected_frozen_pairs = (
        set(frozen_evaluation.predicted_pairs)
        if frozen_evaluation is not None
        else set()
    )
    if semantic_candidate_pairs != expected_frozen_pairs:
        reasons.append(
            "semantic candidates do not match scores selected by the frozen threshold"
        )
    if not frozen_threshold_validated:
        recommendation = (
            f"; calibrated recommendation is {calibrated_evaluation.threshold:.6f}"
            if calibrated_evaluation is not None
            else ""
        )
        reasons.append(
            "reviewed gold set does not validate a frozen semantic threshold"
            f"{recommendation}"
        )
    return QualityResult(
        policy_version=QUALITY_POLICY_VERSION,
        candidate_policy_version=FROZEN_CANDIDATE_POLICY.version,
        frozen_semantic_threshold=frozen_threshold,
        calibrated_semantic_threshold=(
            calibrated_evaluation.threshold
            if calibrated_evaluation is not None
            else None
        ),
        frozen_threshold_validated=frozen_threshold_validated,
        calibration_tie_break=CALIBRATION_TIE_BREAK,
        reviewed_applications=len(rows),
        reviewed_semantic_pairs=len(pair_scores),
        semantic_pair_scores=pair_scores,
        expected_capabilities=expected_capabilities,
        matched_capabilities=matched_capabilities,
        expected_related_pairs=len(expected_pairs),
        predicted_related_pairs=(
            len(frozen_evaluation.predicted_pairs)
            if frozen_evaluation is not None
            else 0
        ),
        matched_related_pairs=(
            len(frozen_evaluation.matched_pairs)
            if frozen_evaluation is not None
            else 0
        ),
        capability_recall=capability_recall,
        related_pair_precision=(
            frozen_evaluation.precision if frozen_evaluation is not None else None
        ),
        related_pair_recall=(
            frozen_evaluation.recall if frozen_evaluation is not None else None
        ),
        related_pair_f1=(
            frozen_evaluation.f1 if frozen_evaluation is not None else None
        ),
        calibrated_pair_precision=(
            calibrated_evaluation.precision
            if calibrated_evaluation is not None
            else None
        ),
        calibrated_pair_recall=(
            calibrated_evaluation.recall
            if calibrated_evaluation is not None
            else None
        ),
        calibrated_pair_f1=(
            calibrated_evaluation.f1 if calibrated_evaluation is not None else None
        ),
        passed=not reasons,
        reasons=tuple(reasons),
    )


def _calibrate_threshold(
    pair_scores: tuple[SemanticPairScore, ...],
) -> _ThresholdEvaluation | None:
    if not pair_scores:
        return None
    thresholds = tuple(
        sorted(
            {
                0.0,
                1.0,
                *(item.score for item in pair_scores if item.score > 0.0),
            }
        )
    )
    evaluations = tuple(
        _evaluate_threshold(pair_scores, threshold) for threshold in thresholds
    )
    return max(
        evaluations,
        key=lambda item: (
            _metric_value(item.f1),
            _metric_value(item.precision),
            _metric_value(item.recall),
            item.threshold,
        ),
    )


def _evaluate_threshold(
    pair_scores: tuple[SemanticPairScore, ...],
    threshold: float,
) -> _ThresholdEvaluation:
    expected_pairs = {
        item.application_ids for item in pair_scores if item.expected_related
    }
    predicted_pairs = {
        item.application_ids
        for item in pair_scores
        if item.score > 0.0 and item.score >= threshold
    }
    matched_pairs = expected_pairs & predicted_pairs
    precision = (
        len(matched_pairs) / len(predicted_pairs)
        if predicted_pairs
        else 0.0
        if expected_pairs
        else None
    )
    recall = (
        len(matched_pairs) / len(expected_pairs) if expected_pairs else None
    )
    return _ThresholdEvaluation(
        threshold=threshold,
        predicted_pairs=frozenset(predicted_pairs),
        matched_pairs=frozenset(matched_pairs),
        precision=precision,
        recall=recall,
        f1=_f1(precision, recall),
    )


def _metric_value(value: float | None) -> float:
    return value if value is not None else -1.0


def _ordered_pair(source_id: str, target_id: str) -> tuple[str, str]:
    return (source_id, target_id) if source_id < target_id else (target_id, source_id)


def _read_gold_set(path: Path) -> tuple[tuple[int, dict[str, str]], ...]:
    try:
        with path.open(newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            missing = set(GOLD_HEADERS) - set(reader.fieldnames or ())
            if missing:
                raise ValueError(
                    "gold-set CSV is missing columns: " + ", ".join(sorted(missing))
                )
            return tuple(
                (
                    row_number,
                    {header: (row.get(header) or "").strip() for header in GOLD_HEADERS},
                )
                for row_number, row in enumerate(reader, start=2)
                if any((row.get(header) or "").strip() for header in GOLD_HEADERS)
            )
    except OSError as exc:
        raise ValueError(f"cannot read gold-set CSV: {path}") from exc


def _terms(value: str) -> set[str]:
    return {
        term.strip().casefold()
        for fragment in value.split(";")
        for term in fragment.split("|")
        if term.strip()
    }


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _f1(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None:
        return None
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)
