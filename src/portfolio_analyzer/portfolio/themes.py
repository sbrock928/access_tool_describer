"""Report discovered evidence groups and grounded optional architecture proposals."""

from __future__ import annotations

from collections.abc import Iterable

from portfolio_analyzer.models import (
    AnalysisCoverage,
    Datasource,
    Evidence,
    PortfolioTheme,
    Recommendation,
    SemanticPortfolioState,
    StagedArtifact,
    ThemeLocation,
)
from portfolio_analyzer.portfolio.discovery import discover_themes, evidence_location


def build_portfolio_themes(
    state: SemanticPortfolioState | None,
    recommendations: Iterable[Recommendation] = (),
    evidence: Iterable[Evidence] = (),
    coverage: Iterable[AnalysisCoverage] = (),
    *,
    artifacts: Iterable[StagedArtifact] = (),
    datasources: Iterable[Datasource] = (),
) -> list[PortfolioTheme]:
    # Old recommendations contribute their observations, never predefined group names.
    observations = {e.evidence_id: e for e in evidence}
    for recommendation in recommendations:
        observations.update((e.evidence_id, e) for e in recommendation.evidence)
    artifacts = list(artifacts)
    coverage = list(coverage)
    datasources = list(datasources)
    themes = (
        [t.model_copy(deep=True) for t in state.discovered_themes]
        if state and state.discovered_themes else
        discover_themes(
            state.applications if state else [], state.sources if state else [],
            state.application_irs if state else [], observations.values(), datasources,
            coverage, artifacts,
        )
    )
    if state and state.metadata.architecture_model_generation:
        source_by_id = {s.source_id: s for s in state.sources}
        for component in state.architecture.components:
            if (
                component.review_status == "rejected"
                or component.component_type == "discovered_boundary"
            ):
                continue
            locations: list[ThemeLocation] = []
            for ref in component.evidence_ids:
                if ref in observations:
                    locations.append(evidence_location(observations[ref]))
                elif ref in source_by_id:
                    source = source_by_id[ref]
                    locations.append(
                        ThemeLocation(
                            tool_inventory_id=source.tool_inventory_id,
                            object_type=source.object_type,
                            object_name=source.object_name,
                            artifact=source.artifact_hash,
                            location=source.location or "",
                            observation=source.excerpt[:400],
                            evidence_id=ref,
                        )
                    )
            locations = [
                loc for loc in locations if loc.tool_inventory_id in component.application_ids
            ]
            supported = {loc.tool_inventory_id for loc in locations}
            if not supported or supported != set(component.application_ids):
                continue
            themes.append(
                PortfolioTheme(
                    theme_id="theme-" + component.component_id,
                    title=component.name,
                    category="model-proposed design",
                    generation_method="local_model_proposal",
                    observed_pattern="Proposed from cited application evidence; requires review.",
                    proposed_solution=component.description,
                    affected_tool_ids=sorted(supported),
                    grouping_basis=["Optional architecture model proposal"],
                    locations=locations,
                    confidence=component.confidence,
                    validation_questions=[
                        "Does this proposed boundary fit all affected applications?"
                    ],
                    coverage_note="A model interpretation; requires validation before adoption.",
                )
            )
    paths = {(a.tool_inventory_id, a.sha256): str(a.original_source_path) for a in artifacts}
    for theme in themes:
        for location in theme.locations:
            location.artifact = paths.get(
                (location.tool_inventory_id, location.artifact), location.artifact
            )
    return sorted(themes, key=lambda t: (-len(t.affected_tool_ids), t.title, t.theme_id))


def theme_rows(themes: list[PortfolioTheme], names: dict[str, str]) -> list[dict[str, str]]:
    return [
        {
            "theme_id": t.theme_id,
            "theme": t.title,
            "category": t.category,
            "observed_pattern": t.observed_pattern,
            "proposed_solution": t.proposed_solution,
            "affected_applications": " | ".join(names.get(i, i) for i in t.affected_tool_ids),
            "application_count": str(len(t.affected_tool_ids)),
            "next_steps": " | ".join(t.next_steps),
            "validation_questions": " | ".join(t.validation_questions),
            "confidence": t.confidence.value,
            "coverage_note": t.coverage_note,
            "grouping_basis": " | ".join(t.grouping_basis),
            "alternative_options": " | ".join(t.alternative_options),
            "generation_method": t.generation_method,
        }
        for t in themes
    ]


def theme_location_rows(
    themes: list[PortfolioTheme],
    names: dict[str, str],
) -> list[dict[str, str]]:
    return [
        {
            "theme_id": t.theme_id,
            "theme": t.title,
            "euc_name": names.get(loc.tool_inventory_id, loc.tool_inventory_id),
            "object_type": loc.object_type,
            "object_name": loc.object_name,
            "artifact_path_or_hash": loc.artifact,
            "location": loc.location,
            "observation": loc.observation,
            "evidence_id": loc.evidence_id,
        }
        for t in themes
        for loc in t.locations
    ]


THEME_HEADERS = [
    "theme_id",
    "theme",
    "category",
    "observed_pattern",
    "proposed_solution",
    "affected_applications",
    "application_count",
    "next_steps",
    "validation_questions",
    "confidence",
    "coverage_note",
    "grouping_basis",
    "alternative_options",
    "generation_method",
]
THEME_LOCATION_HEADERS = [
    "theme_id",
    "theme",
    "euc_name",
    "object_type",
    "object_name",
    "artifact_path_or_hash",
    "location",
    "observation",
    "evidence_id",
]
