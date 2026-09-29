"""Isolated evaluation of pinned local extraction; never publish production analysis."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from portfolio_analyzer.analysis.bundle import build_application_evidence_bundle
from portfolio_analyzer.performance import InferenceCallLimitReached, PerformanceRecorder
from portfolio_analyzer.progress import AnalysisProgressReporter
from portfolio_analyzer.qwen.experiments import EXPERIMENTS
from portfolio_analyzer.qwen.pipeline import analyze_application_two_stage, build_logical_units
from portfolio_analyzer.qwen.provider import LocalQwenProvider
from portfolio_analyzer.runtime import ResolvedQwenRuntime
from portfolio_analyzer.v2.analysis_state import application_payload_from_result
from portfolio_analyzer.v2.identity import canonical_json_bytes, canonical_sha256
from portfolio_analyzer.v2.inference_cache import PersistentInferenceOutputCache
from portfolio_analyzer.v2.state import (
    RunPhase,
    RunStatus,
    V2StateStore,
    _atomic_write_bytes,
    initialize_workspace,
)
from portfolio_analyzer.v2.workflow import ExtractedArtifactSnapshot, StageIndex


def evaluate_applications(
    *, workspace: Path, evaluation_dir: Path, model_dir: Path, experiment: str,
    threads: int = 4, application: str | None = None, resume: bool = False,
    max_calls: int | None = None, progress: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any], int]:
    """Return metadata and exit status; detailed validated artifacts stay in evaluation_dir."""
    from portfolio_analyzer.cli.v2 import _model_provenance, _reference_by_schema

    workspace, evaluation_dir, model_dir = (
        path.resolve() for path in (workspace, evaluation_dir, model_dir)
    )
    if experiment not in {"baseline", "contract-private"} or threads not in {1, 2, 3, 4}:
        raise ValueError("unsupported application evaluation policy")
    if max_calls is not None and max_calls < 1:
        raise ValueError("invalid call ceiling")
    for protected in (workspace, model_dir):
        if evaluation_dir.is_relative_to(protected) or protected.is_relative_to(evaluation_dir):
            raise ValueError("evaluation directory must be separate from source and model")
    if evaluation_dir.exists() != resume:
        raise ValueError("use a new evaluation directory, or resume an existing evaluation")

    # Pin immutable manifests before inference. All source operations are reads.
    source = V2StateStore(workspace)
    stage = source.load_current_run(RunPhase.STAGE)
    stage_ref = _reference_by_schema(stage, "stage-index-v2")
    index = source.load(stage_ref, StageIndex)
    extraction = source.load_current_run(RunPhase.EXTRACT)
    records = {record.application_id: record for record in extraction.applications}
    if stage_ref not in extraction.input_references or set(records) != {
        item.application_id for item in index.applications
    }:
        raise ValueError("extraction does not match current staging")
    selected = [(ordinal, item) for ordinal, item in enumerate(index.applications, 1)
                if application is None or item.application_id == application]
    if not selected or any(records[item.application_id].status != RunStatus.COMPLETE
                           for _, item in selected):
        raise ValueError("selected applications require complete current extraction")

    metrics = PerformanceRecorder(max_calls_per_application=max_calls)
    source.performance = metrics
    provider = LocalQwenProvider(
        ResolvedQwenRuntime(model_dir=model_dir, device="cpu", cpu_threads=threads,
                            cpu_interop_threads=1),
        progress=AnalysisProgressReporter(performance=metrics),
    )
    provider.experiment = EXPERIMENTS[experiment]
    provenance = _model_provenance(provider)
    identity = {
        "version": "application-evaluation-v1",
        "source": canonical_sha256({"stage": stage, "extraction": extraction}),
        "applications": [ordinal for ordinal, _ in selected],
        "provenance": canonical_sha256(provenance), "experiment": experiment, "threads": threads,
    }
    if not resume:
        evaluation_dir.mkdir(parents=True, exist_ok=False)
        initialize_workspace(evaluation_dir)
    target = V2StateStore(evaluation_dir)
    target.performance = metrics
    local_records: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    exit_code = 0
    with target.exclusive_lock():
        identity_path = evaluation_dir / "evaluation-identity.json"
        if resume:
            if identity_path.read_bytes() != canonical_json_bytes(identity):
                raise ValueError("evaluation input or policy changed; use a new directory")
        else:
            _atomic_write_bytes(identity_path, canonical_json_bytes(identity))
        attempt = 1
        while (evaluation_dir / f"metrics-{attempt:04}.json").exists():
            attempt += 1
        cache = PersistentInferenceOutputCache(target.state_root, performance=metrics)
        try:
            for ordinal, item in selected:
                if progress is not None:
                    progress(f"Starting application {ordinal}; experiment={experiment}.")
                metrics.begin_application(ordinal)
                started = time.monotonic()
                row: dict[str, Any] = {"application": ordinal, "status": "pending"}
                rows.append(row)
                snapshots = tuple(source.load(ref, ExtractedArtifactSnapshot)
                                  for ref in records[item.application_id].extraction_snapshots)
                with metrics.phase("deterministic"):
                    bundle = build_application_evidence_bundle(
                        index, item.application_id, snapshots,
                    )
                    expected_units = {unit.logical_unit_id for unit in build_logical_units(bundle)}
                row["units_expected"] = len(expected_units)
                bundle_ref = target.put(bundle)
                result = analyze_application_two_stage(
                    bundle, provider, provenance=provenance, cache=cache,
                )
                payload = application_payload_from_result(result)
                returned_units = {unit.logical_unit_id for unit in payload.unit_results}
                if expected_units != returned_units:
                    raise ValueError("application evaluation has incomplete unit accounting")
                payload_ref = target.put(payload)
                row.update(
                    status=payload.status, units_completed=sum(
                        unit.status == "complete" for unit in payload.unit_results
                    ), units_abstained=sum(
                        unit.status == "abstained" for unit in payload.unit_results
                    ),
                    units_failed=sum(unit.status not in {"complete", "abstained"}
                                     for unit in payload.unit_results),
                    elapsed_seconds=time.monotonic() - started,
                )
                local_records.append({"application": ordinal, "application_id": item.application_id,
                                      "evidence": bundle_ref.model_dump(mode="json"),
                                      "analysis": payload_ref.model_dump(mode="json")})
                # Local review index contains identifiers and references, never raw generations.
                _atomic_write_bytes(
                    evaluation_dir / f"LOCAL-REVIEW-{attempt:04}.json", canonical_json_bytes({
                    "applications": local_records,
                    "notice": "LOCAL ONLY: contains application identifiers; do not export",
                    }),
                )
                metrics.finish_application()
                if progress is not None:
                    progress(f"Application {ordinal}: {payload.status}; "
                             f"elapsed={row['elapsed_seconds']:.1f}s.")
            metrics.outcome = "complete" if all(row["status"] == "complete" for row in rows) else (
                "failed"
            )
            exit_code = 0 if metrics.outcome == "complete" else 1
        except (KeyboardInterrupt, InferenceCallLimitReached) as exc:
            metrics.outcome = "interrupted" if isinstance(exc, KeyboardInterrupt) else "call_limit"
            exit_code = 130 if isinstance(exc, KeyboardInterrupt) else 2
        except Exception:
            # Raw exceptions may contain enterprise evidence; export only fixed status.
            metrics.outcome = "failed"
            exit_code = 1
        finally:
            metrics.finish_application()
            report = {
                "schema_version": "application-evaluation-metrics-v1",
                "experiment": experiment, "source_identity": identity["source"],
                "cache_scenario": "resumed" if resume else "cold",
                "threads": threads, "outcome": metrics.outcome,
                "applications_selected": len(selected), "applications": rows,
                "applications_remaining": len(selected) - sum(
                    row["status"] == "complete" for row in rows
                ),
                "quality_review": "required", "portfolio_synthesis": "not_evaluated",
                "extraction": "reused_current_snapshots", "metrics": metrics.payload(),
            }
            _atomic_write_bytes(evaluation_dir / f"metrics-{attempt:04}.json",
                                canonical_json_bytes(report))
    return report, exit_code
