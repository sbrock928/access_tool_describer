"""Minimal operator-owned V2 runtime configuration."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

ANALYZER_CONFIG_SCHEMA = "access-analyzer-config-v2"
MODEL_DIRECTORY_ENVIRONMENT = "ACCESS_ANALYZER_MODEL_DIR"


class QwenRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path | None = None
    device: str = "cpu"
    cpu_threads: int = Field(default=4, ge=1, le=128)
    cpu_interop_threads: int = Field(default=1, ge=1, le=16)

    @field_validator("device")
    @classmethod
    def validate_device(cls, value: str) -> str:
        normalized = value.casefold().strip()
        if normalized not in {"auto", "cpu", "cuda", "mps"}:
            raise ValueError("qwen.device must be auto, cpu, cuda, or mps")
        return normalized


class AnalyzerRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = ANALYZER_CONFIG_SCHEMA
    qwen: QwenRuntimeConfig = Field(default_factory=QwenRuntimeConfig)

    @field_validator("schema_version")
    @classmethod
    def validate_schema(cls, value: str) -> str:
        if value != ANALYZER_CONFIG_SCHEMA:
            raise ValueError(
                f"unsupported analyzer config schema '{value}'; use a fresh V2 workspace"
            )
        return value


class ResolvedQwenRuntime(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_dir: Path
    device: str
    cpu_threads: int
    cpu_interop_threads: int


def load_runtime_config(workspace: Path) -> AnalyzerRuntimeConfig:
    path = workspace / "analyzer.toml"
    if not path.exists():
        return AnalyzerRuntimeConfig()
    try:
        with path.open("rb") as source:
            return AnalyzerRuntimeConfig.model_validate(tomllib.load(source))
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"invalid analyzer configuration: {path}") from exc


def resolve_qwen_runtime(
    workspace: Path,
    *,
    model_dir_override: Path | None = None,
) -> ResolvedQwenRuntime:
    config = load_runtime_config(workspace)
    configured = model_dir_override or config.qwen.path
    if configured is None:
        environment_value = os.environ.get(MODEL_DIRECTORY_ENVIRONMENT)
        configured = Path(environment_value) if environment_value else None
    if configured is None:
        raise ValueError(
            "Qwen model directory is required: pass --model-dir, set [qwen].path in "
            "analyzer.toml, or set ACCESS_ANALYZER_MODEL_DIR"
        )
    model_dir = configured if configured.is_absolute() else workspace / configured
    return ResolvedQwenRuntime(
        model_dir=model_dir.resolve(),
        device=config.qwen.device,
        cpu_threads=config.qwen.cpu_threads,
        cpu_interop_threads=config.qwen.cpu_interop_threads,
    )
