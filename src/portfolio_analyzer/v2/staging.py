"""Construction and validation of the V2 staging index."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from portfolio_analyzer.models import ArtifactStatus, InventoryRecord, StagedArtifact
from portfolio_analyzer.v2.identity import opaque_source_identity
from portfolio_analyzer.v2.models import OwnerClaim
from portfolio_analyzer.v2.workflow import (
    InventoryExclusion,
    StagedApplication,
    StagedArtifactRecord,
    StageIndex,
    StagingFailure,
)


def build_stage_index(
    *,
    workspace: Path,
    inventory_sha256: str,
    records: list[InventoryRecord],
    artifacts: list[StagedArtifact],
    owner_claims: Mapping[str, tuple[OwnerClaim, ...]] | None = None,
    generated_at: datetime | None = None,
) -> StageIndex:
    """Group inventory rows into applications while retaining every exclusion or failure."""
    record_by_source = {
        (record.tool_inventory_id, _source_key(record.filepath)): record for record in records
    }
    staged: list[StagedArtifactRecord] = []
    exclusions: list[InventoryExclusion] = []
    failures: list[StagingFailure] = []
    artifact_ids_by_application: dict[str, list[str]] = defaultdict(list)
    descriptions_by_application: dict[str, set[str]] = defaultdict(set)
    names = {record.tool_inventory_id: record.tool_name for record in records}
    for record in records:
        if record.stated_description:
            descriptions_by_application[record.tool_inventory_id].add(
                record.stated_description
            )

    workspace_root = workspace.resolve()
    for artifact in artifacts:
        matched_record = record_by_source.get(
            (artifact.tool_inventory_id, _source_key(artifact.original_source_path))
        )
        application_name = (
            matched_record.tool_name
            if matched_record is not None
            else names[artifact.tool_inventory_id]
        )
        if artifact.status is ArtifactStatus.SKIPPED_UNSUPPORTED_FORMAT:
            exclusions.append(
                InventoryExclusion(
                    application_id=artifact.tool_inventory_id,
                    application_name=application_name,
                    filename=artifact.filename,
                    source_locator=opaque_source_identity(
                        str(artifact.original_source_path)
                    ),
                    reason=artifact.error or "unsupported primary format",
                )
            )
            continue
        if artifact.status is not ArtifactStatus.STAGED:
            failures.append(
                StagingFailure(
                    application_id=artifact.tool_inventory_id,
                    application_name=application_name,
                    filename=artifact.filename,
                    source_locator=opaque_source_identity(
                        str(artifact.original_source_path)
                    ),
                    reason=artifact.error or "staging failed",
                )
            )
            continue
        if (
            artifact.local_staged_path is None
            or artifact.sha256 is None
            or artifact.size_bytes is None
        ):
            raise ValueError("successfully staged artifact lacks path, size, or hash")
        staged_path = artifact.local_staged_path.resolve()
        try:
            relative_path = staged_path.relative_to(workspace_root).as_posix()
        except ValueError as exc:
            raise ValueError("staged artifact is outside the V2 workspace") from exc
        staged_record = StagedArtifactRecord(
            application_id=artifact.tool_inventory_id,
            application_name=application_name,
            source_locator=opaque_source_identity(str(artifact.original_source_path)),
            staged_relative_path=relative_path,
            filename=artifact.filename,
            access_format=cast(
                Literal["accdb", "mdb"],
                artifact.extension.removeprefix(".").casefold(),
            ),
            size_bytes=artifact.size_bytes,
            sha256=artifact.sha256,
            artifact_id=artifact.artifact_id,
        )
        staged.append(staged_record)
        artifact_ids_by_application[artifact.tool_inventory_id].append(
            staged_record.artifact_id
        )

    applications = tuple(
        StagedApplication(
            application_id=application_id,
            application_name=names[application_id],
            inventory_descriptions=tuple(descriptions_by_application[application_id]),
            owner_claims=(owner_claims or {}).get(application_id, ()),
            artifact_ids=tuple(artifact_ids),
        )
        for application_id, artifact_ids in artifact_ids_by_application.items()
    )
    return StageIndex(
        generated_at=generated_at or datetime.now(UTC),
        inventory_sha256=inventory_sha256,
        applications=applications,
        artifacts=tuple(staged),
        exclusions=tuple(exclusions),
        failures=tuple(failures),
    )


def _source_key(path: Path) -> str:
    return str(path).replace("\\", "/").casefold()
