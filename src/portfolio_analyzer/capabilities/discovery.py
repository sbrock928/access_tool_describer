"""Discover recurring capability labels from evidence present in the corpus."""

from __future__ import annotations

from collections import defaultdict

from portfolio_analyzer.models import CapabilityFinding, Confidence, Evidence

_CONFIDENCE_RANK = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}


def discover_capabilities(evidence: list[Evidence]) -> list[CapabilityFinding]:
    """Build technical candidates from observed inference labels, without domain taxonomy."""
    grouped: dict[tuple[str, str], list[Evidence]] = defaultdict(list)
    for fact in evidence:
        if fact.inference:
            grouped[(fact.tool_inventory_id, fact.inference)].append(fact)
    return [
        CapabilityFinding(
            tool_inventory_id=tool_id,
            capability=capability,
            layer="technical",
            confidence=min(
                (fact.confidence for fact in facts),
                key=_CONFIDENCE_RANK.__getitem__,
                default=Confidence.LOW,
            ),
            evidence=facts,
        )
        for (tool_id, capability), facts in sorted(grouped.items())
    ]


def capability_taxonomy(findings: list[CapabilityFinding]) -> dict[str, int]:
    return dict(
        sorted(
            (
                (capability, len({item.tool_inventory_id for item in items}))
                for capability, items in _group(findings).items()
            ),
            key=lambda pair: (-pair[1], pair[0]),
        )
    )


def _group(findings: list[CapabilityFinding]) -> dict[str, list[CapabilityFinding]]:
    result: dict[str, list[CapabilityFinding]] = defaultdict(list)
    for finding in findings:
        result[finding.capability].append(finding)
    return result
