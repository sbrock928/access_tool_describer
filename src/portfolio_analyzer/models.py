"""Stable, platform-independent domain models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ArtifactStatus(StrEnum):
    STAGED = "staged"
    FAILED = "failed"
    SKIPPED = "skipped"


class InventoryRecord(BaseModel):
    """A row preserved from the authoritative inventory workbook."""

    model_config = ConfigDict(frozen=True)
    tool_inventory_id: str
    tool_name: str
    inventory_filename: str
    stated_description: str | None = None
    filepath: Path
    original_values: dict[str, Any] = Field(default_factory=dict)


class StagedArtifact(BaseModel):
    """Provenance token for an artifact eligible for extraction."""

    tool_inventory_id: str
    original_source_path: Path
    local_staged_path: Path | None = None
    filename: str
    extension: str
    size_bytes: int | None = None
    source_modified_at: datetime | None = None
    sha256: str | None = None
    status: ArtifactStatus
    error: str | None = None
    is_primary: bool = True


class Evidence(BaseModel):
    tool_inventory_id: str
    artifact_path: str
    object_type: str
    object_name: str
    location: str | None = None
    text: str
    inference: str | None = None
    confidence: Confidence = Confidence.HIGH


class Datasource(BaseModel):
    tool_inventory_id: str
    platform: str
    server: str | None = None
    database: str | None = None
    schema_name: str | None = None
    object_name: str | None = None
    operation: str = "UNKNOWN"
    connection_summary: str | None = None
    confidence: Confidence = Confidence.MEDIUM
    evidence: list[Evidence] = Field(default_factory=list)


class Dependency(BaseModel):
    tool_inventory_id: str
    source: str
    target: str
    dependency_type: str
    operation: str = "UNKNOWN"
    confidence: Confidence = Confidence.MEDIUM
    evidence: list[Evidence] = Field(default_factory=list)


class ExtractedObject(BaseModel):
    object_type: str
    name: str
    definition: str | None = None
    properties: dict[str, str] = Field(default_factory=dict)


class ExtractedApplication(BaseModel):
    tool_inventory_id: str
    staged_path: Path
    extractor_version: str
    objects: list[ExtractedObject] = Field(default_factory=list)
    extraction_errors: list[str] = Field(default_factory=list)


class CapabilityFinding(BaseModel):
    tool_inventory_id: str
    capability: str
    layer: str
    confidence: Confidence
    evidence: list[Evidence] = Field(default_factory=list)


class SimilarityRelationship(BaseModel):
    source_tool_id: str
    target_tool_id: str
    score: float
    reasons: list[str]
    confidence: Confidence


class Recommendation(BaseModel):
    """An evidence-backed modernization opportunity, not an automatic decision."""

    category: str
    title: str
    rationale: str
    affected_tool_ids: list[str]
    confidence: Confidence
    evidence: list[Evidence] = Field(default_factory=list)
