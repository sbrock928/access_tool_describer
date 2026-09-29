"""Integrity-verified, deterministic, local-only Qwen JSON generation.

Prompts exist only as call-local values. This module does not expose a prompt history, log prompt
content, or write prompts to disk.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import threading
import time
from collections import Counter
from collections.abc import Mapping
from contextlib import nullcontext, suppress
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ValidationError

from portfolio_analyzer.performance import PerformanceRecorder, ValidationDiagnostic, measured
from portfolio_analyzer.progress import (
    AnalysisProgressReporter,
    GenerationHeartbeat,
    TokenProgressCriteria,
)
from portfolio_analyzer.qwen.experiments import (
    EXPERIMENTS,
    PRIVACY_RULE,
    JsonObjectStop,
    compact_schema,
    response_contract,
)
from portfolio_analyzer.runtime import ResolvedQwenRuntime
from portfolio_analyzer.semantic.model_store import (
    QWEN_MODEL,
    VerifiedModel,
    verify_model_directory,
)

DEFAULT_MAX_OUTPUT_TOKENS = 1_024
REVIEWED_CONTEXT_TOKENS = 32_768
LOGICAL_UNIT_OPERATIONAL_CONTEXT_TOKENS = 8_192
SYNTHESIS_OPERATIONAL_CONTEXT_TOKENS = 16_384
_SCHEMA_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}\Z")
_CHATML_CONTROL_TOKENS = ("<|im_start|>", "<|im_end|>", "<|endoftext|>")
_JSON_RESPONSE_PREFIX = "{"
_MARKDOWN_JSON_FENCE = re.compile(
    r"\A```(?:json)?[ \t]*\r?\n(?P<body>.*?)\r?\n```[ \t]*\Z",
    re.IGNORECASE | re.DOTALL,
)


class QwenProviderError(RuntimeError):
    """Base error for the fixed local inference boundary."""


class QwenPromptBudgetError(QwenProviderError):
    """The complete prompt plus output reservation does not fit the reviewed context window."""


class QwenOutputError(QwenProviderError):
    """The model returned output that is not one strict JSON object."""


@dataclass(frozen=True, slots=True)
class PromptBudget:
    """Tokenizer-measured budget for the exact schema-bearing ChatML prompt."""

    prompt_tokens: int
    reserved_output_tokens: int
    context_tokens: int
    operational_context_tokens: int | None = None

    @property
    def effective_context_tokens(self) -> int:
        if self.operational_context_tokens is None:
            return self.context_tokens
        return min(self.context_tokens, self.operational_context_tokens)

    @property
    def fits(self) -> bool:
        return (
            self.prompt_tokens + self.reserved_output_tokens
            <= self.effective_context_tokens
        )


class QwenJsonProvider(Protocol):
    """Small inference seam implemented by the local provider and test fakes."""

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: Mapping[str, Any],
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> dict[str, Any]: ...


class BudgetedQwenJsonProvider(QwenJsonProvider, Protocol):
    """Inference seam with exact tokenizer measurement for safe pipeline batching."""

    def measure_prompt(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: Mapping[str, Any],
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> PromptBudget: ...


@dataclass(frozen=True, slots=True)
class OutputValidationIssue:
    """A safe validation summary that deliberately omits rejected input values."""

    attempt: Literal[1, 2]
    kind: Literal["json", "schema"]
    details: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StructuredGenerationSuccess[ResponseModelT: BaseModel]:
    """A schema-valid response, including schema-defined abstentions."""

    value: ResponseModelT
    attempts: Literal[1, 2]


@dataclass(frozen=True, slots=True)
class StructuredGenerationFailure:
    """Explicit terminal result after the initial output and one repair are invalid."""

    issues: tuple[OutputValidationIssue, OutputValidationIssue]
    code: Literal["invalid_output"] = "invalid_output"
    attempts: Literal[2] = 2


def generate_validated_json[ResponseModelT: BaseModel](
    provider: QwenJsonProvider,
    *,
    response_model: type[ResponseModelT],
    system: str,
    user: str,
    schema_name: str | None = None,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
) -> StructuredGenerationSuccess[ResponseModelT] | StructuredGenerationFailure:
    """Generate once, make one bounded repair attempt, and return success or explicit failure.

    Only malformed JSON-object output and Pydantic validation failures are repairable. Runtime,
    integrity, dependency, device, and token-budget failures remain exceptions because another
    generation attempt cannot safely repair them. Whether an abstention is valid is exclusively a
    responsibility of ``response_model``.
    """

    output_schema = response_model.model_json_schema()
    requested_schema_name = schema_name or response_model.__name__
    issues: list[OutputValidationIssue] = []
    current_system = system
    progress = _provider_progress(provider)
    metrics = getattr(provider, "performance", None)
    if not isinstance(metrics, PerformanceRecorder):
        metrics = None

    for attempt in (1, 2):
        typed_attempt: Literal[1, 2] = attempt
        field_issues: list[ValidationDiagnostic] = []
        previous_generations = len(metrics.generations) if metrics is not None else 0
        if metrics is not None:
            metrics.attempt = attempt
        progress.detail(
            f"Structured generation {requested_schema_name}: attempt {attempt}/2"
        )
        try:
            candidate = provider.complete_json(
                system=current_system,
                user=user,
                schema_name=requested_schema_name,
                schema=output_schema,
                max_output_tokens=max_output_tokens,
            )
        except QwenOutputError:
            if metrics is not None:
                metrics.failures["malformed_json"] += 1
                if len(metrics.generations) > previous_generations:
                    event = metrics.generations[-1]
                    event.outcome = "invalid"
                    event.validation_issues.append(ValidationDiagnostic("malformed_json", "<root>"))
                    if event.hit_output_limit:
                        event.validation_issues.append(ValidationDiagnostic("truncation", "<root>"))
            progress.detail(
                f"Structured generation {requested_schema_name}: malformed JSON"
            )
            field_issues = [ValidationDiagnostic("malformed_json", "<root>")]
            issue = OutputValidationIssue(
                attempt=typed_attempt,
                kind="json",
                details=("malformed_json",),
            )
            candidate = None
        else:
            try:
                with metrics.phase("validation") if metrics is not None else nullcontext():
                    validated = response_model.model_validate(candidate)
            except ValidationError as exc:
                field_issues = _validation_field_details(exc, output_schema)
                if metrics is not None:
                    metrics.failures.update(_validation_details(exc))
                    if len(metrics.generations) > previous_generations:
                        metrics.generations[-1].outcome = "invalid"
                        metrics.generations[-1].validation_issues = field_issues
                progress.detail(
                    f"Structured generation {requested_schema_name}: schema validation failed"
                )
                issue = OutputValidationIssue(
                    attempt=typed_attempt,
                    kind="schema",
                    details=_validation_details(exc),
                )
            else:
                progress.detail(
                    f"Structured generation {requested_schema_name}: validation succeeded"
                )
                if metrics is not None and len(metrics.generations) > previous_generations:
                    metrics.generations[-1].outcome = "valid"
                return StructuredGenerationSuccess(value=validated, attempts=typed_attempt)

        issues.append(issue)
        if attempt == 2:
            return StructuredGenerationFailure(issues=(issues[0], issues[1]))
        failure_kind = (
            "was not valid JSON" if issue.kind == "json" else "did not match the JSON Schema"
        )
        current_system = (
            f"{system.rstrip()}\n"
            f"Your previous response {failure_kind}. This is the only repair attempt. Re-read "
            "the unchanged user request and return a complete replacement JSON object matching "
            "the supplied schema exactly."
        )

        experiment = getattr(provider, "experiment", EXPERIMENTS["baseline"])
        if experiment.targeted_repair:
            # Fixed categories and code-owned top-level fields only; cap prompt overhead.
            feedback = "; ".join(
                f"{item.category}:{item.field}" for item in field_issues[:8]
            )
            current_system += (
                f"\nValidation categories and fields: {feedback}. "
                "Include every required field at the top level; omit extra fields and wrappers. "
                "Use only the schema's allowed enum values."
            )

    raise AssertionError("the fixed two-attempt loop must return")  # pragma: no cover


class LocalQwenProvider:
    """Load the one approved Qwen model once and generate strict JSON objects offline."""

    def __init__(
        self,
        runtime: ResolvedQwenRuntime,
        *,
        progress: AnalysisProgressReporter | None = None,
    ) -> None:
        self.runtime = runtime
        self.experiment = EXPERIMENTS["baseline"]
        self.benchmark_canary: str | None = None
        self.collect_response_shape = False
        self.progress = progress or AnalysisProgressReporter()
        self.performance = self.progress.performance or PerformanceRecorder()
        _enable_offline_mode()
        if runtime.device == "cpu":
            os.environ.setdefault("OMP_NUM_THREADS", str(runtime.cpu_threads))
            os.environ.setdefault("MKL_NUM_THREADS", str(runtime.cpu_threads))
        self.progress.basic("Verifying approved Qwen 0.5B model files...")
        verification_started = time.monotonic()
        with self.performance.phase("verification"):
            self.verified: VerifiedModel = verify_model_directory(runtime.model_dir)
        _require_exact_qwen(self.verified)
        self.performance.model_revision = QWEN_MODEL.revision
        self.performance.model_manifest_sha256 = self.verified.manifest.manifest_sha256
        self.progress.basic(
            "Approved Qwen model verified; "
            f"elapsed={time.monotonic() - verification_started:.1f}s"
        )
        self._load_lock = threading.Lock()
        self._config: Any = None
        self._tokenizer: Any = None
        self._model: Any = None
        self._torch: Any = None
        self._transformers: Any = None
        self._device: str | None = None
        self._generation_count = 0
        self._last_prompt_components: dict[str, int] = {}

    def measure_prompt(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: Mapping[str, Any],
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> PromptBudget:
        """Measure the exact ChatML prompt and reserved output with the verified tokenizer."""

        _, _, budget = self._prepare_prompt(
            system=system,
            user=user,
            schema_name=schema_name,
            schema=schema,
            max_output_tokens=max_output_tokens,
        )
        return budget

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: Mapping[str, Any],
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> dict[str, Any]:
        """Generate one object without truncating any caller-supplied prompt content."""

        self.progress.detail(f"Preparing prompt for {schema_name}...")
        inputs, prompt_tokens, budget = self._prepare_prompt(
            system=system,
            user=user,
            schema_name=schema_name,
            schema=schema,
            max_output_tokens=max_output_tokens,
        )
        if not budget.fits:
            raise QwenPromptBudgetError(
                "Complete prompt and reserved output exceed the operational token budget "
                f"({budget.prompt_tokens} + {budget.reserved_output_tokens} > "
                f"{budget.effective_context_tokens}); split the logical unit before inference"
            )
        self.progress.detail(
            f"Prompt ready for {schema_name}; tokens={prompt_tokens}; "
            f"reserved_output={max_output_tokens}; "
            f"operational_context={budget.effective_context_tokens}; "
            f"model_context={budget.context_tokens}"
        )
        with self.performance.phase("loading"):
            self._load()
        tokenizer = self._tokenizer
        model = self._model
        torch = self._torch
        transformers = self._transformers
        if (
            tokenizer is None
            or model is None
            or torch is None
            or transformers is None
            or self._device is None
        ):
            raise QwenProviderError("Local Qwen provider did not initialize")

        try:
            device_inputs = _move_inputs(inputs, self._device)
            generation: dict[str, Any] = {
                "max_new_tokens": max_output_tokens,
                "do_sample": False,
                "num_beams": 1,
                "use_cache": True,
            }
            self._generation_count += 1
            generation_id = self._generation_count
            heartbeat = GenerationHeartbeat(self.progress, generation_id)
            criteria_type = getattr(transformers, "StoppingCriteriaList", list)
            generation["stopping_criteria"] = criteria_type(
                [TokenProgressCriteria(prompt_tokens, heartbeat)]
            )
            if self.experiment.stop_json:
                generation["stopping_criteria"].append(JsonObjectStop(tokenizer, prompt_tokens))
            eos_token_id = getattr(tokenizer, "eos_token_id", None)
            pad_token_id = getattr(tokenizer, "pad_token_id", None)
            if eos_token_id is not None:
                generation["eos_token_id"] = eos_token_id
            if pad_token_id is None:
                pad_token_id = eos_token_id
            if pad_token_id is not None:
                generation["pad_token_id"] = pad_token_id

            torch.manual_seed(0)
            cuda = getattr(torch, "cuda", None)
            if cuda is not None and hasattr(cuda, "manual_seed_all"):
                cuda.manual_seed_all(0)
            self.progress.detail(
                f"Generation {generation_id}: starting {schema_name}; "
                f"prompt_tokens={prompt_tokens}; reserved_output_tokens={max_output_tokens}; "
                f"operational_context_tokens={budget.effective_context_tokens}; "
                f"model_context_tokens={budget.context_tokens}"
            )
            event = self.performance.begin_generation(
                user=user, prompt_tokens=prompt_tokens, output_limit=max_output_tokens,
            )
            if self.performance.warning_due():
                self.progress.basic("Application has exceeded ten minutes; checkpoints retained.")
            event.prompt_components = dict(self._last_prompt_components)
            heartbeat.start()
            try:
                with self.performance.phase("generation"), torch.inference_mode():
                    output = model.generate(**device_inputs, **generation)
            except Exception as exc:
                event.outcome = "runtime_failure"
                self.progress.detail(
                    f"Generation {generation_id}: failed; error_type={type(exc).__name__}"
                )
                raise
            finally:
                generated_so_far, elapsed = heartbeat.finish()
                event.generation_seconds = elapsed
                event.generated_tokens = generated_so_far
                event.first_token_seconds = heartbeat.first_token_seconds
                first, last = heartbeat.first_token_seconds, heartbeat.last_token_seconds
                if first is not None and last is not None and last > first and generated_so_far > 1:
                    event.decode_seconds = last - first
                    event.decode_tokens_per_second = (generated_so_far - 1) / (last - first)
            sequence = output[0]
            if len(sequence) < prompt_tokens:
                raise QwenOutputError("Local model returned an invalid token sequence")
            generated_tokens = len(sequence) - prompt_tokens
            event.generated_tokens = generated_tokens
            event.hit_output_limit = generated_tokens >= max_output_tokens
            rate = generated_tokens / elapsed if elapsed > 0 else 0.0
            self.progress.detail(
                f"Generation {generation_id}: complete; generated_tokens={generated_tokens}; "
                f"elapsed={elapsed:.1f}s; speed={rate:.2f} tokens/s"
            )
            decoded = tokenizer.decode(
                sequence[prompt_tokens:], skip_special_tokens=True
            )
            if self.benchmark_canary is not None:
                event.secret_canary_detected = self.benchmark_canary in decoded
            text, normalization = _restore_prefilled_json(decoded)
            try:
                with self.performance.phase("validation"):
                    parsed = parse_json_object(text)
                    if self.collect_response_shape:
                        event.response_shape = _response_shape(parsed, schema, schema_name, user)
                    if self.benchmark_canary is not None:
                        event.secret_canary_detected = bool(event.secret_canary_detected) or (
                            self.benchmark_canary in json.dumps(parsed, ensure_ascii=False)
                        )
                event.outcome = "json_valid"
                return parsed
            except QwenOutputError as exc:
                event.outcome = "invalid"
                hit_output_limit = generated_tokens >= max_output_tokens
                if hit_output_limit:
                    self.performance.failures["truncation"] += 1
                self.progress.detail(
                    f"Generation {generation_id}: invalid structured output; "
                    f"hit_output_limit={str(hit_output_limit).lower()}; "
                    f"normalization={normalization}"
                )
                if hit_output_limit:
                    raise QwenOutputError(
                        "Local model returned malformed JSON after reaching the output token limit"
                    ) from exc
                raise
        except (QwenPromptBudgetError, QwenOutputError):
            raise
        except Exception as exc:
            raise QwenProviderError("Local Qwen inference failed") from exc

    @measured("tokenization")
    def _prepare_prompt(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: Mapping[str, Any],
        max_output_tokens: int,
    ) -> tuple[Any, int, PromptBudget]:
        if not _SCHEMA_NAME.fullmatch(schema_name):
            raise ValueError("schema_name must be a short identifier")
        if (
            isinstance(max_output_tokens, bool)
            or not isinstance(max_output_tokens, int)
            or max_output_tokens < 1
        ):
            raise ValueError("max_output_tokens must be a positive integer")
        try:
            schema_text = json.dumps(
                compact_schema(schema) if self.experiment.compact_schema else dict(schema),
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("schema must be JSON serializable") from exc

        object_instruction = (
            "Return exactly one JSON object matching this JSON Schema. "
            "Put the schema properties directly in the response object, with no enclosing key. "
            if self.experiment.clear_object else
            f"Return exactly one JSON object named {schema_name} matching this JSON Schema. "
        )
        system_message = (
            f"{system.rstrip()}\n"
            f"{object_instruction}"
            "The response is already prefixed with one opening brace. Continue with the first "
            "quoted member name and finish the same JSON object. Do not repeat the opening brace. "
            "Do not use Markdown, wrappers, or commentary.\n"
            f"{schema_text}"
        )
        if self.experiment.field_contract:
            system_message += response_contract(schema)
        if self.experiment.privacy_rule:
            system_message += PRIVACY_RULE
        prompt = _render_qwen_chatml(system_message, user)
        with self.performance.phase("loading"):
            self._load_tokenizer()
        tokenizer = self._tokenizer
        if tokenizer is None:
            raise QwenProviderError("Local Qwen provider did not initialize")

        try:
            with self.performance.phase("tokenization"):
                inputs = tokenizer(
                    prompt,
                    add_special_tokens=False,
                    return_tensors="pt",
                    truncation=False,
                )
            prompt_tokens = _input_token_count(inputs)
            self._last_prompt_components = (
                self._measure_components(system_message, schema_text, user, prompt_tokens)
                if self.performance.collect_prompt_components else {}
            )
            context_tokens = _context_window_tokens(tokenizer, SimpleNamespace(config=self._config))
            budget = PromptBudget(
                prompt_tokens=prompt_tokens,
                reserved_output_tokens=max_output_tokens,
                context_tokens=context_tokens,
                operational_context_tokens=_operational_context_tokens(schema_name),
            )
            return inputs, prompt_tokens, budget
        except Exception as exc:
            raise QwenProviderError("Local Qwen prompt measurement failed") from exc

    @measured("tokenization")
    def _measure_components(
        self, system_message: str, schema_text: str, user: str, total: int,
    ) -> dict[str, int]:
        """Independent component counts plus signed BPE/wrapper residual; no text retained."""
        parts = {
            "schema": schema_text,
            "instructions": system_message.removesuffix(schema_text),
            "source": "", "context": "", "identifiers_and_metadata": user,
        }
        try:
            payload = json.loads(user)
            if isinstance(payload, dict):
                definitions = payload.pop("definitions", None)
                context = payload.pop("authoritative_context", None)
                if context is None:
                    context_keys = {
                        "dependency_graph", "datasources", "connections", "tables", "queries",
                        "interactions", "terminal_evidence", "owner_claims", "coverage",
                        "logical_unit_interpretations", "application_interpretations",
                        "chunk_interpretations", "candidates", "profiles",
                    }
                    context = {
                        key: payload.pop(key) for key in sorted(context_keys) if key in payload
                    }

                def serialize(value: Any) -> str:
                    return json.dumps(
                        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                    )
                parts["source"] = serialize(definitions) if definitions is not None else ""
                parts["context"] = serialize(context) if context is not None else ""
                parts["identifiers_and_metadata"] = serialize(payload)
        except (ValueError, TypeError):
            pass
        counts = {
            key: _input_token_count(self._tokenizer(
                value, add_special_tokens=False, return_tensors="pt", truncation=False,
            )) if value else 0
            for key, value in parts.items()
        }
        counts["boundary_and_wrapper_residual"] = total - sum(counts.values())
        return counts

    def _load_tokenizer(self) -> None:
        if self._tokenizer is not None:
            return
        with self._load_lock:
            if self._tokenizer is not None:
                return
            _enable_offline_mode()
            try:
                transformers = importlib.import_module("transformers")
                config = json.loads(
                    (self.verified.directory / "config.json").read_text(encoding="utf-8")
                )
                self.progress.detail("Loading tokenizer...")
                tokenizer = transformers.AutoTokenizer.from_pretrained(
                    str(self.verified.directory), local_files_only=True, trust_remote_code=False,
                )
            except Exception as exc:
                raise QwenProviderError(
                    "Verified local tokenizer/config could not be loaded"
                ) from exc
            self._transformers = transformers
            self._config = SimpleNamespace(
                max_position_embeddings=config["max_position_embeddings"],
            )
            self._tokenizer = tokenizer

    def _load(self) -> None:
        self._load_tokenizer()
        if self._model is not None:
            return
        with self._load_lock:
            if self._model is not None:
                return
            _enable_offline_mode()
            try:
                torch = importlib.import_module("torch")
                transformers = importlib.import_module("transformers")
                auto_model = transformers.AutoModelForCausalLM
            except (ImportError, AttributeError) as exc:  # pragma: no cover - install guidance
                raise QwenProviderError(
                    "Local Qwen dependencies are not installed; install the semantic extra"
                ) from exc

            try:
                device = _select_device(self.runtime.device, torch)
                if device == "cpu":
                    torch.set_num_threads(self.runtime.cpu_threads)
                    with suppress(RuntimeError):
                        torch.set_num_interop_threads(self.runtime.cpu_interop_threads)
                local_path = str(self.verified.directory)
                load_started = time.monotonic()
                tokenizer = self._tokenizer
                weights_started = time.monotonic()
                self.progress.basic("Loading Qwen 0.5B model weights...")
                model = auto_model.from_pretrained(
                    local_path,
                    local_files_only=True,
                    trust_remote_code=False,
                    use_safetensors=True,
                    dtype="auto",
                )
                self.progress.detail(
                    f"Model weights loaded; elapsed={time.monotonic() - weights_started:.1f}s"
                )
                device_started = time.monotonic()
                self.progress.detail(f"Preparing model for {device} execution...")
                if device != "cpu":
                    self.progress.detail(f"Moving model to {device}...")
                    model.to(device)
                else:
                    self.progress.detail(
                        "Model loaded directly on CPU; redundant CPU transfer skipped"
                    )
                self.progress.detail(
                    f"Model device preparation complete for {device}; "
                    f"elapsed={time.monotonic() - device_started:.1f}s"
                )
                evaluation_started = time.monotonic()
                self.progress.detail("Enabling model evaluation mode...")
                model.eval()
                self.progress.detail(
                    "Model evaluation mode enabled; "
                    f"elapsed={time.monotonic() - evaluation_started:.1f}s"
                )
                self.progress.basic(
                    f"Qwen 0.5B model ready on {device}; "
                    f"elapsed={time.monotonic() - load_started:.1f}s"
                )
            except Exception as exc:
                raise QwenProviderError(
                    "The integrity-verified Qwen model could not be loaded locally"
                ) from exc

            self.performance.dtype = str(getattr(model, "dtype", "unknown"))
            self.performance.cpu_threads = (
                int(torch.get_num_threads()) if hasattr(torch, "get_num_threads") else None
            )
            self.performance.cpu_interop_threads = (
                int(torch.get_num_interop_threads())
                if hasattr(torch, "get_num_interop_threads") else None
            )
            self._torch = torch
            self._transformers = transformers
            self._tokenizer = tokenizer
            self._model = model
            self._device = device


def _provider_progress(provider: QwenJsonProvider) -> AnalysisProgressReporter:
    value = getattr(provider, "progress", None)
    return value if isinstance(value, AnalysisProgressReporter) else AnalysisProgressReporter()


def parse_json_object(value: str) -> dict[str, Any]:
    """Parse exactly one standards-compliant JSON object with unique member names."""

    text = value.strip()
    if not text:
        raise QwenOutputError("Local model returned empty structured output")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise QwenOutputError("Local model JSON contains duplicate object members")
            result[key] = item
        return result

    def reject_constant(_value: str) -> None:
        raise QwenOutputError("Local model JSON contains a non-finite number")

    try:
        parsed = json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise QwenOutputError("Local model returned malformed JSON") from exc
    if not isinstance(parsed, dict):
        raise QwenOutputError("Local model structured output must be one JSON object")
    return parsed


def _require_exact_qwen(verified: VerifiedModel) -> None:
    manifest = verified.manifest
    actual = (
        manifest.repo_id,
        manifest.revision,
        manifest.license,
        manifest.architecture,
        manifest.model_type,
    )
    expected = (
        QWEN_MODEL.repo_id,
        QWEN_MODEL.revision,
        QWEN_MODEL.license,
        QWEN_MODEL.architecture,
        QWEN_MODEL.model_type,
    )
    if actual != expected:
        raise ValueError("Local model manifest is not the approved fixed Qwen model")


def _enable_offline_mode() -> None:
    for name in (
        "HF_HUB_OFFLINE",
        "TRANSFORMERS_OFFLINE",
        "HF_DATASETS_OFFLINE",
        "HF_HUB_DISABLE_TELEMETRY",
        "DISABLE_TELEMETRY",
        "DO_NOT_TRACK",
    ):
        os.environ[name] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"


def _render_qwen_chatml(system: str, user: str) -> str:
    for message in (system, user):
        if any(token in message for token in _CHATML_CONTROL_TOKENS):
            raise QwenProviderError("Prompt content contains a reserved Qwen control token")
    return (
        f"<|im_start|>system\n{system}<|im_end|>\n"
        f"<|im_start|>user\n{user}<|im_end|>\n"
        f"<|im_start|>assistant\n{_JSON_RESPONSE_PREFIX}"
    )


def _restore_prefilled_json(value: str) -> tuple[str, str]:
    """Restore the fixed opening brace and unwrap only one exact Markdown fence."""

    stripped = value.strip()
    normalization = "prefilled_prefix"
    fenced = _MARKDOWN_JSON_FENCE.fullmatch(stripped)
    if fenced is not None:
        stripped = fenced.group("body").strip()
        normalization = "markdown_fence"
    if stripped.startswith(_JSON_RESPONSE_PREFIX):
        return stripped, f"{normalization}_repeated"
    return _JSON_RESPONSE_PREFIX + stripped, normalization


def _input_token_count(inputs: Any) -> int:
    try:
        shape = inputs["input_ids"].shape
        count = int(shape[-1])
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
        raise QwenProviderError("Tokenizer returned an invalid input shape") from exc
    if count < 1:
        raise QwenProviderError("Tokenizer returned an empty prompt")
    return count


def _context_window_tokens(tokenizer: Any, model: Any) -> int:
    candidates = [REVIEWED_CONTEXT_TOKENS]
    for value in (
        getattr(getattr(model, "config", None), "max_position_embeddings", None),
        getattr(tokenizer, "model_max_length", None),
    ):
        if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 1_000_000:
            candidates.append(value)
    return min(candidates)


def _operational_context_tokens(schema_name: str) -> int:
    if schema_name in {"ApplicationInterpretation", "PortfolioAnalysis"}:
        return SYNTHESIS_OPERATIONAL_CONTEXT_TOKENS
    return LOGICAL_UNIT_OPERATIONAL_CONTEXT_TOKENS


def _move_inputs(inputs: Any, device: str) -> Mapping[str, Any]:
    if hasattr(inputs, "to"):
        moved = inputs.to(device)
        if isinstance(moved, Mapping):
            return moved
    if not isinstance(inputs, Mapping):
        raise QwenProviderError("Tokenizer returned an invalid input batch")
    return {
        key: value.to(device) if hasattr(value, "to") else value
        for key, value in inputs.items()
    }


def _select_device(requested: str, torch: Any) -> str:
    if requested == "auto":
        if torch.cuda.is_available():
            return "cuda"
        mps = getattr(torch.backends, "mps", None)
        if mps is not None and mps.is_available():
            return "mps"
        return "cpu"
    if requested == "cuda" and not torch.cuda.is_available():
        raise QwenProviderError("CUDA was requested but is unavailable")
    if requested == "mps":
        mps = getattr(torch.backends, "mps", None)
        if mps is None or not mps.is_available():
            raise QwenProviderError("MPS was requested but is unavailable")
    return requested


def _validation_details(error: ValidationError) -> tuple[str, ...]:
    """Fixed categories only: Pydantic locations/messages may contain untrusted keys."""
    details: list[str] = []
    for item in error.errors(include_input=False, include_url=False, include_context=False):
        kind = item["type"]
        if kind == "missing":
            code = "missing_field"
        elif kind == "extra_forbidden":
            code = "extra_field"
        elif kind in {"literal_error", "enum"}:
            code = "invalid_enum"
        elif kind.endswith(("_type", "_parsing")):
            code = "wrong_type"
        else:
            # Match only code-owned exact messages; never export the original text.
            code = {
                "Value error, logical interpretation cites non-terminal or unknown evidence":
                    "unknown_citation",
                "Value error, logical interpretation cites an unknown technical object":
                    "unknown_object",
                "Value error, logical interpretation cites an unknown interaction":
                    "unknown_interaction",
                "Value error, application interpretation cites non-terminal or unknown evidence":
                    "unknown_citation",
                "Value error, application finding cites non-terminal or unknown evidence":
                    "unknown_citation",
                "Value error, application interpretation cites an unknown owner claim":
                    "unknown_citation",
                "Value error, application finding cites an unknown owner claim":
                    "unknown_citation",
                "Value error, portfolio similarity is not closed by one supplied candidate":
                    "unknown_citation",
                "Value error, portfolio finding is not closed by one supplied candidate":
                    "unknown_citation",
            }.get(item.get("msg", ""), "schema")
        details.append(code)
    return tuple(details) or ("schema",)


def _validation_field_details(
    error: ValidationError, schema: Mapping[str, Any],
) -> list[ValidationDiagnostic]:
    """Attribute errors to code-owned top-level fields, never to generated keys/values.

    Nested paths, dictionary keys, array indices, union labels, and unknown extra fields
    are intentionally not copied. Nested issues identify only their enclosing field.
    """
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        properties = {}
    counts: Counter[tuple[str, str]] = Counter()
    errors = error.errors(include_input=False, include_url=False, include_context=False)
    for item, category in zip(errors, _validation_details(error), strict=True):
        location = item.get("loc", ())
        field_name = "<root>" if not location else "<unknown>"
        if location:
            # Emit the schema key itself, never a string taken from rejected input.
            field_name = next((name for name in properties if name == location[0]), field_name)
        counts[category, field_name] += 1
    return [ValidationDiagnostic(category, name, count)
            for (category, name), count in sorted(counts.items())]


def _response_shape(
    candidate: Mapping[str, Any], schema: Mapping[str, Any], schema_name: str, user: str,
) -> dict[str, int | bool]:
    """Export only counts/flags distinguishing input copying from a named wrapper."""
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    allowed = set(properties) if isinstance(properties, dict) else set()
    required_keys = {key for key in required if isinstance(key, str)} if isinstance(
        required, list,
    ) else set()
    extras = set(candidate) - allowed
    try:
        inputs = json.loads(user)
    except (ValueError, RecursionError):
        inputs = None
    input_keys = set(inputs) if isinstance(inputs, dict) else set()
    return {
        "extra_keys": len(extras),
        "extra_keys_matching_input": len(extras & input_keys),
        "schema_name_wrapper": schema_name in extras and isinstance(candidate[schema_name], dict),
        "missing_required_keys": len(required_keys - set(candidate)),
    }
