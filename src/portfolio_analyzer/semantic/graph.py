"""Explainable similarity graph and conservative portfolio clustering."""

from __future__ import annotations

import hashlib
import math
import random
from collections import Counter, defaultdict

from portfolio_analyzer.models import (
    Confidence,
    Datasource,
    PortfolioCluster,
    SemanticApplicationProfile,
    SimilarityEdge,
)
from portfolio_analyzer.semantic.config import ClusteringSettings


def build_similarity_graph(
    profiles: list[SemanticApplicationProfile],
    embeddings: dict[str, list[float]],
    datasources: list[Datasource],
    settings: ClusteringSettings,
) -> tuple[list[SimilarityEdge], list[PortfolioCluster]]:
    capabilities = {
        profile.tool_inventory_id: {
            finding.label.casefold()
            for finding in profile.findings
            if finding.category in {"business_capability", "technical_capability", "workflow"}
            and finding.evidence_ids
            and finding.review_status != "rejected"
        }
        for profile in profiles
    }
    datasource_sets: dict[str, set[str]] = defaultdict(set)
    for source in datasources:
        datasource_sets[source.tool_inventory_id].add(
            "|".join(
                str(value or "").casefold()
                for value in (
                    source.platform,
                    source.server,
                    source.database,
                    source.schema_name,
                    source.object_name,
                )
            )
        )

    node_ids = [profile.tool_inventory_id for profile in profiles]
    adjacency: dict[str, dict[str, float]] = {node: {} for node in node_ids}
    edges: list[SimilarityEdge] = []
    for index, left in enumerate(profiles):
        for right in profiles[index + 1 :]:
            left_vector = embeddings.get(left.tool_inventory_id)
            right_vector = embeddings.get(right.tool_inventory_id)
            if left_vector is None or right_vector is None:
                continue
            similarity = _cosine_similarity(left_vector, right_vector)
            shared_capabilities = sorted(
                capabilities[left.tool_inventory_id] & capabilities[right.tool_inventory_id]
            )
            shared_datasources = sorted(
                datasource_sets[left.tool_inventory_id] & datasource_sets[right.tool_inventory_id]
            )
            corroborated = bool(shared_capabilities or shared_datasources)
            if similarity < settings.strong_similarity and not (
                similarity >= settings.corroborated_similarity and corroborated
            ):
                continue
            edge = SimilarityEdge(
                source_tool_id=left.tool_inventory_id,
                target_tool_id=right.tool_inventory_id,
                semantic_similarity=round(similarity, 6),
                shared_capabilities=shared_capabilities,
                shared_datasources=shared_datasources,
            )
            edges.append(edge)
            weight = max(similarity, 0.0)
            adjacency[left.tool_inventory_id][right.tool_inventory_id] = weight
            adjacency[right.tool_inventory_id][left.tool_inventory_id] = weight

    communities = _seeded_weighted_communities(adjacency, seed=settings.fixed_seed)
    profiles_by_id = {profile.tool_inventory_id: profile for profile in profiles}
    clusters = [
        _cluster(index + 1, sorted(community), profiles_by_id)
        for index, community in enumerate(sorted(communities, key=lambda group: sorted(group)))
    ]
    return sorted(edges, key=lambda edge: (edge.source_tool_id, edge.target_tool_id)), clusters


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
            "Grouped by local semantic similarity"
            + (" with shared observed capabilities or datasources." if len(tool_ids) > 1 else ".")
        ),
        confidence=_minimum_confidence(confidences),
        evidence_ids=sorted(evidence_ids),
    )


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return numerator / (left_norm * right_norm)


def _minimum_confidence(values: list[Confidence]) -> Confidence:
    rank = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}
    return min(values, key=rank.__getitem__, default=Confidence.LOW)
