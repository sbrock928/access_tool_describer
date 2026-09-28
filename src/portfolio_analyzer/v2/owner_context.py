"""Strict optional owner-context ingestion, kept separate from observed evidence."""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from portfolio_analyzer.v2.models import OwnerClaim

APPLICATION_ID_HEADER = "application_id"
OWNER_CONTEXT_FIELDS = (
    "business_owner",
    "technical_owner",
    "business_purpose",
    "criticality",
    "user_band",
    "lifecycle_intent",
    "data_sensitivity",
    "pain_points",
    "target_constraints",
)
OWNER_CONTEXT_HEADERS = (APPLICATION_ID_HEADER, *OWNER_CONTEXT_FIELDS)


def load_owner_context(
    path: Path,
    *,
    application_ids: set[str],
) -> dict[str, tuple[OwnerClaim, ...]]:
    """Load reviewed claims, rejecting unknown apps and duplicate/conflicting fields."""

    if not path.exists():
        return {}
    output: dict[str, list[OwnerClaim]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    try:
        with path.open(newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            headers = tuple(reader.fieldnames or ())
            missing = set(OWNER_CONTEXT_HEADERS) - set(headers)
            unknown = set(headers) - set(OWNER_CONTEXT_HEADERS)
            if missing:
                raise ValueError(
                    "owner context is missing columns: " + ", ".join(sorted(missing))
                )
            if unknown:
                raise ValueError(
                    "owner context contains unknown columns: " + ", ".join(sorted(unknown))
                )
            for row_number, row in enumerate(reader, start=2):
                application_id = (row.get(APPLICATION_ID_HEADER) or "").strip()
                values = {
                    field: (row.get(field) or "").strip()
                    for field in OWNER_CONTEXT_FIELDS
                }
                if not application_id and not any(values.values()):
                    continue
                if application_id not in application_ids:
                    raise ValueError(
                        f"owner context row {row_number} references unknown application "
                        f"'{application_id}'"
                    )
                for field, value in values.items():
                    if not value:
                        continue
                    identity = (application_id, field)
                    if identity in seen:
                        raise ValueError(
                            f"owner context repeats {field} for application {application_id}"
                        )
                    seen.add(identity)
                    output[application_id].append(
                        OwnerClaim(
                            application_id=application_id,
                            field=field,
                            value=value,
                            source=path.name,
                        )
                    )
    except OSError as exc:
        raise ValueError(f"cannot read owner context: {path}") from exc
    return {
        application_id: tuple(sorted(items, key=lambda item: item.claim_id))
        for application_id, items in output.items()
    }
