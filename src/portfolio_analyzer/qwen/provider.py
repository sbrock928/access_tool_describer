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
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ValidationError

from portfolio_analyzer.runtime import ResolvedQwenRuntime
from portfolio_analyzer.semantic.model_store import (
    QWEN_MODEL,
    VerifiedModel,
    verify_model_directory,
)

DEFAULT_MAX_OUTPUT_TOKENS = 1_024
_REVIEWED_CONTEXT_TOKENS = 32_768
_SCHEMA_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}\Z")
_CHATML_CONTROL_TOKENS = ("<|im_start|>", "<|im_end|>", "<|endoftext|>")


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

    @property
    def fits(self) -> bool:
        return self.prompt_tokens + self.reserved_output_tokens <= self.context_tokens


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
    current_user = user

    for attempt in (1, 2):
        typed_attempt: Literal[1, 2] = attempt
        try:
            candidate = provider.complete_json(
                system=current_system,
                user=current_user,
                schema_name=requested_schema_name,
                schema=output_schema,
                max_output_tokens=max_output_tokens,
            )
        except QwenOutputError as exc:
            issue = OutputValidationIssue(
                attempt=typed_attempt,
                kind="json",
                details=(str(exc),),
            )
            candidate = None
        else:
            try:
                validated = response_model.model_validate(candidate)
            except ValidationError as exc:
                issue = OutputValidationIssue(
                    attempt=typed_attempt,
                    kind="schema",
                    details=_validation_details(exc),
                )
            else:
                return StructuredGenerationSuccess(value=validated, attempts=typed_attempt)

        issues.append(issue)
        if attempt == 2:
            return StructuredGenerationFailure(issues=(issues[0], issues[1]))
        current_system = (
            f"{system.rstrip()}\n"
            "Your previous response was invalid. This is the only repair attempt. Return a "
            "complete replacement JSON object that matches the supplied schema exactly."
        )
        current_user = _repair_request(user, candidate, issue)

    raise AssertionError("the fixed two-attempt loop must return")  # pragma: no cover


class LocalQwenProvider:
    """Load the one approved Qwen model once and generate strict JSON objects offline."""

    def __init__(self, runtime: ResolvedQwenRuntime) -> None:
        self.runtime = runtime
        _enable_offline_mode()
        if runtime.device == "cpu":
            os.environ.setdefault("OMP_NUM_THREADS", str(runtime.cpu_threads))
            os.environ.setdefault("MKL_NUM_THREADS", str(runtime.cpu_threads))
        self.verified: VerifiedModel = verify_model_directory(runtime.model_dir)
        _require_exact_qwen(self.verified)
        self._load_lock = threading.Lock()
        self._tokenizer: Any = None
        self._model: Any = None
        self._torch: Any = None
        self._device: str | None = None

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

        inputs, prompt_tokens, budget = self._prepare_prompt(
            system=system,
            user=user,
            schema_name=schema_name,
            schema=schema,
            max_output_tokens=max_output_tokens,
        )
        if not budget.fits:
            raise QwenPromptBudgetError(
                "Complete prompt and reserved output exceed the model token budget "
                f"({budget.prompt_tokens} + {budget.reserved_output_tokens} > "
                f"{budget.context_tokens}); split the logical unit before inference"
            )
        tokenizer = self._tokenizer
        model = self._model
        torch = self._torch
        if tokenizer is None or model is None or torch is None or self._device is None:
            raise QwenProviderError("Local Qwen provider did not initialize")

        try:
            device_inputs = _move_inputs(inputs, self._device)
            generation: dict[str, Any] = {
                "max_new_tokens": max_output_tokens,
                "do_sample": False,
                "num_beams": 1,
                "use_cache": True,
            }
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
            with torch.inference_mode():
                output = model.generate(**device_inputs, **generation)
            sequence = output[0]
            if len(sequence) < prompt_tokens:
                raise QwenOutputError("Local model returned an invalid token sequence")
            text = tokenizer.decode(sequence[prompt_tokens:], skip_special_tokens=True)
            return parse_json_object(text)
        except (QwenPromptBudgetError, QwenOutputError):
            raise
        except Exception as exc:
            raise QwenProviderError("Local Qwen inference failed") from exc

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
                dict(schema),
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("schema must be JSON serializable") from exc

        system_message = (
            f"{system.rstrip()}\n"
            f"Return exactly one JSON object named {schema_name} matching this JSON Schema. "
            "Do not use Markdown, wrappers, or commentary.\n"
            f"{schema_text}"
        )
        prompt = _render_qwen_chatml(system_message, user)
        self._load()
        tokenizer = self._tokenizer
        model = self._model
        torch = self._torch
        if tokenizer is None or model is None or torch is None or self._device is None:
            raise QwenProviderError("Local Qwen provider did not initialize")

        try:
            inputs = tokenizer(
                prompt,
                add_special_tokens=False,
                return_tensors="pt",
                truncation=False,
            )
            prompt_tokens = _input_token_count(inputs)
            context_tokens = _context_window_tokens(tokenizer, model)
            budget = PromptBudget(
                prompt_tokens=prompt_tokens,
                reserved_output_tokens=max_output_tokens,
                context_tokens=context_tokens,
            )
            return inputs, prompt_tokens, budget
        except Exception as exc:
            raise QwenProviderError("Local Qwen prompt measurement failed") from exc

    def _load(self) -> None:
        if self._model is not None:
            return
        with self._load_lock:
            if self._model is not None:
                return
            _enable_offline_mode()
            try:
                torch = importlib.import_module("torch")
                transformers = importlib.import_module("transformers")
                auto_tokenizer = transformers.AutoTokenizer
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
                tokenizer = auto_tokenizer.from_pretrained(
                    local_path,
                    local_files_only=True,
                    trust_remote_code=False,
                )
                model = auto_model.from_pretrained(
                    local_path,
                    local_files_only=True,
                    trust_remote_code=False,
                    use_safetensors=True,
                    dtype="auto",
                )
                model.to(device)
                model.eval()
            except Exception as exc:
                raise QwenProviderError(
                    "The integrity-verified Qwen model could not be loaded locally"
                ) from exc

            self._torch = torch
            self._tokenizer = tokenizer
            self._model = model
            self._device = device


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
        "<|im_start|>assistant\n"
    )


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
    candidates = [_REVIEWED_CONTEXT_TOKENS]
    for value in (
        getattr(getattr(model, "config", None), "max_position_embeddings", None),
        getattr(tokenizer, "model_max_length", None),
    ):
        if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 1_000_000:
            candidates.append(value)
    return min(candidates)


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
    details: list[str] = []
    for item in error.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in item.get("loc", ())) or "<root>"
        details.append(f"{location}: {item['msg']} [{item['type']}]")
    return tuple(details) or ("response failed schema validation",)


def _repair_request(
    original_user: str,
    candidate: dict[str, Any] | None,
    issue: OutputValidationIssue,
) -> str:
    payload = {
        "original_request": original_user,
        "previous_response": candidate,
        "validation_issues": list(issue.details),
    }
    return json.dumps(payload, allow_nan=False, ensure_ascii=True, separators=(",", ":"))
