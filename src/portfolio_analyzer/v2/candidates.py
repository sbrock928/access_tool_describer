"""Deterministic, evidence-backed portfolio overlap candidates.

Candidates are deliberately non-exclusive: an application can participate in any number of
endpoint, object, file, code, or semantic comparisons.  They are prompts for review, never an
automatic consolidation decision.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations

from portfolio_analyzer.v2.identity import normalize_source_identity, stable_id
from portfolio_analyzer.v2.models import (
    ApplicationEvidenceBundle,
    ApplicationProfile,
    InteractionScope,
    PortfolioCandidate,
    PortfolioCandidateType,
    ResolutionStatus,
)

CANDIDATE_POLICY_VERSION = "portfolio-candidates-v2"


@dataclass(frozen=True, slots=True)
class CandidatePolicy:
    version: str
    semantic_overlap_min_score: float


FROZEN_CANDIDATE_POLICY = CandidatePolicy(
    version=CANDIDATE_POLICY_VERSION,
    semantic_overlap_min_score=0.5,
)


def generate_portfolio_candidates(
    bundles: tuple[ApplicationEvidenceBundle, ...],
    profiles: tuple[ApplicationProfile, ...] = (),
) -> tuple[PortfolioCandidate, ...]:
    """Build deterministic overlapping candidates from facts and interpreted feature labels."""

    policy = FROZEN_CANDIDATE_POLICY
    bundle_by_application = _unique_applications(bundles)
    profile_by_application = _unique_profiles(profiles)
    unknown_profiles = set(profile_by_application) - set(bundle_by_application)
    if unknown_profiles:
        raise ValueError(
            "profiles reference applications without evidence bundles: "
            + ", ".join(sorted(unknown_profiles))
        )

    candidates = [
        *_shared_endpoint_candidates(bundles, policy.version),
        *_shared_object_candidates(bundles, policy.version),
        *_shared_file_candidates(bundles, policy.version),
        *_exact_code_candidates(bundles, policy.version),
        *_semantic_overlap_candidates(
            tuple(profile_by_application.values()),
            policy.semantic_overlap_min_score,
            policy.version,
        ),
    ]
    unique = {candidate.candidate_id: candidate for candidate in candidates}
    return tuple(unique[identifier] for identifier in sorted(unique))


def _shared_endpoint_candidates(
    bundles: tuple[ApplicationEvidenceBundle, ...], policy_version: str
) -> tuple[PortfolioCandidate, ...]:
    groups: dict[str, _CandidateAccumulator] = defaultdict(_CandidateAccumulator)
    for bundle in bundles:
        for connection in bundle.connections:
            if (
                connection.datasource_id is None
                or connection.resolution_status != ResolutionStatus.RESOLVED
            ):
                continue
            group = groups[connection.datasource_id]
            group.applications.add(bundle.application_id)
            group.evidence.update(connection.evidence_ids)
            group.basis.update((connection.datasource_id, connection.connection_id))
    return _materialize_groups(
        groups,
        PortfolioCandidateType.SHARED_ENDPOINT,
        policy_version,
    )


def _shared_object_candidates(
    bundles: tuple[ApplicationEvidenceBundle, ...], policy_version: str
) -> tuple[PortfolioCandidate, ...]:
    groups: dict[str, _CandidateAccumulator] = defaultdict(_CandidateAccumulator)
    for bundle in bundles:
        for access in bundle.interactions:
            if (
                access.scope != InteractionScope.EXTERNAL
                or access.datasource_id is None
                or access.object_name is None
            ):
                continue
            subject = stable_id(
                "external_object",
                access.datasource_id,
                _normalized_optional(access.catalog),
                _normalized_optional(access.database),
                _normalized_optional(access.schema_name),
                access.object_name.casefold(),
            )
            group = groups[subject]
            group.applications.add(bundle.application_id)
            group.evidence.update(access.evidence_ids)
            group.basis.update((access.interaction_id, access.datasource_id))
    return _materialize_groups(
        groups,
        PortfolioCandidateType.SHARED_OBJECT,
        policy_version,
    )


def _shared_file_candidates(
    bundles: tuple[ApplicationEvidenceBundle, ...], policy_version: str
) -> tuple[PortfolioCandidate, ...]:
    groups: dict[str, _CandidateAccumulator] = defaultdict(_CandidateAccumulator)
    for bundle in bundles:
        datasources = {item.datasource_id: item for item in bundle.datasources}
        for connection in bundle.connections:
            if (
                connection.datasource_id is None
                or connection.resolution_status != ResolutionStatus.RESOLVED
            ):
                continue
            datasource = datasources.get(connection.datasource_id)
            if datasource is None or datasource.resource is None:
                continue
            subject = stable_id(
                "shared_file", normalize_source_identity(datasource.resource)
            )
            group = groups[subject]
            group.applications.add(bundle.application_id)
            group.evidence.update(connection.evidence_ids)
            group.basis.update((connection.datasource_id, connection.connection_id))
    return _materialize_groups(
        groups,
        PortfolioCandidateType.SHARED_FILE,
        policy_version,
    )


def _exact_code_candidates(
    bundles: tuple[ApplicationEvidenceBundle, ...], policy_version: str
) -> tuple[PortfolioCandidate, ...]:
    groups: dict[str, _CandidateAccumulator] = defaultdict(_CandidateAccumulator)
    for bundle in bundles:
        for access_object in bundle.objects:
            if not access_object.sanitized_definition:
                continue
            normalized = _normalize_code(access_object.sanitized_definition)
            if not normalized:
                continue
            subject = stable_id("exact_code", normalized)
            group = groups[subject]
            group.applications.add(bundle.application_id)
            group.evidence.update(access_object.evidence_ids)
            group.basis.add(access_object.object_id)
    return _materialize_groups(
        groups,
        PortfolioCandidateType.EXACT_CODE,
        policy_version,
    )


def _semantic_overlap_candidates(
    profiles: tuple[ApplicationProfile, ...],
    threshold: float,
    policy_version: str,
) -> tuple[PortfolioCandidate, ...]:
    output: list[PortfolioCandidate] = []
    for source, target in combinations(
        sorted(profiles, key=lambda item: item.application_id), 2
    ):
        score = semantic_profile_overlap_score(source, target)
        if score == 0.0 or score < threshold:
            continue
        evidence_ids = tuple(sorted(set(source.evidence_ids) | set(target.evidence_ids)))
        if not evidence_ids:
            continue
        output.append(
            PortfolioCandidate(
                candidate_type=PortfolioCandidateType.SEMANTIC_OVERLAP,
                application_ids=(source.application_id, target.application_id),
                evidence_ids=evidence_ids,
                basis_ids=(),
                score=score,
                policy_version=policy_version,
            )
        )
    return tuple(output)


def semantic_profile_overlap_score(
    source: ApplicationProfile,
    target: ApplicationProfile,
) -> float:
    """Return the deterministic score used by generation and gold-set calibration."""

    source_features = _semantic_features(source)
    target_features = _semantic_features(target)
    union = source_features | target_features
    if not union:
        return 0.0
    return round(len(source_features & target_features) / len(union), 6)


class _CandidateAccumulator:
    def __init__(self) -> None:
        self.applications: set[str] = set()
        self.evidence: set[str] = set()
        self.basis: set[str] = set()


def _materialize_groups(
    groups: dict[str, _CandidateAccumulator],
    candidate_type: PortfolioCandidateType,
    policy_version: str,
) -> tuple[PortfolioCandidate, ...]:
    output: list[PortfolioCandidate] = []
    for _subject, group in sorted(groups.items()):
        if len(group.applications) < 2 or not group.evidence:
            continue
        output.append(
            PortfolioCandidate(
                candidate_type=candidate_type,
                application_ids=tuple(group.applications),
                evidence_ids=tuple(group.evidence),
                basis_ids=tuple(group.basis),
                score=1.0,
                policy_version=policy_version,
            )
        )
    return tuple(output)


def _unique_applications(
    bundles: tuple[ApplicationEvidenceBundle, ...],
) -> dict[str, ApplicationEvidenceBundle]:
    output: dict[str, ApplicationEvidenceBundle] = {}
    for bundle in bundles:
        if bundle.application_id in output:
            raise ValueError(f"duplicate application evidence bundle: {bundle.application_id}")
        output[bundle.application_id] = bundle
    return output


def _unique_profiles(
    profiles: tuple[ApplicationProfile, ...],
) -> dict[str, ApplicationProfile]:
    output: dict[str, ApplicationProfile] = {}
    for profile in profiles:
        if profile.application_id in output:
            raise ValueError(f"duplicate application profile: {profile.application_id}")
        output[profile.application_id] = profile
    return output


def _normalize_code(value: str) -> str:
    return "\n".join(line.rstrip() for line in value.replace("\r\n", "\n").split("\n")).strip()


def _normalized_optional(value: str | None) -> str | None:
    return value.casefold() if value is not None else None


def _semantic_features(profile: ApplicationProfile) -> set[str]:
    return {
        value.casefold()
        for value in (*profile.capabilities, *profile.major_workflows)
        if value.strip()
    }
