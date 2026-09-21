from pathlib import Path

from portfolio_analyzer.models import (
    ArtifactStatus,
    CapabilityFinding,
    Confidence,
    InventoryRecord,
    StagedArtifact,
)
from portfolio_analyzer.reporting.coverage import build_analysis_coverage


def test_coverage_distinguishes_complete_zero_findings_from_not_analyzed() -> None:
    inventory = [
        InventoryRecord(
            tool_inventory_id=tool_id,
            tool_name=f"Tool {tool_id}",
            inventory_filename=f"{tool_id}.accdb",
            filepath=Path(f"/source/{tool_id}.accdb"),
        )
        for tool_id in ("1", "2")
    ]
    artifacts = [
        StagedArtifact(
            tool_inventory_id="1",
            original_source_path=Path("/source/1.accdb"),
            local_staged_path=Path("/staged/1.accdb"),
            filename="1.accdb",
            extension=".accdb",
            sha256="hash-1",
            status=ArtifactStatus.STAGED,
        ),
        StagedArtifact(
            tool_inventory_id="2",
            original_source_path=Path("/source/2.accdb"),
            filename="2.accdb",
            extension=".accdb",
            status=ArtifactStatus.FAILED,
            error="missing",
        ),
    ]
    extraction_state = {
        "applications": [
            {
                "tool_inventory_id": "1",
                "sha256": "hash-1",
                "extracted": {"objects": [], "extraction_errors": []},
            }
        ]
    }
    analysis_state = {
        "applications": [
            {
                "tool_inventory_id": "1",
                "sha256": "hash-1",
                "evidence": [],
                "datasources": [],
                "dependencies": [],
            }
        ]
    }
    capabilities = [
        CapabilityFinding(
            tool_inventory_id="1",
            capability="Example",
            layer="technical",
            confidence=Confidence.MEDIUM,
        )
    ]

    coverage = build_analysis_coverage(
        inventory, artifacts, extraction_state, analysis_state, capabilities
    )

    assert coverage[0].analysis_status == "complete"
    assert coverage[0].evidence_count == 0
    assert coverage[0].capability_count == 1
    assert coverage[1].extraction_status == "not_eligible"
    assert coverage[1].analysis_status == "not_eligible"
    assert coverage[1].notes == ["Staging: missing"]
