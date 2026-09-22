"""Optional owner context and semantic gold-set files."""

from __future__ import annotations

import csv
from collections import defaultdict, deque
from pathlib import Path

from portfolio_analyzer.models import AnalysisCoverage, Claim, InventoryRecord

CONTEXT_HEADERS = [
    "euc_name",
    "business_owner",
    "technical_owner",
    "business_purpose",
    "criticality",
    "user_band",
    "lifecycle_intent",
    "data_sensitivity",
    "pain_points",
    "target_constraints",
]
GOLD_SET_HEADERS = [
    "euc_name",
    "expected_primary_archetype",
    "expected_business_capabilities",
    "expected_disposition",
    "reviewer_notes",
]


def initialize_context_file(path: Path, inventory: list[InventoryRecord]) -> bool:
    return _initialize_csv(
        path,
        CONTEXT_HEADERS,
        [{"euc_name": record.tool_name} for record in _unique_inventory(inventory)],
    )


def initialize_gold_set(
    path: Path,
    inventory: list[InventoryRecord],
    coverage: list[AnalysisCoverage],
    *,
    size: int = 20,
) -> bool:
    selected = select_gold_set(inventory, coverage, size=size)
    return _initialize_csv(
        path,
        GOLD_SET_HEADERS,
        [{"euc_name": record.tool_name} for record in selected],
    )


def select_gold_set(
    inventory: list[InventoryRecord],
    coverage: list[AnalysisCoverage],
    *,
    size: int,
) -> list[InventoryRecord]:
    unique = _unique_inventory(inventory)
    status_by_name = {item.tool_name.casefold(): item.analysis_status for item in coverage}
    groups: dict[str, deque[InventoryRecord]] = defaultdict(deque)
    for record in sorted(unique, key=lambda item: item.tool_name.casefold()):
        groups[status_by_name.get(record.tool_name.casefold(), "unknown")].append(record)
    selected: list[InventoryRecord] = []
    while len(selected) < min(size, len(unique)) and groups:
        for key in sorted(list(groups)):
            group = groups[key]
            if group:
                selected.append(group.popleft())
                if len(selected) >= min(size, len(unique)):
                    break
            if not group:
                groups.pop(key, None)
    return selected


def load_claims(
    path: Path,
    inventory: list[InventoryRecord],
    *,
    include_inventory_description: bool,
) -> list[Claim]:
    by_name = {record.tool_name.casefold(): record.tool_inventory_id for record in inventory}
    claims: list[Claim] = []
    if include_inventory_description:
        seen: set[tuple[str, str]] = set()
        for record in inventory:
            if (
                record.stated_description
                and (
                    record.tool_inventory_id,
                    record.stated_description,
                )
                not in seen
            ):
                claims.append(
                    Claim(
                        tool_inventory_id=record.tool_inventory_id,
                        field="inventory_description",
                        value=record.stated_description,
                        source="inventory workbook",
                    )
                )
                seen.add((record.tool_inventory_id, record.stated_description))
    if not path.exists():
        return claims
    with path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        missing = set(CONTEXT_HEADERS) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Context CSV is missing columns: {', '.join(sorted(missing))}")
        for row_number, row in enumerate(reader, start=2):
            name = (row.get("euc_name") or "").strip()
            tool_id = by_name.get(name.casefold())
            if not tool_id:
                if name:
                    raise ValueError(f"Unknown EUC name in context CSV row {row_number}: {name}")
                continue
            for field in CONTEXT_HEADERS[1:]:
                value = (row.get(field) or "").strip()
                if value:
                    claims.append(
                        Claim(
                            tool_inventory_id=tool_id,
                            field=field,
                            value=value,
                            source=path.name,
                        )
                    )
    return claims


def read_gold_set(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        missing = set(GOLD_SET_HEADERS) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Gold-set CSV is missing columns: {', '.join(sorted(missing))}")
        return [{key: (value or "").strip() for key, value in row.items()} for row in reader]


def _initialize_csv(path: Path, headers: list[str], rows: list[dict[str, str]]) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as destination:
        writer = csv.DictWriter(destination, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)
    return True


def _unique_inventory(inventory: list[InventoryRecord]) -> list[InventoryRecord]:
    output: list[InventoryRecord] = []
    seen: set[str] = set()
    for record in inventory:
        if record.tool_inventory_id not in seen:
            output.append(record)
            seen.add(record.tool_inventory_id)
    return output
