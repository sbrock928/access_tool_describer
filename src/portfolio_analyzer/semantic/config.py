"""Configuration for local semantic inference and portfolio clustering."""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field, field_validator


class ModelEndpointSettings(BaseModel):
    base_url: str
    model: str
    model_sha256: str
    timeout_seconds: int = Field(default=600, ge=10, le=3600)
    max_retries: int = Field(default=2, ge=0, le=5)

    @field_validator("model_sha256")
    @classmethod
    def validate_model_sha256(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized.startswith("replace_"):
            return normalized
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("model_sha256 must be a 64-character hexadecimal digest")
        return normalized


class SemanticExecutionSettings(BaseModel):
    worker_count: int = Field(default=1, ge=1, le=4)
    context_tokens: int = Field(default=8192, ge=2048)
    max_output_tokens: int = Field(default=3072, ge=512)
    max_object_characters: int = Field(default=6000, ge=500, le=20000)
    max_profile_characters: int = Field(default=24000, ge=2000, le=100000)
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)


class SemanticPolicySettings(BaseModel):
    retain_raw_prompts: bool = False
    redact_paths: bool = True
    include_inventory_description: bool = True


class ClusteringSettings(BaseModel):
    strong_similarity: float = Field(default=0.88, ge=0.0, le=1.0)
    corroborated_similarity: float = Field(default=0.78, ge=0.0, le=1.0)
    fixed_seed: int = 0

    @field_validator("corroborated_similarity")
    @classmethod
    def validate_threshold_order(cls, value: float, info: object) -> float:
        data = getattr(info, "data", {})
        strong = data.get("strong_similarity") if isinstance(data, dict) else None
        if isinstance(strong, int | float) and value > float(strong):
            raise ValueError("corroborated_similarity cannot exceed strong_similarity")
        return value


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
    chat: ModelEndpointSettings
    embeddings: ModelEndpointSettings
    execution: SemanticExecutionSettings = Field(default_factory=SemanticExecutionSettings)
    policy: SemanticPolicySettings = Field(default_factory=SemanticPolicySettings)
    clustering: ClusteringSettings = Field(default_factory=ClusteringSettings)
    microsoft: MicrosoftArchitectureSettings = Field(default_factory=MicrosoftArchitectureSettings)


def load_semantic_settings(path: Path) -> SemanticSettings:
    with path.open("rb") as source:
        return SemanticSettings.model_validate(tomllib.load(source))


def write_semantic_settings_template(path: Path) -> bool:
    """Write a conservative local-only template without replacing user configuration."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """# Local semantic inference. Model weights are managed outside this repository.
[chat]
base_url = "http://127.0.0.1:8080/v1"
model = "REPLACE_WITH_APPROVED_CHAT_MODEL"
model_sha256 = "REPLACE_WITH_64_CHARACTER_SHA256"
timeout_seconds = 600
max_retries = 2

[embeddings]
base_url = "http://127.0.0.1:8081/v1"
model = "REPLACE_WITH_APPROVED_EMBEDDING_MODEL"
model_sha256 = "REPLACE_WITH_64_CHARACTER_SHA256"
timeout_seconds = 600
max_retries = 2

[execution]
worker_count = 1
context_tokens = 8192
max_output_tokens = 3072
max_object_characters = 6000
max_profile_characters = 24000
temperature = 0.0

[policy]
retain_raw_prompts = false
redact_paths = true
include_inventory_description = true

[clustering]
strong_similarity = 0.88
corroborated_similarity = 0.78
fixed_seed = 0

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
""",
        encoding="utf-8",
        newline="\n",
    )
    return True
