"""Two-stage, evidence-closed Qwen interpretation for V2 application bundles.

This module is deliberately persistence-free. It partitions semantic-bearing Access objects,
interprets every logical unit, and synthesizes an application interpretation without inventing a
deterministic semantic fallback.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol, Self, cast

from pydantic import Field, model_validator

from portfolio_analyzer.performance import PerformanceRecorder
from portfolio_analyzer.progress import AnalysisProgressReporter
from portfolio_analyzer.qwen.provider import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    BudgetedQwenJsonProvider,
    PromptBudget,
    QwenProviderError,
    StructuredGenerationFailure,
    generate_validated_json,
)
from portfolio_analyzer.redaction import redact_sensitive_text
from portfolio_analyzer.v2.fingerprints import (
    evidence_bundle_fingerprint,
    interpretation_fingerprint,
)
from portfolio_analyzer.v2.identity import canonical_json_bytes, canonical_sha256, stable_id
from portfolio_analyzer.v2.models import (
    AccessObjectEvidence,
    AccessObjectType,
    ApplicationEvidenceBundle,
    ApplicationInterpretation,
    ApplicationSimilarity,
    Confidence,
    EvidenceOrigin,
    InterpretationKind,
    InterpretiveFinding,
    LogicalUnitInterpretation,
    ModelProvenance,
    NonEmptyString,
    PortfolioAnalysis,
    PortfolioCandidate,
    PortfolioFinding,
    QueryEvidence,
    StrictModel,
)

TWO_STAGE_PROMPT_VERSION = "qwen-two-stage-v8"
UNIT_PROMPT_VERSION = "qwen-logical-unit-v6"
UNIT_BATCH_PROMPT_VERSION = "qwen-logical-unit-batch-v5"
APPLICATION_PROMPT_VERSION = "qwen-application-synthesis-v5"
PORTFOLIO_PROMPT_VERSION = "qwen-portfolio-interpretation-v5"
DEFAULT_DEFINITION_CHARS = 12_000
DEFAULT_PORTFOLIO_BATCH_SIZE = 25
# The approved 0.5B model is not authorized for array-shaped logical-unit responses.
DEFAULT_LOGICAL_UNIT_BATCH_SIZE = 1

_VBA_BOUNDARY = re.compile(
    r"(?im)^(?=(?:(?:public|private|friend|static)\s+)?"
    r"(?:sub|function|property\s+(?:get|let|set))\b)"
)
_VBA_PROCEDURE_START = re.compile(
    r"(?im)^(?P<header>(?:(?:public|private|friend|static)\s+)?"
    r"(?P<kind>sub|function|property\s+(?:get|let|set))\s+"
    r"(?P<name>[A-Za-z_]\w*)\b)"
)
_FORM_BOUNDARY = re.compile(r"(?im)^(?=(?:begin|end|codebehindform)\b)")
_PARAGRAPH_BOUNDARY = re.compile(r"(?m)(?<=\n)\s*(?=\n)")


class LogicalUnitKind(StrEnum):
    QUERY = "query"
    VBA_PROCEDURE = "vba_procedure"
    VBA_MODULE_BATCH = "vba_module_batch"
    FORM_REPORT_COMPOUND = "form_report_compound"
    MACRO = "macro"


class InterpretationRunStatus(StrEnum):
    COMPLETE = "complete"
    ABSTAINED = "abstained"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


class PortfolioRunStatus(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class DefinitionSource:
    object_id: str
    source: str
    text: str


@dataclass(frozen=True, slots=True)
class DefinitionFragment:
    object_id: str
    source: str
    fragment_index: int
    fragment_count: int
    text: str


@dataclass(frozen=True, slots=True)
class LogicalUnitPlan:
    logical_unit_id: str
    kind: LogicalUnitKind
    primary_object_ids: tuple[str, ...]
    definition_sources: tuple[DefinitionSource, ...]
    context: dict[str, Any]
    allowed_evidence_ids: tuple[str, ...]
    allowed_object_ids: tuple[str, ...]
    allowed_interaction_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InferenceCacheKey:
    content_sha256: str
    schema_sha256: str
    prompt_sha256: str
    model_sha256: str
    cache_key: str

    def to_payload(self) -> dict[str, str]:
        """Return a canonical-JSON-compatible persistence payload."""

        return {
            "content_sha256": self.content_sha256,
            "schema_sha256": self.schema_sha256,
            "prompt_sha256": self.prompt_sha256,
            "model_sha256": self.model_sha256,
            "cache_key": self.cache_key,
        }


class InferenceOutputCache(Protocol):
    """Validated structured-output cache; implementations must not persist prompts."""

    def get(self, key: InferenceCacheKey) -> dict[str, Any] | None: ...

    def put(self, key: InferenceCacheKey, value: dict[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class LogicalUnitAnalysisResult:
    logical_unit_id: str
    status: InterpretationRunStatus
    interpretation: LogicalUnitInterpretation | None
    chunk_count: int
    cache_keys: tuple[InferenceCacheKey, ...] = ()
    failure: str | None = None

    def to_payload(self) -> dict[str, Any]:
        """Persist the unit disposition, reason, interpretation, and every inference key."""

        return {
            "logical_unit_id": self.logical_unit_id,
            "status": self.status.value,
            "reason": self.failure,
            "chunk_count": self.chunk_count,
            "interpretation": (
                self.interpretation.model_dump(mode="json")
                if self.interpretation is not None
                else None
            ),
            "cache_keys": [item.to_payload() for item in self.cache_keys],
        }


class _LogicalUnitDraft(StrictModel):
    """Compact model-authored fields; deterministic envelope fields are added in code."""

    schema_version: Literal["logical-unit-draft-v1"] = "logical-unit-draft-v1"
    purpose: NonEmptyString
    business_entities: tuple[NonEmptyString, ...] = ()
    workflow_actions: tuple[NonEmptyString, ...] = ()
    datasource_interaction_ids: tuple[NonEmptyString, ...] = ()
    called_object_ids: tuple[NonEmptyString, ...] = ()
    generated_outputs: tuple[NonEmptyString, ...] = ()
    user_interactions: tuple[NonEmptyString, ...] = ()
    important_business_terms: tuple[NonEmptyString, ...] = ()
    uncertainties: tuple[NonEmptyString, ...] = ()
    evidence_ids: tuple[NonEmptyString, ...]

    @model_validator(mode="before")
    @classmethod
    def accept_json_arrays(cls, value: Any) -> Any:
        return _logical_json_arrays_to_tuples(value)


class _LogicalUnitBatchItemDraft(_LogicalUnitDraft):
    logical_unit_id: NonEmptyString


class _LogicalUnitBatchOutput(StrictModel):
    schema_version: Literal["logical-unit-batch-draft-v1"] = (
        "logical-unit-batch-draft-v1"
    )
    interpretations: tuple[_LogicalUnitBatchItemDraft, ...]

    @model_validator(mode="before")
    @classmethod
    def accept_json_arrays(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        interpretations = normalized.get("interpretations")
        if isinstance(interpretations, list):
            normalized["interpretations"] = tuple(
                _logical_json_arrays_to_tuples(item) for item in interpretations
            )
        return normalized


class _InterpretiveFindingDraft(StrictModel):
    kind: InterpretationKind
    title: NonEmptyString
    explanation: NonEmptyString
    confidence: Confidence
    evidence_ids: tuple[NonEmptyString, ...]
    claim_ids: tuple[NonEmptyString, ...] = ()
    uncertainties: tuple[NonEmptyString, ...] = ()


class _ApplicationDraft(StrictModel):
    schema_version: Literal["application-draft-v1"] = "application-draft-v1"
    summary: NonEmptyString
    business_purpose: NonEmptyString
    major_workflows: tuple[NonEmptyString, ...] = ()
    capabilities: tuple[NonEmptyString, ...] = ()
    modernization_concerns: tuple[NonEmptyString, ...] = ()
    uncertainties: tuple[NonEmptyString, ...] = ()
    findings: tuple[_InterpretiveFindingDraft, ...] = ()
    evidence_ids: tuple[NonEmptyString, ...]
    claim_ids: tuple[NonEmptyString, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def accept_json_arrays(cls, value: Any) -> Any:
        return _application_json_arrays_to_tuples(value)


class _ApplicationSimilarityDraft(StrictModel):
    source_application_id: NonEmptyString
    target_application_id: NonEmptyString
    score: Annotated[float, Field(ge=0.0, le=1.0)]
    shared_features: tuple[NonEmptyString, ...]
    evidence_ids: tuple[NonEmptyString, ...]


class _PortfolioFindingDraft(StrictModel):
    kind: InterpretationKind
    title: NonEmptyString
    narrative: NonEmptyString
    application_ids: tuple[NonEmptyString, ...]
    evidence_ids: tuple[NonEmptyString, ...]
    confidence: Confidence
    uncertainties: tuple[NonEmptyString, ...] = ()


class _PortfolioDraft(StrictModel):
    schema_version: Literal["portfolio-draft-v1"] = "portfolio-draft-v1"
    similarities: tuple[_ApplicationSimilarityDraft, ...] = ()
    findings: tuple[_PortfolioFindingDraft, ...] = ()
    uncertainties: tuple[NonEmptyString, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def accept_json_arrays(cls, value: Any) -> Any:
        return _portfolio_json_arrays_to_tuples(value)


@dataclass(frozen=True, slots=True)
class ApplicationAnalysisResult:
    application_id: str
    source_bundle_sha256: str
    status: InterpretationRunStatus
    unit_results: tuple[LogicalUnitAnalysisResult, ...]
    interpretation: ApplicationInterpretation | None
    cache_key: InferenceCacheKey | None = None
    cache_keys: tuple[InferenceCacheKey, ...] = ()
    failure: str | None = None

    def to_payload(self) -> dict[str, Any]:
        """Return the complete application analysis audit envelope for persistence."""

        return {
            "application_id": self.application_id,
            "source_bundle_sha256": self.source_bundle_sha256,
            "status": self.status.value,
            "reason": self.failure,
            "unit_results": [item.to_payload() for item in self.unit_results],
            "interpretation": (
                self.interpretation.model_dump(mode="json")
                if self.interpretation is not None
                else None
            ),
            "cache_key": self.cache_key.to_payload() if self.cache_key is not None else None,
            "cache_keys": [item.to_payload() for item in self.cache_keys],
        }


@dataclass(frozen=True, slots=True)
class PortfolioAnalysisResult:
    status: PortfolioRunStatus
    analysis: PortfolioAnalysis | None
    batch_count: int
    cache_keys: tuple[InferenceCacheKey, ...] = ()
    failure: str | None = None

    def to_payload(self) -> dict[str, Any]:
        """Return an explicit complete/incomplete portfolio audit envelope."""

        return {
            "status": self.status.value,
            "reason": self.failure,
            "batch_count": self.batch_count,
            "analysis": (
                self.analysis.model_dump(mode="json") if self.analysis is not None else None
            ),
            "cache_keys": [item.to_payload() for item in self.cache_keys],
        }


def content_fingerprint(value: Any) -> str:
    """Return the sanitized canonical content component of an inference key."""

    return canonical_sha256(value)


def schema_fingerprint(model_type: type[Any]) -> str:
    """Return the canonical output-schema component of an inference key."""

    return canonical_sha256(model_type.model_json_schema())


def prompt_fingerprint(*, prompt_version: str, system_prompt: str) -> str:
    """Return the versioned prompt component without retaining prompt state."""

    return canonical_sha256(
        {"prompt_version": prompt_version, "system_prompt": system_prompt}
    )


def model_fingerprint(provenance: ModelProvenance) -> str:
    """Return the fixed-model and deterministic-generation component."""

    return canonical_sha256(
        {
            "model_repo_id": provenance.model_repo_id,
            "model_revision": provenance.model_revision,
            "model_manifest_sha256": provenance.model_manifest_sha256,
            "inference_library_version": provenance.inference_library_version,
            "generation_parameters": provenance.generation_parameters,
        }
    )


def inference_cache_key(
    *,
    content_sha256: str,
    schema_sha256: str,
    prompt_sha256: str,
    model_sha256: str,
) -> InferenceCacheKey:
    """Combine independent invalidation dimensions into a pure cache key."""

    key = canonical_sha256(
        {
            "content_sha256": content_sha256,
            "schema_sha256": schema_sha256,
            "prompt_sha256": prompt_sha256,
            "model_sha256": model_sha256,
        }
    )
    return InferenceCacheKey(
        content_sha256=content_sha256,
        schema_sha256=schema_sha256,
        prompt_sha256=prompt_sha256,
        model_sha256=model_sha256,
        cache_key=key,
    )


def build_logical_units(bundle: ApplicationEvidenceBundle) -> tuple[LogicalUnitPlan, ...]:
    """Partition every semantic-bearing object exactly once; data objects remain context."""

    objects = {item.object_id: item for item in bundle.objects}
    query_ids = {item.object_id for item in bundle.queries}
    interaction_sources = {item.source_object_id for item in bundle.interactions}
    graph_object_ids = {
        node.object_id for node in bundle.dependency_nodes if node.object_id is not None
    }

    def semantic_bearing(item: AccessObjectEvidence) -> bool:
        return bool(
            item.sanitized_definition
            or item.attributes
            or item.evidence_ids
            or item.object_id in query_ids
            or item.object_id in interaction_sources
            or item.object_id in graph_object_ids
        )

    assigned: set[str] = set()
    groups: list[tuple[LogicalUnitKind, tuple[AccessObjectEvidence, ...]]] = []

    for object_type, kind in (
        (AccessObjectType.QUERY, LogicalUnitKind.QUERY),
        (AccessObjectType.PROCEDURE, LogicalUnitKind.VBA_PROCEDURE),
        (AccessObjectType.FORM, LogicalUnitKind.FORM_REPORT_COMPOUND),
        (AccessObjectType.REPORT, LogicalUnitKind.FORM_REPORT_COMPOUND),
        (AccessObjectType.MACRO, LogicalUnitKind.MACRO),
    ):
        for item in sorted(bundle.objects, key=lambda value: value.object_id):
            if item.object_type == object_type and semantic_bearing(item):
                groups.append((kind, (item,)))
                assigned.add(item.object_id)

    plans = [
        _build_unit_plan(bundle, kind, primary_objects)
        for kind, primary_objects in groups
    ]
    modules_without_procedures: dict[str, list[AccessObjectEvidence]] = {}
    for item in bundle.objects:
        if (
            item.object_type == AccessObjectType.MODULE
            and item.object_id not in assigned
            and semantic_bearing(item)
        ):
            procedure_sources = _vba_procedure_definition_sources(item)
            if procedure_sources:
                for discriminator, sources in procedure_sources:
                    plans.append(
                        _build_unit_plan(
                            bundle,
                            LogicalUnitKind.VBA_PROCEDURE,
                            (item,),
                            definition_sources=sources,
                            logical_discriminator=discriminator,
                        )
                    )
            else:
                modules_without_procedures.setdefault(item.artifact_id, []).append(item)
            assigned.add(item.object_id)
    for artifact_id in sorted(modules_without_procedures):
        batch = tuple(
            sorted(
                modules_without_procedures[artifact_id],
                key=lambda item: item.object_id,
            )
        )
        plans.append(_build_unit_plan(bundle, LogicalUnitKind.VBA_MODULE_BATCH, batch))
    semantic_ids = {
        item.object_id
        for item in objects.values()
        if item.object_type
        in {
            AccessObjectType.QUERY,
            AccessObjectType.PROCEDURE,
            AccessObjectType.MODULE,
            AccessObjectType.FORM,
            AccessObjectType.REPORT,
            AccessObjectType.MACRO,
        }
        and semantic_bearing(item)
    }
    planned_ids = {object_id for plan in plans for object_id in plan.primary_object_ids}
    nonmodule_ids = {
        item.object_id
        for item in objects.values()
        if item.object_id in semantic_ids and item.object_type != AccessObjectType.MODULE
    }
    nonmodule_assignments = [
        object_id
        for plan in plans
        for object_id in plan.primary_object_ids
        if object_id in nonmodule_ids
    ]
    if planned_ids != semantic_ids or len(nonmodule_assignments) != len(
        set(nonmodule_assignments)
    ):
        raise ValueError("logical-unit partition did not assign semantic objects exactly once")
    return tuple(sorted(plans, key=lambda item: item.logical_unit_id))


def split_definition_at_boundaries(
    value: str,
    *,
    max_chars: int,
    kind: LogicalUnitKind,
) -> tuple[str, ...]:
    """Split without dropping characters, preferring semantic then line/token boundaries."""

    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    if len(value) <= max_chars:
        return (value,)
    blocks = _semantic_blocks(value, kind)
    pieces: list[str] = []
    for block in blocks:
        pieces.extend(_split_oversized_block(block, max_chars))
    chunks = _pack_strings(pieces, max_chars)
    if "".join(chunks) != value or any(len(chunk) > max_chars for chunk in chunks):
        raise AssertionError("definition splitting must be lossless and bounded")
    return chunks


def build_definition_chunks(
    plan: LogicalUnitPlan,
    *,
    max_definition_chars: int,
) -> tuple[tuple[DefinitionFragment, ...], ...]:
    """Create bounded calls while retaining every definition fragment exactly once."""

    fragments: list[DefinitionFragment] = []
    for source in plan.definition_sources:
        values = split_definition_at_boundaries(
            source.text,
            max_chars=max_definition_chars,
            kind=plan.kind,
        )
        fragments.extend(
            DefinitionFragment(
                object_id=source.object_id,
                source=source.source,
                fragment_index=index,
                fragment_count=len(values),
                text=text,
            )
            for index, text in enumerate(values, start=1)
        )
    if not fragments:
        return ((),)
    chunks: list[list[DefinitionFragment]] = []
    current: list[DefinitionFragment] = []
    current_size = 0
    for fragment in fragments:
        size = len(fragment.text)
        if current and current_size + size > max_definition_chars:
            chunks.append(current)
            current = []
            current_size = 0
        current.append(fragment)
        current_size += size
    if current:
        chunks.append(current)
    return tuple(tuple(chunk) for chunk in chunks)


def _fit_definition_chunks_to_token_budget(
    plan: LogicalUnitPlan,
    chunks: tuple[tuple[DefinitionFragment, ...], ...],
    provider: BudgetedQwenJsonProvider,
    *,
    max_output_tokens: int,
) -> tuple[tuple[tuple[DefinitionFragment, ...], ...], str | None]:
    """Split at semantic/lexical boundaries until every exact unit prompt fits."""

    pending = [tuple(chunk) for chunk in chunks]
    while True:
        normalized = list(_renumber_definition_fragments(tuple(pending)))
        for index, chunk in enumerate(normalized, start=1):
            expected_id = (
                plan.logical_unit_id
                if len(normalized) == 1
                else stable_id("logical_chunk", plan.logical_unit_id, index, len(normalized))
            )
            payload = _unit_payload(
                plan,
                chunk,
                chunk_index=index,
                chunk_count=len(normalized),
                expected_logical_unit_id=expected_id,
            )
            response_type = _unit_response_model(
                allowed_evidence_ids=plan.allowed_evidence_ids,
                allowed_object_ids=plan.allowed_object_ids,
                allowed_interaction_ids=plan.allowed_interaction_ids,
            )
            budget, failure = _measure_prompt_budget(
                provider,
                system=_unit_system_prompt(reduction=False),
                user=canonical_json_bytes(payload).decode("utf-8"),
                schema_name="LogicalUnitInterpretation",
                response_type=response_type,
                max_output_tokens=max_output_tokens,
            )
            if failure is not None:
                return tuple(normalized), failure
            if budget is not None and budget.fits:
                continue
            split = _split_fragment_chunk(chunk, plan.kind)
            if split is None:
                return (
                    tuple(normalized),
                    "an indivisible logical definition fragment exceeds the tokenizer-measured "
                    "prompt budget",
                )
            pending = [*normalized[: index - 1], *split, *normalized[index:]]
            break
        else:
            return tuple(normalized), None


def _renumber_definition_fragments(
    chunks: tuple[tuple[DefinitionFragment, ...], ...],
) -> tuple[tuple[DefinitionFragment, ...], ...]:
    counts: dict[tuple[str, str], int] = {}
    for chunk in chunks:
        for fragment in chunk:
            key = (fragment.object_id, fragment.source)
            counts[key] = counts.get(key, 0) + 1
    seen: dict[tuple[str, str], int] = {}
    output: list[tuple[DefinitionFragment, ...]] = []
    for chunk in chunks:
        normalized: list[DefinitionFragment] = []
        for fragment in chunk:
            key = (fragment.object_id, fragment.source)
            seen[key] = seen.get(key, 0) + 1
            normalized.append(
                DefinitionFragment(
                    object_id=fragment.object_id,
                    source=fragment.source,
                    fragment_index=seen[key],
                    fragment_count=counts[key],
                    text=fragment.text,
                )
            )
        output.append(tuple(normalized))
    return tuple(output)


def _split_fragment_chunk(
    chunk: tuple[DefinitionFragment, ...],
    kind: LogicalUnitKind,
) -> tuple[tuple[DefinitionFragment, ...], tuple[DefinitionFragment, ...]] | None:
    if len(chunk) > 1:
        target = sum(len(item.text) for item in chunk) / 2
        running = 0
        split_at = 1
        for candidate in range(1, len(chunk)):
            running += len(chunk[candidate - 1].text)
            split_at = candidate
            if running >= target:
                break
        return chunk[:split_at], chunk[split_at:]
    if not chunk:
        return None
    fragment = chunk[0]
    logical_split_at = _logical_split_position(fragment.text, kind)
    if logical_split_at is None:
        return None
    first = DefinitionFragment(
        object_id=fragment.object_id,
        source=fragment.source,
        fragment_index=fragment.fragment_index,
        fragment_count=fragment.fragment_count,
        text=fragment.text[:logical_split_at],
    )
    second = DefinitionFragment(
        object_id=fragment.object_id,
        source=fragment.source,
        fragment_index=fragment.fragment_index,
        fragment_count=fragment.fragment_count,
        text=fragment.text[logical_split_at:],
    )
    return ((first,), (second,))


def _logical_split_position(value: str, kind: LogicalUnitKind) -> int | None:
    positions: set[int] = set()
    running = 0
    for block in _semantic_blocks(value, kind):
        running += len(block)
        positions.add(running)
    positions.update(match.end() for match in re.finditer(r"\n|\s+|[,;]\s*", value))
    candidates = [item for item in positions if 0 < item < len(value)]
    if not candidates:
        return None
    midpoint = len(value) / 2
    return min(candidates, key=lambda item: (abs(item - midpoint), item))


def analyze_application_two_stage(
    bundle: ApplicationEvidenceBundle,
    provider: BudgetedQwenJsonProvider,
    *,
    provenance: ModelProvenance,
    cache: InferenceOutputCache | None = None,
    max_definition_chars: int = DEFAULT_DEFINITION_CHARS,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
) -> ApplicationAnalysisResult:
    """Interpret all units, then synthesize all valid results with explicit failure state."""

    source_bundle_sha256 = evidence_bundle_fingerprint(bundle)
    progress = _provider_progress(provider)
    progress.detail(f"Application {bundle.application_id}: building logical-unit plans")
    plans = build_logical_units(bundle)
    metrics = getattr(provider, "performance", None)
    if isinstance(metrics, PerformanceRecorder):
        metrics.plan_units([(item.logical_unit_id, item.kind.value) for item in plans])
    kinds: dict[str, int] = {}
    for plan in plans:
        kinds[plan.kind.value] = kinds.get(plan.kind.value, 0) + 1
    kind_summary = ", ".join(
        f"{kind}={count}" for kind, count in sorted(kinds.items())
    ) or "none"
    progress.detail(
        f"Application {bundle.application_id}: planned {len(plans)} logical unit(s); "
        f"{kind_summary}"
    )
    unit_results = _analyze_logical_units(
        bundle,
        plans,
        provider,
        provenance=provenance,
        cache=cache,
        source_bundle_sha256=source_bundle_sha256,
        max_definition_chars=max_definition_chars,
        max_output_tokens=max_output_tokens,
    )
    valid = tuple(
        result.interpretation
        for result in unit_results
        if result.interpretation is not None
    )
    completed_units = sum(
        item.status in {InterpretationRunStatus.COMPLETE, InterpretationRunStatus.ABSTAINED}
        for item in unit_results
    )
    progress.detail(
        f"Application {bundle.application_id}: logical units complete; "
        f"successful={completed_units}; total={len(unit_results)}"
    )
    if plans and not valid:
        return ApplicationAnalysisResult(
            application_id=bundle.application_id,
            source_bundle_sha256=source_bundle_sha256,
            status=InterpretationRunStatus.FAILED,
            unit_results=unit_results,
            interpretation=None,
            failure="all logical-unit interpretations failed",
        )

    terminal_evidence_ids = _terminal_evidence_ids(bundle)
    claim_ids = tuple(item.claim_id for item in bundle.owner_claims)
    interpretation, synthesis_keys, synthesis_failure = _synthesize_application(
        bundle,
        valid,
        provider,
        provenance=provenance,
        cache=cache,
        source_bundle_sha256=source_bundle_sha256,
        terminal_evidence_ids=terminal_evidence_ids,
        claim_ids=claim_ids,
        max_output_tokens=max_output_tokens,
    )
    final_cache_key = synthesis_keys[-1] if synthesis_keys else None
    if interpretation is None:
        return ApplicationAnalysisResult(
            application_id=bundle.application_id,
            source_bundle_sha256=source_bundle_sha256,
            status=InterpretationRunStatus.FAILED,
            unit_results=unit_results,
            interpretation=None,
            cache_key=final_cache_key,
            cache_keys=synthesis_keys,
            failure=synthesis_failure,
        )
    status = (
        InterpretationRunStatus.PARTIAL
        if any(
            item.status
            in {InterpretationRunStatus.FAILED, InterpretationRunStatus.SKIPPED}
            for item in unit_results
        )
        else InterpretationRunStatus.COMPLETE
    )
    return ApplicationAnalysisResult(
        application_id=bundle.application_id,
        source_bundle_sha256=source_bundle_sha256,
        status=status,
        unit_results=unit_results,
        interpretation=interpretation,
        cache_key=final_cache_key,
        cache_keys=synthesis_keys,
    )


type _ApplicationSynthesisInput = LogicalUnitInterpretation | ApplicationInterpretation


def _synthesize_application(
    bundle: ApplicationEvidenceBundle,
    logical_units: tuple[LogicalUnitInterpretation, ...],
    provider: BudgetedQwenJsonProvider,
    *,
    provenance: ModelProvenance,
    cache: InferenceOutputCache | None,
    source_bundle_sha256: str,
    terminal_evidence_ids: tuple[str, ...],
    claim_ids: tuple[str, ...],
    max_output_tokens: int,
) -> tuple[
    ApplicationInterpretation | None,
    tuple[InferenceCacheKey, ...],
    str | None,
]:
    direct = _application_request(
        bundle,
        logical_units,
        stage="application_synthesis",
        provenance=provenance,
        source_bundle_sha256=source_bundle_sha256,
        terminal_evidence_ids=terminal_evidence_ids,
        claim_ids=claim_ids,
        max_output_tokens=max_output_tokens,
    )
    budget, budget_failure = _measure_application_request(
        provider, direct, max_output_tokens=max_output_tokens
    )
    if budget_failure is not None:
        return None, (), budget_failure
    if budget is not None and budget.fits:
        value, failure = _generate_application_request(
            provider,
            direct,
            cache=cache,
            max_output_tokens=max_output_tokens,
        )
        return value, (direct.cache_key,), failure
    if len(logical_units) < 2:
        return (
            None,
            (),
            "application synthesis exceeds the tokenizer-measured prompt budget and has no "
            "reducible logical-unit set",
        )

    groups, failure = _partition_application_inputs(
        bundle,
        logical_units,
        provider,
        stage="application_synthesis_batch",
        provenance=provenance,
        source_bundle_sha256=source_bundle_sha256,
        terminal_evidence_ids=terminal_evidence_ids,
        claim_ids=claim_ids,
        max_output_tokens=max_output_tokens,
    )
    if failure is not None:
        return None, (), failure
    cache_keys: list[InferenceCacheKey] = []
    partials: list[ApplicationInterpretation] = []
    for index, logical_group in enumerate(groups, start=1):
        request = _application_request(
            bundle,
            logical_group,
            stage="application_synthesis_batch",
            provenance=provenance,
            source_bundle_sha256=source_bundle_sha256,
            terminal_evidence_ids=terminal_evidence_ids,
            claim_ids=claim_ids,
            max_output_tokens=max_output_tokens,
        )
        value, generation_failure = _generate_application_request(
            provider,
            request,
            cache=cache,
            max_output_tokens=max_output_tokens,
        )
        cache_keys.append(request.cache_key)
        if value is None:
            return (
                None,
                tuple(cache_keys),
                f"application synthesis batch {index}/{len(groups)} failed: "
                f"{generation_failure}",
            )
        partials.append(value)

    while len(partials) > 1:
        reduction_groups, failure = _partition_application_inputs(
            bundle,
            tuple(partials),
            provider,
            stage="application_synthesis_reduction",
            provenance=provenance,
            source_bundle_sha256=source_bundle_sha256,
            terminal_evidence_ids=terminal_evidence_ids,
            claim_ids=claim_ids,
            max_output_tokens=max_output_tokens,
        )
        if failure is not None:
            return None, tuple(cache_keys), failure
        if all(len(group) == 1 for group in reduction_groups):
            return (
                None,
                tuple(cache_keys),
                "application partial interpretations cannot be logically reduced within the "
                "tokenizer-measured prompt budget",
            )
        reduced: list[ApplicationInterpretation] = []
        for index, reduction_group in enumerate(reduction_groups, start=1):
            if len(reduction_group) == 1:
                reduced.append(reduction_group[0])
                continue
            request = _application_request(
                bundle,
                reduction_group,
                stage="application_synthesis_reduction",
                provenance=provenance,
                source_bundle_sha256=source_bundle_sha256,
                terminal_evidence_ids=terminal_evidence_ids,
                claim_ids=claim_ids,
                max_output_tokens=max_output_tokens,
            )
            value, generation_failure = _generate_application_request(
                provider,
                request,
                cache=cache,
                max_output_tokens=max_output_tokens,
            )
            cache_keys.append(request.cache_key)
            if value is None:
                return (
                    None,
                    tuple(cache_keys),
                    f"application reduction group {index}/{len(reduction_groups)} failed: "
                    f"{generation_failure}",
                )
            reduced.append(value)
        partials = reduced
    return partials[0], tuple(cache_keys), None


@dataclass(frozen=True, slots=True)
class _ApplicationRequest:
    payload: dict[str, Any]
    response_type: type[_ApplicationDraft]
    system: str
    cache_key: InferenceCacheKey
    application_id: str
    source_bundle_sha256: str
    logical_unit_interpretation_ids: tuple[str, ...]
    provenance: ModelProvenance


def _application_request(
    bundle: ApplicationEvidenceBundle,
    inputs: Sequence[_ApplicationSynthesisInput],
    *,
    stage: str,
    provenance: ModelProvenance,
    source_bundle_sha256: str,
    terminal_evidence_ids: tuple[str, ...],
    claim_ids: tuple[str, ...],
    max_output_tokens: int,
) -> _ApplicationRequest:
    logical_ids = _application_input_logical_ids(inputs)
    response_type = _application_response_model(
        allowed_evidence_ids=terminal_evidence_ids,
        allowed_claim_ids=claim_ids,
    )
    payload: dict[str, Any] = {
        "stage": stage,
        "application": {
            "application_id": bundle.application_id,
            "application_name": bundle.application_name,
        },
        "dependency_graph": {
            "nodes": [item.model_dump(mode="json") for item in bundle.dependency_nodes],
            "edges": [item.model_dump(mode="json") for item in bundle.dependency_edges],
        },
        "datasources": [item.model_dump(mode="json") for item in bundle.datasources],
        "connections": [item.model_dump(mode="json") for item in bundle.connections],
        "tables": [item.model_dump(mode="json") for item in bundle.tables],
        "queries": [_query_metadata(item) for item in bundle.queries],
        "interactions": [item.model_dump(mode="json") for item in bundle.interactions],
        "coverage": bundle.coverage.model_dump(mode="json"),
        "owner_claims": [item.model_dump(mode="json") for item in bundle.owner_claims],
        "terminal_evidence": [
            item.model_dump(mode="json")
            for item in bundle.evidence
            if item.evidence_id in terminal_evidence_ids
        ],
        "allowed_ids": {
            "logical_unit_interpretation_ids": logical_ids,
            "evidence_ids": terminal_evidence_ids,
            "claim_ids": claim_ids,
        },
    }
    if inputs and isinstance(inputs[0], ApplicationInterpretation):
        payload["application_interpretations"] = [
            _application_as_draft_payload(cast(ApplicationInterpretation, item))
            for item in inputs
        ]
    else:
        payload["logical_unit_interpretations"] = [
            {
                "logical_unit_interpretation_id": item.interpretation_id,
                **_logical_unit_as_draft_payload(
                    cast(LogicalUnitInterpretation, item)
                ),
            }
            for item in inputs
        ]
    reduction = stage == "application_synthesis_reduction"
    system = _application_system_prompt(reduction=reduction)
    return _ApplicationRequest(
        payload=payload,
        response_type=response_type,
        system=system,
        cache_key=_cache_key_for(
            payload=payload,
            response_type=response_type,
            prompt_version=APPLICATION_PROMPT_VERSION,
            system=system,
            provenance=provenance,
            max_output_tokens=max_output_tokens,
        ),
        application_id=bundle.application_id,
        source_bundle_sha256=source_bundle_sha256,
        logical_unit_interpretation_ids=logical_ids,
        provenance=provenance,
    )


def _application_input_logical_ids(
    inputs: Sequence[_ApplicationSynthesisInput],
) -> tuple[str, ...]:
    values: list[str] = []
    for item in inputs:
        if isinstance(item, LogicalUnitInterpretation):
            values.append(item.interpretation_id)
        else:
            values.extend(item.logical_unit_interpretation_ids)
    if len(values) != len(set(values)):
        raise ValueError("application synthesis inputs overlap logical-unit interpretation IDs")
    return tuple(sorted(values))


def _measure_application_request(
    provider: BudgetedQwenJsonProvider,
    request: _ApplicationRequest,
    *,
    max_output_tokens: int,
) -> tuple[PromptBudget | None, str | None]:
    return _measure_prompt_budget(
        provider,
        system=request.system,
        user=canonical_json_bytes(request.payload).decode("utf-8"),
        schema_name="ApplicationInterpretation",
        response_type=request.response_type,
        max_output_tokens=max_output_tokens,
    )


def _partition_application_inputs[InputT: _ApplicationSynthesisInput](
    bundle: ApplicationEvidenceBundle,
    inputs: Sequence[InputT],
    provider: BudgetedQwenJsonProvider,
    *,
    stage: str,
    provenance: ModelProvenance,
    source_bundle_sha256: str,
    terminal_evidence_ids: tuple[str, ...],
    claim_ids: tuple[str, ...],
    max_output_tokens: int,
) -> tuple[tuple[tuple[InputT, ...], ...], str | None]:
    groups: list[tuple[InputT, ...]] = []
    current: tuple[InputT, ...] = ()
    for item in inputs:
        proposed = (*current, item)
        request = _application_request(
            bundle,
            proposed,
            stage=stage,
            provenance=provenance,
            source_bundle_sha256=source_bundle_sha256,
            terminal_evidence_ids=terminal_evidence_ids,
            claim_ids=claim_ids,
            max_output_tokens=max_output_tokens,
        )
        budget, failure = _measure_application_request(
            provider, request, max_output_tokens=max_output_tokens
        )
        if failure is not None:
            return (), failure
        if budget is not None and budget.fits:
            current = proposed
            continue
        if not current:
            return (
                (),
                "one application synthesis input exceeds the tokenizer-measured prompt budget",
            )
        groups.append(current)
        current = (item,)
        singleton = _application_request(
            bundle,
            current,
            stage=stage,
            provenance=provenance,
            source_bundle_sha256=source_bundle_sha256,
            terminal_evidence_ids=terminal_evidence_ids,
            claim_ids=claim_ids,
            max_output_tokens=max_output_tokens,
        )
        singleton_budget, failure = _measure_application_request(
            provider, singleton, max_output_tokens=max_output_tokens
        )
        if failure is not None:
            return (), failure
        if singleton_budget is None or not singleton_budget.fits:
            return (
                (),
                "one application synthesis input exceeds the tokenizer-measured prompt budget",
            )
    if current:
        groups.append(current)
    return tuple(groups), None


def _generate_application_request(
    provider: BudgetedQwenJsonProvider,
    request: _ApplicationRequest,
    *,
    cache: InferenceOutputCache | None,
    max_output_tokens: int,
) -> tuple[ApplicationInterpretation | None, str | None]:
    if cache is not None:
        cached = cache.get(request.cache_key)
        if cached is not None:
            _provider_progress(provider).detail(
                "Application synthesis cache hit"
            )
            grounded = request.response_type.model_validate(cached)
            return (
                _finalize_application_draft(grounded, request),
                None,
            )
        _provider_progress(provider).detail("Application synthesis cache miss")
    budget_failure = _prompt_budget_failure(
        provider,
        system=request.system,
        user=canonical_json_bytes(request.payload).decode("utf-8"),
        schema_name="ApplicationInterpretation",
        response_type=request.response_type,
        max_output_tokens=max_output_tokens,
    )
    if budget_failure is not None:
        return None, budget_failure
    try:
        generated = generate_validated_json(
            provider,
            response_model=request.response_type,
            system=request.system,
            user=canonical_json_bytes(request.payload).decode("utf-8"),
            schema_name="ApplicationInterpretation",
            max_output_tokens=max_output_tokens,
        )
    except QwenProviderError as exc:
        return None, f"provider failure: {exc}"
    if isinstance(generated, StructuredGenerationFailure):
        return None, "output remained invalid after one repair attempt"
    interpretation = _finalize_application_draft(generated.value, request)
    if cache is not None:
        cache.put(request.cache_key, generated.value.model_dump(mode="json"))
    return interpretation, None


def analyze_portfolio_candidates(
    candidates: Sequence[PortfolioCandidate],
    application_profiles: Sequence[ApplicationInterpretation],
    evidence_bundles: Sequence[ApplicationEvidenceBundle],
    provider: BudgetedQwenJsonProvider,
    *,
    provenance: ModelProvenance,
    cache: InferenceOutputCache | None = None,
    max_candidates_per_batch: int = DEFAULT_PORTFOLIO_BATCH_SIZE,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
) -> PortfolioAnalysisResult:
    """Interpret only supplied deterministic candidates, with no invented membership or facts."""

    if max_candidates_per_batch < 1:
        raise ValueError("max_candidates_per_batch must be positive")
    preflight = _portfolio_preflight(candidates, application_profiles, evidence_bundles)
    if isinstance(preflight, str):
        return PortfolioAnalysisResult(
            status=PortfolioRunStatus.INCOMPLETE,
            analysis=None,
            batch_count=0,
            failure=preflight,
        )
    context = preflight
    ordered_candidates = tuple(sorted(candidates, key=lambda item: item.candidate_id))
    initial_batches = tuple(
        ordered_candidates[index : index + max_candidates_per_batch]
        for index in range(0, len(ordered_candidates), max_candidates_per_batch)
    ) or ((),)
    batches, batching_failure = _fit_portfolio_candidate_batches(
        initial_batches,
        application_profiles,
        evidence_bundles,
        provider,
        context=context,
        max_output_tokens=max_output_tokens,
    )
    if batching_failure is not None:
        return PortfolioAnalysisResult(
            status=PortfolioRunStatus.INCOMPLETE,
            analysis=None,
            batch_count=len(batches),
            failure=batching_failure,
        )
    analyses: list[PortfolioAnalysis] = []
    cache_keys: list[InferenceCacheKey] = []

    for batch_index, batch in enumerate(batches, start=1):
        payload = _portfolio_payload(
            batch,
            application_profiles,
            evidence_bundles,
            context,
            batch_index=batch_index,
            batch_count=len(batches),
        )
        analysis, failure, cache_key = _generate_portfolio_analysis(
            provider,
            payload=payload,
            candidates=batch,
            context=context,
            provenance=provenance,
            cache=cache,
            max_output_tokens=max_output_tokens,
            system=_portfolio_system_prompt(reduction=False),
        )
        cache_keys.append(cache_key)
        if analysis is None:
            return PortfolioAnalysisResult(
                status=PortfolioRunStatus.INCOMPLETE,
                analysis=None,
                batch_count=len(batches),
                cache_keys=tuple(cache_keys),
                failure=f"portfolio batch {batch_index}/{len(batches)} failed: {failure}",
            )
        analyses.append(analysis)

    if len(analyses) == 1:
        return PortfolioAnalysisResult(
            status=PortfolioRunStatus.COMPLETE,
            analysis=analyses[0],
            batch_count=1,
            cache_keys=tuple(cache_keys),
        )

    analysis, reduction_keys, failure = _reduce_portfolio_analyses(
        tuple(
            _PortfolioPartial(analysis=analysis, candidates=batch)
            for analysis, batch in zip(analyses, batches, strict=True)
        ),
        provider,
        context=context,
        provenance=provenance,
        cache=cache,
        max_output_tokens=max_output_tokens,
    )
    cache_keys.extend(reduction_keys)
    if analysis is None:
        return PortfolioAnalysisResult(
            status=PortfolioRunStatus.INCOMPLETE,
            analysis=None,
            batch_count=len(batches),
            cache_keys=tuple(cache_keys),
            failure=f"portfolio reduction failed: {failure}",
        )
    return PortfolioAnalysisResult(
        status=PortfolioRunStatus.COMPLETE,
        analysis=analysis,
        batch_count=len(batches),
        cache_keys=tuple(cache_keys),
    )


@dataclass(frozen=True, slots=True)
class _PortfolioPartial:
    analysis: PortfolioAnalysis
    candidates: tuple[PortfolioCandidate, ...]


def _reduce_portfolio_analyses(
    values: tuple[_PortfolioPartial, ...],
    provider: BudgetedQwenJsonProvider,
    *,
    context: _PortfolioContext,
    provenance: ModelProvenance,
    cache: InferenceOutputCache | None,
    max_output_tokens: int,
) -> tuple[PortfolioAnalysis | None, tuple[InferenceCacheKey, ...], str | None]:
    """Pairwise-reduce all bounded batches without constructing one unbounded prompt."""

    current = values
    cache_keys: list[InferenceCacheKey] = []
    while len(current) > 1:
        reduced: list[_PortfolioPartial] = []
        for index in range(0, len(current), 2):
            group = current[index : index + 2]
            if len(group) == 1:
                reduced.append(group[0])
                continue
            candidates = tuple(
                sorted(
                    (item for partial in group for item in partial.candidates),
                    key=lambda item: item.candidate_id,
                )
            )
            payload = {
                "stage": "portfolio_reduction",
                "batch_analyses": [
                    _portfolio_as_draft_payload(item.analysis) for item in group
                ],
                "candidate_registry": [
                    item.model_dump(mode="json") for item in candidates
                ],
                "allowed_ids": context.allowed_ids,
                "lifecycle_owner_claims": context.lifecycle_claims,
            }
            analysis, failure, cache_key = _generate_portfolio_analysis(
                provider,
                payload=payload,
                candidates=candidates,
                context=context,
                provenance=provenance,
                cache=cache,
                max_output_tokens=max_output_tokens,
                system=_portfolio_system_prompt(reduction=True),
            )
            cache_keys.append(cache_key)
            if analysis is None:
                return None, tuple(cache_keys), failure
            reduced.append(_PortfolioPartial(analysis=analysis, candidates=candidates))
        current = tuple(reduced)
    return current[0].analysis, tuple(cache_keys), None


def _fit_portfolio_candidate_batches(
    initial_batches: tuple[tuple[PortfolioCandidate, ...], ...],
    profiles: Sequence[ApplicationInterpretation],
    bundles: Sequence[ApplicationEvidenceBundle],
    provider: BudgetedQwenJsonProvider,
    *,
    context: _PortfolioContext,
    max_output_tokens: int,
) -> tuple[tuple[tuple[PortfolioCandidate, ...], ...], str | None]:
    """Adapt candidate-boundary batches until each exact schema-bearing prompt fits."""

    batches = list(initial_batches)
    while True:
        for index, batch in enumerate(batches):
            payload = _portfolio_payload(
                batch,
                profiles,
                bundles,
                context,
                batch_index=index + 1,
                batch_count=len(batches),
            )
            response_type = _portfolio_response_model(
                candidates=batch,
                context=context,
            )
            budget, failure = _measure_prompt_budget(
                provider,
                system=_portfolio_system_prompt(reduction=False),
                user=canonical_json_bytes(payload).decode("utf-8"),
                schema_name="PortfolioAnalysis",
                response_type=response_type,
                max_output_tokens=max_output_tokens,
            )
            if failure is not None:
                return tuple(batches), failure
            if budget is not None and budget.fits:
                continue
            if len(batch) < 2:
                return (
                    tuple(batches),
                    "one portfolio candidate exceeds the tokenizer-measured prompt budget",
                )
            midpoint = len(batch) // 2
            batches[index : index + 1] = [batch[:midpoint], batch[midpoint:]]
            break
        else:
            return tuple(batches), None


@dataclass(frozen=True, slots=True)
class _PortfolioContext:
    profile_sha256s: tuple[str, ...]
    terminal_evidence: dict[str, dict[str, Any]]
    evidence_application: dict[str, str]
    application_ids: tuple[str, ...]
    allowed_basis: dict[str, dict[str, Any]]
    basis_applications: dict[str, frozenset[str]]
    lifecycle_claims: dict[str, tuple[dict[str, Any], ...]]

    @property
    def allowed_ids(self) -> dict[str, tuple[str, ...]]:
        return {
            "application_ids": self.application_ids,
            "evidence_ids": tuple(sorted(self.terminal_evidence)),
            "basis_ids": tuple(sorted(self.allowed_basis)),
        }


def _portfolio_preflight(
    candidates: Sequence[PortfolioCandidate],
    profiles: Sequence[ApplicationInterpretation],
    bundles: Sequence[ApplicationEvidenceBundle],
) -> _PortfolioContext | str:
    bundle_by_app = {item.application_id: item for item in bundles}
    profile_by_app = {item.application_id: item for item in profiles}
    if len(bundle_by_app) != len(bundles) or len(profile_by_app) != len(profiles):
        return "portfolio inputs contain duplicate application IDs"
    if set(bundle_by_app) != set(profile_by_app):
        return "every application profile must have exactly one evidence registry"

    terminal: dict[str, dict[str, Any]] = {}
    evidence_application: dict[str, str] = {}
    allowed_basis: dict[str, dict[str, Any]] = {}
    basis_applications: dict[str, set[str]] = {}
    lifecycle_claims: dict[str, tuple[dict[str, Any], ...]] = {}
    for application_id, bundle in bundle_by_app.items():
        profile = profile_by_app[application_id]
        if profile.source_bundle_sha256 != evidence_bundle_fingerprint(bundle):
            return f"application profile bundle hash mismatch: {application_id}"
        allowed_evidence = set(_terminal_evidence_ids(bundle))
        allowed_claims = {item.claim_id for item in bundle.owner_claims}
        if not set(profile.evidence_ids) <= allowed_evidence or not set(
            profile.claim_ids
        ) <= allowed_claims:
            return f"application profile citation closure failed: {application_id}"
        for finding in profile.findings:
            if not set(finding.evidence_ids) <= allowed_evidence or not set(
                finding.claim_ids
            ) <= allowed_claims:
                return f"application finding citation closure failed: {application_id}"
        for evidence in bundle.evidence:
            if evidence.evidence_id in allowed_evidence:
                if evidence.evidence_id in terminal:
                    return "evidence IDs must be globally unique across applications"
                terminal[evidence.evidence_id] = evidence.model_dump(mode="json")
                evidence_application[evidence.evidence_id] = application_id
        for collection in (
            bundle.datasources,
            bundle.connections,
            bundle.interactions,
            bundle.dependency_nodes,
            bundle.dependency_edges,
        ):
            for item in collection:
                identifier = _technical_identifier(item)
                if identifier in allowed_basis:
                    if allowed_basis[identifier] != item.model_dump(mode="json"):
                        return "technical basis ID collision across applications"
                else:
                    allowed_basis[identifier] = item.model_dump(mode="json")
                basis_applications.setdefault(identifier, set()).add(application_id)
        for object_item in bundle.objects:
            identifier = object_item.object_id
            representation = _object_metadata(object_item)
            if identifier in allowed_basis and allowed_basis[identifier] != representation:
                return "technical basis ID collision across applications"
            allowed_basis[identifier] = representation
            basis_applications.setdefault(identifier, set()).add(application_id)
        lifecycle_claims[application_id] = tuple(
            item.model_dump(mode="json")
            for item in bundle.owner_claims
            if _is_lifecycle_claim(item.field, item.value)
        )

    known_apps = set(bundle_by_app)
    candidate_ids = [item.candidate_id for item in candidates]
    if len(candidate_ids) != len(set(candidate_ids)):
        return "portfolio inputs contain duplicate candidate IDs"
    for candidate in candidates:
        candidate_apps = set(candidate.application_ids)
        if not candidate_apps <= known_apps:
            return (
                "portfolio candidate has unknown application membership: "
                f"{candidate.candidate_id}"
            )
        if not set(candidate.evidence_ids) <= set(terminal):
            return f"portfolio candidate cites non-terminal evidence: {candidate.candidate_id}"
        if any(
            evidence_application[item] not in candidate_apps
            for item in candidate.evidence_ids
        ):
            return f"portfolio candidate evidence crosses membership: {candidate.candidate_id}"
        if not set(candidate.basis_ids) <= set(allowed_basis):
            return f"portfolio candidate cites an unknown technical basis: {candidate.candidate_id}"
        if any(
            not basis_applications[item] <= candidate_apps
            for item in candidate.basis_ids
        ):
            return (
                "portfolio candidate technical basis crosses membership: "
                f"{candidate.candidate_id}"
            )

    return _PortfolioContext(
        profile_sha256s=tuple(sorted(interpretation_fingerprint(item) for item in profiles)),
        terminal_evidence=terminal,
        evidence_application=evidence_application,
        application_ids=tuple(sorted(known_apps)),
        allowed_basis=allowed_basis,
        basis_applications={
            identifier: frozenset(application_ids)
            for identifier, application_ids in basis_applications.items()
        },
        lifecycle_claims=lifecycle_claims,
    )


def _portfolio_payload(
    candidates: Sequence[PortfolioCandidate],
    profiles: Sequence[ApplicationInterpretation],
    bundles: Sequence[ApplicationEvidenceBundle],
    context: _PortfolioContext,
    *,
    batch_index: int,
    batch_count: int,
) -> dict[str, Any]:
    member_ids = {app for candidate in candidates for app in candidate.application_ids}
    candidate_evidence = {
        evidence_id for candidate in candidates for evidence_id in candidate.evidence_ids
    }
    basis_ids = {basis_id for candidate in candidates for basis_id in candidate.basis_ids}
    return {
        "stage": "portfolio_candidate_interpretation",
        "batch_index": batch_index,
        "batch_count": batch_count,
        "candidates": [item.model_dump(mode="json") for item in candidates],
        "application_profiles": [
            {
                "application_id": item.application_id,
                **_application_as_draft_payload(item),
            }
            for item in profiles
            if item.application_id in member_ids
        ],
        "terminal_evidence": [
            context.terminal_evidence[item] for item in sorted(candidate_evidence)
        ],
        "technical_basis": [
            context.allowed_basis[item] for item in sorted(basis_ids)
        ],
        "lifecycle_owner_claims": {
            application_id: context.lifecycle_claims[application_id]
            for application_id in sorted(member_ids)
        },
        "coverage": {
            item.application_id: item.coverage.model_dump(mode="json")
            for item in bundles
            if item.application_id in member_ids
        },
        "allowed_ids": {
            "application_ids": tuple(sorted(member_ids)),
            "evidence_ids": tuple(sorted(candidate_evidence)),
            "basis_ids": tuple(sorted(basis_ids)),
            "candidate_ids": tuple(item.candidate_id for item in candidates),
        },
    }


def _generate_portfolio_analysis(
    provider: BudgetedQwenJsonProvider,
    *,
    payload: dict[str, Any],
    candidates: Sequence[PortfolioCandidate],
    context: _PortfolioContext,
    provenance: ModelProvenance,
    cache: InferenceOutputCache | None,
    max_output_tokens: int,
    system: str,
) -> tuple[PortfolioAnalysis | None, str | None, InferenceCacheKey]:
    response_type = _portfolio_response_model(
        candidates=candidates,
        context=context,
    )
    cache_key = _cache_key_for(
        payload=payload,
        response_type=response_type,
        prompt_version=PORTFOLIO_PROMPT_VERSION,
        system=system,
        provenance=provenance,
        max_output_tokens=max_output_tokens,
    )
    if cache is not None:
        cached = cache.get(cache_key)
        if cached is not None:
            _provider_progress(provider).detail("Portfolio synthesis cache hit")
            grounded = response_type.model_validate(cached)
            return (
                _finalize_portfolio_draft(
                    grounded,
                    context=context,
                    provenance=provenance,
                ),
                None,
                cache_key,
            )
        _provider_progress(provider).detail("Portfolio synthesis cache miss")
    user = canonical_json_bytes(payload).decode("utf-8")
    budget_failure = _prompt_budget_failure(
        provider,
        system=system,
        user=user,
        schema_name="PortfolioAnalysis",
        response_type=response_type,
        max_output_tokens=max_output_tokens,
    )
    if budget_failure is not None:
        return None, budget_failure, cache_key
    try:
        generated = generate_validated_json(
            provider,
            response_model=response_type,
            system=system,
            user=user,
            schema_name="PortfolioAnalysis",
            max_output_tokens=max_output_tokens,
        )
    except QwenProviderError as exc:
        return None, f"provider failure: {exc}", cache_key
    if isinstance(generated, StructuredGenerationFailure):
        return None, "output remained invalid after one repair attempt", cache_key
    analysis = _finalize_portfolio_draft(
        generated.value,
        context=context,
        provenance=provenance,
    )
    if cache is not None:
        cache.put(cache_key, generated.value.model_dump(mode="json"))
    return analysis, None, cache_key


def _portfolio_response_model(
    *,
    candidates: Sequence[PortfolioCandidate],
    context: _PortfolioContext,
) -> type[_PortfolioDraft]:
    candidate_sets = tuple(
        (
            frozenset(item.application_ids),
            frozenset(item.evidence_ids),
        )
        for item in candidates
    )

    class GroundedPortfolioDraft(_PortfolioDraft):
        @model_validator(mode="after")
        def validate_grounding(self) -> Self:
            for similarity in self.similarities:
                apps = frozenset(
                    (similarity.source_application_id, similarity.target_application_id)
                )
                evidence = frozenset(similarity.evidence_ids)
                if not evidence or not any(
                    apps == candidate_apps and evidence <= candidate_evidence
                    for candidate_apps, candidate_evidence in candidate_sets
                ):
                    raise ValueError("portfolio similarity is not closed by one supplied candidate")
            for finding in self.findings:
                apps = frozenset(finding.application_ids)
                evidence = frozenset(finding.evidence_ids)
                if not any(
                    apps == candidate_apps and evidence <= candidate_evidence
                    for candidate_apps, candidate_evidence in candidate_sets
                ):
                    raise ValueError("portfolio finding is not closed by one supplied candidate")
                if _is_retirement_proposal(finding.title, finding.narrative) and any(
                    not context.lifecycle_claims.get(application_id)
                    for application_id in finding.application_ids
                ):
                    raise ValueError(
                        "retirement proposals require a lifecycle owner claim for every member"
                    )
            return self

    return GroundedPortfolioDraft


def _finalize_portfolio_draft(
    draft: _PortfolioDraft,
    *,
    context: _PortfolioContext,
    provenance: ModelProvenance,
) -> PortfolioAnalysis:
    similarities = tuple(
        ApplicationSimilarity(
            source_application_id=item.source_application_id,
            target_application_id=item.target_application_id,
            score=item.score,
            shared_features=item.shared_features,
            evidence_ids=item.evidence_ids,
        )
        for item in draft.similarities
    )
    findings = tuple(
        PortfolioFinding(
            kind=item.kind,
            title=item.title,
            narrative=item.narrative,
            application_ids=item.application_ids,
            evidence_ids=item.evidence_ids,
            confidence=item.confidence,
            uncertainties=item.uncertainties,
        )
        for item in draft.findings
    )
    return PortfolioAnalysis(
        application_interpretation_sha256s=context.profile_sha256s,
        similarities=similarities,
        findings=findings,
        uncertainties=draft.uncertainties,
        provenance=provenance,
    )


def _portfolio_as_draft_payload(analysis: PortfolioAnalysis) -> dict[str, Any]:
    return _PortfolioDraft(
        similarities=tuple(
            _ApplicationSimilarityDraft(
                source_application_id=item.source_application_id,
                target_application_id=item.target_application_id,
                score=item.score,
                shared_features=item.shared_features,
                evidence_ids=item.evidence_ids,
            )
            for item in analysis.similarities
        ),
        findings=tuple(
            _PortfolioFindingDraft(
                kind=item.kind,
                title=item.title,
                narrative=item.narrative,
                application_ids=item.application_ids,
                evidence_ids=item.evidence_ids,
                confidence=item.confidence,
                uncertainties=item.uncertainties,
            )
            for item in analysis.findings
        ),
        uncertainties=analysis.uncertainties,
    ).model_dump(mode="json")


def _portfolio_json_arrays_to_tuples(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    result = dict(value)
    for name in ("application_interpretation_sha256s", "uncertainties"):
        if isinstance(result.get(name), list):
            result[name] = tuple(result[name])
    similarities: list[Any] = []
    for item in result.get("similarities", ()):
        if isinstance(item, dict):
            normalized = dict(item)
            for name in ("shared_features", "evidence_ids"):
                if isinstance(normalized.get(name), list):
                    normalized[name] = tuple(normalized[name])
            similarities.append(normalized)
        else:
            similarities.append(item)
    if isinstance(result.get("similarities"), list):
        result["similarities"] = tuple(similarities)
    findings: list[Any] = []
    for item in result.get("findings", ()):
        if isinstance(item, dict):
            normalized = dict(item)
            if isinstance(normalized.get("kind"), str):
                normalized["kind"] = InterpretationKind(normalized["kind"])
            if isinstance(normalized.get("confidence"), str):
                normalized["confidence"] = Confidence(normalized["confidence"])
            for name in ("application_ids", "evidence_ids", "uncertainties"):
                if isinstance(normalized.get(name), list):
                    normalized[name] = tuple(normalized[name])
            findings.append(normalized)
        else:
            findings.append(item)
    if isinstance(result.get("findings"), list):
        result["findings"] = tuple(findings)
    if "provenance" in result:
        result["provenance"] = _provenance_json_arrays_to_tuples(
            result.get("provenance")
        )
    return result


def _technical_identifier(item: Any) -> str:
    for name in (
        "interaction_id",
        "node_id",
        "edge_id",
        "connection_id",
        "datasource_id",
        "object_id",
    ):
        value = getattr(item, name, None)
        if isinstance(value, str) and value:
            return value
    raise ValueError("technical basis object has no stable identifier")


def _is_lifecycle_claim(field: str, value: str) -> bool:
    normalized_field = re.sub(r"[^a-z]", "", field.casefold())
    if normalized_field not in {
        "lifecycle",
        "lifecyclestatus",
        "status",
        "disposition",
        "retirement",
        "retirementstatus",
    }:
        return False
    return bool(
        re.search(
            r"\b(retire|retired|retirement|decommission|decommissioned|sunset|obsolete|"
            r"end[- ]of[- ]life)\b",
            value,
            re.IGNORECASE,
        )
    )


def _is_retirement_proposal(title: str, narrative: str) -> bool:
    return bool(
        re.search(
            r"\b(retire|retirement|decommission|sunset|end[- ]of[- ]life)\b",
            f"{title}\n{narrative}",
            re.IGNORECASE,
        )
    )


def _build_unit_plan(
    bundle: ApplicationEvidenceBundle,
    kind: LogicalUnitKind,
    primary_objects: tuple[AccessObjectEvidence, ...],
    *,
    definition_sources: tuple[DefinitionSource, ...] | None = None,
    logical_discriminator: str | None = None,
) -> LogicalUnitPlan:
    primary_ids = tuple(sorted(item.object_id for item in primary_objects))
    unit_id = stable_id(
        "logical_unit",
        bundle.application_id,
        kind,
        primary_ids,
        logical_discriminator,
    )
    context, allowed_evidence, allowed_objects, allowed_interactions = _unit_context(
        bundle, primary_ids
    )
    query_by_object = {item.object_id: item for item in bundle.queries}
    definitions: list[DefinitionSource] = list(definition_sources or ())
    if definition_sources is None:
        for item in primary_objects:
            query = query_by_object.get(item.object_id)
            if query is not None and query.sanitized_sql is not None:
                definitions.append(
                    DefinitionSource(
                        item.object_id,
                        "QueryEvidence.sanitized_sql",
                        query.sanitized_sql,
                    )
                )
            if item.sanitized_definition is not None and (
                query is None or item.sanitized_definition != query.sanitized_sql
            ):
                definitions.append(
                    DefinitionSource(
                        item.object_id,
                        "AccessObjectEvidence.sanitized_definition",
                        item.sanitized_definition,
                    )
                )
    return LogicalUnitPlan(
        logical_unit_id=unit_id,
        kind=kind,
        primary_object_ids=primary_ids,
        definition_sources=tuple(definitions),
        context=context,
        allowed_evidence_ids=allowed_evidence,
        allowed_object_ids=allowed_objects,
        allowed_interaction_ids=allowed_interactions,
    )


def _vba_procedure_definition_sources(
    item: AccessObjectEvidence,
) -> tuple[tuple[str, tuple[DefinitionSource, ...]], ...]:
    """Split one module into procedure units without dropping module declarations."""

    definition = item.sanitized_definition
    if not definition:
        return ()
    matches = list(_VBA_PROCEDURE_START.finditer(definition))
    if not matches:
        return ()
    declarations = definition[: matches[0].start()]
    occurrence_by_signature: dict[tuple[str, str], int] = {}
    output: list[tuple[str, tuple[DefinitionSource, ...]]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(definition)
        procedure_text = definition[match.start() : end]
        signature = (
            " ".join(match.group("kind").casefold().split()),
            match.group("name").casefold(),
        )
        occurrence = occurrence_by_signature.get(signature, 0) + 1
        occurrence_by_signature[signature] = occurrence
        discriminator = f"{signature[0]}:{signature[1]}:{occurrence}"
        sources: list[DefinitionSource] = []
        if declarations:
            sources.append(
                DefinitionSource(
                    item.object_id,
                    "AccessObjectEvidence.module_declarations",
                    declarations,
                )
            )
        sources.append(
            DefinitionSource(
                item.object_id,
                f"AccessObjectEvidence.vba_procedure:{discriminator}",
                procedure_text,
            )
        )
        output.append((discriminator, tuple(sources)))
    return tuple(output)


def _unit_context(
    bundle: ApplicationEvidenceBundle,
    primary_ids: tuple[str, ...],
) -> tuple[dict[str, Any], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    primary = set(primary_ids)
    nodes_by_id = {item.node_id: item for item in bundle.dependency_nodes}
    primary_node_ids = {
        item.node_id for item in bundle.dependency_nodes if item.object_id in primary
    }
    edges = tuple(
        item
        for item in bundle.dependency_edges
        if item.source_node_id in primary_node_ids or item.target_node_id in primary_node_ids
    )
    relevant_node_ids = primary_node_ids | {
        node_id
        for edge in edges
        for node_id in (edge.source_node_id, edge.target_node_id)
    }
    nodes = tuple(nodes_by_id[item] for item in sorted(relevant_node_ids))
    context_object_ids = primary | {
        item.object_id for item in nodes if item.object_id is not None
    }
    interactions = tuple(
        item
        for item in bundle.interactions
        if item.source_object_id in primary or item.local_target_object_id in primary
    )
    interaction_ids = tuple(sorted(item.interaction_id for item in interactions))
    query_metadata = tuple(
        item for item in bundle.queries if item.object_id in context_object_ids
    )
    table_metadata = tuple(
        item for item in bundle.tables if item.object_id in context_object_ids
    )
    connection_ids = {
        value
        for value in (
            *(item.connection_id for item in interactions),
            *(item.connection_id for item in query_metadata),
            *(item.connection_id for item in table_metadata),
        )
        if value is not None
    }
    connections = tuple(
        item for item in bundle.connections if item.connection_id in connection_ids
    )
    datasource_ids = {
        value
        for value in (
            *(item.datasource_id for item in interactions),
            *(item.datasource_id for item in connections),
        )
        if value is not None
    }
    datasources = tuple(
        item for item in bundle.datasources if item.datasource_id in datasource_ids
    )
    unresolved = tuple(
        item for item in bundle.unresolved_references if item.source_object_id in primary
    )
    objects = tuple(item for item in bundle.objects if item.object_id in context_object_ids)

    referenced_evidence = {
        evidence_id
        for collection in (
            objects,
            query_metadata,
            table_metadata,
            connections,
            interactions,
            edges,
            unresolved,
        )
        for item in collection
        for evidence_id in item.evidence_ids
    }
    referenced_evidence.update(
        evidence_id
        for item in objects
        for attribute in item.attributes
        for evidence_id in attribute.evidence_ids
    )
    evidence = tuple(
        item
        for item in bundle.evidence
        if item.evidence_id in referenced_evidence or item.object_id in context_object_ids
    )
    known_object_ids = {item.object_id for item in bundle.objects}
    terminal = tuple(
        sorted(
            item.evidence_id
            for item in evidence
            if item.origin == EvidenceOrigin.OBSERVED
            and item.object_id is not None
            and item.object_id in known_object_ids
        )
    )
    context = {
        "primary_objects": [
            _object_metadata(item) for item in objects if item.object_id in primary
        ],
        "context_objects": [
            _object_metadata(item) for item in objects if item.object_id not in primary
        ],
        "query_metadata": [_query_metadata(item) for item in query_metadata],
        "table_context": [item.model_dump(mode="json") for item in table_metadata],
        "connection_context": [item.model_dump(mode="json") for item in connections],
        "datasource_context": [item.model_dump(mode="json") for item in datasources],
        "interactions": [item.model_dump(mode="json") for item in interactions],
        "dependency_nodes": [item.model_dump(mode="json") for item in nodes],
        "dependency_edges": [item.model_dump(mode="json") for item in edges],
        "authoritative_facts": [item.model_dump(mode="json") for item in evidence],
        "unresolved_references": [item.model_dump(mode="json") for item in unresolved],
    }
    return (
        context,
        terminal,
        tuple(sorted(context_object_ids)),
        interaction_ids,
    )


def _object_metadata(item: AccessObjectEvidence) -> dict[str, Any]:
    value = item.model_dump(mode="json", exclude={"sanitized_definition"})
    value["has_sanitized_definition"] = item.sanitized_definition is not None
    return value


def _query_metadata(item: QueryEvidence) -> dict[str, Any]:
    value = item.model_dump(mode="json", exclude={"sanitized_sql"})
    value["has_sanitized_sql"] = item.sanitized_sql is not None
    return value


@dataclass(frozen=True, slots=True)
class _BatchableLogicalUnit:
    plan: LogicalUnitPlan
    chunk: tuple[DefinitionFragment, ...]


@dataclass(frozen=True, slots=True)
class _LogicalUnitBatchRequest:
    payload: dict[str, Any]
    response_type: type[_LogicalUnitBatchOutput]
    system: str
    cache_key: InferenceCacheKey


def _analyze_logical_units(
    bundle: ApplicationEvidenceBundle,
    plans: tuple[LogicalUnitPlan, ...],
    provider: BudgetedQwenJsonProvider,
    *,
    provenance: ModelProvenance,
    cache: InferenceOutputCache | None,
    source_bundle_sha256: str,
    max_definition_chars: int,
    max_output_tokens: int,
) -> tuple[LogicalUnitAnalysisResult, ...]:
    """Batch small related units when the exact tokenizer-measured request fits."""

    objects = {item.object_id: item for item in bundle.objects}
    related: dict[tuple[str, tuple[str, ...]], list[_BatchableLogicalUnit]] = {}
    individual: list[LogicalUnitPlan] = []
    for plan in plans:
        chunks = build_definition_chunks(
            plan,
            max_definition_chars=max_definition_chars,
        )
        artifact_ids = tuple(
            sorted(
                {
                    objects[object_id].artifact_id
                    for object_id in plan.primary_object_ids
                    if object_id in objects
                }
            )
        )
        if (
            len(chunks) != 1
            or not plan.allowed_evidence_ids
            or not artifact_ids
        ):
            individual.append(plan)
            continue
        key = (plan.kind.value, artifact_ids)
        related.setdefault(key, []).append(
            _BatchableLogicalUnit(plan=plan, chunk=chunks[0])
        )

    results: dict[str, LogicalUnitAnalysisResult] = {}
    for group in related.values():
        cursor = 0
        while cursor < len(group):
            remaining = len(group) - cursor
            upper = min(DEFAULT_LOGICAL_UNIT_BATCH_SIZE, remaining)
            selected: tuple[_BatchableLogicalUnit, ...] | None = None
            for size in range(upper, 1, -1):
                candidate = tuple(group[cursor : cursor + size])
                request = _logical_unit_batch_request(
                    candidate,
                    provenance=provenance,
                    max_output_tokens=max_output_tokens,
                )
                budget, failure = _measure_prompt_budget(
                    provider,
                    system=request.system,
                    user=canonical_json_bytes(request.payload).decode("utf-8"),
                    schema_name="LogicalUnitBatchOutput",
                    response_type=request.response_type,
                    max_output_tokens=max_output_tokens,
                )
                if failure is None and budget is not None and budget.fits:
                    selected = candidate
                    break
            if selected is None:
                individual.append(group[cursor].plan)
                cursor += 1
                continue
            batch_results = _analyze_logical_unit_batch(
                bundle,
                selected,
                provider,
                provenance=provenance,
                cache=cache,
                source_bundle_sha256=source_bundle_sha256,
                max_output_tokens=max_output_tokens,
            )
            results.update((item.logical_unit_id, item) for item in batch_results)
            cursor += len(selected)

    for plan in individual:
        result = _analyze_unit(
            bundle,
            plan,
            provider,
            provenance=provenance,
            cache=cache,
            source_bundle_sha256=source_bundle_sha256,
            max_definition_chars=max_definition_chars,
            max_output_tokens=max_output_tokens,
        )
        results[result.logical_unit_id] = result
        metrics = getattr(provider, "performance", None)
        if isinstance(metrics, PerformanceRecorder):
            metrics.unit_status(result.logical_unit_id, result.chunk_count, result.status.value)
    if set(results) != {item.logical_unit_id for item in plans}:
        raise ValueError("logical-unit batching did not account for every planned unit")
    return tuple(results[item.logical_unit_id] for item in plans)


def _logical_unit_batch_request(
    units: tuple[_BatchableLogicalUnit, ...],
    *,
    provenance: ModelProvenance,
    max_output_tokens: int,
) -> _LogicalUnitBatchRequest:
    payload = {
        "stage": "logical_unit_batch",
        "units": [
            _unit_payload(
                item.plan,
                item.chunk,
                chunk_index=1,
                chunk_count=1,
                expected_logical_unit_id=item.plan.logical_unit_id,
            )
            for item in units
        ],
    }
    response_type = _unit_batch_response_model(units=units)
    system = _unit_batch_system_prompt()
    cache_key = _cache_key_for(
        payload=payload,
        response_type=response_type,
        prompt_version=UNIT_BATCH_PROMPT_VERSION,
        system=system,
        provenance=provenance,
        max_output_tokens=max_output_tokens,
    )
    return _LogicalUnitBatchRequest(
        payload=payload,
        response_type=response_type,
        system=system,
        cache_key=cache_key,
    )


def _analyze_logical_unit_batch(
    bundle: ApplicationEvidenceBundle,
    units: tuple[_BatchableLogicalUnit, ...],
    provider: BudgetedQwenJsonProvider,
    *,
    provenance: ModelProvenance,
    cache: InferenceOutputCache | None,
    source_bundle_sha256: str,
    max_output_tokens: int,
) -> tuple[LogicalUnitAnalysisResult, ...]:
    request = _logical_unit_batch_request(
        units,
        provenance=provenance,
        max_output_tokens=max_output_tokens,
    )
    interpretations: tuple[LogicalUnitInterpretation, ...] | None = None
    failure: str | None = None
    if cache is not None:
        cached = cache.get(request.cache_key)
        if cached is not None:
            _provider_progress(provider).detail(
                f"Logical-unit batch cache hit; units={len(units)}"
            )
            grounded = request.response_type.model_validate(cached)
            interpretations = tuple(
                _finalize_logical_unit_draft(
                    item,
                    application_id=bundle.application_id,
                    logical_unit_id=item.logical_unit_id,
                    source_bundle_sha256=source_bundle_sha256,
                    provenance=provenance,
                )
                for item in grounded.interpretations
            )
        else:
            _provider_progress(provider).detail(
                f"Logical-unit batch cache miss; units={len(units)}"
            )
    if interpretations is None:
        user = canonical_json_bytes(request.payload).decode("utf-8")
        budget_failure = _prompt_budget_failure(
            provider,
            system=request.system,
            user=user,
            schema_name="LogicalUnitBatchOutput",
            response_type=request.response_type,
            max_output_tokens=max_output_tokens,
        )
        if budget_failure is not None:
            failure = budget_failure
        else:
            try:
                generated = generate_validated_json(
                    provider,
                    response_model=request.response_type,
                    system=request.system,
                    user=user,
                    schema_name="LogicalUnitBatchOutput",
                    max_output_tokens=max_output_tokens,
                )
            except QwenProviderError as exc:
                failure = f"provider failure: {exc}"
            else:
                if isinstance(generated, StructuredGenerationFailure):
                    failure = "output remained invalid after one repair attempt"
                else:
                    interpretations = tuple(
                        _finalize_logical_unit_draft(
                            item,
                            application_id=bundle.application_id,
                            logical_unit_id=item.logical_unit_id,
                            source_bundle_sha256=source_bundle_sha256,
                            provenance=provenance,
                        )
                        for item in generated.value.interpretations
                    )
                    if cache is not None:
                        cache.put(
                            request.cache_key,
                            generated.value.model_dump(mode="json"),
                        )
    if interpretations is None:
        return tuple(
            LogicalUnitAnalysisResult(
                logical_unit_id=item.plan.logical_unit_id,
                status=InterpretationRunStatus.FAILED,
                interpretation=None,
                chunk_count=1,
                cache_keys=(request.cache_key,),
                failure=f"logical-unit batch failed: {failure}",
            )
            for item in units
        )
    by_id = {item.logical_unit_id: item for item in interpretations}
    return tuple(
        LogicalUnitAnalysisResult(
            logical_unit_id=item.plan.logical_unit_id,
            status=_successful_unit_status(by_id[item.plan.logical_unit_id]),
            interpretation=by_id[item.plan.logical_unit_id],
            chunk_count=1,
            cache_keys=(request.cache_key,),
        )
        for item in units
    )


def _analyze_unit(
    bundle: ApplicationEvidenceBundle,
    plan: LogicalUnitPlan,
    provider: BudgetedQwenJsonProvider,
    *,
    provenance: ModelProvenance,
    cache: InferenceOutputCache | None,
    source_bundle_sha256: str,
    max_definition_chars: int,
    max_output_tokens: int,
) -> LogicalUnitAnalysisResult:
    chunks = build_definition_chunks(
        plan, max_definition_chars=max_definition_chars
    )
    if not plan.allowed_evidence_ids:
        return LogicalUnitAnalysisResult(
            logical_unit_id=plan.logical_unit_id,
            status=InterpretationRunStatus.SKIPPED,
            interpretation=None,
            chunk_count=len(chunks),
            failure="skipped because the logical unit has no terminal observed evidence",
        )
    chunks, budget_failure = _fit_definition_chunks_to_token_budget(
        plan,
        chunks,
        provider,
        max_output_tokens=max_output_tokens,
    )
    if budget_failure is not None:
        return LogicalUnitAnalysisResult(
            logical_unit_id=plan.logical_unit_id,
            status=InterpretationRunStatus.FAILED,
            interpretation=None,
            chunk_count=len(chunks),
            failure=budget_failure,
        )

    metrics = getattr(provider, "performance", None)
    if isinstance(metrics, PerformanceRecorder):
        metrics.unit_status(plan.logical_unit_id, len(chunks), "in_progress")
    chunk_interpretations: list[LogicalUnitInterpretation] = []
    cache_keys: list[InferenceCacheKey] = []
    for index, chunk in enumerate(chunks, start=1):
        expected_id = (
            plan.logical_unit_id
            if len(chunks) == 1
            else stable_id("logical_chunk", plan.logical_unit_id, index, len(chunks))
        )
        payload = _unit_payload(
            plan,
            chunk,
            chunk_index=index,
            chunk_count=len(chunks),
            expected_logical_unit_id=expected_id,
        )
        interpretation, failure, cache_key = _generate_unit_interpretation(
            provider,
            payload=payload,
            expected_logical_unit_id=expected_id,
            bundle=bundle,
            plan=plan,
            provenance=provenance,
            cache=cache,
            source_bundle_sha256=source_bundle_sha256,
            max_output_tokens=max_output_tokens,
            system=_unit_system_prompt(reduction=False),
            prompt_version=UNIT_PROMPT_VERSION,
        )
        cache_keys.append(cache_key)
        if interpretation is None:
            return LogicalUnitAnalysisResult(
                logical_unit_id=plan.logical_unit_id,
                status=InterpretationRunStatus.FAILED,
                interpretation=None,
                chunk_count=len(chunks),
                cache_keys=tuple(cache_keys),
                failure=f"definition chunk {index}/{len(chunks)} failed: {failure}",
            )
        chunk_interpretations.append(interpretation)

    if len(chunks) == 1:
        return LogicalUnitAnalysisResult(
            logical_unit_id=plan.logical_unit_id,
            status=_successful_unit_status(chunk_interpretations[0]),
            interpretation=chunk_interpretations[0],
            chunk_count=1,
            cache_keys=tuple(cache_keys),
        )

    interpretation, reduction_keys, failure = _reduce_unit_interpretations(
        tuple(
            _UnitPartial(interpretation=item, chunk_indices=(index,))
            for index, item in enumerate(chunk_interpretations, start=1)
        ),
        bundle,
        plan,
        provider,
        provenance=provenance,
        cache=cache,
        source_bundle_sha256=source_bundle_sha256,
        max_output_tokens=max_output_tokens,
    )
    cache_keys.extend(reduction_keys)
    if interpretation is None:
        return LogicalUnitAnalysisResult(
            logical_unit_id=plan.logical_unit_id,
            status=InterpretationRunStatus.FAILED,
            interpretation=None,
            chunk_count=len(chunks),
            cache_keys=tuple(cache_keys),
            failure=f"chunk reduction failed: {failure}",
        )
    return LogicalUnitAnalysisResult(
        logical_unit_id=plan.logical_unit_id,
        status=_successful_unit_status(interpretation),
        interpretation=interpretation,
        chunk_count=len(chunks),
        cache_keys=tuple(cache_keys),
    )


@dataclass(frozen=True, slots=True)
class _UnitPartial:
    interpretation: LogicalUnitInterpretation
    chunk_indices: tuple[int, ...]


def _reduce_unit_interpretations(
    values: tuple[_UnitPartial, ...],
    bundle: ApplicationEvidenceBundle,
    plan: LogicalUnitPlan,
    provider: BudgetedQwenJsonProvider,
    *,
    provenance: ModelProvenance,
    cache: InferenceOutputCache | None,
    source_bundle_sha256: str,
    max_output_tokens: int,
) -> tuple[
    LogicalUnitInterpretation | None,
    tuple[InferenceCacheKey, ...],
    str | None,
]:
    current = values
    all_indices = tuple(index for item in values for index in item.chunk_indices)
    cache_keys: list[InferenceCacheKey] = []
    while len(current) > 1:
        reduced: list[_UnitPartial] = []
        for index in range(0, len(current), 2):
            group = current[index : index + 2]
            if len(group) == 1:
                reduced.append(group[0])
                continue
            covered = tuple(
                sorted(chunk_index for item in group for chunk_index in item.chunk_indices)
            )
            expected_id = (
                plan.logical_unit_id
                if covered == all_indices
                else stable_id("logical_reduction", plan.logical_unit_id, covered, all_indices)
            )
            payload = {
                "stage": "logical_unit_reduction",
                "logical_unit": {
                    "logical_unit_id": plan.logical_unit_id,
                    "output_id": expected_id,
                    "kind": plan.kind,
                    "primary_object_ids": plan.primary_object_ids,
                    "covered_definition_chunks": covered,
                    "definition_chunk_count": len(all_indices),
                },
                "chunk_interpretations": [
                    _logical_unit_as_draft_payload(item.interpretation) for item in group
                ],
                "authoritative_context": plan.context,
                "allowed_ids": _allowed_ids(plan),
            }
            interpretation, failure, cache_key = _generate_unit_interpretation(
                provider,
                payload=payload,
                expected_logical_unit_id=expected_id,
                bundle=bundle,
                plan=plan,
                provenance=provenance,
                cache=cache,
                source_bundle_sha256=source_bundle_sha256,
                max_output_tokens=max_output_tokens,
                system=_unit_system_prompt(reduction=True),
                prompt_version=UNIT_PROMPT_VERSION,
            )
            cache_keys.append(cache_key)
            if interpretation is None:
                return None, tuple(cache_keys), failure
            reduced.append(
                _UnitPartial(interpretation=interpretation, chunk_indices=covered)
            )
        current = tuple(reduced)
    return current[0].interpretation, tuple(cache_keys), None


def _unit_payload(
    plan: LogicalUnitPlan,
    chunk: tuple[DefinitionFragment, ...],
    *,
    chunk_index: int,
    chunk_count: int,
    expected_logical_unit_id: str,
) -> dict[str, Any]:
    return {
        "stage": "logical_unit",
        "logical_unit": {
            "logical_unit_id": plan.logical_unit_id,
            "output_id": expected_logical_unit_id,
            "kind": plan.kind,
            "primary_object_ids": plan.primary_object_ids,
            "definition_chunk_index": chunk_index,
            "definition_chunk_count": chunk_count,
        },
        "definitions": [
            {
                "object_id": item.object_id,
                "source": item.source,
                "fragment_index": item.fragment_index,
                "fragment_count": item.fragment_count,
                "text": item.text,
            }
            for item in chunk
        ],
        "authoritative_context": plan.context,
        "allowed_ids": _allowed_ids(plan),
    }


def _allowed_ids(plan: LogicalUnitPlan) -> dict[str, tuple[str, ...]]:
    return {
        "evidence_ids": plan.allowed_evidence_ids,
        "object_ids": plan.allowed_object_ids,
        "datasource_interaction_ids": plan.allowed_interaction_ids,
    }


def _generate_unit_interpretation(
    provider: BudgetedQwenJsonProvider,
    *,
    payload: dict[str, Any],
    expected_logical_unit_id: str,
    bundle: ApplicationEvidenceBundle,
    plan: LogicalUnitPlan,
    provenance: ModelProvenance,
    cache: InferenceOutputCache | None,
    source_bundle_sha256: str,
    max_output_tokens: int,
    system: str,
    prompt_version: str,
) -> tuple[LogicalUnitInterpretation | None, str | None, InferenceCacheKey]:
    response_type = _unit_response_model(
        allowed_evidence_ids=plan.allowed_evidence_ids,
        allowed_object_ids=plan.allowed_object_ids,
        allowed_interaction_ids=plan.allowed_interaction_ids,
    )
    cache_key = _cache_key_for(
        payload=payload,
        response_type=response_type,
        prompt_version=prompt_version,
        system=system,
        provenance=provenance,
        max_output_tokens=max_output_tokens,
    )
    if cache is not None:
        cached = cache.get(cache_key)
        if cached is not None:
            _provider_progress(provider).detail(
                f"Logical-unit cache hit; kind={plan.kind.value}"
            )
            grounded = response_type.model_validate(cached)
            return (
                _finalize_logical_unit_draft(
                    grounded,
                    application_id=bundle.application_id,
                    logical_unit_id=expected_logical_unit_id,
                    source_bundle_sha256=source_bundle_sha256,
                    provenance=provenance,
                ),
                None,
                cache_key,
            )
        _provider_progress(provider).detail(
            f"Logical-unit cache miss; kind={plan.kind.value}"
        )
    user = canonical_json_bytes(payload).decode("utf-8")
    budget_failure = _prompt_budget_failure(
        provider,
        system=system,
        user=user,
        schema_name="LogicalUnitInterpretation",
        response_type=response_type,
        max_output_tokens=max_output_tokens,
    )
    if budget_failure is not None:
        return None, budget_failure, cache_key
    try:
        generated = generate_validated_json(
            provider,
            response_model=response_type,
            system=system,
            user=user,
            schema_name="LogicalUnitInterpretation",
            max_output_tokens=max_output_tokens,
        )
    except QwenProviderError as exc:
        return None, f"provider failure: {exc}", cache_key
    if isinstance(generated, StructuredGenerationFailure):
        return None, "output remained invalid after one repair attempt", cache_key
    interpretation = _finalize_logical_unit_draft(
        generated.value,
        application_id=bundle.application_id,
        logical_unit_id=expected_logical_unit_id,
        source_bundle_sha256=source_bundle_sha256,
        provenance=provenance,
    )
    if cache is not None:
        cache.put(cache_key, generated.value.model_dump(mode="json"))
    return interpretation, None, cache_key


def _provider_progress(
    provider: BudgetedQwenJsonProvider,
) -> AnalysisProgressReporter:
    value = getattr(provider, "progress", None)
    return value if isinstance(value, AnalysisProgressReporter) else AnalysisProgressReporter()


def _unit_response_model(
    *,
    allowed_evidence_ids: tuple[str, ...],
    allowed_object_ids: tuple[str, ...],
    allowed_interaction_ids: tuple[str, ...],
) -> type[_LogicalUnitDraft]:
    evidence = frozenset(allowed_evidence_ids)
    objects = frozenset(allowed_object_ids)
    interactions = frozenset(allowed_interaction_ids)

    class GroundedLogicalUnitDraft(_LogicalUnitDraft):
        @model_validator(mode="after")
        def validate_grounding(self) -> Self:
            if not set(self.evidence_ids) <= evidence:
                raise ValueError("logical interpretation cites non-terminal or unknown evidence")
            if not set(self.called_object_ids) <= objects:
                raise ValueError("logical interpretation cites an unknown technical object")
            if not set(self.datasource_interaction_ids) <= interactions:
                raise ValueError("logical interpretation cites an unknown interaction")
            return self

    return GroundedLogicalUnitDraft


def _finalize_logical_unit_draft(
    draft: _LogicalUnitDraft,
    *,
    application_id: str,
    logical_unit_id: str,
    source_bundle_sha256: str,
    provenance: ModelProvenance,
) -> LogicalUnitInterpretation:
    """Attach authoritative run fields that Qwen must never reproduce or alter."""

    return LogicalUnitInterpretation(
        application_id=application_id,
        logical_unit_id=logical_unit_id,
        source_bundle_sha256=source_bundle_sha256,
        purpose=draft.purpose,
        business_entities=draft.business_entities,
        workflow_actions=draft.workflow_actions,
        datasource_interaction_ids=draft.datasource_interaction_ids,
        called_object_ids=draft.called_object_ids,
        generated_outputs=draft.generated_outputs,
        user_interactions=draft.user_interactions,
        important_business_terms=draft.important_business_terms,
        uncertainties=draft.uncertainties,
        evidence_ids=draft.evidence_ids,
        provenance=provenance,
    )


def _logical_unit_as_draft_payload(
    interpretation: LogicalUnitInterpretation,
) -> dict[str, Any]:
    return _LogicalUnitDraft(
        purpose=interpretation.purpose,
        business_entities=interpretation.business_entities,
        workflow_actions=interpretation.workflow_actions,
        datasource_interaction_ids=interpretation.datasource_interaction_ids,
        called_object_ids=interpretation.called_object_ids,
        generated_outputs=interpretation.generated_outputs,
        user_interactions=interpretation.user_interactions,
        important_business_terms=interpretation.important_business_terms,
        uncertainties=interpretation.uncertainties,
        evidence_ids=interpretation.evidence_ids,
    ).model_dump(mode="json")


def _unit_batch_response_model(
    *,
    units: tuple[_BatchableLogicalUnit, ...],
) -> type[_LogicalUnitBatchOutput]:
    validators = {
        item.plan.logical_unit_id: _unit_response_model(
            allowed_evidence_ids=item.plan.allowed_evidence_ids,
            allowed_object_ids=item.plan.allowed_object_ids,
            allowed_interaction_ids=item.plan.allowed_interaction_ids,
        )
        for item in units
    }
    expected_ids = frozenset(validators)

    class GroundedLogicalUnitBatchOutput(_LogicalUnitBatchOutput):
        @model_validator(mode="after")
        def validate_grounding(self) -> Self:
            actual_ids = tuple(item.logical_unit_id for item in self.interpretations)
            if len(actual_ids) != len(expected_ids) or set(actual_ids) != expected_ids:
                raise ValueError(
                    "logical-unit batch must return exactly one result for every supplied unit"
                )
            for interpretation in self.interpretations:
                validators[interpretation.logical_unit_id].model_validate(
                    {
                        key: value
                        for key, value in interpretation.model_dump(mode="python").items()
                        if key != "logical_unit_id"
                    }
                )
            object.__setattr__(
                self,
                "interpretations",
                tuple(
                    sorted(
                        self.interpretations,
                        key=lambda item: item.logical_unit_id,
                    )
                ),
            )
            return self

    return GroundedLogicalUnitBatchOutput


def _application_response_model(
    *,
    allowed_evidence_ids: tuple[str, ...],
    allowed_claim_ids: tuple[str, ...],
) -> type[_ApplicationDraft]:
    evidence = frozenset(allowed_evidence_ids)
    claims = frozenset(allowed_claim_ids)

    class GroundedApplicationDraft(_ApplicationDraft):
        @model_validator(mode="after")
        def validate_grounding(self) -> Self:
            if not set(self.evidence_ids) <= evidence:
                raise ValueError(
                    "application interpretation cites non-terminal or unknown evidence"
                )
            if not set(self.claim_ids) <= claims:
                raise ValueError("application interpretation cites an unknown owner claim")
            for finding in self.findings:
                if not set(finding.evidence_ids) <= evidence:
                    raise ValueError("application finding cites non-terminal or unknown evidence")
                if not set(finding.claim_ids) <= claims:
                    raise ValueError("application finding cites an unknown owner claim")
            return self

    return GroundedApplicationDraft


def _finalize_application_draft(
    draft: _ApplicationDraft,
    request: _ApplicationRequest,
) -> ApplicationInterpretation:
    findings = tuple(
        InterpretiveFinding(
            application_id=request.application_id,
            kind=item.kind,
            title=item.title,
            explanation=item.explanation,
            confidence=item.confidence,
            evidence_ids=item.evidence_ids,
            claim_ids=item.claim_ids,
            uncertainties=item.uncertainties,
        )
        for item in draft.findings
    )
    return ApplicationInterpretation(
        application_id=request.application_id,
        source_bundle_sha256=request.source_bundle_sha256,
        logical_unit_interpretation_ids=request.logical_unit_interpretation_ids,
        summary=draft.summary,
        business_purpose=draft.business_purpose,
        major_workflows=draft.major_workflows,
        capabilities=draft.capabilities,
        modernization_concerns=draft.modernization_concerns,
        uncertainties=draft.uncertainties,
        findings=findings,
        evidence_ids=draft.evidence_ids,
        claim_ids=draft.claim_ids,
        provenance=request.provenance,
    )


def _application_as_draft_payload(
    interpretation: ApplicationInterpretation,
) -> dict[str, Any]:
    draft = _ApplicationDraft(
        summary=interpretation.summary,
        business_purpose=interpretation.business_purpose,
        major_workflows=interpretation.major_workflows,
        capabilities=interpretation.capabilities,
        modernization_concerns=interpretation.modernization_concerns,
        uncertainties=interpretation.uncertainties,
        findings=tuple(
            _InterpretiveFindingDraft(
                kind=item.kind,
                title=item.title,
                explanation=item.explanation,
                confidence=item.confidence,
                evidence_ids=item.evidence_ids,
                claim_ids=item.claim_ids,
                uncertainties=item.uncertainties,
            )
            for item in interpretation.findings
        ),
        evidence_ids=interpretation.evidence_ids,
        claim_ids=interpretation.claim_ids,
    ).model_dump(mode="json")
    return {
        "covered_logical_unit_interpretation_ids": (
            interpretation.logical_unit_interpretation_ids
        ),
        **draft,
    }


def _logical_json_arrays_to_tuples(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    result = dict(value)
    for name in (
        "business_entities",
        "workflow_actions",
        "datasource_interaction_ids",
        "called_object_ids",
        "generated_outputs",
        "user_interactions",
        "important_business_terms",
        "uncertainties",
        "evidence_ids",
    ):
        if isinstance(result.get(name), list):
            result[name] = tuple(result[name])
    if "provenance" in result:
        result["provenance"] = _provenance_json_arrays_to_tuples(
            result.get("provenance")
        )
    return result


def _application_json_arrays_to_tuples(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    result = dict(value)
    for name in (
        "logical_unit_interpretation_ids",
        "major_workflows",
        "capabilities",
        "modernization_concerns",
        "uncertainties",
        "evidence_ids",
        "claim_ids",
    ):
        if isinstance(result.get(name), list):
            result[name] = tuple(result[name])
    findings: list[Any] = []
    for finding in result.get("findings", ()):
        if isinstance(finding, dict):
            normalized = dict(finding)
            if isinstance(normalized.get("kind"), str):
                normalized["kind"] = InterpretationKind(normalized["kind"])
            if isinstance(normalized.get("confidence"), str):
                normalized["confidence"] = Confidence(normalized["confidence"])
            for name in ("evidence_ids", "claim_ids", "uncertainties"):
                if isinstance(normalized.get(name), list):
                    normalized[name] = tuple(normalized[name])
            findings.append(normalized)
        else:
            findings.append(finding)
    if isinstance(result.get("findings"), list):
        result["findings"] = tuple(findings)
    if "provenance" in result:
        result["provenance"] = _provenance_json_arrays_to_tuples(
            result.get("provenance")
        )
    return result


def _provenance_json_arrays_to_tuples(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    result = dict(value)
    parameters: list[Any] = []
    for parameter in result.get("generation_parameters", ()):
        if isinstance(parameter, dict):
            normalized = dict(parameter)
            if isinstance(normalized.get("evidence_ids"), list):
                normalized["evidence_ids"] = tuple(normalized["evidence_ids"])
            parameters.append(normalized)
        else:
            parameters.append(parameter)
    if isinstance(result.get("generation_parameters"), list):
        result["generation_parameters"] = tuple(parameters)
    return result


def _measure_prompt_budget(
    provider: BudgetedQwenJsonProvider,
    *,
    system: str,
    user: str,
    schema_name: str,
    response_type: type[Any],
    max_output_tokens: int,
) -> tuple[PromptBudget | None, str | None]:
    try:
        budget = provider.measure_prompt(
            system=system,
            user=user,
            schema_name=schema_name,
            schema=response_type.model_json_schema(),
            max_output_tokens=max_output_tokens,
        )
    except QwenProviderError as exc:
        return None, f"provider prompt measurement failed: {exc}"
    if not isinstance(budget, PromptBudget):
        return None, "provider returned an invalid prompt-budget measurement"
    if (
        budget.prompt_tokens < 1
        or budget.reserved_output_tokens != max_output_tokens
        or budget.context_tokens < 1
        or (
            budget.operational_context_tokens is not None
            and budget.operational_context_tokens < 1
        )
    ):
        return None, "provider returned an invalid prompt-budget measurement"
    return budget, None


def _prompt_budget_failure(
    provider: BudgetedQwenJsonProvider,
    *,
    system: str,
    user: str,
    schema_name: str,
    response_type: type[Any],
    max_output_tokens: int,
) -> str | None:
    budget, failure = _measure_prompt_budget(
        provider,
        system=system,
        user=user,
        schema_name=schema_name,
        response_type=response_type,
        max_output_tokens=max_output_tokens,
    )
    if failure is not None:
        return failure
    if budget is not None and not budget.fits:
        return (
            "tokenizer-measured prompt budget exceeded "
            f"({budget.prompt_tokens} + {budget.reserved_output_tokens} > "
            f"{budget.effective_context_tokens}; model ceiling={budget.context_tokens})"
        )
    return None


def _cache_key_for(
    *,
    payload: Any,
    response_type: type[Any],
    prompt_version: str,
    system: str,
    provenance: ModelProvenance,
    max_output_tokens: int,
) -> InferenceCacheKey:
    return inference_cache_key(
        content_sha256=content_fingerprint(payload),
        schema_sha256=schema_fingerprint(response_type),
        prompt_sha256=prompt_fingerprint(
            prompt_version=(
                f"{prompt_version};max_output_tokens={max_output_tokens}"
            ),
            system_prompt=system,
        ),
        model_sha256=model_fingerprint(provenance),
    )


def _sanitize_generated_payload(value: Any) -> Any:
    """Redact model-authored strings before caching or V2 persistence."""

    if isinstance(value, StrEnum):
        return value
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, dict):
        return {
            str(key): _sanitize_generated_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_generated_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_generated_payload(item) for item in value)
    return value


def _terminal_evidence_ids(bundle: ApplicationEvidenceBundle) -> tuple[str, ...]:
    known_objects = {item.object_id for item in bundle.objects}
    return tuple(
        sorted(
            item.evidence_id
            for item in bundle.evidence
            if item.origin == EvidenceOrigin.OBSERVED
            and item.object_id is not None
            and item.object_id in known_objects
        )
    )


def _successful_unit_status(
    interpretation: LogicalUnitInterpretation,
) -> InterpretationRunStatus:
    positive_fields = (
        interpretation.business_entities,
        interpretation.workflow_actions,
        interpretation.datasource_interaction_ids,
        interpretation.called_object_ids,
        interpretation.generated_outputs,
        interpretation.user_interactions,
        interpretation.important_business_terms,
    )
    abstention_text = "\n".join((interpretation.purpose, *interpretation.uncertainties))
    if not any(positive_fields) and re.search(
        r"\b(abstain|insufficient evidence|unable to determine)\b",
        abstention_text,
        re.IGNORECASE,
    ):
        return InterpretationRunStatus.ABSTAINED
    return InterpretationRunStatus.COMPLETE


def _semantic_blocks(value: str, kind: LogicalUnitKind) -> list[str]:
    if kind in {LogicalUnitKind.VBA_PROCEDURE, LogicalUnitKind.VBA_MODULE_BATCH}:
        matcher = _VBA_BOUNDARY
    elif kind == LogicalUnitKind.QUERY:
        positions = [0, *(match.end() for match in re.finditer(r";", value)), len(value)]
        return _slices(value, positions)
    elif kind == LogicalUnitKind.FORM_REPORT_COMPOUND:
        matcher = _FORM_BOUNDARY
    else:
        matcher = _PARAGRAPH_BOUNDARY
    positions = [0, *(match.start() for match in matcher.finditer(value)), len(value)]
    return _slices(value, positions)


def _slices(value: str, positions: list[int]) -> list[str]:
    unique = sorted(set(position for position in positions if 0 <= position <= len(value)))
    return [
        value[start:end]
        for start, end in zip(unique, unique[1:], strict=False)
        if start < end
    ]


def _split_oversized_block(value: str, max_chars: int) -> list[str]:
    if len(value) <= max_chars:
        return [value]
    lines = value.splitlines(keepends=True)
    if len(lines) > 1:
        pieces: list[str] = []
        for line in lines:
            pieces.extend(_split_oversized_block(line, max_chars))
        return pieces
    tokens = re.findall(r"\S+\s*|\s+", value)
    if len(tokens) > 1:
        pieces = []
        for token in tokens:
            pieces.extend(_split_oversized_block(token, max_chars))
        return pieces
    raise ValueError(
        "an indivisible definition token exceeds max_definition_chars; refusing an arbitrary "
        "mid-token split"
    )


def _pack_strings(values: list[str], max_chars: int) -> tuple[str, ...]:
    output: list[str] = []
    current = ""
    for value in values:
        if current and len(current) + len(value) > max_chars:
            output.append(current)
            current = ""
        current += value
    if current:
        output.append(current)
    return tuple(output)


def _unit_system_prompt(*, reduction: bool) -> str:
    action = "Reduce every supplied chunk interpretation" if reduction else "Interpret this unit"
    return (
        f"{action} using only the structured Access evidence supplied. Technical IDs are opaque: "
        "copy only IDs listed in allowed_ids. Never invent objects, interactions, operations, "
        "datasources, or evidence. Cite terminal observed evidence for every interpretation. "
        "Return only the compact model-authored fields required by the response schema; run IDs, "
        "hashes, provenance, and stable IDs are added by the caller. An explicit uncertainty or "
        "abstention is valid when the evidence is insufficient."
    )


def _unit_batch_system_prompt() -> str:
    return (
        "Interpret every supplied logical unit independently using only that unit's structured "
        "Access evidence. Return exactly one interpretation per expected logical_unit_id. "
        "Technical IDs are opaque: copy only IDs listed in the corresponding allowed_ids. Never "
        "invent objects, interactions, operations, datasources, or evidence. Cite terminal "
        "observed evidence for every interpretation. Return only the compact model-authored "
        "fields required by the response schema; envelope fields are added by the caller. An "
        "explicit uncertainty or abstention is valid when one unit's evidence is insufficient."
    )


def _application_system_prompt(*, reduction: bool = False) -> str:
    input_description = (
        "every supplied partial application interpretation"
        if reduction
        else "every supplied valid logical-unit interpretation"
    )
    return (
        f"Synthesize the application from {input_description} plus "
        "the authoritative graph, datasource, coverage, and owner-claim context. Copy only IDs "
        "listed in allowed_ids. Do not invent technical facts or replace observed facts with owner "
        "claims. Preserve uncertainty and extraction gaps; abstain when evidence is insufficient."
    )


def _portfolio_system_prompt(*, reduction: bool) -> str:
    action = (
        "Reduce every supplied bounded portfolio batch"
        if reduction
        else "Interpret only the supplied portfolio candidates"
    )
    return (
        f"{action}. Candidate membership, application IDs, technical basis IDs, and evidence IDs "
        "are authoritative and cannot be expanded, combined across unrelated candidates, or "
        "invented. Do not invent endpoints or dependency edges. Every finding and similarity must "
        "be closed by one supplied candidate and terminal observed evidence. A retirement, sunset, "
        "or decommission proposal is forbidden unless lifecycle_owner_claims contains a supporting "
        "owner claim for every affected application. Empty findings are a valid abstention."
    )
