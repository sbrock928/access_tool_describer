"""Atomic semantic state and review-decision persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from portfolio_analyzer.models import ReviewDecision, SemanticPortfolioState


def read_semantic_state(path: Path) -> SemanticPortfolioState | None:
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as source:
            raw = json.load(source)
        return SemanticPortfolioState.model_validate(raw)
    except (json.JSONDecodeError, ValidationError, KeyError, TypeError) as exc:
        raise ValueError(
            "semantic state uses an invalid or legacy schema (including the removed embedding "
            "format); preserve deterministic analysis and rerun 'semantic'"
        ) from exc


def write_semantic_state(path: Path, state: SemanticPortfolioState) -> None:
    _write_json_atomic(path, state.model_dump(mode="json"))


def read_review_decisions(path: Path) -> dict[str, ReviewDecision]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as source:
        raw = json.load(source)
    return {
        item.proposal_id: item
        for item in (ReviewDecision.model_validate(value) for value in raw.get("decisions", []))
    }


def write_review_decisions(path: Path, decisions: dict[str, ReviewDecision]) -> None:
    _write_json_atomic(
        path,
        {
            "decisions": [
                item.model_dump(mode="json")
                for item in sorted(decisions.values(), key=lambda decision: decision.proposal_id)
            ]
        },
    )


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as destination:
            json.dump(value, destination, indent=2, ensure_ascii=True)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
