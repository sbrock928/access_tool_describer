"""Direct, offline semantic inference from one integrity-verified local model."""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
from typing import Any, Protocol

from portfolio_analyzer.semantic.config import SemanticSettings
from portfolio_analyzer.semantic.model_store import VerifiedModel, verify_model_directory


class SemanticProviderError(RuntimeError):
    pass


class SemanticProvider(Protocol):
    def health(self) -> dict[str, str | bool | None]: ...

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: dict[str, Any],
        max_output_tokens: int | None = None,
        require_full_input: bool = False,
    ) -> dict[str, Any]: ...


class LocalTransformersProvider:
    """Load the approved model once and run it in-process with offline-only APIs."""

    def __init__(self, settings: SemanticSettings) -> None:
        self.settings = settings
        self.verified: VerifiedModel = verify_model_directory(settings.model.local_path)
        self._tokenizer: Any = None
        self._model: Any = None
        self._torch: Any = None
        self._device: str | None = None
        _enable_offline_mode()

    def health(self) -> dict[str, str | bool | None]:
        self._load()
        manifest = self.verified.manifest
        return {
            "ready": True,
            "model_repo_id": manifest.repo_id,
            "model_revision": manifest.revision,
            "model_manifest_sha256": manifest.manifest_sha256,
            "model_architecture": manifest.architecture,
            "model_license": manifest.license,
            "local_model_identifier": self.verified.directory.name,
            "inference_library": "transformers",
            "inference_library_version": importlib.metadata.version("transformers"),
            "device": self._device,
            "offline": True,
        }

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: dict[str, Any],
        max_output_tokens: int | None = None,
        require_full_input: bool = False,
    ) -> dict[str, Any]:
        self._load()
        schema_text = json.dumps(schema, sort_keys=True, ensure_ascii=True)
        system_message = (
            f"{system}\nThe response must be exactly one JSON object named "
            f"{schema_name} matching this JSON Schema; do not use Markdown or commentary:\n"
            f"{schema_text}"
        )
        try:
            output_limit = min(
                max_output_tokens or self.settings.execution.max_output_tokens,
                self.settings.execution.max_output_tokens,
            )
            input_limit = max(
                1,
                self.settings.execution.context_tokens - output_limit,
            )
            inputs = self._bounded_inputs(
                system_message,
                user,
                input_limit,
                require_full_input=require_full_input,
            ).to(self._device)
            generation: dict[str, Any] = {
                "max_new_tokens": output_limit,
                "do_sample": self.settings.execution.temperature > 0,
                "pad_token_id": self._tokenizer.eos_token_id,
            }
            if self.settings.execution.temperature > 0:
                generation["temperature"] = self.settings.execution.temperature
            self._torch.manual_seed(0)
            with self._torch.inference_mode():
                output = self._model.generate(**inputs, **generation)
            prompt_length = int(inputs["input_ids"].shape[-1])
            text = self._tokenizer.decode(
                output[0][prompt_length:],
                skip_special_tokens=True,
            )
            return _parse_json_object(text)
        except SemanticProviderError:
            raise
        except Exception as exc:
            raise SemanticProviderError("Local model inference failed") from exc

    def _bounded_inputs(
        self,
        system: str,
        user: str,
        token_limit: int,
        *,
        require_full_input: bool = False,
    ) -> Any:
        """Fit untrusted data without allowing token truncation to remove its closing boundary."""
        if require_full_input and len(user) > self.settings.execution.max_profile_characters:
            raise SemanticProviderError(
                "Complete semantic input exceeds max_profile_characters; reduce the configured "
                "application IR/profile payload instead of truncating source data"
            )
        upper = min(len(user), self.settings.execution.max_profile_characters)

        def render(budget: int) -> Any:
            bounded_user = _bound_untrusted_message(user, budget)
            prompt = _granite_prompt(system, bounded_user)
            return self._tokenizer(prompt, return_tensors="pt")

        largest = render(upper)
        if int(largest["input_ids"].shape[-1]) <= token_limit:
            return largest
        if require_full_input:
            raise SemanticProviderError(
                "Complete semantic input exceeds the model token budget; reduce the configured "
                "application IR/profile payload instead of truncating source data"
            )
        upper -= 1
        lower = 0
        best: Any = None
        while lower <= upper:
            budget = (lower + upper) // 2
            candidate = render(budget)
            if int(candidate["input_ids"].shape[-1]) <= token_limit:
                best = candidate
                lower = budget + 1
            else:
                upper = budget - 1
        if best is None:
            raise SemanticProviderError(
                "System prompt and JSON schema exceed the input token budget"
            )
        return best

    def _load(self) -> None:
        if self._model is not None:
            return
        _enable_offline_mode()
        try:
            torch = importlib.import_module("torch")
            transformers = importlib.import_module("transformers")
            auto_model = transformers.AutoModelForCausalLM
            auto_tokenizer = transformers.AutoTokenizer
        except (ImportError, AttributeError) as exc:  # pragma: no cover - dependency guidance
            raise SemanticProviderError(
                "Semantic inference dependencies are not installed. Run "
                "'python -m pip install -e \".[semantic]\"'."
            ) from exc
        local_path = str(self.verified.directory)
        try:
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
            device = _select_device(self.settings.execution.device, torch)
            model.to(device)
            model.eval()
        except Exception as exc:
            raise SemanticProviderError(
                "The integrity-verified approved model could not be loaded locally"
            ) from exc
        self._torch = torch
        self._tokenizer = tokenizer
        self._model = model
        self._device = device


def _enable_offline_mode() -> None:
    # These are deliberately process-wide. Inference has no network-enabled fallback.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_DATASETS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["DISABLE_TELEMETRY"] = "1"


def _select_device(requested: str, torch: Any) -> str:
    if requested == "auto":
        if torch.cuda.is_available():
            return "cuda"
        mps = getattr(torch.backends, "mps", None)
        if mps is not None and mps.is_available():
            return "mps"
        return "cpu"
    if requested == "cuda" and not torch.cuda.is_available():
        raise SemanticProviderError("CUDA was requested but is unavailable")
    if requested == "mps":
        mps = getattr(torch.backends, "mps", None)
        if mps is None or not mps.is_available():
            raise SemanticProviderError("MPS was requested but is unavailable")
    return requested


def _parse_json_object(value: str) -> dict[str, Any]:
    text = value.strip()
    if text.startswith("<response>") and text.endswith("</response>"):
        text = text[len("<response>") : -len("</response>")].strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[len("```json") : -len("```")].strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SemanticProviderError("Local model returned malformed structured output") from exc
    if not isinstance(parsed, dict):
        raise SemanticProviderError("Local model structured output must be a JSON object")
    return parsed


def _bound_untrusted_message(value: str, budget: int) -> str:
    if len(value) <= budget:
        return value
    opening = "<UNTRUSTED_SOURCE_DATA>\n"
    closing = "\n</UNTRUSTED_SOURCE_DATA>"
    if value.startswith(opening) and value.endswith(closing):
        fixed = opening + '{"truncated":true,"source_prefix":""}' + closing
        if budget <= len(fixed):
            return opening + "{}" + closing
        inner = value[len(opening) : -len(closing)]
        available = max(0, budget - len(fixed) - 12)
        payload = json.dumps(
            {"truncated": True, "source_prefix": inner[:available]}, ensure_ascii=True
        )
        return opening + payload + closing
    return value[: max(0, budget)]


def _granite_prompt(system: str, user: str) -> str:
    """Use the reviewed Granite role format without executing a repository chat template."""
    return (
        f"<|start_of_role|>system<|end_of_role|>{system}<|end_of_text|>\n"
        f"<|start_of_role|>user<|end_of_role|>{user}<|end_of_text|>\n"
        "<|start_of_role|>assistant<|end_of_role|>"
    )
