"""Excel, CSV, and PDF outputs from normalized static-analysis results."""

from __future__ import annotations

import csv
from collections import Counter
from collections.abc import Iterable, Sequence
from html import escape
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from portfolio_analyzer.models import (
    AnalysisCoverage,
    CapabilityFinding,
    Datasource,
    Dependency,
    Evidence,
    InventoryRecord,
    Recommendation,
    StagedArtifact,
)


def write_csv(
    path: Path,
    rows: Iterable[dict[str, Any]],
    *,
    headers: Sequence[str] | None = None,
) -> None:
    records = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(headers or dict.fromkeys(key for row in records for key in row))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(
            {key: _spreadsheet_safe(value) for key, value in record.items()}
            for record in records
        )


def write_workbook(
    path: Path,
    inventory: list[InventoryRecord],
    artifacts: list[StagedArtifact],
    evidence: list[Evidence],
    datasources: list[Datasource],
    dependencies: list[Dependency],
    capabilities: list[CapabilityFinding],
    *,
    recommendations: list[Recommendation] | None = None,
    coverage: list[AnalysisCoverage] | None = None,
) -> None:
    recommendations = recommendations or []
    coverage = coverage or []
    application_names = {item.tool_inventory_id: item.tool_name for item in inventory}
    workbook = Workbook()
    workbook.remove(workbook.active)
    analyzed = (
        sum(item.analysis_status == "complete" for item in coverage)
        if coverage
        else len({item.tool_inventory_id for item in evidence})
    )
    extraction_attention = (
        sum(
            item.extraction_status in {"failed", "complete_with_warnings"}
            for item in coverage
        )
        if coverage
        else sum(item.is_primary and bool(item.error) for item in artifacts)
    )
    _sheet(
        workbook,
        "Portfolio Summary",
        [
            ["Measure", "Value"],
            ["Inventory applications", len(inventory)],
            ["Successfully staged primary artifacts", sum(
                item.is_primary and item.status.value == "staged" for item in artifacts
            )],
            ["Applications analyzed", analyzed],
            ["Extraction failures or warnings", extraction_attention],
            ["Evidence items", len(evidence)],
            ["Normalized datasource interactions", len(datasources)],
            ["External dependencies", len(dependencies)],
            ["Evidence-backed recommendations", len(recommendations)],
        ],
    )
    if coverage:
        _sheet(
            workbook,
            "Analysis Coverage",
            [
                [
                    "EUC Name",
                    "Primary File",
                    "Staging",
                    "Extraction",
                    "Analysis",
                    "Objects",
                    "Warnings",
                    "Evidence",
                    "Data Sources",
                    "Dependencies",
                    "Capabilities",
                    "Notes",
                ],
                *[
                    [
                        item.tool_name,
                        item.inventory_filename,
                        item.staging_status,
                        item.extraction_status,
                        item.analysis_status,
                        item.extracted_object_count,
                        item.extraction_warning_count,
                        item.evidence_count,
                        item.datasource_count,
                        item.dependency_count,
                        item.capability_count,
                        " | ".join(item.notes),
                    ]
                    for item in coverage
                ],
            ],
        )
    _sheet(
        workbook,
        "Applications",
        [
            [
                "EUC Name",
                "Inventory File Name",
                "Stated Description",
                "Original Source Path",
            ],
            *[
                [
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
                "EUC Name",
                "Original Path",
                "Local Staged Path",
                "SHA-256",
                "Status",
                "Error",
            ],
            *[
                [
                    _application_name(a.tool_inventory_id, application_names),
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
                "EUC Name",
                "Platform",
                "Server",
                "Database",
                "Schema",
                "Object",
                "Operation",
                "Confidence",
                "Evidence Items",
                "Connection Summary",
            ],
            *[
                [
                    _application_name(d.tool_inventory_id, application_names),
                    d.platform,
                    d.server or "",
                    d.database or "",
                    d.schema_name or "",
                    d.object_name or "",
                    d.operation,
                    d.confidence,
                    len(d.evidence),
                    d.connection_summary or "",
                ]
                for d in datasources
            ],
        ],
    )
    _sheet(
        workbook,
        "Dependencies",
        [
            ["EUC Name", "Source", "Target", "Type", "Operation", "Confidence"],
            *[
                [
                    _application_name(d.tool_inventory_id, application_names),
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
            ["EUC Name", "Capability", "Layer", "Confidence", "Evidence Items"],
            *[
                [
                    _application_name(c.tool_inventory_id, application_names),
                    c.capability,
                    c.layer,
                    c.confidence,
                    len(c.evidence),
                ]
                for c in capabilities
            ],
        ],
    )
    _sheet(
        workbook,
        "Recommendations",
        [
            [
                "Category",
                "Title",
                "Confidence",
                "Affected EUC Names",
                "Application Count",
                "Evidence Items",
                "Rationale",
            ],
            *[
                [
                    item.category,
                    item.title,
                    item.confidence,
                    ", ".join(
                        _application_name(tool_id, application_names)
                        for tool_id in item.affected_tool_ids
                    ),
                    len(item.affected_tool_ids),
                    len(item.evidence),
                    item.rationale,
                ]
                for item in recommendations
            ],
        ],
    )
    _sheet(
        workbook,
        "Evidence",
        [
            [
                "EUC Name",
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
                    _application_name(e.tool_inventory_id, application_names),
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
            ["EUC Name", "Original Source Path", "Error"],
            *[
                [
                    _application_name(a.tool_inventory_id, application_names),
                    str(a.original_source_path),
                    a.error or "",
                ]
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
        sheet.append([_spreadsheet_safe(value) for value in row])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="123047")
        cell.font = Font(bold=True, color="FFFFFF")
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for column in sheet.columns:
        letter = column[0].column_letter
        sheet.column_dimensions[letter].width = min(
            max(len(str(cell.value or "")) for cell in column) + 2, 60
        )
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)


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
    coverage: list[AnalysisCoverage] | None = None,
) -> None:
    """Render an evidence-backed, multi-page executive and architecture report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    recommendations = recommendations or []
    evidence = evidence or []
    datasources = datasources or []
    dependencies = dependencies or []
    coverage = coverage or []
    application_names = {item.tool_inventory_id: item.tool_name for item in inventory}
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
                        "Applications with completed static analysis",
                        str(sum(item.analysis_status == "complete" for item in coverage))
                        if coverage
                        else str(len({item.tool_inventory_id for item in evidence})),
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
    story.extend(_coverage_section(coverage, styles))
    story.append(PageBreak())
    story.extend(_capability_section(capabilities, styles))
    story.append(PageBreak())
    story.extend(_dependency_section(datasources, dependencies, styles))
    story.append(PageBreak())
    story.extend(_recommendation_section(recommendations, application_names, styles))
    story.append(PageBreak())
    story.extend(
        _risk_and_next_steps_section(
            evidence,
            artifacts,
            coverage,
            application_names,
            styles,
        )
    )
    document.build(story, onFirstPage=_footer, onLaterPages=_footer)


def _coverage_section(
    coverage: list[AnalysisCoverage], styles: dict[str, ParagraphStyle]
) -> list[object]:
    content: list[object] = [Paragraph("Analysis Coverage and Confidence", styles["ReportHeading"])]
    if not coverage:
        content.append(
            Paragraph(
                "Pipeline coverage was not available; absence of findings must not be "
                "interpreted as absence of functionality.",
                styles["ReportBody"],
            )
        )
        return content
    counts = Counter(item.analysis_status for item in coverage)
    content.extend(
        [
            Paragraph(
                "Findings are conclusive only for applications with completed analysis. "
                "Extraction warnings may reduce completeness even when analysis ran successfully.",
                styles["ReportBody"],
            ),
            Spacer(1, 0.12 * inch),
            _table(
                [["Analysis state", "Applications"]]
                + [
                    [status.replace("_", " ").title(), str(count)]
                    for status, count in sorted(counts.items())
                ],
                [4.65 * inch, 1.45 * inch],
            ),
        ]
    )
    attention = [
        item
        for item in coverage
        if item.analysis_status != "complete" or item.extraction_warning_count
    ]
    if attention:
        content.extend(
            [
                Spacer(1, 0.18 * inch),
                Paragraph("Applications requiring attention", styles["Heading2"]),
                _table(
                    [["EUC Name", "Primary File", "Extraction", "Analysis", "Warnings"]]
                    + [
                        [
                            item.tool_name,
                            item.inventory_filename,
                            item.extraction_status.replace("_", " "),
                            item.analysis_status.replace("_", " "),
                            str(item.extraction_warning_count),
                        ]
                        for item in attention[:25]
                    ],
                    [1.25 * inch, 1.35 * inch, 1.35 * inch, 1.25 * inch, 0.9 * inch],
                ),
            ]
        )
    return content


def _capability_section(
    capabilities: list[CapabilityFinding], styles: dict[str, ParagraphStyle]
) -> list[object]:
    grouped: dict[str, list[CapabilityFinding]] = {}
    for item in capabilities:
        grouped.setdefault(item.capability, []).append(item)
    confidence_rank = {"high": 3, "medium": 2, "low": 1}
    rows: list[list[str]] = [
        ["Observed technical capability", "Applications", "Evidence", "Confidence"]
    ]
    for name, findings in sorted(
        grouped.items(),
        key=lambda pair: (-len({item.tool_inventory_id for item in pair[1]}), pair[0]),
    ):
        confidence = min(
            (item.confidence.value for item in findings),
            key=lambda value: confidence_rank[value],
        )
        rows.append(
            [
                name,
                str(len({item.tool_inventory_id for item in findings})),
                str(sum(len(item.evidence) for item in findings)),
                confidence.title(),
            ]
        )
    content: list[object] = [Paragraph("Capability Map", styles["ReportHeading"])]
    if grouped:
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
                _table(rows, [3.25 * inch, 0.95 * inch, 0.85 * inch, 1.05 * inch]),
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
        content.append(Paragraph("Most-used datasource interactions", styles["ReportBody"]))
        grouped: dict[tuple[str, str, str, str], list[Datasource]] = {}
        for item in datasources:
            key = (
                item.platform,
                " / ".join(value for value in (item.server, item.database) if value),
                ".".join(value for value in (item.schema_name, item.object_name) if value),
                item.operation,
            )
            grouped.setdefault(key, []).append(item)
        rows = [["Platform", "Server / database", "Object", "Operation", "Apps", "Evidence"]]
        ranked = sorted(
            grouped.items(),
            key=lambda pair: (
                -len({item.tool_inventory_id for item in pair[1]}),
                -sum(len(item.evidence) for item in pair[1]),
                pair[0],
            ),
        )
        rows.extend(
            [
                [
                    platform,
                    location,
                    object_name,
                    operation,
                    str(len({item.tool_inventory_id for item in items})),
                    str(sum(len(item.evidence) for item in items)),
                ]
                for (platform, location, object_name, operation), items in ranked[:20]
            ]
        )
        content.extend(
            [
                Spacer(1, 0.1 * inch),
                _table(
                    rows,
                    [0.8 * inch, 1.45 * inch, 1.35 * inch, 0.7 * inch, 0.6 * inch, 0.7 * inch],
                ),
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
    recommendations: list[Recommendation],
    application_names: dict[str, str],
    styles: dict[str, ParagraphStyle],
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
        affected = ", ".join(
            _application_name(tool_id, application_names)
            for tool_id in recommendation.affected_tool_ids
        )
        content.extend(
            [
                Spacer(1, 0.14 * inch),
                Paragraph(escape(recommendation.title), styles["Heading2"]),
                Paragraph(
                    f"<b>Candidate boundary:</b> {escape(recommendation.category)}<br/>"
                    f"<b>Confidence:</b> {escape(recommendation.confidence.value.title())}<br/>"
                    f"<b>EUC names affected:</b> {escape(affected)}<br/>"
                    f"<b>Evidence items:</b> {len(recommendation.evidence)}<br/>"
                    f"{escape(recommendation.rationale)}",
                    styles["ReportBody"],
                ),
            ]
        )
    return content


def _risk_and_next_steps_section(
    evidence: list[Evidence],
    artifacts: list[StagedArtifact],
    coverage: list[AnalysisCoverage],
    application_names: dict[str, str],
    styles: dict[str, ParagraphStyle],
) -> list[object]:
    from portfolio_analyzer.portfolio.recommendations import modernization_risks

    content: list[object] = [
        Paragraph("Modernization Risks and Next Steps", styles["ReportHeading"])
    ]
    risks = modernization_risks(evidence)
    if risks:
        rows = [["Observed pattern", "EUC Names", "Modernization implication"]]
        rows.extend(
            [
                [
                    pattern,
                    ", ".join(
                        _application_name(tool_id, application_names) for tool_id in tools
                    ),
                    implication,
                ]
                for pattern, tools, implication in risks
            ]
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
    coverage_attention = [
        item
        for item in coverage
        if item.analysis_status != "complete" or item.extraction_warning_count
    ]
    content.extend(
        [
            Spacer(1, 0.22 * inch),
            Paragraph("Recommended Next Steps", styles["Heading2"]),
            Paragraph(
                "1. Validate a representative sample with application owners.",
                styles["ReportBody"],
            ),
            Paragraph(
                "2. Resolve staging and extraction limitations before inferring absence of "
                "functionality.",
                styles["ReportBody"],
            ),
            Paragraph(
                "3. Review opportunities with business and operational constraints before "
                "choosing a boundary.",
                styles["ReportBody"],
            ),
        ]
    )
    if failed or coverage_attention:
        content.append(Spacer(1, 0.12 * inch))
        content.append(
            Paragraph(
                "Manual review queue: "
                f"{len(coverage_attention) if coverage else len(failed)} application(s) have "
                "incomplete or warning-qualified coverage.",
                styles["ReportBody"],
            )
        )
    return content


def _table(rows: list[list[str]], widths: list[float]) -> Table:
    header_style = ParagraphStyle(
        name="TableHeader",
        fontName="Helvetica-Bold",
        fontSize=8,
        leading=10,
        textColor=colors.white,
    )
    body_style = ParagraphStyle(
        name="TableBody",
        fontName="Helvetica",
        fontSize=8,
        leading=10,
        textColor=colors.black,
    )
    wrapped = [
        [
            Paragraph(escape(str(value)), header_style if row_index == 0 else body_style)
            for value in row
        ]
        for row_index, row in enumerate(rows)
    ]
    table = Table(wrapped, colWidths=widths, repeatRows=1, hAlign="LEFT")
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


def _spreadsheet_safe(value: object) -> object:
    """Prevent report text from being interpreted as a spreadsheet formula."""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return f"'{value}"
    return value


def _application_name(tool_inventory_id: str, names: dict[str, str]) -> str:
    return names.get(tool_inventory_id, "Unknown EUC")
