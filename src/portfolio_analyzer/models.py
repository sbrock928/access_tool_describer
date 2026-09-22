"""Stable, platform-independent domain models."""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _stable_identifier(prefix: str, *values: object) -> str:
    payload = "\x1f".join("" if value is None else str(value) for value in values)
    return f"{prefix}_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:20]}"


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
    evidence_id: str = ""
    rule_id: str | None = None
    tool_inventory_id: str
    artifact_path: str
    object_type: str
    object_name: str
    location: str | None = None
    text: str
    inference: str | None = None
    confidence: Confidence = Confidence.HIGH

    @model_validator(mode="after")
    def assign_stable_identifiers(self) -> Evidence:
        if not self.evidence_id:
            self.evidence_id = _stable_identifier(
                "ev",
                self.tool_inventory_id,
                self.artifact_path,
                self.object_type,
                self.object_name,
                self.location,
                self.text,
                self.inference,
            )
        if self.rule_id is None and self.inference:
            self.rule_id = _stable_identifier("rule", self.inference.casefold())
        return self


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


class AnalysisCoverage(BaseModel):
    """Per-application pipeline coverage used to qualify absence-of-evidence claims."""

    tool_inventory_id: str
    tool_name: str
    inventory_filename: str = ""
    staging_status: str
    extraction_status: str
    analysis_status: str
    extracted_object_count: int = 0
    extraction_warning_count: int = 0
    evidence_count: int = 0
    datasource_count: int = 0
    dependency_count: int = 0
    capability_count: int = 0
    notes: list[str] = Field(default_factory=list)


class Claim(BaseModel):
    """Owner-supplied context kept distinct from observed technical evidence."""

    claim_id: str = ""
    tool_inventory_id: str
    field: str
    value: str
    source: str

    @model_validator(mode="after")
    def assign_stable_identifier(self) -> Claim:
        if not self.claim_id:
            self.claim_id = _stable_identifier(
                "claim", self.tool_inventory_id, self.field, self.value, self.source
            )
        return self


class SemanticSource(BaseModel):
    """A bounded, redacted source segment or deterministic inventory record."""

    source_id: str
    tool_inventory_id: str
    artifact_hash: str
    object_type: str
    object_name: str
    location: str | None = None
    excerpt: str
    content_sha256: str
    model_eligible: bool = True
    segment_index: int = Field(default=1, ge=1)
    segment_count: int = Field(default=1, ge=1)


class SemanticBatchSummary(BaseModel):
    """A resumable semantic reduction over source segments or earlier summaries."""

    batch_id: str
    tool_inventory_id: str
    level: int = Field(default=0, ge=0)
    source_ids: list[str] = Field(default_factory=list)
    child_summary_ids: list[str] = Field(default_factory=list)
    summary: str
    business_terms: list[str] = Field(default_factory=list)
    workflows: list[str] = Field(default_factory=list)
    data_entities: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    input_fingerprint: str


class SemanticCoverage(BaseModel):
    """Auditable inventory and model coverage for one application."""

    inventory_objects: int = Field(ge=0)
    model_eligible_objects: int = Field(ge=0)
    modeled_objects: int = Field(ge=0)
    model_eligible_segments: int = Field(ge=0)
    modeled_segments: int = Field(ge=0)
    object_type_inventory: dict[str, int] = Field(default_factory=dict)
    object_type_modeled: dict[str, int] = Field(default_factory=dict)
    complete_code_coverage: bool = False


class SemanticFinding(BaseModel):
    finding_id: str = ""
    tool_inventory_id: str
    category: str
    label: str
    description: str = ""
    confidence: Confidence = Confidence.LOW
    evidence_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    review_status: Literal["pending", "accepted", "edited", "rejected"] = "pending"

    @model_validator(mode="after")
    def assign_stable_identifier(self) -> SemanticFinding:
        if not self.finding_id:
            self.finding_id = _stable_identifier(
                "sf", self.tool_inventory_id, self.category, self.label.casefold()
            )
        return self


class ObjectSemanticSummary(BaseModel):
    source_id: str
    summary: str
    business_terms: list[str] = Field(default_factory=list)
    workflows: list[str] = Field(default_factory=list)
    data_entities: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)


class SemanticApplicationProfile(BaseModel):
    tool_inventory_id: str
    tool_name: str
    summary: str
    business_purpose: str
    primary_archetype: str
    proposed_disposition: str
    confidence: Confidence
    findings: list[SemanticFinding] = Field(default_factory=list)
    object_summaries: list[ObjectSemanticSummary] = Field(default_factory=list)
    batch_summary_ids: list[str] = Field(default_factory=list)
    semantic_coverage: SemanticCoverage | None = None
    open_questions: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    artifact_hashes: list[str] = Field(default_factory=list)
    input_fingerprint: str
    semantic_version: str
    model_repo_id: str
    model_revision: str
    model_manifest_sha256: str
    status: Literal["complete", "partial", "failed"] = "complete"
    error: str | None = None


class SimilarityEdge(BaseModel):
    source_tool_id: str
    target_tool_id: str
    overall_similarity: float = Field(ge=0.0, le=1.0)
    category_scores: dict[str, float] = Field(default_factory=dict)
    shared_features: dict[str, list[str]] = Field(default_factory=dict)
    shared_capabilities: list[str] = Field(default_factory=list)
    shared_datasources: list[str] = Field(default_factory=list)


class PortfolioCluster(BaseModel):
    cluster_id: str
    label: str
    application_ids: list[str]
    shared_capabilities: list[str] = Field(default_factory=list)
    shared_data_domains: list[str] = Field(default_factory=list)
    rationale: str = ""
    confidence: Confidence = Confidence.LOW
    evidence_ids: list[str] = Field(default_factory=list)


class ArchitectureComponent(BaseModel):
    component_id: str
    track: Literal["vendor_neutral", "microsoft"]
    name: str
    component_type: str
    description: str
    platform_service: str | None = None
    application_ids: list[str] = Field(default_factory=list)
    cluster_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    confidence: Confidence = Confidence.LOW
    review_status: Literal["pending", "accepted", "edited", "rejected"] = "pending"


class ArchitectureRelation(BaseModel):
    relation_id: str
    track: Literal["vendor_neutral", "microsoft"]
    source_component_id: str
    target_component_id: str
    relationship: str
    description: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: Confidence = Confidence.LOW


class ApplicationTargetMapping(BaseModel):
    mapping_id: str
    tool_inventory_id: str
    disposition: Literal[
        "retain/remediate",
        "wrap/integrate",
        "replatform",
        "rebuild",
        "consolidate",
        "retire candidate",
        "investigate",
    ]
    target_component_ids: list[str] = Field(default_factory=list)
    wave: int = Field(ge=0, le=4)
    rationale: str
    prerequisites: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    confidence: Confidence = Confidence.LOW
    review_status: Literal["pending", "accepted", "edited", "rejected"] = "pending"


class MigrationWave(BaseModel):
    wave: int = Field(ge=0, le=4)
    name: str
    purpose: str
    application_ids: list[str] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)


class ReviewDecision(BaseModel):
    proposal_id: str
    decision: Literal["Accept", "Edit", "Reject"]
    edited_value: str | None = None
    reviewer: str | None = None
    notes: str | None = None
    reviewed_at: datetime | None = None


class TargetArchitecture(BaseModel):
    title: str = "Proposed modular target architecture"
    summary: str = ""
    components: list[ArchitectureComponent] = Field(default_factory=list)
    relations: list[ArchitectureRelation] = Field(default_factory=list)
    mappings: list[ApplicationTargetMapping] = Field(default_factory=list)
    migration_waves: list[MigrationWave] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class SemanticRunMetadata(BaseModel):
    run_mode: Literal["production", "quick"] = "production"
    run_status: Literal["in_progress", "complete"] = "complete"
    max_objects_per_application: int | None = None
    semantic_version: str
    semantic_schema_version: str
    prompt_version: str
    static_analysis_version: str
    deterministic_similarity_version: str
    model_repo_id: str
    model_revision: str
    model_manifest_sha256: str
    local_model_identifier: str
    model_architecture: str
    model_license: str
    inference_library: str
    inference_library_version: str
    generation_parameters: dict[str, object] = Field(default_factory=dict)
    clustering_parameters: dict[str, object] = Field(default_factory=dict)
    approved_services: list[str] = Field(default_factory=list)
    context_hash: str = ""
    generated_at: datetime
    input_fingerprint: str


class SemanticPortfolioState(BaseModel):
    metadata: SemanticRunMetadata
    sources: list[SemanticSource] = Field(default_factory=list)
    observed_evidence_ids: list[str] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    batch_summaries: list[SemanticBatchSummary] = Field(default_factory=list)
    applications: list[SemanticApplicationProfile] = Field(default_factory=list)
    similarity_edges: list[SimilarityEdge] = Field(default_factory=list)
    clusters: list[PortfolioCluster] = Field(default_factory=list)
    architecture: TargetArchitecture = Field(default_factory=TargetArchitecture)
    errors: dict[str, str] = Field(default_factory=dict)
