import json
import re
from pathlib import Path

from portfolio_analyzer.models import (
    Datasource,
    Dependency,
    Evidence,
    InventoryRecord,
)
from portfolio_analyzer.reporting.intelligence import write_intelligence_html
from portfolio_analyzer.reporting.network import dependency_network


def inventory() -> list[InventoryRecord]:
    return [
        InventoryRecord(
            tool_inventory_id=i,
            tool_name=f"App {i}",
            inventory_filename=f"{i}.accdb",
            filepath=Path(f"{i}.accdb"),
        )
        for i in ("1", "2", "3")
    ]


def evidence(app: str, name: str = "Orders", artifact: str = "a.accdb") -> Evidence:
    return Evidence(
        tool_inventory_id=app,
        artifact_path=artifact,
        object_type="query",
        object_name=name,
        location="SQL line 1",
        text="SELECT * FROM Orders",
    )


def test_network_merges_confirmed_databases_preserving_objects_and_operations() -> None:
    sources = [
        Datasource(
            tool_inventory_id=app,
            platform="SQL Server",
            server=server,
            database="Sales",
            schema_name="dbo",
            object_name=table,
            operation=op,
            evidence=[evidence(app, table)],
        )
        for app, server, table, op in [
            ("1", "sql01", "Orders", "READ"),
            ("1", "SQL01", "Customers", "UPDATE"),
            ("2", "sql01", "Orders", "INSERT"),
            ("3", "sql02", "Orders", "READ"),
        ]
    ]
    graph = dependency_network(inventory(), [], sources)
    assert len(graph["edges"]) == 3
    assert len(graph["candidates"]) == 1
    assert graph["candidates"][0]["application_ids"] == ["1", "2"]
    first = next(e for e in graph["edges"] if e["app_id"] == "1")
    assert first["operations"] == ["READ", "UPDATE"]
    assert {e["target"] for e in first["evidence"]} == {"Orders", "Customers"}
    assert {e["evidence_id"] for e in first["evidence"]} == {
        evidence("1", "Orders").evidence_id,
        evidence("1", "Customers").evidence_id,
    }


def test_same_local_or_unresolved_names_cannot_join_applications_or_artifacts() -> None:
    sources = [
        Datasource(
            tool_inventory_id=app,
            platform=platform,
            object_name="Orders",
            evidence=[evidence(app, artifact=artifact)],
        )
        for app, platform, artifact in [
            ("1", "Access (local)", "one.accdb"),
            ("1", "Access (local)", "two.accdb"),
            ("2", "Access (local)", "two.accdb"),
            ("1", "Unknown", "one.accdb"),
            ("2", "Unknown", "one.accdb"),
        ]
    ]
    graph = dependency_network(inventory(), [], sources)
    assert len(graph["edges"]) == 5
    assert not graph["candidates"]
    assert all(n["consumer_count"] <= 1 for n in graph["nodes"])


def test_network_keeps_all_edges_and_excludes_runtime_from_boundary_candidates() -> None:
    deps = [
        Dependency(
            tool_inventory_id="1",
            source=f"Module{i}",
            target=rf"C:\local\file{i}.csv",
            dependency_type="filesystem",
        )
        for i in range(150)
    ]
    for app in ("1", "2"):
        deps.extend(
            [
                Dependency(
                    tool_inventory_id=app,
                    source="DAO",
                    target=r"C:\system\dao.dll",
                    dependency_type="access_vba_reference",
                ),
                Dependency(
                    tool_inventory_id=app,
                    source="Export",
                    target=r"\\files\team\out.csv",
                    dependency_type="filesystem",
                    operation="WRITE",
                ),
                Dependency(
                    tool_inventory_id=app,
                    source="Query",
                    target="Accounts",
                    dependency_type="query",
                ),
            ]
        )
    graph = dependency_network(inventory(), deps, [])
    assert len(graph["edges"]) == 156
    assert len(graph["candidates"]) == 1
    candidate = graph["candidates"][0]
    assert candidate["label"] == r"\\files\team\out.csv"
    assert {n["kind"] for n in graph["nodes"]} >= {"runtime", "local_file", "shared_file"}
    node_ids = {n["id"] for n in graph["nodes"]}
    assert all(e["source"] in node_ids and e["target"] in node_ids for e in graph["edges"])
    assert graph == dependency_network(inventory(), list(reversed(deps)), [])


def test_report_embeds_complete_safe_network_with_functional_controls(tmp_path: Path) -> None:
    target = '</script><script>alert("bad")</script>'
    deps = [
        Dependency(tool_inventory_id="1", source="Module", target=target, dependency_type="unknown")
    ]
    path = tmp_path / "report.html"
    write_intelligence_html(
        path, inventory(), [], None, semantic_status="unavailable", dependencies=deps
    )
    html = path.read_text()
    data = json.loads(
        re.search(
            r'<script id="portfolio-data" type="application/json">(.*?)</script>', html, re.S
        ).group(1)
    )
    assert data["network"]["raw_dependency_count"] == 1
    assert target not in html
    assert any(n["label"] == target for n in data["network"]["nodes"])
    assert 'id="network-canvas"' in html
    assert 'id="network-shared" checked' in html
    assert "Evidence-supported reuse candidates" in html
    assert "function showResource(" in html
    assert "function networkSelection(" in html
    assert "Consolidation map" not in html


def test_ui_alias_joins_only_its_artifact_and_staging_files_are_separate(tmp_path: Path) -> None:
    from test_intelligence_reporting import _state

    from portfolio_analyzer.models import (
        ArtifactStatus,
        BehaviorFact,
        StagedArtifact,
    )

    artifact_path = tmp_path / "staged.accdb"
    artifact = StagedArtifact(
        tool_inventory_id="1",
        original_source_path=Path("original.accdb"),
        local_staged_path=artifact_path,
        filename="staged.accdb",
        extension=".accdb",
        sha256="a" * 64,
        status=ArtifactStatus.STAGED,
    )
    state = _state()
    state.application_irs[0].behavior_facts = [
        BehaviorFact(
            action="entry",
            artifact_hash=key,
            description="Maintains linked orders",
            object_type="form",
            object_name="OrderEntry",
            targets=["OrderAlias"],
            datasource_scope="external",
            evidence_ids=["source-" + key],
        )
        for key in ("a" * 64, "other-artifact")
    ]
    source = Datasource(
        tool_inventory_id="1",
        platform="SQL Server",
        server="sql01",
        database="Sales",
        object_name="Orders",
        evidence=[
            Evidence(
                tool_inventory_id="1",
                artifact_path=str(artifact_path),
                object_type="linked_table",
                object_name="OrderAlias",
                text="Linked table",
                location="TableDef.Connect",
            )
        ],
    )
    dependency = Dependency(
        tool_inventory_id="1",
        source="Extractor",
        target=str(artifact_path),
        dependency_type="filesystem",
    )
    graph = dependency_network(inventory(), [dependency], [source], state, [artifact])
    external = next(n for n in graph["nodes"] if n["kind"] == "external_data")
    edge = next(e for e in graph["edges"] if e["target"] == external["id"])
    assert "ENTRY" in edge["operations"]
    assert any(e["evidence_id"] == "source-" + "a" * 64 for e in edge["evidence"])
    assert not any(e["evidence_id"] == "source-other-artifact" for e in edge["evidence"])
    assert any(n["kind"] == "unresolved" for n in graph["nodes"])
    assert any(n["kind"] == "analysis_artifact" for n in graph["nodes"])
    assert not graph["candidates"]
