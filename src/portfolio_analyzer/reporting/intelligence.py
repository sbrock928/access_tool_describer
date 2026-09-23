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
    Datasource,
    Dependency,
    InventoryRecord,
    PortfolioTheme,
    SemanticPortfolioState,
    StagedArtifact,
)
from portfolio_analyzer.portfolio.themes import build_portfolio_themes
from portfolio_analyzer.reporting.network import dependency_network
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
                    "purpose_provenance": profile.purpose_provenance,
                    "purpose_claim_ids": " | ".join(profile.purpose_claim_ids),
                    "observed_behavior": " | ".join(profile.observed_behavior),
                    "inputs": " | ".join(profile.inputs),
                    "outputs": " | ".join(profile.outputs),
                    "secondary_capabilities": " | ".join(profile.secondary_capabilities),
                    "classification_rationale": profile.classification_rationale,
                    "classification_evidence_ids": " | ".join(profile.classification_evidence_ids),
                    "summary": profile.summary,
                    "business_purpose": profile.business_purpose,
                    "primary_archetype": profile.primary_archetype,
                    "supported_roles": " | ".join(r.role for r in profile.roles),
                    "role_assessments": json.dumps([r.model_dump() for r in profile.roles]),
                    "proposed_disposition": profile.proposed_disposition,
                    "confidence": profile.confidence.value,
                    "status": profile.status,
                    "generation_method": profile.generation_method,
                    "application_ir_id": profile.application_ir_id or "",
                    "model_input_kind": (
                        profile.semantic_coverage.model_input_kind
                        if profile.semantic_coverage
                        else ""
                    ),
                    "complete_code_coverage": (
                        profile.semantic_coverage.complete_code_coverage
                        if profile.semantic_coverage
                        else False
                    ),
                    "inspected_code_objects": (
                        profile.semantic_coverage.code_objects_inspected
                        if profile.semantic_coverage
                        else 0
                    ),
                    "available_code_objects": (
                        profile.semantic_coverage.code_objects_available
                        if profile.semantic_coverage
                        else 0
                    ),
                    "inspected_code_segments": (
                        profile.semantic_coverage.code_segments_inspected
                        if profile.semantic_coverage
                        else 0
                    ),
                    "available_code_segments": (
                        profile.semantic_coverage.code_segments_available
                        if profile.semantic_coverage
                        else 0
                    ),
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
                "generation_method",
                "application_ir_id",
                "model_input_kind",
                "complete_code_coverage",
                "inspected_code_objects",
                "available_code_objects",
                "inspected_code_segments",
                "available_code_segments",
                "open_questions",
                "evidence_ids",
                "claim_ids",
                "purpose_provenance",
                "purpose_claim_ids",
                "observed_behavior",
                "inputs",
                "outputs",
                "secondary_capabilities",
                "classification_rationale",
                "classification_evidence_ids",
                "supported_roles",
                "role_assessments",
            ],
        )
    )
    outputs.append(
        _csv(
            directory / "semantic_application_irs.csv",
            [
                {
                    "euc_name": application_names.get(item.tool_inventory_id, "Unknown EUC"),
                    "inventory_object_type_counts": json.dumps(
                        item.inventory_object_type_counts, sort_keys=True
                    ),
                    "inventory_object_names_by_type": json.dumps(
                        item.inventory_object_names_by_type, sort_keys=True
                    ),
                    "inventory_source_ids": " | ".join(item.inventory_source_ids),
                    "behavior_fact_count": len(item.behavior_facts),
                    "ir_id": item.ir_id,
                    "ir_version": item.ir_version,
                    "code_object_count": item.code_object_count,
                    "code_segment_count": item.code_segment_count,
                    "model_input_characters": item.model_input_characters,
                    "model_input_sha256": item.model_input_sha256,
                    "model_input_item_counts": json.dumps(
                        item.model_input_item_counts, sort_keys=True
                    ),
                    "model_input_omitted_counts": json.dumps(
                        item.model_input_omitted_counts, sort_keys=True
                    ),
                }
                for item in state.application_irs
            ],
            [
                "euc_name",
                "ir_id",
                "ir_version",
                "code_object_count",
                "code_segment_count",
                "model_input_characters",
                "model_input_sha256",
                "model_input_item_counts",
                "model_input_omitted_counts",
                "inventory_object_type_counts",
                "inventory_object_names_by_type",
                "inventory_source_ids",
                "behavior_fact_count",
            ],
        )
    )
    outputs.append(
        _csv(
            directory / "application_behaviors.csv",
            [
                {
                    "euc_name": application_names.get(ir.tool_inventory_id, "Unknown EUC"),
                    "action": fact.action,
                    "description": fact.description,
                    "object_type": fact.object_type,
                    "object_name": fact.object_name,
                    "targets": " | ".join(fact.targets),
                    "datasource_scope": fact.datasource_scope,
                    "evidence_ids": " | ".join(fact.evidence_ids),
                }
                for ir in state.application_irs
                for fact in ir.behavior_facts
            ],
            [
                "euc_name",
                "action",
                "description",
                "object_type",
                "object_name",
                "targets",
                "datasource_scope",
                "evidence_ids",
            ],
        )
    )
    cited_sources = {
        ref
        for ir in state.application_irs
        for fact in ir.behavior_facts
        for ref in fact.evidence_ids
    }
    outputs.append(
        _csv(
            directory / "behavior_sources.csv",
            [
                {
                    "euc_name": application_names.get(source.tool_inventory_id, "Unknown EUC"),
                    "source_id": source.source_id,
                    "object_type": source.object_type,
                    "object_name": source.object_name,
                    "location": source.location or "",
                    "excerpt": source.excerpt,
                    "ui_properties": json.dumps(source.ui_properties, sort_keys=True),
                }
                for source in state.sources
                if source.source_id in cited_sources
            ],
            [
                "euc_name",
                "source_id",
                "object_type",
                "object_name",
                "location",
                "excerpt",
                "ui_properties",
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
    themes: list[PortfolioTheme] | None = None,
    datasources: list[Datasource] | None = None,
    artifacts: list[StagedArtifact] | None = None,
) -> None:
    names = {item.tool_inventory_id: item.tool_name for item in inventory}
    themes = themes if themes is not None else build_portfolio_themes(state, coverage=coverage)
    profiles = state.applications if state else []
    mappings = state.architecture.mappings if state else []
    mapping_by_id = {item.tool_inventory_id: item for item in mappings}
    profile_by_id = {item.tool_inventory_id: item for item in profiles}
    ir_by_id = {item.tool_inventory_id: item for item in state.application_irs} if state else {}
    sources_by_id: dict[str, list[dict[str, str]]] = {}
    claims_by_id: dict[str, list[dict[str, str]]] = {}
    if state:
        for source in state.sources:
            sources_by_id.setdefault(source.tool_inventory_id, []).append(
                {
                    "source_id": source.source_id,
                    "object": f"{source.object_type}: {source.object_name}",
                    "excerpt": source.excerpt,
                    "ui_properties": json.dumps(source.ui_properties, sort_keys=True),
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
    archetypes = Counter(r.role for item in profiles for r in item.roles)
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
                "purpose_provenance": profile.purpose_provenance if profile else "unconfirmed",
                "purpose_claim_ids": profile.purpose_claim_ids if profile else [],
                "observed_behavior": profile.observed_behavior if profile else [],
                "inputs": profile.inputs if profile else [],
                "outputs": profile.outputs if profile else [],
                "secondary_capabilities": profile.secondary_capabilities if profile else [],
                "classification_rationale": profile.classification_rationale if profile else "",
                "classification_evidence_ids": profile.classification_evidence_ids
                if profile
                else [],
                "archetype": profile.primary_archetype if profile else "unknown",
                "roles": [r.model_dump(mode="json") for r in profile.roles] if profile else [],
                "confidence": profile.confidence.value if profile else "low",
                "disposition": mapping.disposition if mapping else "investigate",
                "wave": mapping.wave if mapping else 0,
                "review": mapping.review_status if mapping else "pending",
                "generation_method": (profile.generation_method if profile else "unavailable"),
                "semantic_coverage": (
                    profile.semantic_coverage.model_dump(mode="json")
                    if profile and profile.semantic_coverage
                    else None
                ),
                "application_ir": (
                    ir_by_id[tool_id].model_dump(mode="json") if tool_id in ir_by_id else None
                ),
                "findings": [item.model_dump(mode="json") for item in profile.findings]
                if profile
                else [],
                "observed_sources": sources_by_id.get(tool_id, []),
                "owner_claims": claims_by_id.get(tool_id, []),
                "open_questions": profile.open_questions if profile else [],
            }
        )
    capability_groups: dict[str, dict[str, Any]] = {}
    for profile in profiles:
        for finding in profile.findings:
            if finding.category not in {"business_capability", "workflow"} or finding.review_status == "rejected":
                continue
            if not finding.evidence_ids and not finding.claim_ids:
                continue
            key = finding.label.strip().casefold()
            group = capability_groups.setdefault(key, {
                "key": key, "label": finding.label, "application_ids": [], "interpretations": [],
            })
            if profile.tool_inventory_id not in group["application_ids"]:
                group["application_ids"].append(profile.tool_inventory_id)
            group["interpretations"].append({
                "app_id": profile.tool_inventory_id, "description": finding.description,
                "category": finding.category, "confidence": finding.confidence.value,
                "review_status": finding.review_status, "evidence_ids": finding.evidence_ids,
                "claim_ids": finding.claim_ids, "method": profile.generation_method,
            })
    data = {
        "capability_groups": sorted(capability_groups.values(), key=lambda g: (-len(g["application_ids"]), g["key"])),
        "network": dependency_network(inventory, dependencies or [], datasources or [], state, artifacts),
        "semantic_status": semantic_status,
        "themes": [theme.model_dump(mode="json") for theme in themes],
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
        cluster_count=len(capability_groups),
        semantic_status=semantic_status,
        archetype_bars=_bars(archetypes),
        wave_bars=_bars({f"Wave {key}": value for key, value in sorted(waves.items())}),
        architecture_html=_architecture_cards(state, names),
        explorer_js=Path(__file__).with_name("explorer.js").read_text(encoding="utf-8"),
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
            "deterministic_application_irs": len(state.application_irs) if state else 0,
            "complete_code_coverage_applications": (
                sum(
                    profile.semantic_coverage is not None
                    and profile.semantic_coverage.complete_code_coverage
                    for profile in state.applications
                )
                if state
                else 0
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


def _architecture_cards(state: SemanticPortfolioState | None, names: dict[str, str]) -> str:
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
            affected = " · ".join(
                f'<button class="link" data-id="{escape(app_id, quote=True)}">'
                f'{escape(names.get(app_id, app_id))}</button>'
                for app_id in item.application_ids
            )
            cards.append(
                '<article class="component"><span class="tag">{}</span><h3>{}</h3>{}'
                "<p>{}</p><p><strong>Affected applications:</strong> {}</p><small>{} confidence · {}</small></article>".format(
                    escape(item.component_type.replace("_", " ")),
                    escape(item.name),
                    service,
                    escape(item.description),
                    affected,
                    escape(item.confidence.value),
                    escape(item.review_status),
                )
            )
        sections.append(f'<h3>{title}</h3><div class="component-grid">{"".join(cards)}</div>')
    return "".join(sections)


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
    explorer_js: str,
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
input[type=search]{{width:100%;max-width:520px;padding:11px;border:1px solid #aebdc5;border-radius:5px;margin-bottom:12px}}table{{width:100%;border-collapse:collapse;background:white}}th{{text-align:left;background:var(--navy);color:white;position:static}}th,td{{padding:9px;border-bottom:1px solid var(--line);vertical-align:top}}tbody tr:hover{{background:#eef5f7}}button.link{{border:0;background:none;color:var(--blue);font-weight:700;cursor:pointer;text-align:left}}
.pill{{display:inline-block;border-radius:12px;padding:2px 8px;background:#e6eef2;font-size:12px}}.pill.low,.pill.pending{{background:#fff0cf;color:#765000}}.pill.high,.pill.accepted{{background:#dcefe4;color:#21543c}}.pill.rejected{{background:#f7dedb;color:#7d2925}}
.drawer{{position:fixed;right:-560px;top:0;width:min(560px,94vw);height:100vh;background:white;z-index:4;box-shadow:-5px 0 22px #0003;padding:24px;overflow:auto;transition:right .2s}}.drawer.open{{right:0}}.drawer button{{float:right}}.finding{{border-left:3px solid var(--cyan);padding:8px 12px;margin:9px 0;background:#f6fafb}}
.cluster-map,.dependency-map{{width:100%;height:auto;background:white;border:1px solid var(--line);border-radius:7px}}.edge{{stroke:#adc1ca;stroke-width:1.2}}.app-node circle{{fill:var(--blue);cursor:pointer}}.app-node text{{font-size:10px;text-anchor:middle;fill:var(--ink)}}.cluster-label,.dependency-heading{{font-weight:700;fill:var(--navy)}}.dependency-node rect{{fill:#edf5f7;stroke:#9bb5c1}}.dependency-node.app-node rect{{fill:#dcecf2;cursor:pointer;stroke:var(--blue)}}.dependency-node.target rect{{fill:#f7f4e9;stroke:#c7b776}}.dependency-node text{{font-size:12px;text-anchor:start;fill:var(--ink)}}
#network-canvas{{width:100%;height:680px;background:white;border:1px solid var(--line);touch-action:none;cursor:grab}}.network-controls{{display:flex;align-items:center;gap:12px;flex-wrap:wrap}}.network-controls select{{max-width:280px}}.network-controls input[type=search]{{max-width:300px;margin:0}}.network-node{{cursor:pointer}}.network-node text{{font-size:12px;paint-order:stroke;stroke:white;stroke-width:3px;fill:var(--ink)}}.network-link{{stroke:#99aeb8;stroke-width:1.7;cursor:pointer}}.network-link:hover,.network-link.selected{{stroke:#bd7010;stroke-width:4}}.network-edge-label{{font-size:10px;fill:#485e69;paint-order:stroke;stroke:white;stroke-width:3px}}.network-node:focus{{outline:none}}.network-node:focus circle,.network-node:focus rect{{stroke:#c77f00;stroke-width:4}}.network-controls button,select{{padding:7px}}.evidence-scroll{{max-height:460px;overflow:auto}}#reuse-pairs .panel{{margin:10px 0}}#capability-groups .component p{{min-height:0}}#network-detail{{margin:12px 0;overflow-wrap:anywhere}}
@media(max-width:700px){{#portfolio th:nth-child(5),#portfolio td:nth-child(5),#portfolio th:nth-child(6),#portfolio td:nth-child(6){{display:none}}#network-canvas{{height:480px}}}}
</style></head><body><header><h1>Access Portfolio Intelligence</h1><p>Evidence-grounded semantic analysis and proposed target architecture</p></header>
<nav>{"".join(f'<button data-tab="{tab}" class="{"active" if tab == "overview" else ""}">{label}</button>' for tab, label in (("overview", "Overview"), ("portfolio", "Application portfolio"), ("themes", "Themes & solutions"), ("clusters", "Reuse candidates"), ("dependencies", "Dependency map"), ("architecture", "Target architecture"), ("roadmap", "Migration roadmap")))}</nav>
<main><section id="overview" class="active"><h2>Portfolio overview</h2><div class="status"><strong>Semantic status:</strong> {escape(semantic_status)}</div>
<div class="cards"><div class="card"><span>Applications</span><strong>{inventory_count}</strong></div><div class="card"><span>Analysis complete</span><strong>{complete_count}</strong></div><div class="card"><span>Semantic profiles</span><strong>{profile_count}</strong></div><div class="card"><span>Capability / workflow labels</span><strong>{cluster_count}</strong></div></div>
<div class="panel"><h3>Business capabilities and workflows</h3><p class="muted">Overlapping interpretations from application evidence and owner context. Labels are discovered, not a fixed taxonomy. Select a label to see affected applications; review its supporting evidence before using it as a business boundary.</p><div id="capability-groups" class="component-grid"></div></div>
<div class="grid2"><div class="panel"><h3>Supported application roles</h3><p class="muted">Roles overlap. One application can contribute to several counts.</p>{archetype_bars}</div><div class="panel"><h3>Proposed migration waves</h3>{wave_bars}</div></div></section>
<section id="portfolio"><h2>Application portfolio</h2><input id="search" type="search" placeholder="Search name, purpose, roles, disposition, or summary" aria-label="Search applications"><label for="capability-filter">Capability: </label><select id="capability-filter"><option value="">All capabilities</option></select><label for="role-filter">Role: </label><select id="role-filter"><option value="">All roles</option></select><div class="panel" style="overflow:auto"><table><thead><tr><th>EUC name</th><th>Observed behavior</th><th>Capabilities / workflows</th><th>Supported roles</th><th>Disposition</th><th>Wave</th><th>Confidence</th></tr></thead><tbody id="apps"></tbody></table></div></section>
<section id="themes"><h2>Discovered groups and design options</h2><p class="status">Applications can participate in several themes. Groups emerge from shared evidence. Names and roles alone do not create a group; applications without supported overlap remain ungrouped.</p><input id="theme-search" type="search" aria-label="Search themes" placeholder="Search solutions, applications, objects or dependencies"><div id="theme-list"></div></section>
<section id="clusters"><h2>Evidence-supported reuse candidates</h2><p class="status">Review which work or data contracts could be shared, and which responsibilities should remain separate. These are overlapping candidates, not instructions to merge whole applications.</p><div id="reuse-summary"></div><input id="reuse-search" type="search" aria-label="Search reuse candidates" placeholder="Search candidate, evidence or application"><div id="reuse-candidates"></div><details><summary>Pairwise semantic similarity</summary><p class="muted">Similarity suggests a comparison; it does not establish a dependency or a shared business owner.</p><div id="reuse-pairs"></div></details></section>
<section id="dependencies"><h2>Observed dependency network</h2><p class="status">Each application and resource appears once. Connections show static references, not proof of execution. Shared dependencies are places to investigate ownership and contracts; they do not establish a microservice boundary.</p>
<div class="network-controls"><label>Application <select id="network-app"><option value="">Whole portfolio</option></select></label><label><input type="checkbox" id="network-shared" checked> Shared resources only</label><label><input type="checkbox" id="network-runtime"> Include runtime / analysis files</label><input type="search" id="network-search" aria-label="Search dependencies" placeholder="Search resource or application"><button id="network-reset">Fit network</button><button id="network-in">Zoom in</button><button id="network-out">Zoom out</button></div>
<p id="network-count" class="muted" aria-live="polite"></p><p class="muted">Blue circles: applications · amber squares: shared data, files or endpoints · gray squares: local or unresolved references. Arrow labels describe operations from the application’s perspective. Drag nodes to arrange; drag the background to pan; use the wheel or buttons to zoom. Select a resource or connection for source evidence.</p>
<svg id="network-canvas" viewBox="0 0 1120 680" role="group" aria-label="Interactive application dependency network"><defs><marker id="network-arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#7d939e"/></marker></defs><g id="network-scene"></g></svg><div id="network-detail" class="panel" aria-live="polite">Select a resource or connection to inspect affected applications, operations and source locations.</div><h3>Shared dependency boundary reviews</h3><div id="network-candidates" class="component-grid"></div></section>
<section id="architecture"><h2>Proposed target architecture</h2><p class="status">All components remain proposals until reviewed. The Microsoft track is limited to the approved service catalog.</p>{architecture_html}</section>
<section id="roadmap"><h2>Migration roadmap</h2><div id="waves" class="component-grid"></div></section></main>
<aside id="drawer" class="drawer" aria-live="polite"><button id="close" type="button" aria-label="Close application details">Close</button><div id="detail"></div></aside>
<script id="portfolio-data" type="application/json">{safe_json}</script><script>
const D=JSON.parse(document.getElementById('portfolio-data').textContent);const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
const apps=document.getElementById('apps'),drawer=document.getElementById('drawer'),detail=document.getElementById('detail');
function rows(q=''){{q=q.toLowerCase();apps.innerHTML=D.applications.filter(a=>JSON.stringify(a).toLowerCase().includes(q)&&matchesCapability(a.id)&&(!document.getElementById("role-filter").value||a.roles.some(r=>r.role===document.getElementById("role-filter").value))).map(a=>`<tr><td><button class="link" data-id="${{esc(a.id)}}">${{esc(a.name)}}</button></td><td>${{esc(a.observed_behavior.slice(0,3).join("; ")||a.summary)}}</td><td>${{capabilityLabels(a.id).map(l=>`<span class="pill">${{esc(l)}}</span>`).join(" ")||"Not established"}}</td><td>${{a.roles.map(r=>`<span class="pill">${{esc(r.role)}}</span>`).join(" ")||"Not established"}}</td><td>${{esc(a.disposition)}}</td><td>${{a.wave}}</td><td><span class="pill ${{esc(a.confidence)}}">${{esc(a.confidence)}}</span></td></tr>`).join('')}}
function openApp(id){{const a=D.applications.find(x=>x.id===id);if(!a)return;const c=a.semantic_coverage;const ir=a.application_ir;const method=a.generation_method==='local_model'?'Local model':'Deterministic rules';detail.innerHTML=`<h2>${{esc(a.name)}}</h2><p>${{esc(a.summary)}}</p><dl><dt>Business purpose</dt><dd>${{esc(a.purpose)}} · ${{esc(a.purpose_provenance.replaceAll("_"," "))}}${{a.purpose_claim_ids.length?" · claims "+esc(a.purpose_claim_ids.join(", ")):""}}</dd><dt>Supported roles</dt><dd>${{a.roles.map(r=>`<p><strong>${{esc(r.role)}}</strong>: ${{esc(r.rationale)}}<br><small>${{esc(r.evidence_ids.join(", "))}}</small></p>`).join("")||"Not established"}}</dd><dt>Legacy summary category</dt><dd>${{esc(a.archetype)}} · Retained for compatibility</dd><dt>Why this classification</dt><dd>${{esc(a.classification_rationale)}}<br><small>${{esc(a.classification_evidence_ids.join(", "))}}</small></dd><dt>Known inputs</dt><dd>${{esc(a.inputs.join(", "))||"Not established"}}</dd><dt>Known outputs / write targets</dt><dd>${{esc(a.outputs.join(", "))||"Not established"}}</dd><dt>Secondary capabilities</dt><dd>${{esc(a.secondary_capabilities.join("; "))||"None established"}}</dd><dt>Proposed disposition</dt><dd>${{esc(a.disposition)}} · Wave ${{a.wave}}</dd><dt>Profile synthesis</dt><dd>${{method}}</dd><dt>Deterministic code coverage</dt><dd>${{c?(c.complete_code_coverage?'Complete':'Sampled')+' · '+c.code_objects_inspected+'/'+c.code_objects_available+' objects · '+c.code_segments_inspected+'/'+c.code_segments_available+' segments':'Unavailable'}}</dd><dt>Model input</dt><dd>${{a.generation_method==='local_model'&&ir?'One deterministic application IR · '+ir.model_input_characters+' characters · '+esc(ir.ir_id):'None'}}</dd></dl><h3>Modernization themes</h3>${{themeCards(D.themes.filter(t=>t.affected_tool_ids.includes(id)),id)}}<h3>Proposed target components</h3>${{(D.architecture.components||[]).filter(c=>c.application_ids.includes(id)&&c.review_status!=="rejected").map(c=>`<div class="finding"><strong>${{esc(c.name)}}</strong><p>${{esc(c.description)}}</p><small>${{esc(c.track)}} · ${{esc(c.platform_service||"Logical boundary")}}</small></div>`).join("")||"No supported target proposed"}}<h3>Observed behavior</h3><p class="muted">Static definitions and code; these are not proof of execution or an inferred execution order.</p>${{(ir?.behavior_facts||[]).map(f=>`<div class="finding"><strong>${{esc(f.description)}}</strong><p>${{esc(f.object_type)}}: ${{esc(f.object_name)}} · ${{esc(f.datasource_scope.replaceAll("_"," "))}}</p><small>Evidence ${{esc(f.evidence_ids.join(", "))}}</small></div>`).join('')||'<p class="muted">No behavior established.</p>'}}<h3>Observed sources</h3>${{a.observed_sources.map(s=>`<div class="finding"><strong>${{esc(s.object)}}</strong><p>${{esc(s.excerpt)}}</p>${{s.ui_properties!=="{{}}"?`<p>Root UI properties: ${{esc(s.ui_properties)}}</p>`:""}}<small>Observed source · ${{esc(s.source_id)}}</small></div>`).join('')||'<p class="muted">No bounded observed source available.</p>'}}<h3>Owner claims</h3>${{a.owner_claims.map(c=>`<div class="finding"><strong>${{esc(c.field.replaceAll('_',' '))}}</strong><p>${{esc(c.value)}}</p><small>Owner claim · ${{esc(c.claim_id)}} · source ${{esc(c.source)}}</small></div>`).join('')||'<p class="muted">No owner context supplied.</p>'}}<h3>Semantic proposals</h3>${{a.findings.map(f=>`<div class="finding"><strong>${{esc(f.category.replaceAll('_',' '))}}: ${{esc(f.label)}}</strong><p>${{esc(f.description)}}</p><small>${{method}} proposal · ${{esc(f.confidence)}} confidence · ${{esc(f.review_status)}} · evidence ${{esc(f.evidence_ids.join(', '))}}${{f.claim_ids.length?' · claims '+esc(f.claim_ids.join(', ')):''}}</small></div>`).join('')||'<p class="muted">No grounded semantic findings.</p>'}}<h3>Open questions</h3><ul>${{a.open_questions.map(x=>`<li>${{esc(x)}}</li>`).join('')||'<li>None recorded</li>'}}</ul>`;drawer.classList.add('open')}}

function appLink(id){{return `<button class="link" data-id="${{esc(id)}}">${{esc(D.names[id]||id)}}</button>`}}
function themeCards(themes,appId=null){{return themes.map(t=>`<article class="panel" style="margin-bottom:14px"><span class="tag">${{esc(t.category)}} · ${{esc(t.confidence)}} confidence</span><h3>${{esc(t.title)}}</h3><p><strong>Observed pattern:</strong> ${{esc(t.observed_pattern)}}</p><p><strong>Proposed solution:</strong> ${{esc(t.proposed_solution)}}</p><p><strong>Affected applications (${{t.affected_tool_ids.length}}):</strong> ${{t.affected_tool_ids.map(appLink).join(" · ")}}</p><p><strong>Other options:</strong></p><ul>${{t.alternative_options.map(x=>`<li>${{esc(x)}}</li>`).join("")||"<li>None recorded</li>"}}</ul><p><strong>Why grouped:</strong> ${{esc(t.grouping_basis.join("; "))}}</p><p><strong>Next steps:</strong></p><ol>${{t.next_steps.map(x=>`<li>${{esc(x)}}</li>`).join("")}}</ol><p><strong>Validate with owners:</strong></p><ul>${{t.validation_questions.map(x=>`<li>${{esc(x)}}</li>`).join("")}}</ul><p class="muted">${{esc(t.coverage_note)}}</p><details><summary>Where this occurs · ${{t.locations.filter(l=>!appId||l.tool_inventory_id===appId).length}} evidence references</summary><div style="overflow:auto"><table><thead><tr><th>Application</th><th>Object / location</th><th>Observation</th></tr></thead><tbody>${{t.locations.filter(l=>!appId||l.tool_inventory_id===appId).map(l=>`<tr><td>${{appLink(l.tool_inventory_id)}}</td><td>${{esc(l.object_type)}}: ${{esc(l.object_name)}}<br>${{esc(l.location)}}<br><small>Artifact: ${{esc(l.artifact)}}<br>${{esc(l.evidence_id)}}</small></td><td>${{esc(l.observation)}}</td></tr>`).join("")}}</tbody></table></div></details></article>`).join("")||'<p class="muted">No shared evidence group was established for this selection. Applications are left ungrouped when overlap is insufficient.</p>'}}
function renderThemes(q=''){{document.getElementById('theme-list').innerHTML=themeCards(D.themes.filter(t=>(JSON.stringify(t)+' '+t.affected_tool_ids.map(id=>D.names[id]).join(' ')).toLowerCase().includes(q.toLowerCase())))}}
renderThemes();document.getElementById('theme-search').addEventListener('input',e=>renderThemes(e.target.value));
const roleFilter=document.getElementById('role-filter');[...new Set(D.applications.flatMap(a=>a.roles.map(r=>r.role)))].sort().forEach(r=>{{const o=document.createElement('option');o.value=r;o.textContent=r;roleFilter.appendChild(o)}});roleFilter.addEventListener('change',()=>rows(document.getElementById('search').value));

{explorer_js}
rows();document.getElementById('search').addEventListener('input',e=>rows(e.target.value));document.addEventListener('click',e=>{{const id=e.target.closest('[data-id]')?.dataset.id||e.target.closest('[data-app-id]')?.dataset.appId;if(id)openApp(id)}});document.getElementById('close').onclick=()=>drawer.classList.remove('open');
document.addEventListener('keydown',e=>{{if(e.key==='Escape')drawer.classList.remove('open')}});
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{{document.querySelectorAll('nav button,main section').forEach(x=>x.classList.remove('active'));b.classList.add('active');document.getElementById(b.dataset.tab).classList.add('active')}});
const waves=D.architecture.migration_waves||[];document.getElementById('waves').innerHTML=waves.map(w=>`<article class="component"><span class="tag">Wave ${{w.wave}}</span><h3>${{esc(w.name)}}</h3><p>${{esc(w.purpose)}}</p><strong>${{w.application_ids.length}} applications</strong><p>${{w.application_ids.map(appLink).join(" · ")}}</p><p><small>${{esc(w.prerequisites.join(' · ')||'No recorded prerequisites')}}</small></p></article>`).join('')||'<p class="muted">No semantic roadmap available.</p>';
</script></body></html>"""


def _mermaid_id(value: str) -> str:
    normalized = "".join(character if character.isalnum() else "_" for character in value)
    return "n_" + normalized[:60]


def _mermaid_label(value: str) -> str:
    return value.replace('"', "'").replace("[", "(").replace("]", ")").replace("\n", " ")


def _short(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."
