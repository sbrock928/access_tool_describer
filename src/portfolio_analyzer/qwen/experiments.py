"""Versioned, benchmark-only inference candidates; production uses baseline."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InferenceExperiment:
    name: str
    clear_object: bool = False
    compact_schema: bool = False
    targeted_repair: bool = False
    stop_json: bool = False

    @property
    def identity(self) -> str:
        return f"inference-experiment-v1:{self.name}"


EXPERIMENTS = {
    item.name: item for item in (
        InferenceExperiment("baseline"),
        InferenceExperiment("clear-object", clear_object=True),
        InferenceExperiment("compact-schema", compact_schema=True),
        InferenceExperiment("targeted-repair", targeted_repair=True),
        InferenceExperiment("json-stop", stop_json=True),
        InferenceExperiment("combined", True, True, True, True),
    )
}


def compact_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Remove annotation overhead, preserving constraints, descriptions and property names.

    Traverse schema locations, not arbitrary JSON: a property or enum literal named
    'title' must never be mistaken for an annotation.
    """
    result: dict[str, Any] = {}
    maps = {"properties", "$defs", "definitions", "patternProperties", "dependentSchemas"}
    singles = {"items", "additionalProperties", "not", "if", "then", "else",
               "contains", "propertyNames", "unevaluatedProperties", "unevaluatedItems"}
    lists = {"allOf", "anyOf", "oneOf", "prefixItems"}
    for key, value in schema.items():
        if key in {"title", "examples", "$comment"}:
            continue
        if key in maps and isinstance(value, dict):
            result[key] = {name: compact_schema(child) if isinstance(child, dict) else child
                           for name, child in value.items()}
        elif key in singles and isinstance(value, dict):
            result[key] = compact_schema(value)
        elif key in lists and isinstance(value, list):
            result[key] = [compact_schema(child) if isinstance(child, dict) else child
                           for child in value]
        else:
            result[key] = value
    return result


def complete_object(text: str) -> bool:
    """Detect a structural end only; strict parsing/grounding still follows generation."""
    text = text.strip()
    if not text.startswith("{"):
        return False
    stack: list[str] = []
    quoted = escaped = False
    for index, character in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "{[":
            stack.append(character)
        elif character in "}]":
            if not stack or stack.pop() != ("{" if character == "}" else "["):
                return False
            if not stack:
                return index == len(text) - 1
    return False


class JsonObjectStop:
    """Batch-one stop probe; ephemeral decoding overhead is included in generation time.

    Decode the full generated suffix to avoid assumptions about tokenizer byte boundaries.
    Never strip trailing text to make invalid output pass validation.
    """

    def __init__(self, tokenizer: Any, prompt_tokens: int) -> None:
        self.tokenizer = tokenizer
        self.prompt_tokens = prompt_tokens

    def __call__(self, input_ids: Any, _scores: Any, **_kwargs: Any) -> bool:
        suffix = self.tokenizer.decode(
            input_ids[0][self.prompt_tokens:], skip_special_tokens=True,
        ).strip()
        text = suffix if suffix.startswith("{") else "{" + suffix
        return complete_object(text)
