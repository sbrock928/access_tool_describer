from __future__ import annotations

from typing import Any

import pytest

from portfolio_analyzer.qwen.experiments import JsonObjectStop, compact_schema, complete_object
from portfolio_analyzer.qwen.provider import QwenOutputError, parse_json_object


def test_compact_schema_keeps_constraints_and_annotation_named_properties() -> None:
    schema = {
        "title": "Response", "type": "object", "additionalProperties": False,
        "required": ["title"],
        "properties": {"title": {"title": "Title", "type": "string", "minLength": 1,
                                 "description": "Required grounding", "examples": ["x"]},
                       "value": {"$ref": "#/$defs/Value"}},
        "$defs": {"Value": {"title": "Value", "enum": [{"title": "literal"}]}},
    }
    result = compact_schema(schema)
    assert "title" not in result
    assert result["properties"]["title"] == {
        "type": "string", "minLength": 1, "description": "Required grounding",
    }
    assert result["$defs"]["Value"]["enum"] == [{"title": "literal"}]
    assert result["required"] == ["title"]
    assert result["additionalProperties"] is False
    assert result["properties"]["value"] == {"$ref": "#/$defs/Value"}
    assert schema["title"] == "Response"


@pytest.mark.parametrize("text", [
    '{}', ' {"items":[{"a":"}"},[1,2]],"b":true} ',
    r'{"text":"escaped quote: \" and backslash: \\"}',
])
def test_stop_waits_until_complete_nested_and_escaped_object(text: str) -> None:
    assert complete_object(text)
    stripped = text.strip()
    assert all(not complete_object(stripped[:end]) for end in range(len(stripped)))


@pytest.mark.parametrize("text", [
    '', '[]', '{"x":[}', '{"x":"unfinished}', '{}{}', '{} trailing', '```json\n{}\n```',
])
def test_stop_never_crops_trailing_or_malformed_structure(text: str) -> None:
    assert not complete_object(text)


@pytest.mark.parametrize("text", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":}'])
def test_structural_stop_does_not_relax_strict_parser(text: str) -> None:
    assert complete_object(text)
    with pytest.raises(QwenOutputError):
        parse_json_object(text)


def test_stop_only_decodes_generated_suffix_and_retains_no_text() -> None:
    class Tokenizer:
        def decode(self, tokens: Any, *, skip_special_tokens: bool) -> str:
            assert tokens == [3, 4]
            assert skip_special_tokens
            return '"status":"unknown"}'

    probe = JsonObjectStop(Tokenizer(), 2)
    assert probe([[1, 2, 3, 4]], None)
    assert set(vars(probe)) == {"tokenizer", "prompt_tokens"}


def test_candidate_provenance_invalidates_cache_without_changing_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from portfolio_analyzer.cli import v2
    from portfolio_analyzer.qwen.experiments import EXPERIMENTS
    from portfolio_analyzer.qwen.pipeline import model_fingerprint

    monkeypatch.setattr(v2.importlib.metadata, "version", lambda _name: "test-runtime")
    provider = SimpleNamespace(verified=SimpleNamespace(
        manifest=SimpleNamespace(manifest_sha256="a" * 64),
    ))
    original = v2._model_provenance(provider)
    fingerprints = []
    for experiment in EXPERIMENTS.values():
        provider.experiment = experiment
        provenance = v2._model_provenance(provider)
        if experiment.name == "baseline":
            assert provenance == original
        reloaded = type(provenance).model_validate_json(provenance.model_dump_json())
        assert model_fingerprint(reloaded) == model_fingerprint(provenance)
        fingerprints.append(model_fingerprint(provenance))
    assert len(set(fingerprints)) == len(EXPERIMENTS)
