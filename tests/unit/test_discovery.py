"""Data-driven discovery must find new patterns without a solution catalogue."""

from pathlib import Path

from portfolio_analyzer.models import (
    AnalysisCoverage,
    ArtifactStatus,
    Datasource,
    Evidence,
    ExtractedApplication,
    ExtractedObject,
    InventoryRecord,
    SemanticPortfolioState,
    SemanticSource,
    StagedArtifact,
)
from portfolio_analyzer.portfolio.discovery import discover_themes
from portfolio_analyzer.semantic.architecture import synthesize_architecture
from portfolio_analyzer.semantic.config import SemanticSettings
from portfolio_analyzer.semantic.pipeline import _citation_validity, run_semantic_pipeline


def discovered_portfolio(*, warning: bool = False) -> tuple[
    SemanticPortfolioState, list[InventoryRecord],
]:
    inventory = [InventoryRecord(tool_inventory_id=str(i), tool_name=f"App {i}",
                                 inventory_filename=f"{i}.accdb", filepath=Path(f"{i}.accdb"))
                 for i in range(1, 4)]
    artifacts = [StagedArtifact(
        tool_inventory_id=str(i), original_source_path=Path(f"{i}.accdb"),
        local_staged_path=Path(f"staged/{i}.accdb"), filename=f"{i}.accdb", extension=".accdb",
        sha256=str(i) * 64, status=ArtifactStatus.STAGED,
    ) for i in range(1, 4)]
    extracted = []
    for i in range(1, 4):
        objects = [ExtractedObject(object_type="report", name=f"rpt{i}")]
        if i in {1, 2}:
            objects.append(ExtractedObject(object_type="query", name=f"Refresh{i}",
                definition="UPDATE Sales SET Amount = Amount + 1 WHERE Amount > 0"))
        if i in {2, 3}:
            objects.extend([
                ExtractedObject(object_type="linked_table", name="Claims"),
                ExtractedObject(object_type="query", name="ReadClaims",
                    definition="SELECT ClaimId, Amount FROM Claims WHERE IsOpen = True"),
            ])
        extracted.append((str(i) * 64, ExtractedApplication(
            tool_inventory_id=str(i), staged_path=Path(f"staged/{i}.accdb"),
            extractor_version="test", objects=objects,
        )))
    datasources = [Datasource(tool_inventory_id=str(i), platform="SQL Server", server="srv",
                             database="ClaimsDb", object_name="Claims") for i in (2, 3)]
    coverage = [AnalysisCoverage(
        tool_inventory_id=str(i), tool_name=f"App {i}", staging_status="staged",
        extraction_status="complete", analysis_status="complete",
        extraction_warning_count=int(warning and i == 2),
    ) for i in range(1, 4)]
    state = run_semantic_pipeline(SemanticSettings(), None, inventory, artifacts, extracted,
                                  [], datasources, coverage, [])
    return state, inventory


def signal(app: str, label: str) -> Evidence:
    return Evidence(tool_inventory_id=app, artifact_path=f"{app}.accdb", object_type="module",
                    object_name=f"Module{app}", text=label, inference=label)


def test_new_observations_create_overlapping_groups_without_taxonomy() -> None:
    facts = [signal(app, label) for app, label in [
        ("a", "Barcode reconciliation"), ("b", "Barcode reconciliation"),
        ("b", "Laboratory instrument feed"), ("c", "Laboratory instrument feed"),
        ("d", "Only seen here"),
    ]]
    themes = discover_themes(evidence=facts)
    assert {tuple(t.affected_tool_ids) for t in themes} == {("a", "b"), ("b", "c")}
    assert any("Barcode reconciliation" in t.title for t in themes)
    assert any("Laboratory instrument feed" in t.title for t in themes)
    assert all(t.alternative_options and t.grouping_basis for t in themes)
    assert [t.model_dump() for t in themes] == [
        t.model_dump() for t in discover_themes(evidence=reversed(facts))
    ]


def test_same_membership_combines_features_without_losing_citations() -> None:
    facts = [signal(app, label) for app in ("a", "b") for label in ("Metric A", "Metric B")]
    themes = discover_themes(evidence=facts)
    assert len(themes) == 1
    assert len(themes[0].grouping_basis) == 2
    assert {loc.evidence_id for loc in themes[0].locations} == {e.evidence_id for e in facts}


def test_discovered_groups_persist_and_drive_overlapping_architecture() -> None:
    state, _ = discovered_portfolio()
    assert {tuple(t.affected_tool_ids) for t in state.discovered_themes} == {("1", "2"), ("2", "3")}
    assert _citation_validity(state) == 1.0
    components = state.architecture.components
    assert len(components) == 2
    assert all(c.component_type == "discovered_boundary" for c in components)
    mapping = next(m for m in state.architecture.mappings if m.tool_inventory_id == "2")
    assert len(mapping.target_component_ids) == 2
    assert all(c.track == "vendor_neutral" for c in components)
    assert any("ClaimsDb" in t.title for t in state.discovered_themes)
    assert any("Refresh" in t.title for t in state.discovered_themes)
    assert all(
        loc.artifact.endswith(".accdb") for t in state.discovered_themes for loc in t.locations
    )


def test_local_names_and_incomplete_connections_do_not_imply_shared_data() -> None:
    for platform, server, database in [
        ("Access (local)", None, "Sales"), ("SQL Server", "srv", None),
        ("Access", None, "local.accdb"), ("Unknown", "srv", "Sales"),
    ]:
        sources = [Datasource(tool_inventory_id=app, platform=platform, server=server,
                              database=database, object_name="Sales", evidence=[Evidence(
                                  tool_inventory_id=app, artifact_path=f"{app}.accdb",
                                  object_type="table", object_name="Sales", text="connection",
                              )]) for app in ("a", "b")]
        assert discover_themes(datasources=sources) == []


def test_different_databases_on_same_server_remain_separate() -> None:
    sources = [Datasource(tool_inventory_id=app, platform="SQL Server", server="srv",
                          database=app, evidence=[Evidence(
                              tool_inventory_id=app, artifact_path=f"{app}.accdb",
                              object_type="table", object_name="Sales", text="connection",
                          )]) for app in ("a", "b")]
    assert discover_themes(datasources=sources) == []


def test_matching_code_requires_complete_definitions_and_preserves_literals() -> None:
    text = 'SELECT CustomerId, Amount FROM Sales WHERE Region = "North East"'
    sources = [SemanticSource(source_id=app, tool_inventory_id=app, artifact_hash=app,
                              object_type="query", object_name=app, excerpt=text,
                              content_sha256="unused") for app in ("a", "b")]
    assert len(discover_themes(sources=sources)) == 1
    sources[1].excerpt = text.replace("North East", "South West")
    assert discover_themes(sources=sources) == []
    sources[1].excerpt = text
    sources[1].segment_count = 2
    assert discover_themes(sources=sources) == []


def test_discovery_references_are_part_of_citation_validation() -> None:
    state, _ = discovered_portfolio()
    state.discovered_themes[0].locations[0].evidence_id = "invented-reference"
    assert _citation_validity(state) < 1


def test_optional_architecture_receives_discovery_without_fixed_group_labels() -> None:
    from typing import Any

    state, _ = discovered_portfolio()

    class ProposalProvider:
        def complete_json(self, **kwargs: Any) -> dict[str, Any]:
            import json
            payload = json.loads(kwargs["user"].split("\n", 1)[1].rsplit("\n", 1)[0])
            assert payload["discovered_groups"]
            assert any("ClaimsDb" in g["label"] for g in payload["discovered_groups"])
            refs = [loc.evidence_id for t in state.discovered_themes for loc in t.locations
                    if loc.tool_inventory_id == "2"]
            return {"title": "Design", "summary": "Review proposal", "components": [{
                "key": "new", "track": "vendor_neutral", "name": "Claims reconciliation hub",
                "component_type": "reconciliation", "description": "A new proposed boundary",
                "application_ids": ["2", "3"], "evidence_ids": refs,
            }]}

    architecture, error = synthesize_architecture(
        ProposalProvider(), state.applications, state.clusters, [], [], [],
        all_tool_ids=["1", "2", "3"], approved_services=[], themes=state.discovered_themes,
    )
    assert error is None
    proposed = next(c for c in architecture.components if c.name == "Claims reconciliation hub")
    assert proposed.application_ids == ["2"]  # App 3 has no supplied support for this proposal.
