"""Explainable deterministic similarity and conservative portfolio clustering."""

from __future__ import annotations

import hashlib
import random
import re
from collections import Counter, defaultdict

from portfolio_analyzer.models import (
    Confidence,
    Datasource,
    Evidence,
    PortfolioCluster,
    SemanticApplicationProfile,
    SemanticSource,
    SimilarityEdge,
)
from portfolio_analyzer.semantic.config import ClusteringSettings

_GENERIC_LABELS = {
    "application",
    "data",
    "data management",
    "database",
    "general reporting",
    "management",
    "report",
    "reporting",
    "workflow",
}
_TECHNICAL_FINDING_CATEGORIES = {
    "technical_capability",
    "integration",
    "automation_trigger",
    "constraint",
    "modernization_blocker",
    "input",
    "output",
}


def build_similarity_graph(
    profiles: list[SemanticApplicationProfile],
    datasources: list[Datasource],
    settings: ClusteringSettings,
    *,
    sources: list[SemanticSource] | None = None,
    evidence: list[Evidence] | None = None,
) -> tuple[list[SimilarityEdge], list[PortfolioCluster]]:
    """Compare transparent category sets; no learned vectors or hidden state are used."""
    features = _portfolio_features(profiles, datasources, sources or [], evidence or [])
    weights = settings.category_weights()
    node_ids = [profile.tool_inventory_id for profile in profiles]
    adjacency: dict[str, dict[str, float]] = {node: {} for node in node_ids}
    edges: list[SimilarityEdge] = []
    for index, left in enumerate(profiles):
        for right in profiles[index + 1 :]:
            left_features = features[left.tool_inventory_id]
            right_features = features[right.tool_inventory_id]
            category_scores: dict[str, float] = {}
            shared_features: dict[str, list[str]] = {}
            similarity = 0.0
            for category, weight in weights.items():
                left_values = left_features[category]
                right_values = right_features[category]
                score = _jaccard(left_values, right_values)
                category_scores[category] = round(score, 6)
                shared = sorted(left_values & right_values)
                if shared:
                    shared_features[category] = shared
                similarity += weight * score
            corroborated = any(
                shared_features.get(category)
                for category in (
                    "business_capabilities",
                    "workflows",
                    "data_domains",
                    "datasources",
                    "technical_characteristics",
                )
            )
            if similarity < settings.strong_similarity and not (
                similarity >= settings.corroborated_similarity and corroborated
            ):
                continue
            overall = round(similarity, 6)
            edge = SimilarityEdge(
                source_tool_id=left.tool_inventory_id,
                target_tool_id=right.tool_inventory_id,
                overall_similarity=overall,
                category_scores=category_scores,
                shared_features=shared_features,
                shared_capabilities=shared_features.get("business_capabilities", []),
                shared_datasources=shared_features.get("datasources", []),
            )
            edges.append(edge)
            adjacency[left.tool_inventory_id][right.tool_inventory_id] = overall
            adjacency[right.tool_inventory_id][left.tool_inventory_id] = overall

    communities = _seeded_weighted_communities(adjacency, seed=settings.fixed_seed)
    profiles_by_id = {profile.tool_inventory_id: profile for profile in profiles}
    clusters = [
        _cluster(index + 1, sorted(community), profiles_by_id)
        for index, community in enumerate(sorted(communities, key=lambda group: sorted(group)))
    ]
    return sorted(edges, key=lambda edge: (edge.source_tool_id, edge.target_tool_id)), clusters


def _portfolio_features(
    profiles: list[SemanticApplicationProfile],
    datasources: list[Datasource],
    sources: list[SemanticSource],
    evidence: list[Evidence],
) -> dict[str, dict[str, set[str]]]:
    output = {
        profile.tool_inventory_id: {
            "business_capabilities": set(),
            "workflows": set(),
            "data_domains": set(),
            "datasources": set(),
            "technical_characteristics": set(),
            "object_composition": set(),
            "application_archetype": {_normalize(profile.primary_archetype)},
        }
        for profile in profiles
    }
    for profile in profiles:
        target = output[profile.tool_inventory_id]
        for finding in profile.findings:
            if not finding.evidence_ids or finding.review_status == "rejected":
                continue
            label = _normalize(finding.label)
            if not label or label in _GENERIC_LABELS:
                continue
            if finding.category == "business_capability":
                target["business_capabilities"].add(label)
            elif finding.category == "workflow":
                target["workflows"].add(label)
            elif finding.category in {"data_domain", "data_entity"}:
                target["data_domains"].add(label)
            elif finding.category in _TECHNICAL_FINDING_CATEGORIES:
                target["technical_characteristics"].add(label)
    for datasource in datasources:
        datasource_target = output.get(datasource.tool_inventory_id)
        if datasource_target is None:
            continue
        values = [
            _normalize(str(value or ""))
            for value in (
                datasource.platform,
                datasource.server,
                datasource.database,
                datasource.schema_name,
                datasource.object_name,
            )
        ]
        datasource_target["datasources"].add("|".join(values))
        if values[0]:
            datasource_target["technical_characteristics"].add(f"datasource platform:{values[0]}")
    for source in sources:
        source_target = output.get(source.tool_inventory_id)
        if source_target is not None:
            source_target["object_composition"].add(_normalize(source.object_type))
    for evidence_item in evidence:
        evidence_target = output.get(evidence_item.tool_inventory_id)
        if evidence_target is None:
            continue
        if evidence_item.inference:
            normalized = _normalize(evidence_item.inference)
            if normalized and normalized not in _GENERIC_LABELS:
                evidence_target["technical_characteristics"].add(normalized)
        if evidence_item.object_type:
            evidence_target["object_composition"].add(_normalize(evidence_item.object_type))
    return output


def _jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.casefold())).strip()


def _seeded_weighted_communities(
    adjacency: dict[str, dict[str, float]], *, seed: int
) -> list[set[str]]:
    """Use fixed-seed weighted label propagation without an external graph runtime."""
    labels = {node: node for node in adjacency}
    generator = random.Random(seed)
    for _ in range(100):
        nodes = sorted(adjacency)
        generator.shuffle(nodes)
        changed = False
        for node in nodes:
            if not adjacency[node]:
                continue
            scores: dict[str, float] = defaultdict(float)
            for neighbor, weight in adjacency[node].items():
                scores[labels[neighbor]] += weight
            maximum = max(scores.values())
            candidates = sorted(label for label, score in scores.items() if score == maximum)
            chosen = candidates[generator.randrange(len(candidates))]
            if labels[node] != chosen:
                labels[node] = chosen
                changed = True
        if not changed:
            break
    grouped: dict[str, set[str]] = defaultdict(set)
    for node, label in labels.items():
        grouped[label].add(node)
    return sorted(grouped.values(), key=lambda group: sorted(group))


def _cluster(
    ordinal: int,
    tool_ids: list[str],
    profiles: dict[str, SemanticApplicationProfile],
) -> PortfolioCluster:
    capability_counts: Counter[str] = Counter()
    domain_counts: Counter[str] = Counter()
    evidence_ids: set[str] = set()
    confidences: list[Confidence] = []
    archetypes: Counter[str] = Counter()
    for tool_id in tool_ids:
        profile = profiles[tool_id]
        confidences.append(profile.confidence)
        evidence_ids.update(profile.evidence_ids)
        archetypes[profile.primary_archetype] += 1
        for finding in profile.findings:
            if not finding.evidence_ids or finding.review_status == "rejected":
                continue
            if finding.category == "business_capability":
                capability_counts[finding.label] += 1
            elif finding.category in {"data_domain", "data_entity"}:
                domain_counts[finding.label] += 1
    shared_capabilities = [
        label for label, count in capability_counts.most_common() if count >= min(2, len(tool_ids))
    ]
    shared_domains = [
        label for label, count in domain_counts.most_common() if count >= min(2, len(tool_ids))
    ]
    label = (
        shared_capabilities[0]
        if shared_capabilities
        else archetypes.most_common(1)[0][0]
        if archetypes
        else f"Application group {ordinal}"
    )
    digest = hashlib.sha256("\x1f".join(tool_ids).encode("utf-8")).hexdigest()[:12]
    return PortfolioCluster(
        cluster_id=f"cluster_{digest}",
        label=label,
        application_ids=tool_ids,
        shared_capabilities=shared_capabilities,
        shared_data_domains=shared_domains,
        rationale=(
            "Grouped by deterministic weighted overlap of evidence-grounded semantic and "
            "technical features."
            if len(tool_ids) > 1
            else "No qualifying deterministic similarity edge to another application."
        ),
        confidence=_minimum_confidence(confidences),
        evidence_ids=sorted(evidence_ids),
    )


def _minimum_confidence(values: list[Confidence]) -> Confidence:
    rank = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}
    return min(values, key=rank.__getitem__, default=Confidence.LOW)
