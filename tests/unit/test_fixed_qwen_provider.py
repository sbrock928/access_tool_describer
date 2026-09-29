from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict, model_validator

from portfolio_analyzer.progress import AnalysisProgressReporter, GenerationHeartbeat
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
    _legacy_manifest_digest,
    _manifest_digest,
    _validated_manifest_digest,
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
    def __init__(self, *, context_tokens: int, generated_token_count: int = 1) -> None:
        self.config = SimpleNamespace(max_position_embeddings=context_tokens)
        self.generated_token_count = generated_token_count
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
        return [input_ids.values + [999] * self.generated_token_count]


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


def test_previous_downloader_manifest_digest_is_recognized_and_canonicalized() -> None:
    manifest = _verified_model(Path("model")).manifest
    canonical = _manifest_digest(manifest)
    manifest.manifest_sha256 = _legacy_manifest_digest(manifest)

    assert _validated_manifest_digest(manifest) == canonical


def test_approved_model_is_the_pinned_qwen_half_billion_manifest() -> None:
    assert QWEN_MODEL.repo_id == "Qwen/Qwen2.5-0.5B-Instruct"
    assert QWEN_MODEL.revision == "7ae557604adf67be50417f59c2c2f167def9a775"
    weights = QWEN_MODEL.artifacts_by_path["model.safetensors"]
    assert weights.size_bytes == 988_097_824
    assert weights.sha256 == (
        "fdf756fa7fcbe7404d5c60e26bff1a0c8b8aa1f72ced49e7dd0210fe288fb7fe"
    )


def test_unrecognized_model_manifest_digest_still_fails_closed() -> None:
    manifest = _verified_model(Path("model")).manifest
    manifest.manifest_sha256 = "b" * 64

    with pytest.raises(ValueError, match="manifest hash mismatch"):
        _validated_manifest_digest(manifest)


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
    generated_token_count: int = 1,
) -> tuple[_FakeTokenizer, _FakeModel, _FakeTorch, dict[str, list[dict[str, Any]]]]:
    (tmp_path / "config.json").write_text(
        json.dumps({"max_position_embeddings": context_tokens}), encoding="utf-8",
    )
    tokenizer = _FakeTokenizer(responses, context_tokens=context_tokens)
    model = _FakeModel(
        context_tokens=context_tokens,
        generated_token_count=generated_token_count,
    )
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
    assert model.to_calls == []
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
    assert tokenizer.prompts[0].endswith("<|im_start|>assistant\n{")
    assert "already prefixed with one opening brace" in tokenizer.prompts[0]
    assert [item.name for item in tmp_path.iterdir()] == ["config.json"]
    for name in (
        "HF_HUB_OFFLINE",
        "TRANSFORMERS_OFFLINE",
        "HF_DATASETS_OFFLINE",
        "HF_HUB_DISABLE_TELEMETRY",
        "DISABLE_TELEMETRY",
        "DO_NOT_TRACK",
    ):
        assert os.environ[name] == "1"


def test_verbose_provider_progress_is_content_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_runtime(
        monkeypatch,
        tmp_path,
        responses=['{"answer":"SECRET output"}'],
    )
    messages: list[str] = []
    provider = LocalQwenProvider(
        _runtime(tmp_path),
        progress=AnalysisProgressReporter(verbose=True, sink=messages.append),
    )

    provider.complete_json(
        system="Analyze without exposing PWD=SECRET.",
        user='{"private_definition":"SECRET input"}',
        schema_name="Finding",
        schema={"type": "object"},
        max_output_tokens=32,
    )

    combined = "\n".join(messages)
    assert "Loading tokenizer" in combined
    assert "model weights" in combined
    assert "Generation 1: starting Finding" in combined
    assert "Generation 1: complete" in combined
    assert "SECRET" not in combined
    assert str(tmp_path) not in combined


def test_verbose_provider_reports_safe_output_limit_diagnosis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_runtime(
        monkeypatch,
        tmp_path,
        responses=["x" * 40],
        generated_token_count=8,
    )
    messages: list[str] = []
    provider = LocalQwenProvider(
        _runtime(tmp_path),
        progress=AnalysisProgressReporter(verbose=True, sink=messages.append),
    )

    with pytest.raises(QwenOutputError, match="output token limit"):
        provider.complete_json(
            system="Return JSON.",
            user="evidence",
            schema_name="Finding",
            schema={"type": "object"},
            max_output_tokens=8,
        )

    combined = "\n".join(messages)
    assert "invalid structured output" in combined
    assert "hit_output_limit=true" in combined
    assert "x" * 8 not in combined


def test_generation_heartbeat_reports_and_stops_cleanly() -> None:
    messages: list[str] = []
    heartbeat = GenerationHeartbeat(
        AnalysisProgressReporter(verbose=True, sink=messages.append),
        generation_id=7,
        interval_seconds=0.01,
    )

    heartbeat.start()
    heartbeat.update(4)
    time.sleep(0.025)
    generated, elapsed = heartbeat.finish()
    message_count = len(messages)
    time.sleep(0.02)

    assert generated == 4
    assert elapsed > 0
    assert any("Generation 7: running" in item for item in messages)
    assert len(messages) == message_count


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
    assert budget.operational_context_tokens == 8_192
    assert budget.effective_context_tokens == 8_192
    assert budget.fits is True
    assert model.generate_calls == []


def test_logical_unit_operational_ceiling_blocks_cpu_hostile_prefill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tokenizer, model, _, _ = _install_fake_runtime(
        monkeypatch,
        tmp_path,
        responses=['{"ok":true}'],
        context_tokens=32_768,
    )
    provider = LocalQwenProvider(_runtime(tmp_path))
    large_user_payload = "x" * 8_500

    with pytest.raises(QwenPromptBudgetError, match="operational token budget"):
        provider.complete_json(
            system="Analyze all supplied evidence.",
            user=large_user_payload,
            schema_name="LogicalUnitInterpretation",
            schema={"type": "object"},
            max_output_tokens=64,
        )

    assert model.generate_calls == []
    synthesis_budget = provider.measure_prompt(
        system="Synthesize all supplied interpretations.",
        user=large_user_payload,
        schema_name="ApplicationInterpretation",
        schema={"type": "object"},
        max_output_tokens=64,
    )
    assert synthesis_budget.context_tokens == 32_768
    assert synthesis_budget.operational_context_tokens == 16_384
    assert synthesis_budget.fits
    assert tokenizer.tokenize_kwargs


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


def test_provider_accepts_prefilled_and_exact_fenced_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_runtime(
        monkeypatch,
        tmp_path,
        responses=[
            '"answer":"prefilled"}',
            '```json\n"answer":"fenced"}\n```',
        ],
    )
    provider = LocalQwenProvider(_runtime(tmp_path))

    first = provider.complete_json(
        system="Return JSON.",
        user="evidence-one",
        schema_name="Finding",
        schema={"type": "object"},
        max_output_tokens=32,
    )
    second = provider.complete_json(
        system="Return JSON.",
        user="evidence-two",
        schema_name="Finding",
        schema={"type": "object"},
        max_output_tokens=32,
    )

    assert first == {"answer": "prefilled"}
    assert second == {"answer": "fenced"}


def test_provider_rejects_prose_wrapped_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_runtime(
        monkeypatch,
        tmp_path,
        responses=['Here is the result: {"answer":"unsafe wrapper"}'],
    )
    provider = LocalQwenProvider(_runtime(tmp_path))

    with pytest.raises(QwenOutputError, match="malformed JSON"):
        provider.complete_json(
            system="Return JSON.",
            user="evidence",
            schema_name="Finding",
            schema={"type": "object"},
            max_output_tokens=32,
        )


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
        schema_name="Answer",
        max_output_tokens=64,
    )

    assert isinstance(result, StructuredGenerationSuccess)
    assert result.attempts == 2
    assert result.value.answer == "Grounded answer"
    assert len(provider.calls) == 2
    assert "only repair attempt" in provider.calls[1]["system"]
    assert provider.calls[1]["user"] == provider.calls[0]["user"]
    assert "previous_response" not in provider.calls[1]["user"]
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


def test_local_provider_repairs_malformed_json_without_replaying_model_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages: list[str] = []
    tokenizer, model, _torch, _loader_calls = _install_fake_runtime(
        monkeypatch,
        tmp_path,
        responses=[
            "MALFORMED-MODEL-SECRET",
            '"status":"abstain","answer":null}',
        ],
    )
    provider = LocalQwenProvider(
        _runtime(tmp_path),
        progress=AnalysisProgressReporter(verbose=True, sink=messages.append),
    )

    result = generate_validated_json(
        provider,
        response_model=_Answer,
        system="Interpret evidence.",
        user='{"evidence_ids":["ev-1"]}',
        schema_name="Answer",
        max_output_tokens=64,
    )

    assert isinstance(result, StructuredGenerationSuccess)
    assert result.attempts == 2
    assert result.value.status == "abstain"
    assert len(model.generate_calls) == 2
    assert len(tokenizer.prompts) == 2
    assert all('{"evidence_ids":["ev-1"]}' in item for item in tokenizer.prompts)
    assert "MALFORMED-MODEL-SECRET" not in tokenizer.prompts[1]
    assert "MALFORMED-MODEL-SECRET" not in "\n".join(messages)


def test_prompt_measurement_never_loads_weights_or_configures_torch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, model, torch, calls = _install_fake_runtime(monkeypatch, tmp_path, responses=[])
    provider = LocalQwenProvider(_runtime(tmp_path))
    provider.measure_prompt(system="Measure", user="{}", schema_name="Finding", schema={})
    assert len(calls["tokenizer"]) == 1
    assert calls["model"] == []
    assert torch.thread_calls == []
    assert model.generate_calls == []
    assert provider._model is None


def test_first_token_and_decode_measurements_are_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from portfolio_analyzer.performance import PerformanceRecorder

    _, model, _, _ = _install_fake_runtime(
        monkeypatch, tmp_path, responses=['{"status":"abstain"}'],
    )
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    metrics = PerformanceRecorder(collect_prompt_components=True)
    provider = LocalQwenProvider(
        _runtime(tmp_path), progress=AnalysisProgressReporter(performance=metrics),
    )

    def generate(**kwargs: Any) -> list[list[int]]:
        inputs = kwargs["input_ids"].values
        criteria = kwargs["stopping_criteria"][0]
        for count, instant in enumerate((10.0, 12.0, 14.0), 1):
            clock[0] = instant
            criteria(SimpleNamespace(shape=(1, len(inputs) + count)), None)
        clock[0] = 15
        return [inputs + [999] * 3]

    monkeypatch.setattr(model, "generate", generate)
    result = generate_validated_json(
        provider, response_model=_Answer, schema_name="Answer",
        system="Analyze", user='{"stage":"logical_unit"}',
    )
    assert isinstance(result, StructuredGenerationSuccess)
    event = metrics.generations[0]
    assert event.first_token_seconds == 10
    assert event.decode_seconds == 4
    assert event.decode_tokens_per_second == 0.5
    assert event.generation_seconds == 15
    assert event.outcome == "valid"
    assert sum(event.prompt_components.values()) == event.prompt_tokens
    assert metrics.phases["generation"] == 15


def test_single_token_without_callback_has_no_fabricated_decode_rate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_runtime(monkeypatch, tmp_path, responses=['{"answer":"ok"}'])
    provider = LocalQwenProvider(_runtime(tmp_path))
    provider.complete_json(system="Analyze", user="{}", schema_name="Answer", schema={})
    event = provider.performance.generations[0]
    assert event.generated_tokens == 1
    assert event.first_token_seconds is None
    assert event.decode_tokens_per_second is None


def test_provider_counts_validation_repairs_without_exposing_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_runtime(monkeypatch, tmp_path, responses=[
        '{"status":"PRIVATE-INVALID-VALUE"}', '{"status":"abstain"}',
    ])
    provider = LocalQwenProvider(_runtime(tmp_path))
    result = generate_validated_json(
        provider, response_model=_Answer, schema_name="Answer",
        system="Analyze", user='{"stage":"logical_unit"}',
    )
    assert isinstance(result, StructuredGenerationSuccess)
    payload = provider.performance.payload()
    assert payload["validation_failures"] == {"invalid_enum": 1}
    assert payload["validation_retry_rate"] == 1
    assert [item["attempt"] for item in payload["generations"]] == [1, 2]
    assert "PRIVATE-INVALID-VALUE" not in json.dumps(payload)
