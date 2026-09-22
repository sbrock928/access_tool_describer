"""Semantic CSV, JSON, HTML, Mermaid, and manifest report surfaces."""

# Embedded HTML, CSS, and JavaScript intentionally retain readable web-native lines.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

from portfolio_analyzer.models import (
    AnalysisCoverage,
    Dependency,
    InventoryRecord,
    SemanticPortfolioState,
)
from portfolio_analyzer.reporting.writers import write_csv


def write_semantic_datasets(
    directory: Path,
    state: SemanticPortfolioState,
    application_names: dict[str, str],
) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    outputs.append(
        _csv(
            directory / "semantic_applications.csv",
            [
                {
                    "euc_name": application_names.get(profile.tool_inventory_id, "Unknown EUC"),
                    "summary": profile.summary,
                    "business_purpose": profile.business_purpose,
                    "primary_archetype": profile.primary_archetype,
                    "proposed_disposition": profile.proposed_disposition,
                    "confidence": profile.confidence.value,
                    "status": profile.status,
                    "open_questions": " | ".join(profile.open_questions),
                    "evidence_ids": " | ".join(profile.evidence_ids),
                    "claim_ids": " | ".join(profile.claim_ids),
                }
                for profile in state.applications
            ],
            [
                "euc_name",
                "summary",
                "business_purpose",
                "primary_archetype",
                "proposed_disposition",
                "confidence",
                "status",
                "open_questions",
                "evidence_ids",
                "claim_ids",
            ],
        )
    )
    outputs.append(
        _csv(
            directory / "semantic_findings.csv",
            [
                {
                    "finding_id": finding.finding_id,
                    "euc_name": application_names.get(profile.tool_inventory_id, "Unknown EUC"),
                    "category": finding.category,
                    "label": finding.label,
                    "description": finding.description,
                    "confidence": finding.confidence.value,
                    "review_status": finding.review_status,
                    "evidence_ids": " | ".join(finding.evidence_ids),
                    "claim_ids": " | ".join(finding.claim_ids),
                }
                for profile in state.applications
                for finding in profile.findings
            ],
            [
                "finding_id",
                "euc_name",
                "category",
                "label",
                "description",
                "confidence",
                "review_status",
                "evidence_ids",
                "claim_ids",
            ],
        )
    )
    outputs.append(
        _csv(
            directory / "portfolio_clusters.csv",
            [
                {
                    "cluster_id": cluster.cluster_id,
                    "label": cluster.label,
                    "application_count": len(cluster.application_ids),
                    "euc_names": " | ".join(
                        application_names.get(tool_id, "Unknown EUC")
                        for tool_id in cluster.application_ids
                    ),
                    "shared_capabilities": " | ".join(cluster.shared_capabilities),
                    "shared_data_domains": " | ".join(cluster.shared_data_domains),
                    "rationale": cluster.rationale,
                    "confidence": cluster.confidence.value,
                    "evidence_ids": " | ".join(cluster.evidence_ids),
                }
                for cluster in state.clusters
            ],
            [
                "cluster_id",
                "label",
                "application_count",
                "euc_names",
                "shared_capabilities",
                "shared_data_domains",
                "rationale",
                "confidence",
                "evidence_ids",
            ],
        )
    )
    outputs.append(
        _csv(
            directory / "similarity_edges.csv",
            [
                {
                    "source_euc_name": application_names.get(edge.source_tool_id, "Unknown EUC"),
                    "target_euc_name": application_names.get(edge.target_tool_id, "Unknown EUC"),
                    "overall_similarity": edge.overall_similarity,
                    "category_scores": json.dumps(edge.category_scores, sort_keys=True),
                    "shared_features": json.dumps(edge.shared_features, sort_keys=True),
                }
                for edge in state.similarity_edges
            ],
            [
                "source_euc_name",
                "target_euc_name",
                "overall_similarity",
                "category_scores",
                "shared_features",
            ],
        )
    )
    outputs.append(
        _csv(
            directory / "architecture_components.csv",
            [
                {
                    "component_id": item.component_id,
                    "track": item.track,
                    "name": item.name,
                    "component_type": item.component_type,
                    "platform_service": item.platform_service or "",
                    "description": item.description,
                    "application_count": len(item.application_ids),
                    "cluster_ids": " | ".join(item.cluster_ids),
                    "confidence": item.confidence.value,
                    "review_status": item.review_status,
                    "evidence_ids": " | ".join(item.evidence_ids),
                    "claim_ids": " | ".join(item.claim_ids),
                }
                for item in state.architecture.components
            ],
            [
                "component_id",
                "track",
                "name",
                "component_type",
                "platform_service",
                "description",
                "application_count",
                "cluster_ids",
                "confidence",
                "review_status",
                "evidence_ids",
                "claim_ids",
            ],
        )
    )
    outputs.append(
        _csv(
            directory / "architecture_relations.csv",
            [item.model_dump(mode="json") for item in state.architecture.relations],
            [
                "relation_id",
                "track",
                "source_component_id",
                "target_component_id",
                "relationship",
                "description",
                "evidence_ids",
                "confidence",
            ],
        )
    )
    outputs.append(
        _csv(
            directory / "application_target_map.csv",
            [
                {
                    "mapping_id": item.mapping_id,
                    "euc_name": application_names.get(item.tool_inventory_id, "Unknown EUC"),
                    "disposition": item.disposition,
                    "target_component_ids": " | ".join(item.target_component_ids),
                    "wave": item.wave,
                    "rationale": item.rationale,
                    "prerequisites": " | ".join(item.prerequisites),
                    "confidence": item.confidence.value,
                    "review_status": item.review_status,
                    "evidence_ids": " | ".join(item.evidence_ids),
                    "claim_ids": " | ".join(item.claim_ids),
                }
                for item in state.architecture.mappings
            ],
            [
                "mapping_id",
                "euc_name",
                "disposition",
                "target_component_ids",
                "wave",
                "rationale",
                "prerequisites",
                "confidence",
                "review_status",
                "evidence_ids",
                "claim_ids",
            ],
        )
    )
    outputs.append(
        _csv(
            directory / "migration_waves.csv",
            [
                {
                    "wave": item.wave,
                    "name": item.name,
                    "purpose": item.purpose,
                    "application_count": len(item.application_ids),
                    "euc_names": " | ".join(
                        application_names.get(tool_id, "Unknown EUC")
                        for tool_id in item.application_ids
                    ),
                    "prerequisites": " | ".join(item.prerequisites),
                }
                for item in state.architecture.migration_waves
            ],
            [
                "wave",
                "name",
                "purpose",
                "application_count",
                "euc_names",
                "prerequisites",
            ],
        )
    )
    architecture_path = directory / "architecture_model.json"
    architecture_path.write_text(
        json.dumps(state.architecture.model_dump(mode="json"), indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    outputs.append(architecture_path)
    return outputs


def write_architecture_mermaid(
    path: Path,
    state: SemanticPortfolioState,
) -> None:
    sections = ["# Proposed Target Architecture", ""]
    for track, title in (
        ("vendor_neutral", "Vendor-neutral architecture"),
        ("microsoft", "Microsoft implementation mapping"),
    ):
        components = [item for item in state.architecture.components if item.track == track]
        relations = [item for item in state.architecture.relations if item.track == track]
        sections.extend([f"## {title}", "", "```mermaid", "flowchart LR"])
        if not components:
            sections.append('  unavailable["No supported components proposed"]')
        else:
            groups: dict[str, list[Any]] = {}
            for component in components:
                groups.setdefault(component.component_type, []).append(component)
            for group_index, (component_type, items) in enumerate(sorted(groups.items())):
                sections.append(
                    f'  subgraph group_{group_index}["{_mermaid_label(component_type)}"]'
                )
                for component in items:
                    label = component.name
                    if component.platform_service:
                        label += f" ({component.platform_service})"
                    sections.append(
                        f'    {_mermaid_id(component.component_id)}["{_mermaid_label(label)}"]'
                    )
                sections.append("  end")
            valid = {item.component_id for item in components}
            for relation in relations:
                if relation.source_component_id in valid and relation.target_component_id in valid:
                    sections.append(
                        f"  {_mermaid_id(relation.source_component_id)} -->|"
                        f"{_mermaid_label(relation.relationship)}| "
                        f"{_mermaid_id(relation.target_component_id)}"
                    )
        sections.extend(["```", ""])
    sections.extend(
        [
            "## Interpretation boundary",
            "",
            "These diagrams are reviewable proposals. Detailed evidence, claims, mappings, and "
            "open decisions are retained in the workbook and machine-readable architecture model.",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(sections), encoding="utf-8", newline="\n")


def write_intelligence_html(
    path: Path,
    inventory: list[InventoryRecord],
    coverage: list[AnalysisCoverage],
    state: SemanticPortfolioState | None,
    *,
    semantic_status: str,
    dependencies: list[Dependency] | None = None,
) -> None:
    names = {item.tool_inventory_id: item.tool_name for item in inventory}
    profiles = state.applications if state else []
    mappings = state.architecture.mappings if state else []
    mapping_by_id = {item.tool_inventory_id: item for item in mappings}
    profile_by_id = {item.tool_inventory_id: item for item in profiles}
    sources_by_id: dict[str, list[dict[str, str]]] = {}
    claims_by_id: dict[str, list[dict[str, str]]] = {}
    if state:
        for source in state.sources:
            sources_by_id.setdefault(source.tool_inventory_id, []).append(
                {
                    "source_id": source.source_id,
                    "object": f"{source.object_type}: {source.object_name}",
                    "excerpt": source.excerpt,
                }
            )
        for claim in state.claims:
            claims_by_id.setdefault(claim.tool_inventory_id, []).append(
                {
                    "claim_id": claim.claim_id,
                    "field": claim.field,
                    "value": claim.value,
                    "source": claim.source,
                }
            )
    unique_ids = list(dict.fromkeys(item.tool_inventory_id for item in inventory))
    complete = len(
        {
            item.tool_inventory_id
            for item in coverage
            if item.analysis_status == "complete" and item.extraction_status == "complete"
        }
    )
    archetypes = Counter(item.primary_archetype for item in profiles)
    waves = Counter(item.wave for item in mappings)
    application_rows = []
    for tool_id in unique_ids:
        profile = profile_by_id.get(tool_id)
        mapping = mapping_by_id.get(tool_id)
        application_rows.append(
            {
                "id": tool_id,
                "name": names.get(tool_id, "Unknown EUC"),
                "summary": profile.summary if profile else "Semantic profile unavailable.",
                "purpose": profile.business_purpose if profile else "Unknown",
                "archetype": profile.primary_archetype if profile else "unknown",
                "confidence": profile.confidence.value if profile else "low",
                "disposition": mapping.disposition if mapping else "investigate",
                "wave": mapping.wave if mapping else 0,
                "review": mapping.review_status if mapping else "pending",
                "findings": [item.model_dump(mode="json") for item in profile.findings]
                if profile
                else [],
                "observed_sources": sources_by_id.get(tool_id, []),
                "owner_claims": claims_by_id.get(tool_id, []),
                "open_questions": profile.open_questions if profile else [],
            }
        )
    data = {
        "semantic_status": semantic_status,
        "applications": application_rows,
        "clusters": [item.model_dump(mode="json") for item in state.clusters] if state else [],
        "edges": [item.model_dump(mode="json") for item in state.similarity_edges] if state else [],
        "architecture": state.architecture.model_dump(mode="json") if state else {},
        "names": names,
    }
    safe_json = (
        json.dumps(data, ensure_ascii=True)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    html = _html_document(
        safe_json=safe_json,
        inventory_count=len(unique_ids),
        complete_count=complete,
        profile_count=len(profiles),
        cluster_count=len(state.clusters) if state else 0,
        semantic_status=semantic_status,
        archetype_bars=_bars(archetypes),
        wave_bars=_bars({f"Wave {key}": value for key, value in sorted(waves.items())}),
        architecture_html=_architecture_cards(state),
        cluster_svg=_cluster_svg(state, names),
        dependency_svg=_dependency_svg(dependencies or [], names),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8", newline="\n")


def write_report_manifest(
    path: Path,
    files: list[Path],
    state: SemanticPortfolioState | None,
    *,
    semantic_status: str,
) -> None:
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "semantic_status": semantic_status,
        "semantic_metadata": state.metadata.model_dump(mode="json") if state else None,
        "counts": {
            "semantic_applications": len(state.applications) if state else 0,
            "semantic_findings": (
                sum(len(profile.findings) for profile in state.applications) if state else 0
            ),
            "clusters": len(state.clusters) if state else 0,
            "architecture_components": len(state.architecture.components) if state else 0,
            "architecture_mappings": len(state.architecture.mappings) if state else 0,
        },
        "files": [
            {
                "name": item.name,
                "sha256": hashlib.sha256(item.read_bytes()).hexdigest(),
                "size_bytes": item.stat().st_size,
            }
            for item in sorted(files)
            if item.exists() and item != path
        ],
    }
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def _csv(path: Path, rows: list[dict[str, Any]], headers: list[str]) -> Path:
    normalized = [
        {
            key: " | ".join(str(item) for item in value) if isinstance(value, list) else value
            for key, value in row.items()
        }
        for row in rows
    ]
    write_csv(path, normalized, headers=headers)
    return path


def _bars(values: Counter[str] | dict[str, int]) -> str:
    if not values:
        return '<p class="muted">No semantic data available.</p>'
    maximum = max(values.values()) or 1
    return "".join(
        f'<div class="bar-row"><span>{escape(str(label))}</span>'
        f'<div class="bar"><i style="width:{count / maximum * 100:.1f}%"></i>'
        f"</div><strong>{count}</strong></div>"
        for label, count in sorted(values.items(), key=lambda item: (-item[1], str(item[0])))
    )


def _architecture_cards(state: SemanticPortfolioState | None) -> str:
    if state is None:
        return '<p class="muted">Run semantic analysis to generate target architecture.</p>'
    sections = []
    for track, title in (
        ("vendor_neutral", "Vendor-neutral"),
        ("microsoft", "Microsoft mapping"),
    ):
        cards = []
        for item in state.architecture.components:
            if item.track != track or item.review_status == "rejected":
                continue
            service = (
                f"<small>{escape(item.platform_service)}</small>" if item.platform_service else ""
            )
            cards.append(
                '<article class="component"><span class="tag">{}</span><h3>{}</h3>{}'
                "<p>{}</p><small>{} confidence · {}</small></article>".format(
                    escape(item.component_type.replace("_", " ")),
                    escape(item.name),
                    service,
                    escape(item.description),
                    escape(item.confidence.value),
                    escape(item.review_status),
                )
            )
        sections.append(f'<h3>{title}</h3><div class="component-grid">{"".join(cards)}</div>')
    return "".join(sections)


def _cluster_svg(state: SemanticPortfolioState | None, names: dict[str, str]) -> str:
    if state is None or not state.clusters:
        return '<p class="muted">No semantic clusters available.</p>'
    width = 900
    row_height = 120
    height = max(260, len(state.clusters) * row_height + 40)
    node_positions: dict[str, tuple[float, float]] = {}
    elements: list[str] = []
    for row, cluster in enumerate(state.clusters):
        y = 70 + row * row_height
        elements.append(
            f'<text x="18" y="{y - 28}" class="cluster-label">{escape(cluster.label)}</text>'
        )
        count = max(1, len(cluster.application_ids))
        for column, tool_id in enumerate(cluster.application_ids):
            x = 190 + (column + 0.5) * min(120, 650 / count)
            node_positions[tool_id] = (x, y)
    for edge in state.similarity_edges:
        left = node_positions.get(edge.source_tool_id)
        right = node_positions.get(edge.target_tool_id)
        if left and right:
            elements.append(
                f'<line x1="{left[0]:.1f}" y1="{left[1]:.1f}" '
                f'x2="{right[0]:.1f}" y2="{right[1]:.1f}" class="edge" />'
            )
    for tool_id, (node_x, node_y) in node_positions.items():
        label = names.get(tool_id, "Unknown EUC")
        elements.append(
            f'<g class="app-node" data-app-id="{escape(tool_id)}" tabindex="0">'
            f'<circle cx="{node_x:.1f}" cy="{node_y:.1f}" r="17" />'
            f"<title>{escape(label)}</title>"
            f'<text x="{node_x:.1f}" y="{node_y + 35:.1f}">{escape(_short(label, 18))}</text></g>'
        )
    return (
        f'<svg class="cluster-map" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Semantic application clusters">{"".join(elements)}</svg>'
    )


def _dependency_svg(dependencies: list[Dependency], names: dict[str, str]) -> str:
    if not dependencies:
        return '<p class="muted">No observed dependencies were extracted.</p>'
    visible = sorted(
        dependencies,
        key=lambda item: (names.get(item.tool_inventory_id, ""), item.source, item.target),
    )[:100]
    width = 1000
    row_height = 54
    height = len(visible) * row_height + 78
    elements = [
        '<text x="18" y="28" class="dependency-heading">Application</text>',
        '<text x="340" y="28" class="dependency-heading">Source object</text>',
        '<text x="700" y="28" class="dependency-heading">Target</text>',
    ]
    for index, item in enumerate(visible):
        y = 58 + index * row_height
        name = names.get(item.tool_inventory_id, "Unknown EUC")
        title = (
            f"{name}: {item.source} to {item.target}; "
            f"{item.dependency_type}; {item.operation}; {item.confidence.value} confidence"
        )
        elements.extend(
            [
                f'<line x1="205" y1="{y}" x2="326" y2="{y}" class="edge" />',
                f'<line x1="555" y1="{y}" x2="686" y2="{y}" class="edge" />',
                f'<g class="dependency-node app-node" data-app-id="{escape(item.tool_inventory_id, quote=True)}" tabindex="0">'
                f'<rect x="18" y="{y - 17}" width="187" height="34" rx="6" />'
                f'<title>{escape(title)}</title><text x="29" y="{y + 4}">{escape(_short(name, 25))}</text></g>',
                f'<g class="dependency-node"><rect x="326" y="{y - 17}" width="229" height="34" rx="6" />'
                f'<title>{escape(item.source)}</title><text x="337" y="{y + 4}">{escape(_short(item.source, 31))}</text></g>',
                f'<g class="dependency-node target"><rect x="686" y="{y - 17}" width="296" height="34" rx="6" />'
                f'<title>{escape(item.target)}</title><text x="697" y="{y + 4}">{escape(_short(item.target, 40))}</text></g>',
            ]
        )
    note = ""
    if len(dependencies) > len(visible):
        note = f'<p class="muted">Showing the first {len(visible)} of {len(dependencies)} observed dependency edges. The complete normalized set remains in the workbook and CSV.</p>'
    return (
        note + f'<svg class="dependency-map" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Observed application dependencies">{"".join(elements)}</svg>'
    )


def _html_document(
    *,
    safe_json: str,
    inventory_count: int,
    complete_count: int,
    profile_count: int,
    cluster_count: int,
    semantic_status: str,
    archetype_bars: str,
    wave_bars: str,
    architecture_html: str,
    cluster_svg: str,
    dependency_svg: str,
) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<title>Access Portfolio Intelligence</title><style>
:root{{--ink:#17232b;--muted:#5b6b75;--navy:#123047;--blue:#2f6f8f;--cyan:#4aa8b5;--paper:#f4f7f8;--line:#d5dfe4;--amber:#d38b16;--red:#a33b35;--green:#39795b}}
*{{box-sizing:border-box}}body{{margin:0;font:15px/1.5 Arial,Helvetica,sans-serif;color:var(--ink);background:var(--paper)}}
header{{background:var(--navy);color:white;padding:28px max(24px,calc((100vw - 1180px)/2)) 22px}}header h1{{margin:0;font-size:28px}}header p{{margin:5px 0 0;color:#dbe8ee}}
nav{{position:sticky;top:0;z-index:2;background:white;border-bottom:1px solid var(--line);padding:8px max(24px,calc((100vw - 1180px)/2));display:flex;gap:8px;flex-wrap:wrap}}
nav button{{border:0;background:white;padding:9px 12px;color:var(--navy);font-weight:700;cursor:pointer}}nav button.active{{background:#e5f0f4;border-radius:5px}}
main{{max-width:1180px;margin:auto;padding:24px}}section{{display:none}}section.active{{display:block}}h2{{font-size:22px;color:var(--navy);margin:0 0 16px}}h3{{color:var(--navy)}}
.status{{border-left:5px solid var(--amber);padding:10px 14px;background:#fff8e7;margin-bottom:18px}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:16px 0 24px}}.card,.panel,.component{{background:white;border:1px solid var(--line);border-radius:7px;padding:16px}}.card strong{{display:block;font-size:30px;color:var(--navy)}}
.grid2{{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:16px}}.bar-row{{display:grid;grid-template-columns:145px 1fr 38px;gap:8px;align-items:center;margin:8px 0}}.bar{{height:12px;background:#e7eef1;border-radius:8px;overflow:hidden}}.bar i{{display:block;height:100%;background:var(--blue)}}
.component-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}}.component h3{{margin:8px 0 5px}}.component p{{min-height:42px}}.tag{{font-size:12px;color:var(--blue);text-transform:uppercase;font-weight:700}}.muted,small{{color:var(--muted)}}
input[type=search]{{width:100%;max-width:520px;padding:11px;border:1px solid #aebdc5;border-radius:5px;margin-bottom:12px}}table{{width:100%;border-collapse:collapse;background:white}}th{{text-align:left;background:var(--navy);color:white;position:sticky;top:49px}}th,td{{padding:9px;border-bottom:1px solid var(--line);vertical-align:top}}tbody tr:hover{{background:#eef5f7}}button.link{{border:0;background:none;color:var(--blue);font-weight:700;cursor:pointer;text-align:left}}
.pill{{display:inline-block;border-radius:12px;padding:2px 8px;background:#e6eef2;font-size:12px}}.pill.low,.pill.pending{{background:#fff0cf;color:#765000}}.pill.high,.pill.accepted{{background:#dcefe4;color:#21543c}}.pill.rejected{{background:#f7dedb;color:#7d2925}}
.drawer{{position:fixed;right:-560px;top:0;width:min(560px,94vw);height:100vh;background:white;z-index:4;box-shadow:-5px 0 22px #0003;padding:24px;overflow:auto;transition:right .2s}}.drawer.open{{right:0}}.drawer button{{float:right}}.finding{{border-left:3px solid var(--cyan);padding:8px 12px;margin:9px 0;background:#f6fafb}}
.cluster-map,.dependency-map{{width:100%;height:auto;background:white;border:1px solid var(--line);border-radius:7px}}.edge{{stroke:#adc1ca;stroke-width:1.2}}.app-node circle{{fill:var(--blue);cursor:pointer}}.app-node text{{font-size:10px;text-anchor:middle;fill:var(--ink)}}.cluster-label,.dependency-heading{{font-weight:700;fill:var(--navy)}}.dependency-node rect{{fill:#edf5f7;stroke:#9bb5c1}}.dependency-node.app-node rect{{fill:#dcecf2;cursor:pointer;stroke:var(--blue)}}.dependency-node.target rect{{fill:#f7f4e9;stroke:#c7b776}}.dependency-node text{{font-size:12px;text-anchor:start;fill:var(--ink)}}
@media(max-width:700px){{th:nth-child(3),td:nth-child(3),th:nth-child(5),td:nth-child(5){{display:none}}}}
</style></head><body><header><h1>Access Portfolio Intelligence</h1><p>Evidence-grounded semantic analysis and proposed target architecture</p></header>
<nav>{"".join(f'<button data-tab="{tab}" class="{"active" if tab == "overview" else ""}">{label}</button>' for tab, label in (("overview", "Overview"), ("portfolio", "Application portfolio"), ("clusters", "Consolidation map"), ("dependencies", "Dependency map"), ("architecture", "Target architecture"), ("roadmap", "Migration roadmap")))}</nav>
<main><section id="overview" class="active"><h2>Portfolio overview</h2><div class="status"><strong>Semantic status:</strong> {escape(semantic_status)}</div>
<div class="cards"><div class="card"><span>Applications</span><strong>{inventory_count}</strong></div><div class="card"><span>Analysis complete</span><strong>{complete_count}</strong></div><div class="card"><span>Semantic profiles</span><strong>{profile_count}</strong></div><div class="card"><span>Capability clusters</span><strong>{cluster_count}</strong></div></div>
<div class="grid2"><div class="panel"><h3>Application archetypes</h3>{archetype_bars}</div><div class="panel"><h3>Proposed migration waves</h3>{wave_bars}</div></div></section>
<section id="portfolio"><h2>Application portfolio</h2><input id="search" type="search" placeholder="Search name, purpose, archetype, disposition, or summary" aria-label="Search applications"><div class="panel" style="overflow:auto"><table><thead><tr><th>EUC name</th><th>Purpose</th><th>Archetype</th><th>Disposition</th><th>Wave</th><th>Confidence</th></tr></thead><tbody id="apps"></tbody></table></div></section>
<section id="clusters"><h2>Consolidation map</h2><p class="muted">Lines show qualified semantic similarity. Select an application node to open its evidence-grounded profile.</p>{cluster_svg}</section>
<section id="dependencies"><h2>Observed dependency map</h2><p class="muted">Edges come from deterministic extraction. Select an application node to open its evidence-grounded profile.</p>{dependency_svg}</section>
<section id="architecture"><h2>Proposed target architecture</h2><p class="status">All components remain proposals until reviewed. The Microsoft track is limited to the approved service catalog.</p>{architecture_html}</section>
<section id="roadmap"><h2>Migration roadmap</h2><div id="waves" class="component-grid"></div></section></main>
<aside id="drawer" class="drawer" aria-live="polite"><button id="close" type="button" aria-label="Close application details">Close</button><div id="detail"></div></aside>
<script id="portfolio-data" type="application/json">{safe_json}</script><script>
const D=JSON.parse(document.getElementById('portfolio-data').textContent);const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
const apps=document.getElementById('apps'),drawer=document.getElementById('drawer'),detail=document.getElementById('detail');
function rows(q=''){{q=q.toLowerCase();apps.innerHTML=D.applications.filter(a=>Object.values(a).join(' ').toLowerCase().includes(q)).map(a=>`<tr><td><button class="link" data-id="${{esc(a.id)}}">${{esc(a.name)}}</button></td><td>${{esc(a.purpose)}}</td><td>${{esc(a.archetype)}}</td><td>${{esc(a.disposition)}}</td><td>${{a.wave}}</td><td><span class="pill ${{esc(a.confidence)}}">${{esc(a.confidence)}}</span></td></tr>`).join('')}}
function openApp(id){{const a=D.applications.find(x=>x.id===id);if(!a)return;detail.innerHTML=`<h2>${{esc(a.name)}}</h2><p>${{esc(a.summary)}}</p><dl><dt>Business purpose</dt><dd>${{esc(a.purpose)}}</dd><dt>Archetype</dt><dd>${{esc(a.archetype)}}</dd><dt>Proposed disposition</dt><dd>${{esc(a.disposition)}} · Wave ${{a.wave}}</dd></dl><h3>Observed sources</h3>${{a.observed_sources.map(s=>`<div class="finding"><strong>${{esc(s.object)}}</strong><p>${{esc(s.excerpt)}}</p><small>Observed source · ${{esc(s.source_id)}}</small></div>`).join('')||'<p class="muted">No bounded observed source available.</p>'}}<h3>Owner claims</h3>${{a.owner_claims.map(c=>`<div class="finding"><strong>${{esc(c.field.replaceAll('_',' '))}}</strong><p>${{esc(c.value)}}</p><small>Owner claim · ${{esc(c.claim_id)}} · source ${{esc(c.source)}}</small></div>`).join('')||'<p class="muted">No owner context supplied.</p>'}}<h3>AI proposals</h3>${{a.findings.map(f=>`<div class="finding"><strong>${{esc(f.category.replaceAll('_',' '))}}: ${{esc(f.label)}}</strong><p>${{esc(f.description)}}</p><small>AI proposal · ${{esc(f.confidence)}} confidence · ${{esc(f.review_status)}} · evidence ${{esc(f.evidence_ids.join(', '))}}${{f.claim_ids.length?' · claims '+esc(f.claim_ids.join(', ')):''}}</small></div>`).join('')||'<p class="muted">No grounded semantic findings.</p>'}}<h3>Open questions</h3><ul>${{a.open_questions.map(x=>`<li>${{esc(x)}}</li>`).join('')||'<li>None recorded</li>'}}</ul>`;drawer.classList.add('open')}}
rows();document.getElementById('search').addEventListener('input',e=>rows(e.target.value));document.addEventListener('click',e=>{{const id=e.target.closest('[data-id]')?.dataset.id||e.target.closest('[data-app-id]')?.dataset.appId;if(id)openApp(id)}});document.getElementById('close').onclick=()=>drawer.classList.remove('open');
document.addEventListener('keydown',e=>{{if(e.key==='Escape')drawer.classList.remove('open')}});
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{{document.querySelectorAll('nav button,main section').forEach(x=>x.classList.remove('active'));b.classList.add('active');document.getElementById(b.dataset.tab).classList.add('active')}});
const waves=D.architecture.migration_waves||[];document.getElementById('waves').innerHTML=waves.map(w=>`<article class="component"><span class="tag">Wave ${{w.wave}}</span><h3>${{esc(w.name)}}</h3><p>${{esc(w.purpose)}}</p><strong>${{w.application_ids.length}} applications</strong><p><small>${{esc(w.prerequisites.join(' · ')||'No recorded prerequisites')}}</small></p></article>`).join('')||'<p class="muted">No semantic roadmap available.</p>';
</script></body></html>"""


def _mermaid_id(value: str) -> str:
    normalized = "".join(character if character.isalnum() else "_" for character in value)
    return "n_" + normalized[:60]


def _mermaid_label(value: str) -> str:
    return value.replace('"', "'").replace("[", "(").replace("]", ")").replace("\n", " ")


def _short(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."
