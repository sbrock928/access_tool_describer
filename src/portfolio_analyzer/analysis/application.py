"""Cross-platform deterministic analysis of extracted definitions."""

from __future__ import annotations

from portfolio_analyzer.models import (
    Confidence,
    Datasource,
    Dependency,
    Evidence,
    ExtractedApplication,
)
from portfolio_analyzer.parsing.connections import (
    infer_platform,
    parse_connection_string,
    redact_connection_string,
)
from portfolio_analyzer.parsing.paths import extract_windows_paths
from portfolio_analyzer.parsing.sql import classify_sql, split_qualified_name
from portfolio_analyzer.parsing.vba import analyze_vba


def analyze_application(
    extracted: ExtractedApplication,
) -> tuple[list[Evidence], list[Datasource], list[Dependency]]:
    evidence: list[Evidence] = []
    datasources: list[Datasource] = []
    dependencies: list[Dependency] = []
    for item in extracted.objects:
        text = item.definition or item.properties.get("connect", "")
        if item.object_type in {"query", "linked_table"}:
            sql = classify_sql(text)
            for object_name in sql.object_names:
                schema, object_leaf = split_qualified_name(object_name)
                fact = _evidence(
                    extracted,
                    item.object_type,
                    item.name,
                    text,
                    f"SQL {sql.operation} {object_name}",
                )
                evidence.append(fact)
                datasources.append(
                    Datasource(
                        tool_inventory_id=extracted.tool_inventory_id,
                        platform="Unknown",
                        schema_name=schema,
                        object_name=object_leaf,
                        operation=sql.operation,
                        evidence=[fact],
                    )
                )
        if item.object_type == "reference":
            target = item.properties.get("full_path") or item.name
            is_broken = item.properties.get("is_broken", "False").casefold() == "true"
            inference = "Broken Access/VBA reference" if is_broken else "Access/VBA reference"
            fact = _evidence(extracted, item.object_type, item.name, target, inference)
            evidence.append(fact)
            dependencies.append(
                Dependency(
                    tool_inventory_id=extracted.tool_inventory_id,
                    source=item.name,
                    target=target,
                    dependency_type="access_vba_reference",
                    operation="READ",
                    confidence=Confidence.HIGH if is_broken else Confidence.MEDIUM,
                    evidence=[fact],
                )
            )
        for path in extract_windows_paths(text):
            fact = _evidence(extracted, item.object_type, item.name, path, "Filesystem dependency")
            evidence.append(fact)
            dependencies.append(
                Dependency(
                    tool_inventory_id=extracted.tool_inventory_id,
                    source=item.name,
                    target=path,
                    dependency_type="filesystem",
                    evidence=[fact],
                )
            )
        for finding in analyze_vba(text):
            evidence.append(
                Evidence(
                    tool_inventory_id=extracted.tool_inventory_id,
                    artifact_path=str(extracted.staged_path),
                    object_type=item.object_type,
                    object_name=item.name,
                    location=finding.location,
                    text=finding.text,
                    inference=finding.kind,
                    confidence=Confidence.HIGH,
                )
            )
        if "=" in text and any(
            token in text.casefold() for token in ("server=", "provider=", "dsn=")
        ):
            pairs = parse_connection_string(text)
            if pairs:
                fact = _evidence(
                    extracted,
                    item.object_type,
                    item.name,
                    redact_connection_string(text),
                    "Connection string",
                )
                evidence.append(fact)
                datasources.append(
                    Datasource(
                        tool_inventory_id=extracted.tool_inventory_id,
                        platform=infer_platform(pairs),
                        server=pairs.get("server") or pairs.get("data source"),
                        database=pairs.get("database") or pairs.get("initial catalog"),
                        connection_summary=redact_connection_string(text),
                        evidence=[fact],
                    )
                )
    return evidence, datasources, dependencies


def _evidence(
    extracted: ExtractedApplication, object_type: str, object_name: str, text: str, inference: str
) -> Evidence:
    return Evidence(
        tool_inventory_id=extracted.tool_inventory_id,
        artifact_path=str(extracted.staged_path),
        object_type=object_type,
        object_name=object_name,
        text=text,
        inference=inference,
        confidence=Confidence.HIGH,
    )
