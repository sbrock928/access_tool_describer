"""Offline cache for validated Qwen outputs keyed by complete inference fingerprints."""

from __future__ import annotations

import re
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import model_validator

from portfolio_analyzer.performance import PerformanceRecorder
from portfolio_analyzer.qwen.pipeline import InferenceCacheKey, inference_cache_key
from portfolio_analyzer.v2.identity import canonical_json_bytes
from portfolio_analyzer.v2.models import Sha256, StrictModel
from portfolio_analyzer.v2.state import _atomic_write_bytes

INFERENCE_CACHE_SCHEMA_VERSION = "inference-output-cache-v2"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class InferenceCacheIntegrityError(ValueError):
    """Raised when a cache entry is malformed, non-canonical, or mismatched."""


class CachedInferenceOutput(StrictModel):
    """One sanitized structured result; prompts and raw generations are never stored."""

    schema_version: Literal["inference-output-cache-v2"] = (
        "inference-output-cache-v2"
    )
    content_sha256: Sha256
    schema_sha256: Sha256
    prompt_sha256: Sha256
    model_sha256: Sha256
    cache_key: Sha256
    output: dict[str, Any]

    @model_validator(mode="after")
    def verify_key(self) -> Self:
        canonical = inference_cache_key(
            content_sha256=self.content_sha256,
            schema_sha256=self.schema_sha256,
            prompt_sha256=self.prompt_sha256,
            model_sha256=self.model_sha256,
        )
        if canonical.cache_key != self.cache_key:
            raise ValueError("cache key does not match its fingerprint components")
        return self


class PersistentInferenceOutputCache:
    """Atomic, input-addressed cache under V2 generated state."""

    def __init__(
        self, state_root: Path, *, bypass_reads: bool = False,
        performance: PerformanceRecorder | None = None,
    ) -> None:
        self._root = state_root.resolve() / "inference-cache"
        self.bypass_reads = bypass_reads
        self.performance = performance

    def get(self, key: InferenceCacheKey) -> dict[str, Any] | None:
        metrics = self.performance
        with metrics.phase("cache_io") if metrics is not None else nullcontext():
            value = None if self.bypass_reads else self._get(key)
        if metrics is not None:
            if value is None:
                metrics.cache_misses += 1
            else:
                metrics.cache_hits += 1
        return value

    def _get(self, key: InferenceCacheKey) -> dict[str, Any] | None:
        path = self._entry_path(key.cache_key)
        if not path.exists():
            return None
        try:
            raw = path.read_bytes()
            entry = CachedInferenceOutput.model_validate_json(raw)
        except (OSError, ValueError) as exc:
            raise InferenceCacheIntegrityError("inference cache entry is invalid") from exc
        if raw != canonical_json_bytes(entry):
            raise InferenceCacheIntegrityError("inference cache entry is not canonical JSON")
        self._verify_requested_key(entry, key)
        return dict(entry.output)

    def put(self, key: InferenceCacheKey, value: dict[str, Any]) -> None:
        metrics = self.performance
        with metrics.phase("cache_io") if metrics is not None else nullcontext():
            self._put(key, value)
        if metrics is not None:
            metrics.cache_writes += 1

    def _put(self, key: InferenceCacheKey, value: dict[str, Any]) -> None:
        entry = CachedInferenceOutput(
            content_sha256=key.content_sha256,
            schema_sha256=key.schema_sha256,
            prompt_sha256=key.prompt_sha256,
            model_sha256=key.model_sha256,
            cache_key=key.cache_key,
            output=value,
        )
        payload = canonical_json_bytes(entry)
        path = self._entry_path(key.cache_key)
        if path.exists():
            try:
                existing = path.read_bytes()
            except OSError as exc:
                raise InferenceCacheIntegrityError(
                    "inference cache entry cannot be read"
                ) from exc
            if existing != payload:
                raise InferenceCacheIntegrityError(
                    "inference cache key already contains different output"
                )
            return
        _atomic_write_bytes(path, payload)

    def _entry_path(self, cache_key: str) -> Path:
        if not _SHA256.fullmatch(cache_key):
            raise InferenceCacheIntegrityError("unsafe inference cache key")
        return self._root / cache_key[:2] / f"{cache_key}.json"

    @staticmethod
    def _verify_requested_key(
        entry: CachedInferenceOutput,
        key: InferenceCacheKey,
    ) -> None:
        actual = (
            entry.content_sha256,
            entry.schema_sha256,
            entry.prompt_sha256,
            entry.model_sha256,
            entry.cache_key,
        )
        expected = (
            key.content_sha256,
            key.schema_sha256,
            key.prompt_sha256,
            key.model_sha256,
            key.cache_key,
        )
        if actual != expected:
            raise InferenceCacheIntegrityError(
                "inference cache entry does not match the requested fingerprint"
            )
