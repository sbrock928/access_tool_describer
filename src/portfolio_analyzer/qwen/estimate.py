"""Read-only cost planning using the executor's exact requests and chunk fitting."""

from __future__ import annotations

from collections import Counter
from typing import Any

from portfolio_analyzer.performance import distribution
from portfolio_analyzer.qwen.pipeline import (
    DEFAULT_DEFINITION_CHARS,
    UNIT_PROMPT_VERSION,
    InferenceOutputCache,
    _cache_key_for,
    _fit_definition_chunks_to_token_budget,
    _unit_payload,
    _unit_response_model,
    _unit_system_prompt,
    build_definition_chunks,
    build_logical_units,
)
from portfolio_analyzer.qwen.provider import DEFAULT_MAX_OUTPUT_TOKENS, BudgetedQwenJsonProvider
from portfolio_analyzer.v2.identity import canonical_json_bytes, stable_id
from portfolio_analyzer.v2.models import ApplicationEvidenceBundle, ModelProvenance


def estimate_application(
    bundle: ApplicationEvidenceBundle,
    provider: BudgetedQwenJsonProvider,
    *,
    provenance: ModelProvenance,
    cache: InferenceOutputCache,
    ordinal: int,
) -> dict[str, Any]:
    plans = build_logical_units(bundle)
    units: list[dict[str, Any]] = []
    tokens: list[int] = []
    hits = 0
    chunks_total = 0
    reductions = 0
    eligible = 0
    blocked = 0
    for index, plan in enumerate(plans, 1):
        if not plan.allowed_evidence_ids:
            units.append({"unit": index, "kind": plan.kind.value, "status": "no_evidence"})
            continue
        eligible += 1
        chunks, failure = _fit_definition_chunks_to_token_budget(
            plan,
            build_definition_chunks(plan, max_definition_chars=DEFAULT_DEFINITION_CHARS),
            provider,
            max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
        )
        if failure is not None:
            blocked += 1
            units.append({"unit": index, "kind": plan.kind.value, "status": "budget_blocked"})
            continue
        unit_tokens: list[int] = []
        unit_hits = 0
        response_type = _unit_response_model(
            allowed_evidence_ids=plan.allowed_evidence_ids,
            allowed_object_ids=plan.allowed_object_ids,
            allowed_interaction_ids=plan.allowed_interaction_ids,
        )
        system = _unit_system_prompt(reduction=False)
        for chunk_index, chunk in enumerate(chunks, 1):
            expected_id = (
                plan.logical_unit_id if len(chunks) == 1
                else stable_id("logical_chunk", plan.logical_unit_id, chunk_index, len(chunks))
            )
            payload = _unit_payload(
                plan, chunk, chunk_index=chunk_index, chunk_count=len(chunks),
                expected_logical_unit_id=expected_id,
            )
            key = _cache_key_for(
                payload=payload, response_type=response_type, prompt_version=UNIT_PROMPT_VERSION,
                system=system, provenance=provenance, max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
            )
            cached = cache.get(key)
            if cached is not None:
                response_type.model_validate(cached)
                unit_hits += 1
            budget = provider.measure_prompt(
                system=system, user=canonical_json_bytes(payload).decode("utf-8"),
                schema_name="LogicalUnitInterpretation", schema=response_type.model_json_schema(),
                max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
            )
            unit_tokens.append(budget.prompt_tokens)
        chunks_total += len(chunks)
        reductions += len(chunks) - 1
        tokens.extend(unit_tokens)
        hits += unit_hits
        units.append({
            "unit": index, "kind": plan.kind.value, "status": "planned",
            "chunks": len(chunks), "chunk_cache_hits": unit_hits,
            "prompt_tokens": distribution(unit_tokens),
        })
    # Each application grouping pass must reduce the number of partials. Initial
    # groups <= U; subsequent merges <= U-1. Actual sizes depend on model outputs.
    synthesis_max = max(1, 2 * eligible - 1)
    return {
        "application": ordinal, "logical_units": len(plans),
        "units_by_type": dict(Counter(item.kind.value for item in plans)),
        "budget_blocked_units": blocked, "initial_chunks": chunks_total,
        "initial_prompt_tokens": distribution(tokens), "chunk_cache_hits": hits,
        "chunk_cache_misses": chunks_total - hits,
        "logical_reduction_calls_if_successful": reductions,
        "cold_calls_min_if_successful": chunks_total + reductions + 1,
        "cold_calls_max_with_repairs": 2 * (chunks_total + reductions + synthesis_max),
        "remaining_calls_min": chunks_total - hits,
        "remaining_calls_max_with_repairs": 2 * (
            chunks_total - hits + reductions + synthesis_max
        ),
        "downstream_cache_coverage": "unknown",
        "bounds_assume_successful_fitting": True,
        "runtime_estimate_seconds": None, "units": units,
    }
