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
    assert coverage[0].capability_count == 0
    assert coverage[1].extraction_status == "not_eligible"
    assert coverage[1].analysis_status == "not_eligible"
    assert coverage[1].notes == ["Staging: missing"]


def test_coverage_matches_repeated_inventory_id_by_primary_file_hash() -> None:
    inventory = [
        InventoryRecord(
            tool_inventory_id="13",
            tool_name="BelloQ",
            inventory_filename=filename,
            filepath=Path(f"/source/{filename}"),
        )
        for filename in ("BelloQ.accdb", "IntexLib.accdb")
    ]
    artifacts = [
        StagedArtifact(
            tool_inventory_id="13",
            original_source_path=record.filepath,
            local_staged_path=Path(f"/staged/{record.inventory_filename}"),
            filename=record.inventory_filename,
            extension=".accdb",
            sha256=f"hash-{index}",
            status=ArtifactStatus.STAGED,
        )
        for index, record in enumerate(inventory, start=1)
    ]
    extraction_state = {
        "applications": [
            {
                "tool_inventory_id": "13",
                "sha256": "hash-1",
                "extracted": {"objects": [{}], "extraction_errors": []},
            },
            {
                "tool_inventory_id": "13",
                "sha256": "hash-2",
                "extracted": {"objects": [{}, {}], "extraction_errors": []},
            },
        ]
    }
    analysis_state = {
        "applications": [
            {
                "tool_inventory_id": "13",
                "sha256": "hash-1",
                "evidence": [{"inference": "Excel automation"}],
                "datasources": [],
                "dependencies": [],
            },
            {
                "tool_inventory_id": "13",
                "sha256": "hash-2",
                "evidence": [
                    {"inference": "Database access"},
                    {"inference": "Filesystem dependency"},
                ],
                "datasources": [],
                "dependencies": [],
            },
        ]
    }

    coverage = build_analysis_coverage(inventory, artifacts, extraction_state, analysis_state, [])

    assert [item.extracted_object_count for item in coverage] == [1, 2]
    assert [item.inventory_filename for item in coverage] == [
        "BelloQ.accdb",
        "IntexLib.accdb",
    ]
    assert [item.evidence_count for item in coverage] == [1, 2]
    assert [item.capability_count for item in coverage] == [1, 2]
