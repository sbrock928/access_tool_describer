"""Repository methods that retain source and staged provenance separately."""

from __future__ import annotations

from sqlalchemy.orm import Session

from portfolio_analyzer.models import InventoryRecord, StagedArtifact
from portfolio_analyzer.persistence.database import ApplicationRow, ArtifactRow


def save_inventory_and_artifact(
    session: Session, record: InventoryRecord, artifact: StagedArtifact
) -> None:
    app = session.get(ApplicationRow, record.tool_inventory_id)
    if app is None:
        app = ApplicationRow(
            tool_inventory_id=record.tool_inventory_id,
            tool_name=record.tool_name,
            stated_description=record.stated_description,
            source_path=str(record.filepath),
            staged_path=str(artifact.local_staged_path) if artifact.local_staged_path else None,
            sha256=artifact.sha256,
        )
        session.add(app)
    else:
        app.staged_path = str(artifact.local_staged_path) if artifact.local_staged_path else None
        app.sha256 = artifact.sha256
    session.add(
        ArtifactRow(
            tool_inventory_id=record.tool_inventory_id,
            original_path=str(artifact.original_source_path),
            local_path=str(artifact.local_staged_path) if artifact.local_staged_path else None,
            sha256=artifact.sha256,
            status=artifact.status.value,
            detail={"error": artifact.error, "is_primary": artifact.is_primary},
        )
    )
