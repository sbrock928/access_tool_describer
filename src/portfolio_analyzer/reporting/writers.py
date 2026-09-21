"""Excel, CSV, and PDF outputs from normalized static-analysis results."""

from __future__ import annotations

import csv
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Font
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen.canvas import Canvas

from portfolio_analyzer.models import (
    CapabilityFinding,
    Datasource,
    Dependency,
    Evidence,
    InventoryRecord,
    StagedArtifact,
)


def write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    records = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    headers = list(dict.fromkeys(key for row in records for key in row))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(records)


def write_workbook(
    path: Path,
    inventory: list[InventoryRecord],
    artifacts: list[StagedArtifact],
    evidence: list[Evidence],
    datasources: list[Datasource],
    dependencies: list[Dependency],
    capabilities: list[CapabilityFinding],
) -> None:
    workbook = Workbook()
    workbook.remove(workbook.active)
    _sheet(
        workbook,
        "Applications",
        [
            [
                "Tool Inventory ID",
                "Tool Name",
                "Inventory File Name",
                "Stated Description",
                "Original Source Path",
            ],
            *[
                [
                    item.tool_inventory_id,
                    item.tool_name,
                    item.inventory_filename,
                    item.stated_description or "",
                    str(item.filepath),
                ]
                for item in inventory
            ],
        ],
    )
    _sheet(
        workbook,
        "Supporting Files",
        [
            [
                "Tool Inventory ID",
                "Original Path",
                "Local Staged Path",
                "SHA-256",
                "Status",
                "Error",
            ],
            *[
                [
                    a.tool_inventory_id,
                    str(a.original_source_path),
                    str(a.local_staged_path or ""),
                    a.sha256 or "",
                    a.status,
                    a.error or "",
                ]
                for a in artifacts
            ],
        ],
    )
    _sheet(
        workbook,
        "Data Sources",
        [
            [
                "Application",
                "Platform",
                "Server",
                "Database",
                "Schema",
                "Object",
                "Operation",
                "Confidence",
            ],
            *[
                [
                    d.tool_inventory_id,
                    d.platform,
                    d.server or "",
                    d.database or "",
                    d.schema_name or "",
                    d.object_name or "",
                    d.operation,
                    d.confidence,
                ]
                for d in datasources
            ],
        ],
    )
    _sheet(
        workbook,
        "Dependencies",
        [
            ["Application", "Source", "Target", "Type", "Operation", "Confidence"],
            *[
                [
                    d.tool_inventory_id,
                    d.source,
                    d.target,
                    d.dependency_type,
                    d.operation,
                    d.confidence,
                ]
                for d in dependencies
            ],
        ],
    )
    _sheet(
        workbook,
        "Application Capabilities",
        [
            ["Application", "Capability", "Layer", "Confidence"],
            *[[c.tool_inventory_id, c.capability, c.layer, c.confidence] for c in capabilities],
        ],
    )
    _sheet(
        workbook,
        "Evidence",
        [
            [
                "Application",
                "Artifact",
                "Object Type",
                "Object",
                "Location",
                "Evidence",
                "Inference",
                "Confidence",
            ],
            *[
                [
                    e.tool_inventory_id,
                    e.artifact_path,
                    e.object_type,
                    e.object_name,
                    e.location or "",
                    e.text,
                    e.inference or "",
                    e.confidence,
                ]
                for e in evidence
            ],
        ],
    )
    _sheet(
        workbook,
        "Extraction Errors",
        [
            ["Application", "Original Source Path", "Error"],
            *[
                [a.tool_inventory_id, str(a.original_source_path), a.error or ""]
                for a in artifacts
                if a.error
            ],
        ],
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


def _sheet(workbook: Workbook, title: str, rows: Sequence[Sequence[object]]) -> None:
    sheet = workbook.create_sheet(title)
    for row in rows:
        sheet.append(row)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for column in sheet.columns:
        letter = column[0].column_letter
        sheet.column_dimensions[letter].width = min(
            max(len(str(cell.value or "")) for cell in column) + 2, 60
        )


def write_executive_pdf(
    path: Path,
    inventory: list[InventoryRecord],
    artifacts: list[StagedArtifact],
    capabilities: list[CapabilityFinding],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas = Canvas(str(path), pagesize=letter)
    canvas.setTitle("Access Portfolio Analysis")
    successful = sum(a.status.value == "staged" and a.is_primary for a in artifacts)
    failed = sum(a.status.value == "failed" and a.is_primary for a in artifacts)
    lines = [
        "Access Portfolio Analysis — Executive Summary",
        f"Inventory applications: {len(inventory)}",
        f"Successfully staged primary artifacts: {successful}",
        f"Failed primary staging attempts: {failed}",
        f"Observed technical capability findings: {len(capabilities)}",
        "All stated descriptions remain claims; implementation evidence is reported separately.",
        "No source path is analyzed directly; failures are reported for manual review.",
    ]
    y = 740
    for line in lines:
        canvas.drawString(54, y, line)
        y -= 24
    canvas.save()
