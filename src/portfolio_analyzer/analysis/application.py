"""Cross-platform deterministic analysis of extracted definitions."""

from __future__ import annotations

from collections.abc import Callable, Hashable

from portfolio_analyzer.models import (
    Confidence,
    Datasource,
    Dependency,
    Evidence,
    ExtractedApplication,
    ExtractedObject,
)
from portfolio_analyzer.parsing.connections import (
    infer_platform,
    parse_connection_string,
    redact_connection_string,
)
from portfolio_analyzer.parsing.paths import extract_windows_paths
from portfolio_analyzer.parsing.sql import classify_sql, split_qualified_name
from portfolio_analyzer.parsing.vba import analyze_vba

CODE_OBJECT_TYPES = frozenset({"module", "form", "report", "macro"})


def analyze_application(
    extracted: ExtractedApplication,
) -> tuple[list[Evidence], list[Datasource], list[Dependency]]:
    evidence: list[Evidence] = []
    datasources: list[Datasource] = []
    dependencies: list[Dependency] = []
    linked_tables = {
        item.name.casefold(): item
        for item in extracted.objects
        if item.object_type == "linked_table"
    }
    local_objects = {
        item.name.casefold()
        for item in extracted.objects
        if item.object_type in {"table", "query"}
    }
    for item in extracted.objects:
        text = item.definition or ""
        if item.object_type == "query":
            sql = classify_sql(text)
            for reference in sql.references:
                schema, object_leaf = split_qualified_name(reference.name)
                location = f"query SQL ({reference.operation})"
                fact = _evidence(
                    extracted,
                    item.object_type,
                    item.name,
                    _excerpt(text),
                    f"SQL {reference.operation} {reference.name}",
                    location=location,
                )
                evidence.append(fact)
                linked = linked_tables.get(reference.name.casefold())
                if linked is not None:
                    datasource = _linked_table_datasource(extracted, linked, reference.operation)
                    datasource.evidence.insert(0, fact)
                    datasources.append(datasource)
                else:
                    datasources.append(
                        Datasource(
                            tool_inventory_id=extracted.tool_inventory_id,
                            platform=(
                                "Access (local)"
                                if reference.name.casefold() in local_objects
                                else "Unknown"
                            ),
                            schema_name=schema,
                            object_name=object_leaf,
                            operation=reference.operation,
                            confidence=(
                                Confidence.HIGH
                                if reference.name.casefold() in local_objects
                                else Confidence.MEDIUM
                            ),
                            evidence=[fact],
                        )
                    )
        if item.object_type == "linked_table":
            datasource = _linked_table_datasource(extracted, item, "UNKNOWN")
            datasources.append(datasource)
            evidence.extend(datasource.evidence)
        if item.object_type == "reference":
            target = item.properties.get("full_path") or item.name
            is_broken = item.properties.get("is_broken", "False").casefold() == "true"
            inference = "Broken Access/VBA reference" if is_broken else "Access/VBA reference"
            fact = _evidence(
                extracted,
                item.object_type,
                item.name,
                target,
                inference,
                location="reference metadata",
            )
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
        for path in extract_windows_paths(text if item.object_type != "reference" else ""):
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
        for finding in analyze_vba(text) if item.object_type in CODE_OBJECT_TYPES else []:
            evidence.append(
                Evidence(
                    tool_inventory_id=extracted.tool_inventory_id,
                    artifact_path=str(extracted.staged_path),
                    object_type=item.object_type,
                    object_name=item.name,
                    location=finding.location,
                    text=finding.text,
                    inference=finding.kind,
                    confidence=finding.confidence,
                )
            )
    return (
        _unique(evidence, key=lambda item: (
            item.tool_inventory_id,
            item.object_type,
            item.object_name,
            item.location,
            item.text,
            item.inference,
        )),
        _merge_datasources(datasources),
        _unique(dependencies, key=lambda item: (
            item.tool_inventory_id,
            item.source,
            item.target.casefold(),
            item.dependency_type,
            item.operation,
        )),
    )


def _evidence(
    extracted: ExtractedApplication,
    object_type: str,
    object_name: str,
    text: str,
    inference: str,
    *,
    location: str | None = None,
    confidence: Confidence = Confidence.HIGH,
) -> Evidence:
    return Evidence(
        tool_inventory_id=extracted.tool_inventory_id,
        artifact_path=str(extracted.staged_path),
        object_type=object_type,
        object_name=object_name,
        location=location,
        text=text,
        inference=inference,
        confidence=confidence,
    )


def _linked_table_datasource(
    extracted: ExtractedApplication, item: ExtractedObject, operation: str
) -> Datasource:
    # Kept as a small helper so linked-table metadata is interpreted consistently both when
    # inventorying the link and when resolving a query reference through it.
    connection = item.properties.get("connect", "")
    pairs = parse_connection_string(connection)
    source_name = item.properties.get("source_table_name") or item.name
    schema, object_name = split_qualified_name(source_name)
    redacted = redact_connection_string(connection)
    fact = _evidence(
        extracted,
        item.object_type,
        item.name,
        redacted or source_name,
        "Linked table connection",
        location="TableDef.Connect",
        confidence=Confidence.HIGH,
    )
    return Datasource(
        tool_inventory_id=extracted.tool_inventory_id,
        platform=infer_platform(pairs),
        server=pairs.get("server") or pairs.get("data source") or pairs.get("host"),
        database=pairs.get("database") or pairs.get("initial catalog") or pairs.get("dbq"),
        schema_name=schema,
        object_name=object_name,
        operation=operation,
        connection_summary=redacted or None,
        confidence=Confidence.HIGH if pairs else Confidence.MEDIUM,
        evidence=[fact],
    )


def _excerpt(value: str, limit: int = 500) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else f"{compact[: limit - 1]}…"


def _unique[T](items: list[T], *, key: Callable[[T], Hashable]) -> list[T]:
    result: list[T] = []
    seen: set[Hashable] = set()
    for item in items:
        marker = key(item)
        if marker not in seen:
            seen.add(marker)
            result.append(item)
    return result


def _merge_datasources(items: list[Datasource]) -> list[Datasource]:
    grouped: dict[tuple[object, ...], Datasource] = {}
    for item in items:
        marker = (
            item.tool_inventory_id,
            item.platform,
            item.server,
            item.database,
            item.schema_name,
            item.object_name,
            item.operation,
        )
        existing = grouped.get(marker)
        if existing is None:
            grouped[marker] = item
        else:
            existing.evidence = _unique(
                [*existing.evidence, *item.evidence],
                key=lambda fact: (
                    fact.object_type,
                    fact.object_name,
                    fact.location,
                    fact.inference,
                    fact.text,
                ),
            )
    return list(grouped.values())
