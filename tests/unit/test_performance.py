from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from portfolio_analyzer.performance import (
    InferenceCallLimitReached,
    PerformanceRecorder,
    distribution,
)
from portfolio_analyzer.qwen.pipeline import inference_cache_key
from portfolio_analyzer.qwen.provider import _validation_details
from portfolio_analyzer.v2.inference_cache import (
    InferenceCacheIntegrityError,
    PersistentInferenceOutputCache,
)


def test_exclusive_phase_accounting_and_opaque_export(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]
    monkeypatch.setattr("portfolio_analyzer.performance.time.monotonic", lambda: clock[0])
    metrics = PerformanceRecorder()
    with metrics.phase("state_io"):
        clock[0] = 1
        with metrics.phase("validation"):
            clock[0] = 3
        clock[0] = 5
    metrics.begin_application(1)
    metrics.begin_generation(
        user=json.dumps({
            "stage": "logical_unit",
            "logical_unit": {"logical_unit_id": "PRIVATE-NAME"},
            "definitions": "Password=SECRET; SELECT confidential FROM payroll",
        }),
        prompt_tokens=123, output_limit=64,
    )
    clock[0] = 6
    metrics.finish_application()
    payload = metrics.payload()
    assert payload["phase_seconds"] == {"state_io": 3, "validation": 2, "other": 1}
    assert sum(payload["phase_percent"].values()) == pytest.approx(100)
    assert payload["generations"][0]["unit"] == 1
    serialized = json.dumps(payload)
    assert all(value not in serialized for value in ("SECRET", "PRIVATE-NAME", "payroll"))


def test_call_limit_includes_repair_and_resets_per_application() -> None:
    metrics = PerformanceRecorder(max_calls_per_application=2)
    metrics.begin_application(1)
    for attempt in (1, 2):
        metrics.attempt = attempt
        metrics.begin_generation(user="{}", prompt_tokens=10, output_limit=20)
    with pytest.raises(InferenceCallLimitReached):
        metrics.begin_generation(user="{}", prompt_tokens=10, output_limit=20)
    assert len(metrics.generations) == 2
    metrics.begin_application(2)
    metrics.begin_generation(user="{}", prompt_tokens=10, output_limit=20)
    metrics.begin_application(None)
    for _ in range(3):
        metrics.begin_generation(user="{}", prompt_tokens=10, output_limit=20)


def test_validation_categories_never_include_untrusted_locations_or_values() -> None:
    class Response(BaseModel):
        values: dict[str, int]

    with pytest.raises(ValidationError) as error:
        Response.model_validate({"values": {"SECRET-KEY": "SECRET-VALUE"}})
    assert _validation_details(error.value) == ("wrong_type",)


def test_forced_cache_writes_are_resumable_and_conflicts_still_fail(tmp_path: Path) -> None:
    key = inference_cache_key(
        content_sha256="a" * 64, schema_sha256="b" * 64,
        prompt_sha256="c" * 64, model_sha256="d" * 64,
    )
    metrics = PerformanceRecorder()
    forced = PersistentInferenceOutputCache(tmp_path, bypass_reads=True, performance=metrics)
    assert forced.get(key) is None
    forced.put(key, {"purpose": "synthetic"})
    assert forced.get(key) is None
    resumed = PersistentInferenceOutputCache(tmp_path, performance=metrics)
    assert resumed.get(key) == {"purpose": "synthetic"}
    forced.put(key, {"purpose": "synthetic"})
    with pytest.raises(InferenceCacheIntegrityError, match="different output"):
        forced.put(key, {"purpose": "different"})
    assert metrics.cache_hits == 1
    assert metrics.cache_misses == 2
    assert metrics.cache_writes == 2
    entry = next(tmp_path.rglob("*.json"))
    entry.write_bytes(entry.read_bytes() + b" ")
    with pytest.raises(InferenceCacheIntegrityError):
        resumed.get(key)


def test_empty_and_nearest_rank_distributions() -> None:
    assert distribution([])["p95"] is None
    assert distribution([1, 2, 3, 4, 100])["p95"] == 100
    assert distribution([1, 2, 3, 4, 100])["p50"] == 3


def test_ten_minute_warning_is_once_per_application(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]
    monkeypatch.setattr("portfolio_analyzer.performance.time.monotonic", lambda: clock[0])
    metrics = PerformanceRecorder()
    metrics.begin_application(1)
    clock[0] = 599
    assert not metrics.warning_due()
    clock[0] = 600
    assert metrics.warning_due()
    assert not metrics.warning_due()
    metrics.begin_application(2)
    assert not metrics.warning_due()


def test_field_diagnostics_mask_dynamic_keys_and_rejected_values() -> None:
    from dataclasses import asdict
    from typing import Literal

    from pydantic import ConfigDict

    from portfolio_analyzer.qwen.provider import _validation_field_details

    class Response(BaseModel):
        model_config = ConfigDict(extra="forbid")
        status: Literal["unknown"]
        summary: str
        values: dict[str, int]

    with pytest.raises(ValidationError) as error:
        Response.model_validate({
            "status": "SECRET-ENUM", "values": {"SECRET-KEY": "SECRET-VALUE"},
            "SECRET-EXTRA": "SECRET-CONTENT",
        })
    details = [asdict(item) for item in _validation_field_details(
        error.value, Response.model_json_schema(),
    )]
    assert {(item["category"], item["field"]) for item in details} == {
        ("invalid_enum", "status"), ("missing_field", "summary"),
        ("wrong_type", "values"), ("extra_field", "<unknown>"),
    }
    assert "SECRET" not in json.dumps(details)
