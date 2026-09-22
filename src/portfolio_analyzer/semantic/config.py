"""Configuration for approved local semantic inference and deterministic clustering."""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

from portfolio_analyzer.semantic.model_store import APPROVED_MODEL


class LocalModelSettings(BaseModel):
    """The single approved model; normal configuration cannot select another repository."""

    repo_id: str = APPROVED_MODEL.repo_id
    revision: str = APPROVED_MODEL.revision
    local_path: Path = Path("models") / APPROVED_MODEL.local_identifier

    @model_validator(mode="after")
    def require_approved_model(self) -> LocalModelSettings:
        if self.repo_id != APPROVED_MODEL.repo_id or self.revision != APPROVED_MODEL.revision:
            raise ValueError(
                "semantic configuration must use the repository and immutable revision "
                "compiled into the approved-model allowlist"
            )
        return self


class SemanticExecutionSettings(BaseModel):
    context_tokens: int = Field(default=8192, ge=2048, le=32768)
    max_output_tokens: int = Field(default=3072, ge=512, le=8192)
    max_object_characters: int = Field(default=6000, ge=500, le=20000)
    max_profile_characters: int = Field(default=24000, ge=2000, le=100000)
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    device: str = "auto"

    @field_validator("device")
    @classmethod
    def validate_device(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized not in {"auto", "cpu", "cuda", "mps"}:
            raise ValueError("device must be auto, cpu, cuda, or mps")
        return normalized

    @model_validator(mode="after")
    def validate_token_budget(self) -> SemanticExecutionSettings:
        if self.max_output_tokens >= self.context_tokens:
            raise ValueError("max_output_tokens must be smaller than context_tokens")
        return self


class SemanticPolicySettings(BaseModel):
    retain_raw_prompts: bool = False
    redact_paths: bool = True
    include_inventory_description: bool = True


class ClusteringSettings(BaseModel):
    strong_similarity: float = Field(default=0.62, ge=0.0, le=1.0)
    corroborated_similarity: float = Field(default=0.28, ge=0.0, le=1.0)
    fixed_seed: int = 0
    business_capability_weight: float = Field(default=0.25, ge=0.0, le=1.0)
    workflow_weight: float = Field(default=0.18, ge=0.0, le=1.0)
    data_domain_weight: float = Field(default=0.17, ge=0.0, le=1.0)
    datasource_weight: float = Field(default=0.15, ge=0.0, le=1.0)
    technical_weight: float = Field(default=0.13, ge=0.0, le=1.0)
    object_composition_weight: float = Field(default=0.08, ge=0.0, le=1.0)
    archetype_weight: float = Field(default=0.04, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_clustering(self) -> ClusteringSettings:
        if self.corroborated_similarity > self.strong_similarity:
            raise ValueError("corroborated_similarity cannot exceed strong_similarity")
        total = sum(self.category_weights().values())
        if abs(total - 1.0) > 1e-9:
            raise ValueError("deterministic similarity category weights must sum to 1.0")
        return self

    def category_weights(self) -> dict[str, float]:
        return {
            "business_capabilities": self.business_capability_weight,
            "workflows": self.workflow_weight,
            "data_domains": self.data_domain_weight,
            "datasources": self.datasource_weight,
            "technical_characteristics": self.technical_weight,
            "object_composition": self.object_composition_weight,
            "application_archetype": self.archetype_weight,
        }


class MicrosoftArchitectureSettings(BaseModel):
    approved_services: list[str] = Field(
        default_factory=lambda: [
            "Microsoft Entra ID",
            "Microsoft Teams",
            "SharePoint Online",
            "Power Apps",
            "Power Automate",
            "Dataverse",
            "Power BI",
            "Azure DevOps",
        ]
    )


class SemanticSettings(BaseModel):
    model: LocalModelSettings = Field(default_factory=LocalModelSettings)
    execution: SemanticExecutionSettings = Field(default_factory=SemanticExecutionSettings)
    policy: SemanticPolicySettings = Field(default_factory=SemanticPolicySettings)
    clustering: ClusteringSettings = Field(default_factory=ClusteringSettings)
    microsoft: MicrosoftArchitectureSettings = Field(default_factory=MicrosoftArchitectureSettings)


def load_semantic_settings(path: Path) -> SemanticSettings:
    with path.open("rb") as source:
        settings = SemanticSettings.model_validate(tomllib.load(source))
    if not settings.model.local_path.is_absolute():
        settings.model.local_path = (path.parent / settings.model.local_path).resolve()
    return settings


def write_semantic_settings_template(path: Path) -> bool:
    """Write a conservative approved-model template without replacing user configuration."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'''# Semantic inference uses one approved, in-process model and never uses HTTP.
# repo_id and revision are allowlisted constants; only local_path is operationally configurable.
[model]
repo_id = "{APPROVED_MODEL.repo_id}"
revision = "{APPROVED_MODEL.revision}"
local_path = "models/{APPROVED_MODEL.local_identifier}"

[execution]
context_tokens = 8192
max_output_tokens = 3072
max_object_characters = 6000
max_profile_characters = 24000
temperature = 0.0
device = "auto"

[policy]
retain_raw_prompts = false
redact_paths = true
include_inventory_description = true

[clustering]
strong_similarity = 0.62
corroborated_similarity = 0.28
fixed_seed = 0
business_capability_weight = 0.25
workflow_weight = 0.18
data_domain_weight = 0.17
datasource_weight = 0.15
technical_weight = 0.13
object_composition_weight = 0.08
archetype_weight = 0.04

[microsoft]
approved_services = [
  "Microsoft Entra ID",
  "Microsoft Teams",
  "SharePoint Online",
  "Power Apps",
  "Power Automate",
  "Dataverse",
  "Power BI",
  "Azure DevOps",
]
''',
        encoding="utf-8",
        newline="\n",
    )
    return True
