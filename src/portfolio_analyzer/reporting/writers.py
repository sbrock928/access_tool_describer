"""Excel, CSV, and PDF outputs from normalized static-analysis results."""

from __future__ import annotations

import csv
from collections import Counter
from collections.abc import Iterable, Sequence
from html import escape
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Font
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from portfolio_analyzer.models import (
    CapabilityFinding,
    Datasource,
    Dependency,
    Evidence,
    InventoryRecord,
    Recommendation,
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
    *,
    recommendations: list[Recommendation] | None = None,
    evidence: list[Evidence] | None = None,
    datasources: list[Datasource] | None = None,
    dependencies: list[Dependency] | None = None,
) -> None:
    """Render an evidence-backed, multi-page executive and architecture report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    recommendations = recommendations or []
    evidence = evidence or []
    datasources = datasources or []
    dependencies = dependencies or []
    successful = sum(a.status.value == "staged" and a.is_primary for a in artifacts)
    failed = sum(a.status.value == "failed" and a.is_primary for a in artifacts)
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="ReportTitle",
            parent=styles["Title"],
            fontName="Helvetica-Bold",
            fontSize=24,
            leading=29,
            textColor=colors.HexColor("#123047"),
            spaceAfter=18,
        )
    )
    styles.add(
        ParagraphStyle(
            name="ReportHeading",
            parent=styles["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=16,
            leading=20,
            textColor=colors.HexColor("#123047"),
            spaceBefore=4,
            spaceAfter=12,
        )
    )
    styles.add(
        ParagraphStyle(name="ReportBody", parent=styles["BodyText"], fontSize=9.5, leading=14)
    )
    document = SimpleDocTemplate(
        str(path),
        pagesize=letter,
        rightMargin=0.62 * inch,
        leftMargin=0.62 * inch,
        topMargin=0.7 * inch,
        bottomMargin=0.65 * inch,
        title="Access Portfolio Analysis",
        author="Access Portfolio Analyzer",
    )
    story: list[object] = []
    story.extend(
        [
            Paragraph("Access Portfolio Analysis", styles["ReportTitle"]),
            Paragraph(
                "Evidence-backed static analysis and modernization assessment", styles["ReportBody"]
            ),
            Spacer(1, 0.25 * inch),
            Paragraph("Executive Summary", styles["ReportHeading"]),
            _table(
                [
                    ["Measure", "Observed value"],
                    ["Inventory applications", str(len(inventory))],
                    ["Successfully staged primary artifacts", str(successful)],
                    ["Failed primary staging attempts", str(failed)],
                    [
                        "Applications with static evidence",
                        str(len({item.tool_inventory_id for item in evidence})),
                    ],
                    ["Evidence-backed modernization opportunities", str(len(recommendations))],
                ],
                [2.75 * inch, 3.35 * inch],
            ),
            Spacer(1, 0.18 * inch),
            Paragraph(
                " ".join(
                    (
                        "The stated inventory description is retained as a claim.",
                        (
                            "Conclusions are based on static evidence from verified staged "
                            "copies only."
                        ),
                    )
                ),
                styles["ReportBody"],
            ),
            Spacer(1, 0.12 * inch),
            Paragraph(
                (
                    "Recommendations require business validation; they are not automatic rewrite "
                    "or service decisions."
                ),
                styles["ReportBody"],
            ),
        ]
    )
    story.append(PageBreak())
    story.extend(_capability_section(capabilities, styles))
    story.append(PageBreak())
    story.extend(_dependency_section(datasources, dependencies, styles))
    story.append(PageBreak())
    story.extend(_recommendation_section(recommendations, styles))
    story.append(PageBreak())
    story.extend(_risk_and_next_steps_section(evidence, artifacts, styles))
    document.build(story, onFirstPage=_footer, onLaterPages=_footer)


def _capability_section(
    capabilities: list[CapabilityFinding], styles: dict[str, ParagraphStyle]
) -> list[object]:
    counts = Counter(item.capability for item in capabilities)
    rows: list[list[str]] = [["Observed technical capability", "Applications"]]
    rows.extend([[name, str(count)] for name, count in counts.most_common()])
    content: list[object] = [Paragraph("Capability Map", styles["ReportHeading"])]
    if counts:
        content.extend(
            [
                Paragraph(
                    " ".join(
                        (
                            (
                                "This taxonomy is assembled from observed static signals "
                                "in the analyzed corpus."
                            ),
                            "It does not impose business-domain labels in advance.",
                        )
                    ),
                    styles["ReportBody"],
                ),
                Spacer(1, 0.12 * inch),
                _table(rows, [4.65 * inch, 1.45 * inch]),
            ]
        )
    else:
        content.append(
            Paragraph(
                (
                    "No implementation evidence is available yet. Run Windows extraction "
                    "before drawing conclusions."
                ),
                styles["ReportBody"],
            )
        )
    return content


def _dependency_section(
    datasources: list[Datasource], dependencies: list[Dependency], styles: dict[str, ParagraphStyle]
) -> list[object]:
    content: list[object] = [Paragraph("Dependency Analysis", styles["ReportHeading"])]
    if datasources:
        content.append(Paragraph("Top observed datasource interactions", styles["ReportBody"]))
        rows = [["Platform", "Server / database", "Object", "Operation", "Application"]]
        rows.extend(
            [
                [
                    item.platform,
                    " / ".join(value for value in (item.server, item.database) if value),
                    ".".join(value for value in (item.schema_name, item.object_name) if value),
                    item.operation,
                    item.tool_inventory_id,
                ]
                for item in datasources[:20]
            ]
        )
        content.extend(
            [
                Spacer(1, 0.1 * inch),
                _table(rows, [0.8 * inch, 1.65 * inch, 1.55 * inch, 0.7 * inch, 1.4 * inch]),
            ]
        )
    else:
        content.append(
            Paragraph("No normalized datasource interactions were extracted.", styles["ReportBody"])
        )
    content.append(Spacer(1, 0.2 * inch))
    if dependencies:
        by_type = Counter(item.dependency_type for item in dependencies)
        content.extend(
            [
                Paragraph("External dependency types", styles["ReportBody"]),
                Spacer(1, 0.08 * inch),
                _table(
                    [["Dependency type", "Relationships"]]
                    + [[name, str(count)] for name, count in by_type.most_common()],
                    [3.4 * inch, 2.7 * inch],
                ),
            ]
        )
    else:
        content.append(
            Paragraph(
                "No external filesystem or application dependencies were extracted.",
                styles["ReportBody"],
            )
        )
    return content


def _recommendation_section(
    recommendations: list[Recommendation], styles: dict[str, ParagraphStyle]
) -> list[object]:
    content: list[object] = [
        Paragraph("Shared Capability and Consolidation Opportunities", styles["ReportHeading"])
    ]
    if not recommendations:
        content.append(
            Paragraph(
                " ".join(
                    (
                        (
                            "No opportunity crossed the threshold for a shared library, platform "
                            "capability, API, or service."
                        ),
                        (
                            "Recommendations are withheld until recurring implementation "
                            "evidence exists."
                        ),
                    )
                ),
                styles["ReportBody"],
            )
        )
        return content
    content.append(
        Paragraph(
            " ".join(
                (
                    "Each opportunity is derived from repeated static evidence.",
                    (
                        "The appropriate boundary depends on ownership, security, transactions, "
                        "and operations."
                    ),
                )
            ),
            styles["ReportBody"],
        )
    )
    for recommendation in recommendations:
        affected = ", ".join(recommendation.affected_tool_ids)
        content.extend(
            [
                Spacer(1, 0.14 * inch),
                Paragraph(escape(recommendation.title), styles["Heading2"]),
                Paragraph(
                    f"<b>Candidate boundary:</b> {escape(recommendation.category)}<br/>"
                    f"<b>Applications affected:</b> {escape(affected)}<br/>"
                    f"<b>Evidence items:</b> {len(recommendation.evidence)}<br/>"
                    f"{escape(recommendation.rationale)}",
                    styles["ReportBody"],
                ),
            ]
        )
    return content


def _risk_and_next_steps_section(
    evidence: list[Evidence], artifacts: list[StagedArtifact], styles: dict[str, ParagraphStyle]
) -> list[object]:
    from portfolio_analyzer.portfolio.recommendations import modernization_risks

    content: list[object] = [
        Paragraph("Modernization Risks and Next Steps", styles["ReportHeading"])
    ]
    risks = modernization_risks(evidence)
    if risks:
        rows = [["Observed pattern", "Applications", "Modernization implication"]]
        rows.extend(
            [[pattern, ", ".join(tools), implication] for pattern, tools, implication in risks]
        )
        content.extend(
            [
                Paragraph("Observed technical-debt themes", styles["ReportBody"]),
                Spacer(1, 0.1 * inch),
                _table(rows, [1.35 * inch, 1.25 * inch, 3.5 * inch]),
            ]
        )
    else:
        content.append(
            Paragraph(
                "No technical-debt themes can be supported until extraction yields evidence.",
                styles["ReportBody"],
            )
        )
    failed = [item for item in artifacts if item.is_primary and item.error]
    content.extend(
        [
            Spacer(1, 0.22 * inch),
            Paragraph("Recommended Next Steps", styles["Heading2"]),
            Paragraph(
                " ".join(
                    (
                        "1. Validate a representative sample with application owners.",
                        (
                            "2. Resolve staging and extraction limitations before inferring "
                            "absence of functionality."
                        ),
                        (
                            "3. Review opportunities with business and operational constraints "
                            "before choosing a boundary."
                        ),
                    )
                ),
                styles["ReportBody"],
            ),
        ]
    )
    if failed:
        content.append(Spacer(1, 0.12 * inch))
        content.append(
            Paragraph(
                f"Manual review queue: {len(failed)} primary artifact(s) failed staging or extraction.",  # noqa: E501
                styles["ReportBody"],
            )
        )
    return content


def _table(rows: list[list[str]], widths: list[float]) -> Table:
    table = Table(rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#123047")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("LEADING", (0, 0), (-1, -1), 10),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#C7D1D8")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#EDF3F6")]),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return table


def _footer(canvas: Any, document: Any) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#53636D"))
    canvas.drawString(
        0.62 * inch, 0.38 * inch, "Access Portfolio Analyzer - evidence-backed static analysis"
    )
    canvas.drawRightString(7.88 * inch, 0.38 * inch, f"Page {document.page}")
    canvas.restoreState()
