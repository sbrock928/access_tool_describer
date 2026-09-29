"""Read a microbenchmark report without loading a model or displaying analyzed content."""

from __future__ import annotations

from pathlib import Path
from statistics import median
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from portfolio_analyzer.performance import FailureCode


class _SafeRecord(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")


class _Issue(_SafeRecord):
    category: FailureCode
    field: Literal["status", "summary", "evidence_ids", "<root>", "<unknown>"]
    count: int = Field(default=1, ge=1)


class _Generation(_SafeRecord):
    attempt: Literal[1, 2]
    generation_seconds: float = Field(ge=0, allow_inf_nan=False)
    outcome: Literal["interrupted", "runtime_failure", "json_valid", "invalid", "valid"]
    validation_issues: list[_Issue] | None = None
    prompt_tokens: int | None = Field(default=None, ge=0)
    generated_tokens: int | None = Field(default=None, ge=0)
    first_token_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    decode_tokens_per_second: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class _Metrics(_SafeRecord):
    generations: list[_Generation]
    elapsed_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    peak_working_set_bytes: int | None = Field(default=None, ge=0)


class _Run(_SafeRecord):
    warmup: bool
    case: int = Field(ge=1, le=3)
    repetition: int = Field(ge=0, le=10)
    schema_valid: bool
    secret_leaked: bool = False
    metrics: _Metrics


class _Worker(_SafeRecord):
    experiment: Literal["baseline", "clear-object", "compact-schema", "targeted-repair",
                        "json-stop", "combined"] = "baseline"
    threads_requested: int = Field(ge=1, le=4)
    suite: Literal["micro"] | None = None
    outcome: Literal["worker_failed"] | None = None
    runs: list[_Run] = Field(default_factory=list)


class _Matrix(_SafeRecord):
    schema_version: Literal["inference-benchmark-matrix-v1"]
    results: list[_Worker]


def summarize_micro_benchmark(path: Path) -> str:
    """Validate numeric/fixed-label data before rendering; ignore all other report keys."""
    report = _Matrix.model_validate_json(path.read_bytes())
    lines = [
        "Timed requests only; warm-up excluded. ValidRequests counts final schema outcomes.",
        "MedianGenSeconds includes initial and repair generation time per request.",
        "Threads Requests ValidRequests Repairs Leaks MedianGenSeconds",
    ]
    details = ["", "Per attempt (Field names are top-level schema properties):",
               "Threads Case Repetition Attempt Outcome Diagnostics"]
    multiple = any(worker.experiment != "baseline" for worker in report.results)
    for worker in report.results:
        if multiple:
            lines.append(f"Experiment: {worker.experiment}")
            details.append(f"Experiment: {worker.experiment}")
        if worker.outcome == "worker_failed":
            lines.append(f"{worker.threads_requested:7} worker_failed")
            continue
        if worker.suite != "micro" or not worker.runs:
            raise ValueError("expected microbenchmark worker results")
        runs = [run for run in worker.runs if not run.warmup]
        if not runs:
            raise ValueError("expected timed microbenchmark requests")
        repairs = sum(event.attempt == 2 for run in runs for event in run.metrics.generations)
        seconds = median(sum(event.generation_seconds for event in run.metrics.generations)
                         for run in runs)
        lines.append(
            f"{worker.threads_requested:7} {len(runs):8} "
            f"{sum(run.schema_valid for run in runs):13} {repairs:7} "
            f"{sum(run.secret_leaked for run in runs):5} {seconds:16.2f}"
        )
        events = [event for run in runs for event in run.metrics.generations]
        if any(run.metrics.elapsed_seconds is not None for run in runs):
            def number(values: list[int | float | None], operation: str) -> str:
                if not values or any(value is None for value in values):
                    return "unmeasured"
                known = [value for value in values if value is not None]
                value = sum(known) if operation == "sum" else (
                    max(known) if operation == "max" else median(known)
                )
                return f"{value:.2f}"

            lines.extend([
                f"Calls={len(events)} RetryPercent={100 * repairs / len(runs):.1f} "
                f"PromptTokens={number([e.prompt_tokens for e in events], 'sum')} "
                f"GeneratedTokens={number([e.generated_tokens for e in events], 'sum')}",
                "MedianRequestSeconds="
                + number([r.metrics.elapsed_seconds for r in runs], "median")
                + " MedianFirstTokenSeconds="
                + number([e.first_token_seconds for e in events], "median")
                + " MedianDecodeTokensPerSecond="
                + number([e.decode_tokens_per_second for e in events], "median")
                + " PeakWorkingSetBytes="
                + number([r.metrics.peak_working_set_bytes for r in runs], "max"),
            ])
        for run in runs:
            for event in run.metrics.generations:
                if event.validation_issues is None:
                    diagnostics = "not_recorded_in_older_report"
                else:
                    diagnostics = ", ".join(
                        f"{issue.category}:{issue.field}={issue.count}"
                        for issue in event.validation_issues
                    ) or "none"
                details.append(
                    f"{worker.threads_requested:7} {run.case:4} {run.repetition:10} "
                    f"{event.attempt:7} {event.outcome:7} {diagnostics}"
                )
    return "\n".join(lines + details)
