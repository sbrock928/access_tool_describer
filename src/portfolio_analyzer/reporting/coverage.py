"""Build explicit pipeline coverage so reports can qualify missing findings."""

from __future__ import annotations

from typing import Any

from portfolio_analyzer.models import (
    AnalysisCoverage,
    CapabilityFinding,
    InventoryRecord,
    StagedArtifact,
)


def build_analysis_coverage(
    inventory: list[InventoryRecord],
    artifacts: list[StagedArtifact],
    extraction_state: dict[str, Any],
    analysis_state: dict[str, Any],
    capabilities: list[CapabilityFinding],
) -> list[AnalysisCoverage]:
    """Reconcile current staged hashes with extraction and analysis checkpoints."""
    primary = {item.tool_inventory_id: item for item in artifacts if item.is_primary}
    extraction = _current_results(extraction_state)
    analysis = _current_results(analysis_state)
    capability_counts: dict[str, int] = {}
    for finding in capabilities:
        capability_counts[finding.tool_inventory_id] = (
            capability_counts.get(finding.tool_inventory_id, 0) + 1
        )

    coverage: list[AnalysisCoverage] = []
    for record in inventory:
        artifact = primary.get(record.tool_inventory_id)
        staging_status = artifact.status.value if artifact is not None else "missing"
        checksum = artifact.sha256 if artifact is not None else None
        extraction_result = extraction.get((record.tool_inventory_id, checksum))
        analysis_result = analysis.get((record.tool_inventory_id, checksum))
        notes: list[str] = []

        extraction_status = "not_run"
        object_count = 0
        warning_count = 0
        if staging_status != "staged":
            extraction_status = "not_eligible"
            if artifact is not None and artifact.error:
                notes.append(f"Staging: {artifact.error}")
        elif extraction_result is not None and extraction_result.get("error"):
            extraction_status = "failed"
            notes.append(f"Extraction: {extraction_result['error']}")
        elif extraction_result is not None and "extracted" in extraction_result:
            extracted = extraction_result["extracted"]
            errors = extracted.get("extraction_errors", [])
            object_count = len(extracted.get("objects", []))
            warning_count = len(errors)
            extraction_status = "complete_with_warnings" if errors else "complete"
            notes.extend(f"Extraction warning: {warning}" for warning in errors)

        analysis_status = "not_run"
        if extraction_status == "not_eligible":
            analysis_status = "not_eligible"
        elif extraction_status == "failed":
            analysis_status = "blocked"
        elif analysis_result is not None and analysis_result.get("error"):
            analysis_status = "failed"
            notes.append(f"Analysis: {analysis_result['error']}")
        elif analysis_result is not None and "evidence" in analysis_result:
            analysis_status = "complete"

        coverage.append(
            AnalysisCoverage(
                tool_inventory_id=record.tool_inventory_id,
                tool_name=record.tool_name,
                staging_status=staging_status,
                extraction_status=extraction_status,
                analysis_status=analysis_status,
                extracted_object_count=object_count,
                extraction_warning_count=warning_count,
                evidence_count=len(analysis_result.get("evidence", [])) if analysis_result else 0,
                datasource_count=(
                    len(analysis_result.get("datasources", [])) if analysis_result else 0
                ),
                dependency_count=(
                    len(analysis_result.get("dependencies", [])) if analysis_result else 0
                ),
                capability_count=capability_counts.get(record.tool_inventory_id, 0),
                notes=notes,
            )
        )
    return coverage


def _current_results(state: dict[str, Any]) -> dict[tuple[str, str | None], dict[str, Any]]:
    return {
        (str(result["tool_inventory_id"]), result.get("sha256")): result
        for result in state.get("applications", [])
        if "tool_inventory_id" in result
    }
