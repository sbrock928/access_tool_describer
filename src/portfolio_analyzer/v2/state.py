"""Strict, content-addressed V2 workspace state.

Large per-application values are immutable JSON objects addressed by their SHA-256.  A run becomes
visible only when its small manifest is atomically published after every referenced object has been
verified.  Generated state is intentionally not migrated from earlier workspace schemas.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Self, TypeVar

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from portfolio_analyzer.performance import PerformanceRecorder, measured
from portfolio_analyzer.v2.identity import canonical_json_bytes, stable_id
from portfolio_analyzer.v2.models import NonEmptyString, Sha256, StrictModel

WORKSPACE_SCHEMA_VERSION = "access-portfolio-workspace-v2"
RUN_MANIFEST_SCHEMA_VERSION = "run-manifest-v2"
CURRENT_RUN_POINTER_SCHEMA_VERSION = "current-run-pointer-v2"
STATE_DIRECTORY_NAME = ".portfolio_analyzer_v2"
WORKSPACE_METADATA_NAME = "workspace.json"

_SAFE_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_ALLOWED_OPERATOR_INPUT_FILES = frozenset(
    {
        "analyzer.toml",
        "source_inventory/access_libraries.json",
        "source_inventory/owner_context.csv",
    }
)
_ALLOWED_OPERATOR_INPUT_DIRECTORIES = frozenset({"source_inventory"})
ModelT = TypeVar("ModelT", bound=BaseModel)


class WorkspaceSchemaError(ValueError):
    """Raised when a workspace is absent, legacy, or structurally invalid."""


class StateIntegrityError(ValueError):
    """Raised when content does not match its manifest identity."""


class StatePublicationError(RuntimeError):
    """Raised when an immutable run cannot be safely published."""


class WorkspaceLockedError(RuntimeError):
    """Raised when another writer owns the workspace lock."""


class WorkspaceLockRequiredError(RuntimeError):
    """Raised when manifest publication is attempted without the writer lock."""


class RunPhase(StrEnum):
    STAGE = "stage"
    EXTRACT = "extract"
    ANALYZE = "analyze"
    REPORT = "report"


class RunStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class WorkspaceMetadata(StrictModel):
    schema_version: Literal["access-portfolio-workspace-v2"] = (
        "access-portfolio-workspace-v2"
    )
    workspace_id: NonEmptyString
    created_at: datetime
    created_by: NonEmptyString = "access-portfolio-analyzer-v2"

    @field_validator("created_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("workspace timestamp must include a timezone")
        return value


class ContentReference(StrictModel):
    sha256: Sha256
    relative_path: NonEmptyString
    size_bytes: Annotated[int, Field(ge=0)]
    schema_name: NonEmptyString

    @field_validator("sha256")
    @classmethod
    def normalize_digest(cls, value: str) -> str:
        return value.casefold()

    @model_validator(mode="after")
    def path_matches_content_address(self) -> Self:
        expected = _content_relative_path(self.sha256)
        if self.relative_path != expected:
            raise ValueError("content reference path does not match its SHA-256")
        return self


class ApplicationRunRecord(StrictModel):
    application_id: NonEmptyString
    artifact_ids: tuple[NonEmptyString, ...] = ()
    status: RunStatus
    extraction_snapshots: tuple[ContentReference, ...] = ()
    evidence_bundle: ContentReference | None = None
    interpretation: ContentReference | None = None
    warnings: tuple[NonEmptyString, ...] = ()
    errors: tuple[NonEmptyString, ...] = ()

    @model_validator(mode="after")
    def normalize_and_validate(self) -> Self:
        object.__setattr__(self, "artifact_ids", _sorted_unique(self.artifact_ids))
        object.__setattr__(
            self,
            "extraction_snapshots",
            _sorted_references(self.extraction_snapshots),
        )
        object.__setattr__(self, "warnings", _sorted_unique(self.warnings))
        object.__setattr__(self, "errors", _sorted_unique(self.errors))
        if self.status == RunStatus.COMPLETE and self.errors:
            raise ValueError("complete application record cannot contain errors")
        if self.status == RunStatus.FAILED and not self.errors:
            raise ValueError("failed application record must explain its errors")
        return self


class RunManifest(StrictModel):
    schema_version: Literal["run-manifest-v2"] = "run-manifest-v2"
    workspace_schema_version: Literal["access-portfolio-workspace-v2"] = (
        "access-portfolio-workspace-v2"
    )
    run_id: NonEmptyString
    phase: RunPhase
    status: RunStatus
    started_at: datetime
    completed_at: datetime
    parent_run_id: str | None = None
    input_references: tuple[ContentReference, ...] = ()
    applications: tuple[ApplicationRunRecord, ...] = ()
    portfolio_analysis: ContentReference | None = None
    report_model: ContentReference | None = None
    report_publication: ContentReference | None = None
    warnings: tuple[NonEmptyString, ...] = ()
    errors: tuple[NonEmptyString, ...] = ()

    @field_validator("run_id")
    @classmethod
    def safe_run_id(cls, value: str) -> str:
        if not _SAFE_RUN_ID.fullmatch(value):
            raise ValueError("run_id is not safe for a workspace path")
        return value

    @field_validator("started_at", "completed_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("run timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_run(self) -> Self:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at cannot precede started_at")
        application_ids = [item.application_id for item in self.applications]
        if len(set(application_ids)) != len(application_ids):
            raise ValueError("run application IDs must be unique")
        if self.status == RunStatus.COMPLETE and (
            self.errors
            or any(item.status != RunStatus.COMPLETE for item in self.applications)
        ):
            raise ValueError("complete run cannot contain errors or incomplete applications")
        if self.status == RunStatus.FAILED and not self.errors:
            raise ValueError("failed run must explain its errors")
        for application in self.applications:
            if self.phase == RunPhase.STAGE:
                if (
                    application.extraction_snapshots
                    or application.evidence_bundle is not None
                    or application.interpretation is not None
                ):
                    raise ValueError("stage application records may contain artifact IDs only")
                if application.status == RunStatus.COMPLETE and not application.artifact_ids:
                    raise ValueError("complete stage record must contain an artifact ID")
            elif self.phase == RunPhase.EXTRACT:
                if (
                    application.evidence_bundle is not None
                    or application.interpretation is not None
                ):
                    raise ValueError("extract application records cannot contain analysis outputs")
                if (
                    application.status == RunStatus.COMPLETE
                    and not application.extraction_snapshots
                ):
                    raise ValueError("complete extract record must contain an extraction snapshot")
            elif self.phase == RunPhase.ANALYZE and application.status == RunStatus.COMPLETE:
                if (
                    not application.extraction_snapshots
                    or application.evidence_bundle is None
                    or application.interpretation is None
                ):
                    raise ValueError(
                        "complete analyze record requires extraction lineage, evidence, "
                        "and interpretation"
                    )
        if self.phase in {RunPhase.STAGE, RunPhase.EXTRACT} and (
            self.portfolio_analysis is not None
            or self.report_model is not None
            or self.report_publication is not None
        ):
            raise ValueError("stage and extract runs cannot contain analysis/report outputs")
        if self.phase == RunPhase.ANALYZE and (
            self.report_model is not None or self.report_publication is not None
        ):
            raise ValueError("analyze run cannot contain report outputs")
        if (
            self.phase == RunPhase.REPORT
            and self.status != RunStatus.FAILED
            and (self.report_model is None or self.report_publication is None)
        ):
            raise ValueError(
                "non-failed report run must reference a report model and publication manifest"
            )
        object.__setattr__(
            self,
            "input_references",
            tuple(sorted(set(self.input_references), key=lambda item: item.sha256)),
        )
        object.__setattr__(
            self,
            "applications",
            tuple(sorted(self.applications, key=lambda item: item.application_id)),
        )
        object.__setattr__(self, "warnings", _sorted_unique(self.warnings))
        object.__setattr__(self, "errors", _sorted_unique(self.errors))
        return self


class PublishedRun(StrictModel):
    run_id: NonEmptyString
    phase: RunPhase
    manifest_sha256: Sha256
    manifest_relative_path: NonEmptyString

    @field_validator("run_id")
    @classmethod
    def safe_run_id(cls, value: str) -> str:
        if not _SAFE_RUN_ID.fullmatch(value):
            raise ValueError("run_id is not safe for a workspace path")
        return value

    @model_validator(mode="after")
    def path_matches_run(self) -> Self:
        if self.manifest_relative_path != _manifest_relative_path(self.phase, self.run_id):
            raise ValueError("published manifest path does not match its run identity")
        return self


class CurrentRunPointer(StrictModel):
    """Small atomic selector for the coherent current generation of one phase."""

    schema_version: Literal["current-run-pointer-v2"] = "current-run-pointer-v2"
    workspace_id: NonEmptyString
    run_id: NonEmptyString
    phase: RunPhase
    manifest_sha256: Sha256
    manifest_relative_path: NonEmptyString

    @model_validator(mode="after")
    def path_matches_run(self) -> Self:
        if not _SAFE_RUN_ID.fullmatch(self.run_id):
            raise ValueError("run_id is not safe for a workspace path")
        if self.manifest_relative_path != _manifest_relative_path(self.phase, self.run_id):
            raise ValueError("current manifest path does not match its run identity")
        return self


def initialize_workspace(
    root: Path,
    *,
    created_at: datetime | None = None,
    workspace_id: str | None = None,
) -> WorkspaceMetadata:
    """Initialize an empty V2 workspace, refusing to adopt legacy generated state."""

    resolved = root.resolve()
    if resolved.exists():
        if not resolved.is_dir():
            raise WorkspaceSchemaError(f"Workspace path is not a directory: {resolved}")
        marker = _metadata_path(resolved)
        if marker.exists():
            return preflight_workspace(resolved)
    resolved.mkdir(parents=True, exist_ok=True)
    state_root = resolved / STATE_DIRECTORY_NAME
    created_state_root = not state_root.exists()
    state_root.mkdir(parents=True, exist_ok=True)
    try:
        with WorkspaceLock(resolved):
            marker = _metadata_path(resolved)
            if marker.exists():
                return preflight_workspace(resolved)
            _validate_uninitialized_workspace_contents(resolved)
            timestamp = created_at or datetime.now(UTC)
            metadata = WorkspaceMetadata(
                workspace_id=workspace_id or stable_id("workspace", timestamp.isoformat()),
                created_at=timestamp,
            )
            _atomic_write_bytes(marker, canonical_json_bytes(metadata))
            return metadata
    except Exception:
        if created_state_root:
            with suppress(OSError):
                state_root.rmdir()
        raise


def preflight_workspace(root: Path) -> WorkspaceMetadata:
    """Validate the exact V2 marker without creating or migrating anything."""

    resolved = root.resolve()
    marker = _metadata_path(resolved)
    if not marker.is_file():
        raise WorkspaceSchemaError(
            "Workspace is not initialized with the V2 schema; use a fresh workspace"
        )
    try:
        raw = marker.read_bytes()
        metadata = WorkspaceMetadata.model_validate_json(raw)
    except (OSError, ValidationError, ValueError, json.JSONDecodeError) as exc:
        raise WorkspaceSchemaError(
            "Workspace schema is invalid or unsupported; generated state is not migrated"
        ) from exc
    if raw != canonical_json_bytes(metadata):
        raise WorkspaceSchemaError("Workspace metadata is not canonical V2 JSON")
    return metadata


class WorkspaceLock:
    """Cross-process fail-fast exclusive writer lock based on atomic file creation."""

    def __init__(self, root: Path) -> None:
        self._path = root.resolve() / STATE_DIRECTORY_NAME / "writer.lock"
        self._token = secrets.token_hex(16)
        self._acquired = False

    @property
    def acquired(self) -> bool:
        return self._acquired

    def acquire(self) -> None:
        if self._acquired:
            raise WorkspaceLockedError("workspace lock is not reentrant")
        payload = canonical_json_bytes(
            {
                "owner_nonce": self._token,
                "pid": os.getpid(),
                "acquired_at": datetime.now(UTC),
            }
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            owner = "unknown writer"
            try:
                existing = json.loads(self._path.read_text(encoding="utf-8"))
                owner = f"PID {existing.get('pid', 'unknown')}"
            except (OSError, ValueError, TypeError):
                pass
            raise WorkspaceLockedError(f"workspace is locked by {owner}") from exc
        try:
            with os.fdopen(descriptor, "wb") as destination:
                destination.write(payload)
                destination.flush()
                os.fsync(destination.fileno())
            _fsync_directory(self._path.parent)
        except Exception:
            self._path.unlink(missing_ok=True)
            raise
        self._acquired = True

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            existing = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise WorkspaceLockedError("workspace lock changed while held") from exc
        if existing.get("owner_nonce") != self._token:
            raise WorkspaceLockedError("workspace lock ownership changed while held")
        self._path.unlink()
        _fsync_directory(self._path.parent)
        self._acquired = False

    def __enter__(self) -> WorkspaceLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()


class V2StateStore:
    """Content-addressed JSON objects plus manifest-last run publication."""

    def __init__(self, root: Path) -> None:
        self.performance: PerformanceRecorder | None = None
        self.root = root.resolve()
        self.metadata = preflight_workspace(self.root)
        self._active_lock: WorkspaceLock | None = None

    @property
    def state_root(self) -> Path:
        return self.root / STATE_DIRECTORY_NAME

    @contextmanager
    def exclusive_lock(self) -> Iterator[WorkspaceLock]:
        if self._active_lock is not None:
            raise WorkspaceLockedError("workspace lock is not reentrant")
        lock = WorkspaceLock(self.root)
        lock.acquire()
        self._active_lock = lock
        try:
            yield lock
        finally:
            self._active_lock = None
            lock.release()

    @measured("state_io")
    def put(self, value: BaseModel, *, schema_name: str | None = None) -> ContentReference:
        """Write an immutable canonical JSON object and return its verified reference."""

        actual_schema_name = _schema_name(value)
        if schema_name is not None and schema_name != actual_schema_name:
            raise StateIntegrityError(
                "requested state schema label does not match the encoded model"
            )
        payload = canonical_json_bytes(value)
        digest = hashlib.sha256(payload).hexdigest()
        relative = _content_relative_path(digest)
        destination = self.state_root / relative
        if destination.exists():
            existing = destination.read_bytes()
            if existing != payload:
                raise StateIntegrityError(
                    f"content-addressed object is corrupt: {relative}"
                )
        else:
            _atomic_write_bytes(destination, payload)
        reference = ContentReference(
            sha256=digest,
            relative_path=relative,
            size_bytes=len(payload),
            schema_name=actual_schema_name,
        )
        self.verify(reference)
        return reference

    @measured("state_io")
    def verify(self, reference: ContentReference) -> Path:
        """Verify containment, byte length, and SHA-256 for a stored object."""

        path = _contained_path(self.state_root, reference.relative_path)
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise StateIntegrityError(
                f"referenced state object is unavailable: {reference.relative_path}"
            ) from exc
        if len(payload) != reference.size_bytes:
            raise StateIntegrityError(
                f"state object size mismatch: {reference.relative_path}"
            )
        if hashlib.sha256(payload).hexdigest() != reference.sha256:
            raise StateIntegrityError(
                f"state object SHA-256 mismatch: {reference.relative_path}"
            )
        _verify_embedded_schema_label(reference, payload)
        return path

    @measured("state_io")
    def load(self, reference: ContentReference, model_type: type[ModelT]) -> ModelT:
        """Load a referenced object only after integrity and canonical-schema checks."""

        path = self.verify(reference)
        try:
            value = model_type.model_validate_json(path.read_bytes())
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            raise StateIntegrityError(
                f"state object does not match {reference.schema_name}"
            ) from exc
        if reference.schema_name != _schema_name(value):
            raise StateIntegrityError(
                "state reference schema label does not match the decoded model type"
            )
        if canonical_json_bytes(value) != path.read_bytes():
            raise StateIntegrityError("state object is not canonical sanitized JSON")
        return value

    @measured("state_io")
    def publish_run(self, manifest: RunManifest) -> PublishedRun:
        """Publish an immutable manifest, then atomically select it for its phase."""

        if self._active_lock is None or not self._active_lock.acquired:
            raise WorkspaceLockRequiredError(
                "run publication requires the exclusive workspace lock"
            )
        for reference in _manifest_references(manifest):
            self.verify(reference)
        relative = _manifest_relative_path(manifest.phase, manifest.run_id)
        destination = _contained_path(self.state_root, relative)
        payload = canonical_json_bytes(manifest)
        if destination.exists():
            if destination.read_bytes() != payload:
                raise StatePublicationError(
                    f"run ID is already published with different content: {manifest.run_id}"
                )
        else:
            _atomic_write_bytes(destination, payload)
        published = PublishedRun(
            run_id=manifest.run_id,
            phase=manifest.phase,
            manifest_sha256=hashlib.sha256(payload).hexdigest(),
            manifest_relative_path=relative,
        )
        self.load_run(manifest.phase, manifest.run_id)
        self.publish_current(published)
        return published

    @measured("state_io")
    def publish_current(self, published: PublishedRun) -> CurrentRunPointer:
        """Atomically select a verified, immutable run as current for its phase."""

        if self._active_lock is None or not self._active_lock.acquired:
            raise WorkspaceLockRequiredError(
                "current-run publication requires the exclusive workspace lock"
            )
        manifest = self.load_run(published.phase, published.run_id)
        manifest_path = _contained_path(self.state_root, published.manifest_relative_path)
        payload = manifest_path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != published.manifest_sha256:
            raise StateIntegrityError("published manifest SHA-256 does not match its bytes")
        if manifest.phase != published.phase or manifest.run_id != published.run_id:
            raise StateIntegrityError("published run identity does not match its manifest")
        pointer = CurrentRunPointer(
            workspace_id=self.metadata.workspace_id,
            run_id=published.run_id,
            phase=published.phase,
            manifest_sha256=published.manifest_sha256,
            manifest_relative_path=published.manifest_relative_path,
        )
        destination = _contained_path(
            self.state_root, _current_relative_path(published.phase)
        )
        _atomic_write_bytes(destination, canonical_json_bytes(pointer))
        return pointer

    @measured("state_io")
    def load_run(self, phase: RunPhase, run_id: str) -> RunManifest:
        """Load only a completely published run manifest."""

        if not _SAFE_RUN_ID.fullmatch(run_id):
            raise StateIntegrityError("unsafe run ID")
        relative = _manifest_relative_path(phase, run_id)
        path = _contained_path(self.state_root, relative)
        try:
            payload = path.read_bytes()
            manifest = RunManifest.model_validate_json(payload)
        except (OSError, ValidationError, ValueError, json.JSONDecodeError) as exc:
            raise StateIntegrityError(f"run is not completely published: {run_id}") from exc
        if manifest.phase != phase or manifest.run_id != run_id:
            raise StateIntegrityError("run manifest identity does not match its path")
        if canonical_json_bytes(manifest) != payload:
            raise StateIntegrityError("run manifest is not canonical V2 JSON")
        for reference in _manifest_references(manifest):
            self.verify(reference)
        return manifest

    @measured("state_io")
    def load_current_run(self, phase: RunPhase) -> RunManifest:
        """Resolve and verify the atomic current pointer for a phase."""

        path = _contained_path(self.state_root, _current_relative_path(phase))
        try:
            payload = path.read_bytes()
            pointer = CurrentRunPointer.model_validate_json(payload)
        except (OSError, ValidationError, ValueError, json.JSONDecodeError) as exc:
            raise StateIntegrityError(
                f"no valid current run is published for phase: {phase.value}"
            ) from exc
        if canonical_json_bytes(pointer) != payload:
            raise StateIntegrityError("current-run pointer is not canonical V2 JSON")
        if pointer.workspace_id != self.metadata.workspace_id or pointer.phase != phase:
            raise StateIntegrityError("current-run pointer belongs to another workspace or phase")
        manifest = self.load_run(phase, pointer.run_id)
        manifest_path = _contained_path(self.state_root, pointer.manifest_relative_path)
        if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != pointer.manifest_sha256:
            raise StateIntegrityError("current-run pointer manifest SHA-256 mismatch")
        return manifest


def _metadata_path(root: Path) -> Path:
    return root / STATE_DIRECTORY_NAME / WORKSPACE_METADATA_NAME


def _validate_uninitialized_workspace_contents(root: Path) -> None:
    """Permit only explicit operator-owned inputs before the V2 marker is created."""

    try:
        entries = tuple(root.rglob("*"))
    except OSError as exc:
        raise WorkspaceSchemaError(f"Cannot inspect workspace: {root}") from exc
    for entry in entries:
        relative = entry.relative_to(root).as_posix()
        if entry.is_symlink():
            raise WorkspaceSchemaError(
                f"Uninitialized workspace contains unsupported symlink: {relative}"
            )
        if entry.is_dir() and relative in {
            *_ALLOWED_OPERATOR_INPUT_DIRECTORIES,
            STATE_DIRECTORY_NAME,
        }:
            continue
        if entry.is_file() and relative == f"{STATE_DIRECTORY_NAME}/writer.lock":
            continue
        if entry.is_file() and relative in _ALLOWED_OPERATOR_INPUT_FILES:
            continue
        if (
            entry.is_file()
            and entry.parent.relative_to(root).as_posix() == "source_inventory"
            and entry.suffix.casefold() == ".xlsx"
        ):
            continue
        raise WorkspaceSchemaError(
            "Workspace has no V2 schema marker and contains generated, legacy, or "
            f"unsupported content: {relative}"
        )


def _content_relative_path(digest: str) -> str:
    return (PurePosixPath("objects") / digest[:2] / f"{digest}.json").as_posix()


def _manifest_relative_path(phase: RunPhase, run_id: str) -> str:
    return (PurePosixPath("runs") / phase.value / run_id / "manifest.json").as_posix()


def _current_relative_path(phase: RunPhase) -> str:
    return (PurePosixPath("current") / f"{phase.value}.json").as_posix()


def _schema_name(value: BaseModel) -> str:
    schema_version = getattr(value, "schema_version", None)
    return str(schema_version or value.__class__.__name__)


def _verify_embedded_schema_label(
    reference: ContentReference,
    payload: bytes,
) -> None:
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateIntegrityError("referenced state object is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise StateIntegrityError("referenced state object must be a JSON object")
    embedded = decoded.get("schema_version")
    if embedded is None:
        return
    if not isinstance(embedded, str) or not embedded.strip():
        raise StateIntegrityError("state object contains an invalid schema version")
    if reference.schema_name != embedded:
        raise StateIntegrityError(
            "state reference schema label does not match encoded schema version"
        )


def _contained_path(root: Path, relative_path: str) -> Path:
    candidate_relative = PurePosixPath(relative_path)
    if candidate_relative.is_absolute() or ".." in candidate_relative.parts:
        raise StateIntegrityError("state path escapes the workspace")
    root_resolved = root.resolve()
    candidate = (root_resolved / Path(*candidate_relative.parts)).resolve()
    if not candidate.is_relative_to(root_resolved):
        raise StateIntegrityError("state path escapes the workspace")
    return candidate


def _manifest_references(manifest: RunManifest) -> tuple[ContentReference, ...]:
    references = list(manifest.input_references)
    for application in manifest.applications:
        references.extend(application.extraction_snapshots)
        if application.evidence_bundle is not None:
            references.append(application.evidence_bundle)
        if application.interpretation is not None:
            references.append(application.interpretation)
    if manifest.portfolio_analysis is not None:
        references.append(manifest.portfolio_analysis)
    if manifest.report_model is not None:
        references.append(manifest.report_model)
    if manifest.report_publication is not None:
        references.append(manifest.report_publication)
    return tuple(references)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as destination:
            descriptor = None
            destination.write(payload)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _sorted_unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(set(values), key=lambda value: (value.casefold(), value)))


def _sorted_references(
    values: tuple[ContentReference, ...],
) -> tuple[ContentReference, ...]:
    unique = {
        (item.sha256, item.relative_path, item.size_bytes, item.schema_name): item
        for item in values
    }
    return tuple(unique[key] for key in sorted(unique))
