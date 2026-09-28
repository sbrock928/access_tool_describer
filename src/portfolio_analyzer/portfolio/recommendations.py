"""Modernization opportunities discovered from repeated static observations."""

from __future__ import annotations

from portfolio_analyzer.models import CapabilityFinding, Datasource, Evidence, Recommendation
from portfolio_analyzer.portfolio.discovery import discover_themes


def build_recommendations(
    capabilities: list[CapabilityFinding],
    datasources: list[Datasource],
) -> list[Recommendation]:
    observations = {
        e.evidence_id: e if e.inference else e.model_copy(update={"inference": finding.capability})
        for finding in capabilities
        for e in finding.evidence
        if e.tool_inventory_id == finding.tool_inventory_id
    }
    observations.update((e.evidence_id, e) for source in datasources for e in source.evidence)
    themes = discover_themes(evidence=observations.values(), datasources=datasources)
    return [
        Recommendation(
            category="discovered_evidence",
            title=theme.title,
            rationale=theme.observed_pattern
            + " "
            + theme.proposed_solution
            + " Alternatives: "
            + "; ".join(theme.alternative_options),
            affected_tool_ids=theme.affected_tool_ids,
            confidence=theme.confidence,
            evidence=[
                observations[ref]
                for ref in sorted({loc.evidence_id for loc in theme.locations})
                if ref in observations
            ],
        )
        for theme in themes
    ]


def modernization_risks(evidence: list[Evidence]) -> list[tuple[str, list[str], str]]:
    """Return fact-based risk themes, including applications and a concise implication."""
    themes = {
        "Outlook automation": "Workstation-bound notification integration can impede migration.",
        "Excel automation": (
            "Desktop automation creates Office-version and unattended-run dependencies."
        ),
        "Shell invocation": "External process calls require security and deployment review.",
        "File operation": "Filesystem coupling can create environment-specific dependencies.",
        "Database access": (
            "Embedded data access may duplicate connection and error handling logic."
        ),
        "Dynamic SQL execution": (
            "Runtime SQL construction limits static dependency completeness and needs "
            "injection review."
        ),
        "HTTP integration": (
            "External endpoints, authentication, timeouts, and retry behavior require validation."
        ),
        "Suppressed VBA errors": (
            "Suppressed errors can hide production failures and complicate behavior validation."
        ),
        "Filesystem dependency": (
            "Hard-coded or network paths create environment-specific deployment dependencies."
        ),
        "Broken Access/VBA reference": (
            "A missing or incompatible library can prevent compilation and safe migration."
        ),
    }
    results: list[tuple[str, list[str], str]] = []
    for inference, implication in themes.items():
        tools = sorted({item.tool_inventory_id for item in evidence if item.inference == inference})
        if tools:
            results.append((inference, tools, implication))
    return results
