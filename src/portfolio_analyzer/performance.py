"""Local, content-free inference diagnostics. Never accept arbitrary log payloads."""

from __future__ import annotations

import json
import math
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any, Literal

Phase = Literal[
    "verification", "loading", "tokenization", "generation", "validation",
    "deterministic", "state_io", "cache_io",
]
FailureCode = Literal[
    "malformed_json", "missing_field", "wrong_type", "invalid_enum", "extra_field",
    "unknown_citation", "unknown_object", "unknown_interaction", "truncation", "schema",
]
STAGES = frozenset({
    "logical_unit", "logical_unit_reduction", "logical_unit_batch",
    "application_synthesis", "application_synthesis_batch", "application_synthesis_reduction",
    "portfolio_synthesis", "portfolio_reduction", "portfolio_candidate_interpretation",
    "portfolio_batch", "benchmark", "other",
})


class InferenceCallLimitReached(BaseException):
    """Intentional stop, bypassing pipeline exception-to-failed-unit recovery."""


def measured[**P, R](name: Phase) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Measure methods on objects carrying an optional PerformanceRecorder."""
    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        @wraps(function)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            metrics = getattr(args[0], "performance", None) if args else None
            if not isinstance(metrics, PerformanceRecorder):
                return function(*args, **kwargs)
            with metrics.phase(name):
                return function(*args, **kwargs)
        return wrapped
    return decorate


@dataclass(slots=True)
class GenerationMeasurement:
    application: int | None
    unit: int | None
    stage: str
    attempt: int
    prompt_tokens: int
    output_limit: int
    prompt_components: dict[str, int] = field(default_factory=dict)
    generated_tokens: int = 0
    first_token_seconds: float | None = None
    decode_seconds: float | None = None
    decode_tokens_per_second: float | None = None
    generation_seconds: float = 0.0
    hit_output_limit: bool = False
    outcome: str = "interrupted"


def distribution(values: Sequence[int | float]) -> dict[str, int | float | None]:
    """Nearest-rank quantiles; no interpolation disguising token/call counts."""
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "p50": None, "p95": None, "max": None, "sum": 0}
    return {
        "count": len(ordered),
        "p50": ordered[math.ceil(len(ordered) * 0.50) - 1],
        "p95": ordered[math.ceil(len(ordered) * 0.95) - 1],
        "max": ordered[-1], "sum": sum(ordered),
    }


def peak_working_set_bytes() -> int | None:
    """OS process lifetime peak, using only the standard library."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD,
            ]
            value = Counters()
            value.cb = ctypes.sizeof(value)
            if psapi.GetProcessMemoryInfo(
                kernel.GetCurrentProcess(), ctypes.byref(value), value.cb,
            ):
                return int(value.PeakWorkingSetSize)
            return None
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak if sys.platform == "darwin" else peak * 1024)
    except (ImportError, AttributeError, OSError, ValueError):
        return None


@dataclass
class PerformanceRecorder:
    """Run-local counters; scope identifiers are never exported verbatim."""

    max_calls_per_application: int | None = None
    collect_prompt_components: bool = False
    started: float = field(default_factory=lambda: time.monotonic())
    phases: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    failures: Counter[str] = field(default_factory=Counter)
    generations: list[GenerationMeasurement] = field(default_factory=list)
    applications: list[dict[str, Any]] = field(default_factory=list)
    units: list[dict[str, Any]] = field(default_factory=list)
    cache_hits: int = 0
    cache_misses: int = 0
    cache_writes: int = 0
    attempt: int = 1
    application: int | None = None
    dtype: str = "unknown"
    model_revision: str | None = None
    model_manifest_sha256: str | None = None
    cpu_threads: int | None = None
    cpu_interop_threads: int | None = None
    outcome: str = "running"
    _stack: list[list[Any]] = field(default_factory=list, repr=False)
    _units: dict[str, int] = field(default_factory=dict, repr=False)
    _application_started: float | None = None
    _application_calls: int = 0
    _warned: bool = False

    @contextmanager
    def phase(self, name: Phase) -> Iterator[None]:
        frame: list[Any] = [name, time.monotonic(), 0.0]
        self._stack.append(frame)
        try:
            yield
        finally:
            elapsed = max(0.0, time.monotonic() - frame[1])
            self._stack.pop()
            self.phases[name] += max(0.0, elapsed - frame[2])
            if self._stack:
                self._stack[-1][2] += elapsed

    def begin_application(self, ordinal: int | None) -> None:
        self.finish_application()
        self.application = ordinal
        self._application_started = time.monotonic() if ordinal is not None else None
        self._application_calls = 0
        self._units.clear()
        self._warned = False

    def plan_units(self, plans: Sequence[tuple[str, str]]) -> None:
        for key, kind in plans:
            ordinal = self._units.setdefault(key, len(self._units) + 1)
            self.units.append({
                "application": self.application, "unit": ordinal, "kind": kind,
                "chunks": None, "status": "pending",
            })

    def unit_status(self, key: str, chunks: int, status: str) -> None:
        ordinal = self._units.get(key)
        if ordinal is not None:
            for item in reversed(self.units):
                if item["application"] == self.application and item["unit"] == ordinal:
                    item.update(chunks=chunks, status=status)
                    break

    def finish_application(self) -> None:
        if self._application_started is not None:
            self.applications.append({
                "application": self.application,
                "elapsed_seconds": time.monotonic() - self._application_started,
                "inference_calls": self._application_calls,
            })
            self._application_started = None

    def warning_due(self) -> bool:
        if (self._application_started is not None and not self._warned
                and time.monotonic() - self._application_started >= 600):
            self._warned = True
            return True
        return False

    def begin_generation(
        self, *, user: str, prompt_tokens: int, output_limit: int,
    ) -> GenerationMeasurement:
        if (self.application is not None and self.max_calls_per_application is not None
                and self._application_calls >= self.max_calls_per_application):
            raise InferenceCallLimitReached()
        stage = "other"
        unit: int | None = None
        try:
            payload = json.loads(user)
            if isinstance(payload, dict):
                candidate = payload.get("stage")
                if isinstance(candidate, str) and candidate in STAGES:
                    stage = candidate
                logical = payload.get("logical_unit")
                key = logical.get("logical_unit_id") if isinstance(logical, dict) else None
                if isinstance(key, str):
                    unit = self._units.setdefault(key, len(self._units) + 1)
        except (ValueError, TypeError):
            pass
        event = GenerationMeasurement(
            self.application, unit, stage, self.attempt, prompt_tokens, output_limit,
        )
        self.generations.append(event)
        self._application_calls += 1
        return event

    def payload(self) -> dict[str, Any]:
        elapsed = max(0.0, time.monotonic() - self.started)
        phases = dict(self.phases)
        phases["other"] = max(0.0, elapsed - sum(phases.values()))
        prefill = sum(item.first_token_seconds or 0 for item in self.generations)
        decode = sum(item.decode_seconds or 0 for item in self.generations)
        generation_elapsed = sum(item.generation_seconds for item in self.generations)
        generation_parts = {
            "prefill_proxy": prefill, "autoregressive_decode": decode,
            "overhead_or_unobserved": max(0.0, generation_elapsed - prefill - decode),
        }
        stages: dict[str, Any] = {}
        for stage in sorted({item.stage for item in self.generations}):
            events = [item for item in self.generations if item.stage == stage]
            stages[stage] = {
                "calls": len(events),
                "repair_calls": sum(item.attempt == 2 for item in events),
                "prompt_tokens": distribution([item.prompt_tokens for item in events]),
                "output_tokens": distribution([item.generated_tokens for item in events]),
                "successful_output_tokens": distribution([
                    item.generated_tokens for item in events if item.outcome == "valid"
                ]),
                "first_token_seconds": distribution([
                    item.first_token_seconds for item in events
                    if item.first_token_seconds is not None
                ]),
                "decode_tokens_per_second": distribution([
                    item.decode_tokens_per_second for item in events
                    if item.decode_tokens_per_second is not None
                ]),
            }
        return {
            "schema_version": "inference-performance-v1", "outcome": self.outcome,
            "elapsed_seconds": elapsed, "phase_seconds": phases,
            "phase_percent": {
                key: 100 * value / elapsed if elapsed else 0 for key, value in phases.items()
            },
            "generation_breakdown": {
                "seconds": generation_parts,
                "percent_of_run": {
                    key: 100 * value / elapsed if elapsed else 0
                    for key, value in generation_parts.items()
                },
                "subset_of_generation_phase": True,
            },
            "backend": "transformers-pytorch", "dtype": self.dtype,
            "model_revision": self.model_revision,
            "model_manifest_sha256": self.model_manifest_sha256,
            "cpu_threads": self.cpu_threads, "cpu_interop_threads": self.cpu_interop_threads,
            "peak_working_set_bytes": peak_working_set_bytes(),
            "cache": {"hits": self.cache_hits, "misses": self.cache_misses,
                      "writes": self.cache_writes},
            "validation_failures": dict(self.failures), "stages": stages,
            "validation_retry_rate": (
                sum(item.attempt == 2 for item in self.generations)
                / sum(item.attempt == 1 for item in self.generations)
                if any(item.attempt == 1 for item in self.generations) else None
            ),
            "slowest_requests": [
                {"application": item.application, "unit": item.unit, "stage": item.stage,
                 "generation_seconds": item.generation_seconds}
                for item in sorted(
                    self.generations, key=lambda value: value.generation_seconds, reverse=True,
                )[:10]
            ],
            "applications": self.applications,
            "logical_units": self.units,
            "generations": [asdict(item) for item in self.generations],
        }

    def write(self, path: Path) -> None:
        # Imported lazily to keep this module usable by the provider and state layer.
        from portfolio_analyzer.v2.identity import canonical_json_bytes
        from portfolio_analyzer.v2.state import _atomic_write_bytes

        _atomic_write_bytes(path, canonical_json_bytes(self.payload()))
