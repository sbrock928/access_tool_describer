from __future__ import annotations

import os
from collections.abc import Mapping
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict, model_validator

from portfolio_analyzer.qwen import (
    LocalQwenProvider,
    QwenOutputError,
    QwenPromptBudgetError,
    StructuredGenerationFailure,
    StructuredGenerationSuccess,
    generate_validated_json,
    parse_json_object,
)
from portfolio_analyzer.qwen import provider as qwen_provider
from portfolio_analyzer.runtime import ResolvedQwenRuntime
from portfolio_analyzer.semantic.model_store import (
    QWEN_MODEL,
    ModelManifest,
    VerifiedModel,
)


class _TokenIds:
    def __init__(self, values: list[int]) -> None:
        self.values = values
        self.shape = (1, len(values))

    def __len__(self) -> int:
        return len(self.values)

    def __getitem__(self, item: slice | int) -> list[int] | int:
        return self.values[item]

    def to(self, _device: str) -> _TokenIds:
        return self


class _Batch(dict[str, Any]):
    def to(self, _device: str) -> _Batch:
        return self


class _FakeTokenizer:
    eos_token_id = 151_645
    pad_token_id = None

    def __init__(self, responses: list[str], *, context_tokens: int) -> None:
        self.responses = responses
        self.model_max_length = context_tokens
        self.prompts: list[str] = []
        self.tokenize_kwargs: list[dict[str, Any]] = []

    def __call__(self, prompt: str, **kwargs: Any) -> _Batch:
        self.prompts.append(prompt)
        self.tokenize_kwargs.append(kwargs)
        return _Batch(input_ids=_TokenIds(list(range(len(prompt)))))

    def decode(self, _tokens: list[int], *, skip_special_tokens: bool) -> str:
        assert skip_special_tokens is True
        return self.responses.pop(0)

    def apply_chat_template(self, *_args: Any, **_kwargs: Any) -> None:
        pytest.fail("repository/tokenizer chat template must not be used")


class _FakeModel:
    def __init__(self, *, context_tokens: int) -> None:
        self.config = SimpleNamespace(max_position_embeddings=context_tokens)
        self.to_calls: list[str] = []
        self.eval_calls = 0
        self.generate_calls: list[dict[str, Any]] = []

    def to(self, device: str) -> None:
        self.to_calls.append(device)

    def eval(self) -> None:
        self.eval_calls += 1

    def generate(self, **kwargs: Any) -> list[list[int]]:
        self.generate_calls.append(kwargs)
        input_ids = kwargs["input_ids"]
        assert isinstance(input_ids, _TokenIds)
        return [input_ids.values + [999]]


class _FakeTorch:
    def __init__(self) -> None:
        self.cuda = SimpleNamespace(
            is_available=lambda: False,
            manual_seed_all=lambda seed: self.cuda_seeds.append(seed),
        )
        self.backends = SimpleNamespace(mps=SimpleNamespace(is_available=lambda: False))
        self.thread_calls: list[int] = []
        self.interop_calls: list[int] = []
        self.seeds: list[int] = []
        self.cuda_seeds: list[int] = []

    def set_num_threads(self, value: int) -> None:
        self.thread_calls.append(value)

    def set_num_interop_threads(self, value: int) -> None:
        self.interop_calls.append(value)

    def manual_seed(self, value: int) -> None:
        self.seeds.append(value)

    def inference_mode(self) -> Any:
        return nullcontext()


class _Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["answer", "abstain"]
    answer: str | None = None

    @model_validator(mode="after")
    def answer_required_when_not_abstaining(self) -> _Answer:
        if self.status == "answer" and not self.answer:
            raise ValueError("answer is required when status is answer")
        return self


class _FakeJsonProvider:
    def __init__(self, responses: list[dict[str, Any] | Exception]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: Mapping[str, Any],
        max_output_tokens: int = 1_024,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "system": system,
                "user": user,
                "schema_name": schema_name,
                "schema": schema,
                "max_output_tokens": max_output_tokens,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _verified_model(path: Path) -> VerifiedModel:
    return VerifiedModel(
        directory=path.resolve(),
        manifest=ModelManifest(
            repo_id=QWEN_MODEL.repo_id,
            revision=QWEN_MODEL.revision,
            license=QWEN_MODEL.license,
            architecture=QWEN_MODEL.architecture,
            model_type=QWEN_MODEL.model_type,
            acquired_at=datetime.now(UTC),
            files=[],
            manifest_sha256="a" * 64,
        ),
    )


def _runtime(path: Path) -> ResolvedQwenRuntime:
    return ResolvedQwenRuntime(
        model_dir=path,
        device="cpu",
        cpu_threads=3,
        cpu_interop_threads=1,
    )


def _install_fake_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    responses: list[str],
    context_tokens: int = 10_000,
) -> tuple[_FakeTokenizer, _FakeModel, _FakeTorch, dict[str, list[dict[str, Any]]]]:
    tokenizer = _FakeTokenizer(responses, context_tokens=context_tokens)
    model = _FakeModel(context_tokens=context_tokens)
    torch = _FakeTorch()
    loader_calls: dict[str, list[dict[str, Any]]] = {"tokenizer": [], "model": []}

    class TokenizerLoader:
        @classmethod
        def from_pretrained(cls, path: str, **kwargs: Any) -> _FakeTokenizer:
            loader_calls["tokenizer"].append({"path": path, **kwargs})
            return tokenizer

    class ModelLoader:
        @classmethod
        def from_pretrained(cls, path: str, **kwargs: Any) -> _FakeModel:
            loader_calls["model"].append({"path": path, **kwargs})
            return model

    transformers = SimpleNamespace(
        AutoTokenizer=TokenizerLoader,
        AutoModelForCausalLM=ModelLoader,
    )
    monkeypatch.setattr(
        qwen_provider,
        "verify_model_directory",
        lambda model_dir: _verified_model(model_dir),
    )
    monkeypatch.setattr(
        "portfolio_analyzer.qwen.provider.importlib.import_module",
        lambda name: torch if name == "torch" else transformers,
    )
    return tokenizer, model, torch, loader_calls


def test_local_qwen_is_fixed_offline_deterministic_and_loaded_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tokenizer, model, torch, loader_calls = _install_fake_runtime(
        monkeypatch,
        tmp_path,
        responses=['{"answer":"first"}', '{"answer":"second"}'],
    )

    provider = LocalQwenProvider(_runtime(tmp_path))
    first = provider.complete_json(
        system="Analyze evidence.",
        user='{"evidence_id":"ev-1"}',
        schema_name="Finding",
        schema={"type": "object"},
        max_output_tokens=32,
    )
    second = provider.complete_json(
        system="Analyze evidence.",
        user='{"evidence_id":"ev-2"}',
        schema_name="Finding",
        schema={"type": "object"},
        max_output_tokens=32,
    )

    assert first == {"answer": "first"}
    assert second == {"answer": "second"}
    assert len(loader_calls["tokenizer"]) == len(loader_calls["model"]) == 1
    tokenizer_load = loader_calls["tokenizer"][0]
    model_load = loader_calls["model"][0]
    assert tokenizer_load == {
        "path": str(tmp_path.resolve()),
        "local_files_only": True,
        "trust_remote_code": False,
    }
    assert model_load == {
        "path": str(tmp_path.resolve()),
        "local_files_only": True,
        "trust_remote_code": False,
        "use_safetensors": True,
        "dtype": "auto",
    }
    assert model.to_calls == ["cpu"]
    assert model.eval_calls == 1
    assert torch.thread_calls == [3]
    assert torch.interop_calls == [1]
    assert torch.seeds == [0, 0]
    assert torch.cuda_seeds == [0, 0]
    assert all(call["do_sample"] is False for call in model.generate_calls)
    assert all(call["num_beams"] == 1 for call in model.generate_calls)
    assert all(call["max_new_tokens"] == 32 for call in model.generate_calls)
    assert all(call["truncation"] is False for call in tokenizer.tokenize_kwargs)
    assert all(call["add_special_tokens"] is False for call in tokenizer.tokenize_kwargs)
    assert tokenizer.prompts[0].startswith("<|im_start|>system\nAnalyze evidence.")
    assert "<|im_start|>user\n{\"evidence_id\":\"ev-1\"}<|im_end|>" in tokenizer.prompts[0]
    assert tokenizer.prompts[0].endswith("<|im_start|>assistant\n")
    assert list(tmp_path.iterdir()) == []
    for name in (
        "HF_HUB_OFFLINE",
        "TRANSFORMERS_OFFLINE",
        "HF_DATASETS_OFFLINE",
        "HF_HUB_DISABLE_TELEMETRY",
        "DISABLE_TELEMETRY",
        "DO_NOT_TRACK",
    ):
        assert os.environ[name] == "1"


def test_local_qwen_rejects_non_qwen_verified_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified = _verified_model(tmp_path)
    wrong_manifest = verified.manifest.model_copy(update={"repo_id": "other/model"})
    monkeypatch.setattr(
        qwen_provider,
        "verify_model_directory",
        lambda _model_dir: verified.model_copy(update={"manifest": wrong_manifest}),
    )

    with pytest.raises(ValueError, match="approved fixed Qwen model"):
        LocalQwenProvider(_runtime(tmp_path))


def test_tokenizer_measured_budget_fails_without_truncating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tokenizer, model, _, _ = _install_fake_runtime(
        monkeypatch,
        tmp_path,
        responses=['{"unused":true}'],
        context_tokens=160,
    )
    provider = LocalQwenProvider(_runtime(tmp_path))

    with pytest.raises(QwenPromptBudgetError, match="split the logical unit"):
        provider.complete_json(
            system="Analyze all supplied evidence.",
            user="x" * 100,
            schema_name="Finding",
            schema={"type": "object", "properties": {"answer": {"type": "string"}}},
            max_output_tokens=32,
        )

    assert tokenizer.tokenize_kwargs == [
        {
            "add_special_tokens": False,
            "return_tensors": "pt",
            "truncation": False,
        }
    ]
    assert model.generate_calls == []


def test_provider_exposes_exact_non_generating_prompt_measurement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tokenizer, model, _, _ = _install_fake_runtime(
        monkeypatch,
        tmp_path,
        responses=[],
        context_tokens=10_000,
    )
    provider = LocalQwenProvider(_runtime(tmp_path))

    budget = provider.measure_prompt(
        system="Measure all supplied evidence.",
        user='{"evidence_id":"ev-1"}',
        schema_name="Finding",
        schema={"type": "object"},
        max_output_tokens=64,
    )

    assert budget.prompt_tokens == len(tokenizer.prompts[0])
    assert budget.reserved_output_tokens == 64
    assert budget.context_tokens == 10_000
    assert budget.fits is True
    assert model.generate_calls == []


@pytest.mark.parametrize(
    "value",
    [
        "[]",
        '```json\n{"answer":1}\n```',
        '{"answer":1} trailing',
        '{"answer":1,"answer":2}',
        '{"answer":NaN}',
    ],
)
def test_json_parser_rejects_anything_except_one_strict_object(value: str) -> None:
    with pytest.raises(QwenOutputError):
        parse_json_object(value)


def test_json_parser_accepts_one_object_with_surrounding_whitespace() -> None:
    assert parse_json_object(' \n {"answer":{"value":1}} \t') == {
        "answer": {"value": 1}
    }


def test_validation_helper_repairs_once_and_returns_typed_success() -> None:
    provider = _FakeJsonProvider(
        [
            {"status": "answer", "answer": None},
            {"status": "answer", "answer": "Grounded answer"},
        ]
    )

    result = generate_validated_json(
        provider,
        response_model=_Answer,
        system="Interpret evidence.",
        user='{"evidence_ids":["ev-1"]}',
        max_output_tokens=64,
    )

    assert isinstance(result, StructuredGenerationSuccess)
    assert result.attempts == 2
    assert result.value.answer == "Grounded answer"
    assert len(provider.calls) == 2
    assert "only repair attempt" in provider.calls[1]["system"]
    assert '"previous_response"' in provider.calls[1]["user"]
    assert provider.calls[1]["max_output_tokens"] == 64


def test_validation_helper_returns_explicit_failure_after_second_invalid_output() -> None:
    provider = _FakeJsonProvider(
        [
            {"unexpected": "FIRST-SECRET-VALUE"},
            {"unexpected": "SECOND-SECRET-VALUE"},
        ]
    )

    result = generate_validated_json(
        provider,
        response_model=_Answer,
        system="Interpret evidence.",
        user="evidence",
    )

    assert isinstance(result, StructuredGenerationFailure)
    assert result.code == "invalid_output"
    assert result.attempts == 2
    assert [issue.attempt for issue in result.issues] == [1, 2]
    assert all(issue.kind == "schema" for issue in result.issues)
    assert "FIRST-SECRET-VALUE" not in repr(result)
    assert "SECOND-SECRET-VALUE" not in repr(result)
    assert len(provider.calls) == 2


def test_validation_helper_accepts_schema_valid_abstention_without_retry() -> None:
    provider = _FakeJsonProvider([{"status": "abstain", "answer": None}])

    result = generate_validated_json(
        provider,
        response_model=_Answer,
        system="Interpret evidence.",
        user="insufficient evidence",
    )

    assert isinstance(result, StructuredGenerationSuccess)
    assert result.attempts == 1
    assert result.value.status == "abstain"
    assert len(provider.calls) == 1


def test_validation_helper_retries_strict_json_failure() -> None:
    provider = _FakeJsonProvider(
        [
            QwenOutputError("Local model returned malformed JSON"),
            {"status": "abstain", "answer": None},
        ]
    )

    result = generate_validated_json(
        provider,
        response_model=_Answer,
        system="Interpret evidence.",
        user="evidence",
    )

    assert isinstance(result, StructuredGenerationSuccess)
    assert result.attempts == 2
    assert len(provider.calls) == 2
