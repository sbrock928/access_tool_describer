"""Approved-model acquisition and fail-closed local integrity verification."""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MANIFEST_SCHEMA_VERSION = "local-model-manifest-v1"
_UNSAFE_SUFFIXES = {
    ".bin",
    ".ckpt",
    ".joblib",
    ".pickle",
    ".pkl",
    ".pt",
    ".pth",
    ".py",
    ".pyc",
    ".sh",
}


class ApprovedArtifact(BaseModel):
    """A code-reviewed artifact identity from the immutable upstream revision."""

    model_config = ConfigDict(frozen=True)

    path: str
    size_bytes: int = Field(ge=0)
    sha256: str

    @field_validator("path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts or value != candidate.as_posix():
            raise ValueError("approved artifact paths must be normalized relative paths")
        return value

    @field_validator("sha256")
    @classmethod
    def valid_digest(cls, value: str) -> str:
        normalized = value.casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("approved artifact SHA-256 must be a 64-character hexadecimal digest")
        return normalized


class ApprovedModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    repo_id: str
    revision: str
    local_identifier: str
    license: str
    architecture: str
    model_type: str
    artifacts: tuple[ApprovedArtifact, ...]

    @property
    def expected_files(self) -> tuple[str, ...]:
        return tuple(artifact.path for artifact in self.artifacts)

    @property
    def artifacts_by_path(self) -> dict[str, ApprovedArtifact]:
        return {artifact.path: artifact for artifact in self.artifacts}

    @model_validator(mode="after")
    def validate_artifacts(self) -> ApprovedModel:
        if len(set(self.expected_files)) != len(self.artifacts):
            raise ValueError("approved model contains duplicate artifact paths")
        if not any(artifact.path.endswith(".safetensors") for artifact in self.artifacts):
            raise ValueError("approved model must contain safetensors weights")
        return self


# This is the only production model. It is published by IBM, Apache-2.0 licensed,
# Transformers-native, approximately 5 GB in BF16, and has safetensors-only weights.
# Sizes and SHA-256 values were independently reviewed against the pinned Hugging Face
# revision on 2026-09-22. The two LFS digests are the publisher-hosted LFS object IDs.
APPROVED_MODEL = ApprovedModel(
    repo_id="ibm-granite/granite-3.3-2b-instruct",
    revision="707f574c62054322f6b5b04b6d075f0a8f05e0f0",
    local_identifier="granite-3.3-2b-instruct",
    license="Apache-2.0",
    architecture="GraniteForCausalLM",
    model_type="granite",
    artifacts=(
        ApprovedArtifact(
            path="README.md",
            size_bytes=37026,
            sha256="dd7a9d401b703f4d6136b70b575a8070b88d173f61804dad48fccc7949afe610",
        ),
        ApprovedArtifact(
            path="added_tokens.json",
            size_bytes=207,
            sha256="bb33d55934aa82d29cc62f3d19cdbc60f315763f6ccee21bdfd8b3bde2f33d3b",
        ),
        ApprovedArtifact(
            path="config.json",
            size_bytes=787,
            sha256="9202d328d8368958aab7dad89a8e6aa35c250b3a2eedd2d8850ee0bcceca65f6",
        ),
        ApprovedArtifact(
            path="generation_config.json",
            size_bytes=132,
            sha256="9c95e80167f08fbeb1feba239e0749507c25d18ffab43ffa10887821eed21c38",
        ),
        ApprovedArtifact(
            path="merges.txt",
            size_bytes=441810,
            sha256="303127a244b0078878156c17229f36d11b7a3a3f8e47b7cfdbb304ff46be5030",
        ),
        ApprovedArtifact(
            path="model-00001-of-00002.safetensors",
            size_bytes=4999999840,
            sha256="12880d33c0ad4726af5cf8c07406905f9b496253c58ee46f52be8bde8ccf2254",
        ),
        ApprovedArtifact(
            path="model-00002-of-00002.safetensors",
            size_bytes=67121712,
            sha256="a8757c5bf7627933e7fddbd9bab0533491b4dc0962820e0617f356ca1a379ffa",
        ),
        ApprovedArtifact(
            path="model.safetensors.index.json",
            size_bytes=29835,
            sha256="32ea3f438335d51c8e630a83898ceb23bdffd34291a0eade07474211928fa243",
        ),
        ApprovedArtifact(
            path="special_tokens_map.json",
            size_bytes=801,
            sha256="21ce694081bb9ae1bd4bc64549e72e0799ebb74705e6b650e3585d85b71ebdc1",
        ),
        ApprovedArtifact(
            path="tokenizer.json",
            size_bytes=3476578,
            sha256="91168e938f05796aa6dcca7e485e4b30ab52785320c7a6391ecef86e6c84681e",
        ),
        ApprovedArtifact(
            path="tokenizer_config.json",
            size_bytes=9930,
            sha256="f65a6a5a911424c85f157c40cfbdf06e025814c755480ba2e998d7fba1178664",
        ),
        ApprovedArtifact(
            path="vocab.json",
            size_bytes=776995,
            sha256="80ab859339a2525fdfbda14bc39df02dffb824aefdaf86426217bbb146d17e01",
        ),
    ),
)


class ModelFileRecord(BaseModel):
    path: str
    size_bytes: int = Field(ge=0)
    sha256: str

    @field_validator("path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts or value != candidate.as_posix():
            raise ValueError("manifest file paths must be normalized relative paths")
        return value

    @field_validator("sha256")
    @classmethod
    def valid_digest(cls, value: str) -> str:
        normalized = value.casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("file SHA-256 must be a 64-character hexadecimal digest")
        return normalized


class ModelManifest(BaseModel):
    schema_version: str = MANIFEST_SCHEMA_VERSION
    repo_id: str
    revision: str
    license: str
    architecture: str
    model_type: str
    acquired_at: datetime
    files: list[ModelFileRecord]
    manifest_sha256: str

    @field_validator("manifest_sha256")
    @classmethod
    def valid_manifest_digest(cls, value: str) -> str:
        normalized = value.casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("manifest SHA-256 must be a 64-character hexadecimal digest")
        return normalized

    @model_validator(mode="after")
    def validate_identity(self) -> ModelManifest:
        if self.schema_version != MANIFEST_SCHEMA_VERSION:
            raise ValueError("unsupported model manifest schema")
        if self.repo_id != APPROVED_MODEL.repo_id:
            raise ValueError("model manifest repository is not approved")
        if self.revision != APPROVED_MODEL.revision:
            raise ValueError(
                "model manifest revision does not match the approved immutable revision"
            )
        if self.license != APPROVED_MODEL.license:
            raise ValueError("model manifest license does not match the approved model")
        if self.architecture != APPROVED_MODEL.architecture:
            raise ValueError("model manifest architecture does not match the approved model")
        if self.model_type != APPROVED_MODEL.model_type:
            raise ValueError("model manifest type does not match the approved model")
        if len({item.path for item in self.files}) != len(self.files):
            raise ValueError("model manifest contains duplicate file records")
        return self


class VerifiedModel(BaseModel):
    directory: Path
    manifest: ModelManifest


def acquire_approved_model(destination: Path) -> ModelManifest:
    """Download only allowlisted files at the approved SHA, then verify and publish atomically."""
    if destination.exists():
        raise FileExistsError(
            f"Approved model destination already exists: {destination}. "
            "Remove or quarantine it explicitly before reacquisition."
        )
    try:
        snapshot_download = importlib.import_module("huggingface_hub").snapshot_download
    except (ImportError, AttributeError) as exc:  # pragma: no cover - dependency guidance
        raise RuntimeError(
            "Model acquisition requires the semantic dependencies. Install with "
            "'python -m pip install -e \".[semantic]\"'."
        ) from exc

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.download-", dir=destination.parent)
    )
    try:
        snapshot_download(
            repo_id=APPROVED_MODEL.repo_id,
            revision=APPROVED_MODEL.revision,
            local_dir=staging,
            allow_patterns=list(APPROVED_MODEL.expected_files),
            token=False,
            force_download=True,
        )
        cache_metadata = staging / ".cache"
        if cache_metadata.exists():
            shutil.rmtree(cache_metadata)
        _validate_file_inventory(staging, include_manifest=False)
        _validate_approved_artifact_digests(staging)
        _validate_transformers_configuration(staging)
        manifest = _build_manifest(staging)
        _write_manifest(staging / "model_manifest.json", manifest)
        verify_model_directory(staging)
        staging.replace(destination)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def verify_model_directory(directory: Path) -> VerifiedModel:
    """Verify identity, inventory, configuration, sizes, and every file digest."""
    if directory.is_symlink():
        raise ValueError(f"Approved model directory cannot be a symbolic link: {directory}")
    manifest_path = directory / "model_manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"Approved model manifest is missing: {manifest_path}")
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = ModelManifest.model_validate(raw)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Approved model manifest is invalid: {manifest_path}") from exc
    expected_manifest_hash = _manifest_digest(manifest)
    if manifest.manifest_sha256 != expected_manifest_hash:
        raise ValueError("Approved model manifest hash mismatch")
    _validate_file_inventory(directory, include_manifest=True)
    expected_names = set(APPROVED_MODEL.expected_files)
    manifest_names = {item.path for item in manifest.files}
    if manifest_names != expected_names:
        raise ValueError("Approved model manifest file inventory does not match the allowlist")
    by_name = {item.path: item for item in manifest.files}
    approved_by_name = APPROVED_MODEL.artifacts_by_path
    for name in sorted(expected_names):
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Approved model file is missing or is a symbolic link: {name}")
        record = by_name[name]
        approved = approved_by_name[name]
        if record.size_bytes != approved.size_bytes or record.sha256 != approved.sha256:
            raise ValueError(f"Model manifest does not match the approved artifact: {name}")
        if path.stat().st_size != approved.size_bytes:
            raise ValueError(f"Approved model file size mismatch: {name}")
        if _sha256(path) != approved.sha256:
            raise ValueError(f"Approved model checksum mismatch: {name}")
    _validate_transformers_configuration(directory)
    return VerifiedModel(directory=directory.resolve(), manifest=manifest)


def _build_manifest(directory: Path) -> ModelManifest:
    records = [
        ModelFileRecord(
            path=name,
            size_bytes=(directory / name).stat().st_size,
            sha256=_sha256(directory / name),
        )
        for name in sorted(APPROVED_MODEL.expected_files)
    ]
    manifest = ModelManifest(
        repo_id=APPROVED_MODEL.repo_id,
        revision=APPROVED_MODEL.revision,
        license=APPROVED_MODEL.license,
        architecture=APPROVED_MODEL.architecture,
        model_type=APPROVED_MODEL.model_type,
        acquired_at=datetime.now(UTC),
        files=records,
        manifest_sha256="0" * 64,
    )
    manifest.manifest_sha256 = _manifest_digest(manifest)
    return manifest


def _manifest_digest(manifest: ModelManifest) -> str:
    payload = manifest.model_dump(mode="json", exclude={"manifest_sha256"})
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_manifest(path: Path, manifest: ModelManifest) -> None:
    path.write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _validate_file_inventory(directory: Path, *, include_manifest: bool) -> None:
    allowed = set(APPROVED_MODEL.expected_files)
    if include_manifest:
        allowed.add("model_manifest.json")
    actual: set[str] = set()
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Symbolic links are forbidden in approved models: {path.name}")
        if path.is_dir():
            continue
        relative = path.relative_to(directory).as_posix()
        actual.add(relative)
        if path.suffix.casefold() in _UNSAFE_SUFFIXES:
            raise ValueError(f"Unsafe model artifact extension is forbidden: {relative}")
    missing = allowed - actual
    unexpected = actual - allowed
    if missing:
        raise ValueError(f"Approved model files are missing: {', '.join(sorted(missing))}")
    if unexpected:
        raise ValueError(f"Unexpected model files are forbidden: {', '.join(sorted(unexpected))}")
    weight_files = [name for name in actual if Path(name).suffix == ".safetensors"]
    if not weight_files:
        raise ValueError("Approved model contains no safetensors weights")


def _validate_approved_artifact_digests(directory: Path) -> None:
    """Reject upstream or transport bytes that differ from the reviewed artifact set."""
    for artifact in APPROVED_MODEL.artifacts:
        path = directory / artifact.path
        if path.stat().st_size != artifact.size_bytes:
            raise ValueError(f"Downloaded model file has an unapproved size: {artifact.path}")
        if _sha256(path) != artifact.sha256:
            raise ValueError(f"Downloaded model file has an unapproved SHA-256: {artifact.path}")


def _validate_transformers_configuration(directory: Path) -> None:
    config_path = directory / "config.json"
    try:
        config: dict[str, Any] = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Approved model config.json is missing or invalid") from exc
    if config.get("model_type") != APPROVED_MODEL.model_type:
        raise ValueError("Approved model config has the wrong model_type")
    if config.get("architectures") != [APPROVED_MODEL.architecture]:
        raise ValueError("Approved model config has an unexpected architecture")
    if config.get("auto_map") or config.get("custom_pipelines") or config.get("trust_remote_code"):
        raise ValueError("Approved model config requests custom or remote executable code")
    tokenizer_path = directory / "tokenizer_config.json"
    try:
        tokenizer_config: dict[str, Any] = json.loads(
            tokenizer_path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Approved model tokenizer_config.json is missing or invalid") from exc
    if tokenizer_config.get("auto_map") or tokenizer_config.get("trust_remote_code"):
        raise ValueError("Approved tokenizer config requests custom or remote executable code")
    if tokenizer_config.get("tokenizer_class") not in {
        None,
        "GPT2Tokenizer",
        "GPT2TokenizerFast",
    }:
        raise ValueError("Approved tokenizer config has an unexpected tokenizer class")
    index_path = directory / "model.safetensors.index.json"
    try:
        index: dict[str, Any] = json.loads(index_path.read_text(encoding="utf-8"))
        weight_names = set(index["weight_map"].values())
    except (OSError, json.JSONDecodeError, KeyError, AttributeError) as exc:
        raise ValueError("Approved model safetensors index is missing or invalid") from exc
    expected_weights = {
        name for name in APPROVED_MODEL.expected_files if name.endswith(".safetensors")
    }
    if weight_names != expected_weights:
        raise ValueError("Approved model weight index references unexpected files")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
