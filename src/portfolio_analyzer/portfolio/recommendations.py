"""Conservative architecture recommendations derived from observed repetition."""

from __future__ import annotations

from collections import defaultdict

from portfolio_analyzer.models import (
    CapabilityFinding,
    Confidence,
    Datasource,
    Evidence,
    Recommendation,
)

MIN_SHARED_LIBRARY_CONSUMERS = 2
MIN_SERVICE_CONSUMERS = 3


def build_recommendations(
    capabilities: list[CapabilityFinding], datasources: list[Datasource]
) -> list[Recommendation]:
    """Recommend boundaries only after repeatable, static evidence crosses thresholds."""
    recommendations: list[Recommendation] = []
    by_capability: dict[str, list[CapabilityFinding]] = defaultdict(list)
    for finding in capabilities:
        by_capability[finding.capability].append(finding)

    recommendations.extend(_capability_recommendations(by_capability))
    recommendations.extend(_datasource_recommendations(datasources))
    return sorted(recommendations, key=lambda item: (-len(item.affected_tool_ids), item.title))


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
    }
    results: list[tuple[str, list[str], str]] = []
    for inference, implication in themes.items():
        tools = sorted({item.tool_inventory_id for item in evidence if item.inference == inference})
        if tools:
            results.append((inference, tools, implication))
    return results


def _capability_recommendations(
    by_capability: dict[str, list[CapabilityFinding]],
) -> list[Recommendation]:
    rules = {
        "Outlook automation": (
            MIN_SERVICE_CONSUMERS,
            "microservice/api",
            "Notification delivery service candidate",
            " ".join(
                (
                    "Independent applications implement Outlook-based delivery.",
                    (
                        "A shared service may centralize channel handling, retry, audit logging, "
                        "and credentials."
                    ),
                    "Validate ownership and delivery requirements before implementation.",
                )
            ),
        ),
        "Excel automation": (
            MIN_SHARED_LIBRARY_CONSUMERS,
            "shared_library",
            "Spreadsheet output library candidate",
            " ".join(
                (
                    "Multiple applications automate Excel.",
                    (
                        "A shared library is the lowest-overhead first boundary for templates "
                        "and generation."
                    ),
                    "It is not automatically a service.",
                )
            ),
        ),
        "Configuration loading": (
            MIN_SHARED_LIBRARY_CONSUMERS,
            "platform_capability",
            "Shared configuration capability candidate",
            " ".join(
                (
                    "Repeated configuration loading warrants a governed convention or library.",
                    "It should include secret handling and environment-specific settings.",
                )
            ),
        ),
    }
    output: list[Recommendation] = []
    for capability, (minimum, category, title, rationale) in rules.items():
        findings = by_capability.get(capability, [])
        tools = sorted({item.tool_inventory_id for item in findings})
        if len(tools) >= minimum:
            output.append(
                Recommendation(
                    category=category,
                    title=title,
                    rationale=rationale,
                    affected_tool_ids=tools,
                    confidence=Confidence.MEDIUM,
                    evidence=[fact for finding in findings for fact in finding.evidence],
                )
            )
    return output


def _datasource_recommendations(datasources: list[Datasource]) -> list[Recommendation]:
    grouped: dict[tuple[str, str | None, str | None], list[Datasource]] = defaultdict(list)
    for source in datasources:
        if source.platform != "Unknown" and (source.server or source.database):
            grouped[(source.platform, source.server, source.database)].append(source)
    output: list[Recommendation] = []
    for (platform, server, database), sources in grouped.items():
        tools = sorted({item.tool_inventory_id for item in sources})
        if len(tools) < MIN_SERVICE_CONSUMERS:
            continue
        target = " / ".join(value for value in (platform, server, database) if value)
        output.append(
            Recommendation(
                category="shared_data_access",
                title=f"Shared data-access review: {target}",
                rationale=" ".join(
                    (
                        "Several applications depend on the same datasource.",
                        (
                            "Evaluate whether contracts, access controls, and ownership "
                            "justify an API."
                        ),
                        "Do not introduce one solely because access is repeated.",
                    )
                ),
                affected_tool_ids=tools,
                confidence=Confidence.MEDIUM,
                evidence=[fact for source in sources for fact in source.evidence],
            )
        )
    return output
