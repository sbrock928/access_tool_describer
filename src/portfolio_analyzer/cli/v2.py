"""Single-path V2 command line workflow."""

from __future__ import annotations

import importlib.metadata
import json
import platform
import secrets
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import typer

from portfolio_analyzer.access.libraries import resolve_approved_libraries
from portfolio_analyzer.access.windows_extractor import WindowsAccessExtractor
from portfolio_analyzer.access.worker import run_extraction_with_timeout
from portfolio_analyzer.analysis.bundle import build_application_evidence_bundle
from portfolio_analyzer.config import AnalyzerSettings
from portfolio_analyzer.inventory.loader import load_inventory
from portfolio_analyzer.models import VerifiedStagedArtifact
from portfolio_analyzer.naming import euc_directory_name
from portfolio_analyzer.performance import InferenceCallLimitReached, PerformanceRecorder
from portfolio_analyzer.progress import AnalysisProgressReporter
from portfolio_analyzer.qwen.estimate import estimate_application
from portfolio_analyzer.qwen.pipeline import (
    TWO_STAGE_PROMPT_VERSION,
    analyze_application_two_stage,
    analyze_portfolio_candidates,
)
from portfolio_analyzer.qwen.provider import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    LOGICAL_UNIT_OPERATIONAL_CONTEXT_TOKENS,
    REVIEWED_CONTEXT_TOKENS,
    SYNTHESIS_OPERATIONAL_CONTEXT_TOKENS,
    LocalQwenProvider,
)
from portfolio_analyzer.redaction import redact_sensitive_text
from portfolio_analyzer.runtime import resolve_qwen_runtime
from portfolio_analyzer.semantic.model_store import (
    QWEN_MODEL,
    acquire_approved_model,
    verify_model_directory,
)
from portfolio_analyzer.staging.copying import ArtifactStager
from portfolio_analyzer.staging.hashing import sha256_file
from portfolio_analyzer.v2.analysis_state import (
    ApplicationAnalysisPayload,
    PortfolioAnalysisPayload,
    application_payload_from_result,
    portfolio_payload_from_result,
)
from portfolio_analyzer.v2.candidates import generate_portfolio_candidates
from portfolio_analyzer.v2.fingerprints import (
    analysis_fingerprint,
    evidence_bundle_fingerprint,
    interpretation_fingerprint,
)
from portfolio_analyzer.v2.inference_cache import PersistentInferenceOutputCache
from portfolio_analyzer.v2.models import (
    APPLICATION_EVIDENCE_SCHEMA_VERSION,
    APPLICATION_INTERPRETATION_SCHEMA_VERSION,
    LOGICAL_INTERPRETATION_SCHEMA_VERSION,
    PORTFOLIO_ANALYSIS_SCHEMA_VERSION,
    ApplicationEvidenceBundle,
    ApplicationInterpretation,
    KeyValueFact,
    ModelProvenance,
    OwnerClaim,
    PortfolioCandidate,
    PortfolioFinding,
    ReportOmission,
    ReportOmissionStage,
    ReportReviewRecord,
    ReportStatus,
    ReviewStatus,
    is_current_model_provenance,
)
from portfolio_analyzer.v2.owner_context import load_owner_context
from portfolio_analyzer.v2.quality import evaluate_quality
from portfolio_analyzer.v2.report_model import build_portfolio_report_model
from portfolio_analyzer.v2.reporting import publish_report_run
from portfolio_analyzer.v2.review import (
    REVIEW_OVERLAY_SCHEMA_VERSION,
    ReviewDecisionKind,
    ReviewOverlay,
    carry_forward_decisions,
    import_review_overlay,
)
from portfolio_analyzer.v2.staging import build_stage_index
from portfolio_analyzer.v2.state import (
    ApplicationRunRecord,
    ContentReference,
    RunManifest,
    RunPhase,
    RunStatus,
    StateIntegrityError,
    V2StateStore,
    initialize_workspace,
    preflight_workspace,
)
from portfolio_analyzer.v2.workflow import (
    EXTRACTION_POLICY_VERSION,
    ExtractedArtifactSnapshot,
    StagedApplication,
    StagedArtifactRecord,
    StageIndex,
    snapshot_from_extraction_result,
)

ACCESS_EXTRACTION_TIMEOUT_SECONDS = 300

app = typer.Typer(
    no_args_is_help=True,
    help="Evidence-first, offline Microsoft Access portfolio analysis.",
)


@app.command()
def stage(
    inventory: Path = typer.Option(..., exists=True, readable=True, dir_okay=False),
    workspace: Path = typer.Option(..., file_okay=False),
) -> None:
    """Copy eligible inventory-listed Access artifacts into a fresh V2 workspace."""

    inventory = inventory.resolve()
    workspace = workspace.resolve()
    try:
        initialize_workspace(workspace)
        settings = AnalyzerSettings(workspace=workspace)
        store = V2StateStore(workspace)
        with store.exclusive_lock():
            settings.ensure_workspace()
            inventory_hash = sha256_file(inventory)
            records = load_inventory(inventory)
            if sha256_file(inventory) != inventory_hash:
                raise ValueError("inventory changed while it was being read")
            owner_claims = load_owner_context(
                settings.source_inventory_dir / "owner_context.csv",
                application_ids={item.tool_inventory_id for item in records},
            )
            stager = ArtifactStager(settings)
            artifacts = [stager.stage_primary(record) for record in records]
            index = build_stage_index(
                workspace=workspace,
                inventory_sha256=inventory_hash,
                records=records,
                artifacts=artifacts,
                owner_claims=owner_claims,
            )
            index_ref = store.put(index)
            previous = _current_or_none(store, RunPhase.STAGE)
            manifest = _stage_manifest(index, index_ref, previous=previous)
            store.publish_run(manifest)
    except Exception as exc:
        _abort("Staging failed safely", exc)

    typer.echo(
        f"Staged {len(index.artifacts)} Access artifact(s) across "
        f"{len(index.applications)} application(s)."
    )
    typer.echo(f"Excluded {len(index.exclusions)} unsupported inventory row(s); copied zero bytes.")
    if index.failures:
        typer.echo(f"Failed to stage {len(index.failures)} required Access artifact(s).")
    typer.echo(f"Published V2 stage run {manifest.run_id}.")
    if not index.artifacts or index.failures:
        raise typer.Exit(code=1)


@app.command()
def extract(
    workspace: Path = typer.Option(..., file_okay=False),
    application: str | None = typer.Option(
        None, "--application", help="Extract one inventory application ID."
    ),
    force: bool = typer.Option(False, help="Re-extract selected current artifacts."),
) -> None:
    """Extract static Access metadata from verified staged copies only."""

    workspace = workspace.resolve()
    try:
        preflight_workspace(workspace)
        settings = AnalyzerSettings(workspace=workspace)
        store = V2StateStore(workspace)
        requested_failures: list[str] = []
        with store.exclusive_lock():
            settings.ensure_workspace()
            stage_run = store.load_current_run(RunPhase.STAGE)
            stage_ref = _reference_by_schema(stage_run, "stage-index-v2")
            index = store.load(stage_ref, StageIndex)
            application_ids = {item.application_id for item in index.applications}
            if application is not None and application not in application_ids:
                raise ValueError(f"application '{application}' is not in the current stage scope")
            previous = _current_or_none(store, RunPhase.EXTRACT)
            previous_records = _compatible_previous_extractions(store, previous, index)
            records: list[ApplicationRunRecord] = []
            stage_records = {item.application_id: item for item in stage_run.applications}
            artifacts_by_application: dict[str, list[StagedArtifactRecord]] = defaultdict(list)
            for artifact in index.artifacts:
                artifacts_by_application[artifact.application_id].append(artifact)

            for staged_application in index.applications:
                application_id = staged_application.application_id
                selected = application is None or application == application_id
                previous_record = previous_records.get(application_id)
                if not selected:
                    records.append(
                        previous_record
                        or ApplicationRunRecord(
                            application_id=application_id,
                            artifact_ids=staged_application.artifact_ids,
                            status=RunStatus.PARTIAL,
                            warnings=("not selected and no compatible prior extraction exists",),
                        )
                    )
                    continue

                stage_record = stage_records.get(application_id)
                stage_errors = (
                    tuple(stage_record.errors)
                    if stage_record is not None and stage_record.status == RunStatus.FAILED
                    else ()
                )
                snapshot_by_artifact = (
                    {}
                    if force or previous_record is None
                    else _snapshot_references_by_artifact(store, previous_record)
                )
                warnings: list[str] = []
                errors = list(stage_errors)
                for artifact_record in artifacts_by_application[application_id]:
                    if artifact_record.artifact_id in snapshot_by_artifact:
                        continue
                    if platform.system() != "Windows":
                        errors.append(
                            f"{artifact_record.filename}: Access extraction requires Windows"
                        )
                        continue
                    try:
                        extraction_artifact = _extraction_artifact(
                            workspace, artifact_record
                        )
                        destination = (
                            settings.extracted_dir
                            / euc_directory_name(staged_application.application_name)
                            / artifact_record.artifact_id
                        )
                        extracted = run_extraction_with_timeout(
                            extraction_artifact,
                            destination,
                            settings,
                            timeout_seconds=ACCESS_EXTRACTION_TIMEOUT_SECONDS,
                            on_progress=_progress_reporter(application_id),
                        )
                        snapshot = snapshot_from_extraction_result(
                            application_id=application_id,
                            artifact_id=artifact_record.artifact_id,
                            artifact_sha256=artifact_record.sha256,
                            staged_path=extraction_artifact.local_staged_path,
                            extracted=extracted,
                        )
                        snapshot_by_artifact[artifact_record.artifact_id] = store.put(snapshot)
                        warnings.extend(snapshot.warnings)
                    except Exception as exc:
                        safe_error = redact_sensitive_text(str(exc))
                        errors.append(f"{artifact_record.filename}: {safe_error}")

                expected = set(staged_application.artifact_ids)
                available = set(snapshot_by_artifact)
                if available != expected:
                    missing = sorted(expected - available)
                    errors.append(
                        "missing required extraction snapshots: " + ", ".join(missing)
                    )
                if errors:
                    status = RunStatus.FAILED
                    requested_failures.extend(
                        f"{application_id}: {item}" for item in errors
                    )
                else:
                    status = RunStatus.COMPLETE
                records.append(
                    ApplicationRunRecord(
                        application_id=application_id,
                        artifact_ids=staged_application.artifact_ids,
                        extraction_snapshots=tuple(snapshot_by_artifact.values()),
                        status=status,
                        warnings=tuple(warnings),
                        errors=tuple(errors),
                    )
                )

            run_status = _aggregate_run_status(records)
            manifest = RunManifest(
                run_id=_new_run_id(RunPhase.EXTRACT),
                phase=RunPhase.EXTRACT,
                status=run_status,
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
                parent_run_id=stage_run.run_id,
                input_references=(stage_ref,),
                applications=tuple(records),
                warnings=(
                    ("targeted extraction left other applications incomplete",)
                    if application is not None
                    and any(item.status != RunStatus.COMPLETE for item in records)
                    else ()
                ),
                errors=tuple(requested_failures),
            )
            store.publish_run(manifest)
    except typer.Exit:
        raise
    except Exception as exc:
        _abort("Extraction failed safely", exc)

    complete_count = sum(item.status == RunStatus.COMPLETE for item in manifest.applications)
    typer.echo(
        f"Published V2 extraction run {manifest.run_id}; "
        f"{complete_count}/{len(manifest.applications)} application(s) current."
    )
    if requested_failures:
        raise typer.Exit(code=1)


@app.command()
def analyze(
    workspace: Path = typer.Option(..., file_okay=False),
    application: str | None = typer.Option(
        None, "--application", help="Analyze one inventory application ID."
    ),
    force: bool = typer.Option(False, help="Re-run Qwen for selected current evidence."),
    model_dir: Path | None = typer.Option(
        None,
        "--model-dir",
        exists=True,
        file_okay=False,
        readable=True,
        help="Verified local Qwen directory (overrides workspace config and environment).",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Show safe token, cache, batching, retry, and timing diagnostics.",
    ),
    estimate: bool = typer.Option(False, help="Read-only request and cache cost estimate."),
    performance_report: Path | None = typer.Option(None, help="Metadata-only JSON diagnostics."),
    max_inference_calls_per_application: int | None = typer.Option(
        None, min=1, help="Stop safely before exceeding this call count, including repairs.",
    ),
) -> None:
    """Build canonical evidence, interpret every logical unit, and synthesize the portfolio."""

    workspace = workspace.resolve()
    if performance_report is not None and performance_report.exists():
        raise typer.BadParameter("Performance report destination must not already exist")
    metrics = PerformanceRecorder(
        max_calls_per_application=max_inference_calls_per_application,
        collect_prompt_components=performance_report is not None,
    )
    progress = AnalysisProgressReporter(
        verbose=verbose,
        sink=lambda message: typer.echo(message, err=True),
        performance=metrics,
    )
    try:
        progress.basic("Preparing analysis runtime...")
        preflight_workspace(workspace)
        store = V2StateStore(workspace)
        store.performance = metrics
        runtime = resolve_qwen_runtime(
            workspace,
            model_dir_override=model_dir.resolve() if model_dir is not None else None,
        )
        if performance_report is not None and (
            performance_report.resolve().is_relative_to(runtime.model_dir)
            or performance_report.resolve().is_relative_to(store.state_root)
        ):
            # Do not publish a diagnostics file into an integrity-controlled directory.
            performance_report = None
            raise ValueError("performance report must be outside model and state directories")
        # Construction performs the independent allowlist/hash verification required before
        # analysis. The provider then lazily loads this same verified model once for the run.
        provider = LocalQwenProvider(runtime, progress=progress)
        provenance = _model_provenance(provider)
        inference_cache = PersistentInferenceOutputCache(
            store.state_root, bypass_reads=force, performance=metrics,
        )
        if estimate:
            result_estimate = _estimate_workspace(
                store, provider, provenance, inference_cache, application,
            )
            typer.echo(json.dumps(result_estimate, sort_keys=True, indent=2))
            metrics.outcome = "estimated"
            return
        requested_failures: list[str] = []
        with store.exclusive_lock():
            stage_run = store.load_current_run(RunPhase.STAGE)
            stage_ref = _reference_by_schema(stage_run, "stage-index-v2")
            index = store.load(stage_ref, StageIndex)
            application_ids = {item.application_id for item in index.applications}
            if application is not None and application not in application_ids:
                raise ValueError(f"application '{application}' is not in the current stage scope")

            extract_run = store.load_current_run(RunPhase.EXTRACT)
            if stage_ref not in extract_run.input_references:
                raise StateIntegrityError(
                    "current extraction is stale for the current staged inventory"
                )
            extraction_by_application = {
                item.application_id: item for item in extract_run.applications
            }
            if set(extraction_by_application) != application_ids:
                raise StateIntegrityError(
                    "current extraction scope does not match the current staged inventory"
                )
            previous = _current_or_none(store, RunPhase.ANALYZE)
            previous_by_application = (
                {item.application_id: item for item in previous.applications}
                if previous is not None
                else {}
            )

            records: list[ApplicationRunRecord] = []
            bundles_by_application: dict[str, ApplicationEvidenceBundle] = {}
            analyses_by_application: dict[str, ApplicationAnalysisPayload] = {}
            total_applications = len(index.applications)
            for application_index, staged_application in enumerate(
                index.applications, start=1
            ):
                metrics.begin_application(application_index)
                application_id = staged_application.application_id
                selected = application is None or application == application_id
                application_started = time.monotonic()
                if selected:
                    progress.basic(
                        f"Starting application {application_index}/{total_applications}: "
                        f"{application_id}"
                    )
                extraction = extraction_by_application.get(application_id)
                if extraction is None or extraction.status != RunStatus.COMPLETE:
                    reasons = (
                        tuple(extraction.errors)
                        if extraction is not None and extraction.errors
                        else ("current extraction is unavailable or incomplete",)
                    )
                    status = RunStatus.FAILED if selected else RunStatus.PARTIAL
                    if selected:
                        requested_failures.extend(
                            f"{application_id}: {reason}" for reason in reasons
                        )
                    records.append(
                        ApplicationRunRecord(
                            application_id=application_id,
                            artifact_ids=staged_application.artifact_ids,
                            extraction_snapshots=(
                                extraction.extraction_snapshots
                                if extraction is not None
                                else ()
                            ),
                            status=status,
                            warnings=() if selected else reasons,
                            errors=reasons if selected else (),
                        )
                    )
                    if selected:
                        progress.basic(
                            f"Application {application_index}/{total_applications} failed "
                            "preflight: extraction unavailable"
                        )
                    continue

                snapshots = tuple(
                    store.load(reference, ExtractedArtifactSnapshot)
                    for reference in extraction.extraction_snapshots
                )
                try:
                    with metrics.phase("deterministic"):
                        bundle = build_application_evidence_bundle(
                            index, application_id, snapshots,
                        )
                    bundle_ref = store.put(bundle)
                    bundles_by_application[application_id] = bundle
                    progress.detail(
                        f"Application {application_id}: evidence ready; "
                        f"artifacts={len(bundle.artifacts)}; objects={len(bundle.objects)}; "
                        f"evidence={len(bundle.evidence)}"
                    )

                    previous_record = previous_by_application.get(application_id)
                    reused = (
                        None
                        if force and selected
                        else _compatible_previous_analysis(
                            store,
                            previous_record,
                            bundle,
                            provenance,
                            extraction.extraction_snapshots,
                        )
                    )
                    if not selected and reused is None:
                        records.append(
                            ApplicationRunRecord(
                                application_id=application_id,
                                artifact_ids=staged_application.artifact_ids,
                                extraction_snapshots=extraction.extraction_snapshots,
                                evidence_bundle=bundle_ref,
                                status=RunStatus.PARTIAL,
                                warnings=(
                                    "not selected and no compatible prior analysis exists",
                                ),
                            )
                        )
                        continue
                    if reused is not None:
                        reused_record, payload = reused
                        records.append(reused_record)
                        analyses_by_application[application_id] = payload
                        if selected:
                            reuse_elapsed = time.monotonic() - application_started
                            progress.basic(
                                f"Application {application_index}/{total_applications} reused "
                                f"current analysis; elapsed={reuse_elapsed:.1f}s"
                            )
                        continue

                    progress.basic(
                        f"Application {application_index}/{total_applications} "
                        "inference started"
                    )
                    result = analyze_application_two_stage(
                        bundle,
                        provider,
                        provenance=provenance,
                        cache=inference_cache,
                    )
                    payload = application_payload_from_result(result)
                    payload_ref = store.put(payload)
                    analyses_by_application[application_id] = payload
                    if payload.status == "complete" and payload.interpretation is not None:
                        status = RunStatus.COMPLETE
                        errors: tuple[str, ...] = ()
                    else:
                        status = RunStatus.FAILED
                        errors = (
                            payload.reason
                            or "one or more required Qwen logical units did not complete",
                        )
                        requested_failures.extend(
                            f"{application_id}: {reason}" for reason in errors
                        )
                    records.append(
                        ApplicationRunRecord(
                            application_id=application_id,
                            artifact_ids=staged_application.artifact_ids,
                            extraction_snapshots=extraction.extraction_snapshots,
                            evidence_bundle=bundle_ref,
                            interpretation=payload_ref,
                            status=status,
                            errors=errors,
                        )
                    )
                    progress.detail(
                        f"Application {application_index}/{total_applications} "
                        f"checkpoint written; status={status.value}"
                    )
                    progress.basic(
                        f"Application {application_index}/{total_applications} "
                        f"{status.value}; elapsed={time.monotonic() - application_started:.1f}s"
                    )
                except Exception as exc:
                    safe_error = redact_sensitive_text(str(exc))
                    requested_failures.append(f"{application_id}: {safe_error}")
                    records.append(
                        ApplicationRunRecord(
                            application_id=application_id,
                            artifact_ids=staged_application.artifact_ids,
                            extraction_snapshots=extraction.extraction_snapshots,
                            status=RunStatus.FAILED,
                            errors=(safe_error,),
                        )
                    )
                    if selected:
                        progress.basic(
                            f"Application {application_index}/{total_applications} failed; "
                            f"elapsed={time.monotonic() - application_started:.1f}s; "
                            f"error_type={type(exc).__name__}"
                        )

            metrics.begin_application(None)
            portfolio_ref: ContentReference | None = None
            portfolio_payload: PortfolioAnalysisPayload | None = None
            all_current = bool(records) and all(
                item.status == RunStatus.COMPLETE for item in records
            )
            portfolio_warning: tuple[str, ...] = ()
            if all_current:
                portfolio_started = time.monotonic()
                progress.basic("Starting portfolio candidate generation and synthesis...")
                ordered_bundles = tuple(
                    bundles_by_application[item.application_id] for item in records
                )
                profiles = tuple(
                    _required_profile(analyses_by_application[item.application_id])
                    for item in records
                )
                profile_hashes = tuple(
                    interpretation_fingerprint(profile) for profile in profiles
                )
                candidates: tuple[PortfolioCandidate, ...] = ()
                try:
                    with metrics.phase("deterministic"):
                        candidates = generate_portfolio_candidates(ordered_bundles, profiles)
                    progress.detail(
                        f"Portfolio candidates ready; count={len(candidates)}"
                    )
                    portfolio_result = analyze_portfolio_candidates(
                        candidates,
                        profiles,
                        ordered_bundles,
                        provider,
                        provenance=provenance,
                        cache=inference_cache,
                    )
                    portfolio_payload = portfolio_payload_from_result(
                        portfolio_result,
                        candidates=candidates,
                        application_profile_sha256s=profile_hashes,
                    )
                    progress.basic(
                        "Portfolio synthesis complete; "
                        f"elapsed={time.monotonic() - portfolio_started:.1f}s"
                    )
                except Exception as exc:
                    reason = "portfolio synthesis failed safely: " + redact_sensitive_text(
                        str(exc)
                    )
                    portfolio_payload = PortfolioAnalysisPayload(
                        status="incomplete",
                        candidates=candidates,
                        application_profile_sha256s=profile_hashes,
                        analysis=None,
                        batch_count=0,
                        reason=reason,
                    )
                    progress.basic(
                        "Portfolio synthesis failed; "
                        f"elapsed={time.monotonic() - portfolio_started:.1f}s; "
                        f"error_type={type(exc).__name__}"
                    )
                portfolio_ref = store.put(portfolio_payload)
                if portfolio_payload.status != "complete":
                    reason = portfolio_payload.reason or "portfolio synthesis did not complete"
                    requested_failures.append(f"portfolio: {reason}")
            else:
                portfolio_warning = (
                    "portfolio analysis invalidated because not every eligible application "
                    "is current",
                )

            status = _aggregate_run_status(records)
            if portfolio_ref is not None:
                portfolio_payload = store.load(portfolio_ref, PortfolioAnalysisPayload)
                if portfolio_payload.status != "complete":
                    status = RunStatus.FAILED
            input_references = (stage_ref,) + tuple(
                reference
                for record in extract_run.applications
                for reference in record.extraction_snapshots
            )
            previous_overlay_reference = (
                _review_overlay_reference_or_none(previous)
                if previous is not None
                else None
            )
            previous_overlay = (
                store.load(previous_overlay_reference, ReviewOverlay)
                if previous_overlay_reference is not None
                else None
            )
            if (
                previous_overlay is not None
                and previous is not None
                and _analysis_manifest_uses_current_model(store, previous)
                and status == RunStatus.COMPLETE
                and portfolio_payload is not None
                and portfolio_payload.analysis is not None
                and portfolio_payload.status == "complete"
            ):
                known_proposals = _known_review_proposals(
                    portfolio_payload.candidates,
                    portfolio_payload.analysis.findings,
                )
                carried = carry_forward_decisions(
                    previous_overlay,
                    known_proposals=known_proposals,
                )
                if carried:
                    current_fingerprint = analysis_fingerprint(
                        tuple(
                            bundles_by_application[item.application_id]
                            for item in records
                        ),
                        tuple(
                            _required_profile(analyses_by_application[item.application_id])
                            for item in records
                        ),
                        portfolio_payload.analysis,
                    )
                    carried_overlay = ReviewOverlay(
                        analysis_fingerprint=current_fingerprint,
                        imported_at=datetime.now(UTC),
                        decisions=carried,
                    )
                    input_references += (
                        store.put(
                            carried_overlay,
                            schema_name=REVIEW_OVERLAY_SCHEMA_VERSION,
                        ),
                    )
            elif (
                previous_overlay_reference is not None
                and status != RunStatus.COMPLETE
            ):
                # Keep the last reviewed decisions reachable across failed/partial runs.
                # They are dormant until a later complete analysis proves that the same
                # proposal and concrete evidence identities still exist.
                input_references += (previous_overlay_reference,)
            manifest = RunManifest(
                run_id=_new_run_id(RunPhase.ANALYZE),
                phase=RunPhase.ANALYZE,
                status=status,
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
                parent_run_id=extract_run.run_id,
                input_references=input_references,
                applications=tuple(records),
                portfolio_analysis=portfolio_ref,
                warnings=portfolio_warning,
                errors=tuple(requested_failures),
            )
            store.publish_run(manifest)
            progress.basic("Analysis run manifest published successfully.")
            metrics.outcome = "failed" if requested_failures else "complete"
    except (KeyboardInterrupt, InferenceCallLimitReached) as exc:
        metrics.outcome = "interrupted" if isinstance(exc, KeyboardInterrupt) else "call_limit"
        progress.basic(
            "Analysis stopped safely; completed inference checkpoints retained. "
            "Current application and remaining synthesis are incomplete. Resume without --force."
        )
        raise typer.Exit(code=130 if isinstance(exc, KeyboardInterrupt) else 2) from None
    except typer.Exit:
        raise
    except Exception as exc:
        metrics.outcome = "failed"
        _abort("Analysis failed safely", exc)
    finally:
        metrics.finish_application()
        if performance_report is not None:
            metrics.write(performance_report)

    complete_count = sum(item.status == RunStatus.COMPLETE for item in manifest.applications)
    typer.echo(
        f"Published V2 analysis run {manifest.run_id}; "
        f"{complete_count}/{len(manifest.applications)} application(s) current."
    )
    if manifest.portfolio_analysis is None:
        typer.echo("Portfolio analysis is not current.")
    if requested_failures:
        raise typer.Exit(code=1)


def _estimate_workspace(
    store: V2StateStore,
    provider: LocalQwenProvider,
    provenance: ModelProvenance,
    cache: PersistentInferenceOutputCache,
    application: str | None,
) -> dict[str, object]:
    """Read pinned immutable extraction references without acquiring a writer lock."""
    stage = store.load_current_run(RunPhase.STAGE)
    stage_ref = _reference_by_schema(stage, "stage-index-v2")
    index = store.load(stage_ref, StageIndex)
    extraction = store.load_current_run(RunPhase.EXTRACT)
    if stage_ref not in extraction.input_references:
        raise StateIntegrityError("current extraction is stale for the current stage")
    known = {item.application_id for item in index.applications}
    records = {item.application_id: item for item in extraction.applications}
    if set(records) != known:
        raise StateIntegrityError("current extraction scope does not match the current stage")
    if application is not None and application not in known:
        raise ValueError("selected application is not in the current stage")
    previous = _current_or_none(store, RunPhase.ANALYZE)
    previous_by_id = (
        {item.application_id: item for item in previous.applications} if previous else {}
    )
    applications: list[dict[str, object]] = []
    for ordinal, item in enumerate(index.applications, 1):
        if application is not None and item.application_id != application:
            continue
        record = records[item.application_id]
        if record.status != RunStatus.COMPLETE:
            applications.append({"application": ordinal, "status": "extraction_unavailable"})
            continue
        snapshots = tuple(
            store.load(reference, ExtractedArtifactSnapshot)
            for reference in record.extraction_snapshots
        )
        with provider.performance.phase("deterministic"):
            bundle = build_application_evidence_bundle(index, item.application_id, snapshots)
        estimated = estimate_application(
            bundle, provider, provenance=provenance, cache=cache, ordinal=ordinal,
        )
        reusable = None if cache.bypass_reads else _compatible_previous_analysis(
            store, previous_by_id.get(item.application_id), bundle, provenance,
            record.extraction_snapshots,
        )
        estimated["current_application_reusable"] = reusable is not None
        if reusable is not None:
            estimated["remaining_calls_min"] = 0
            estimated["remaining_calls_max_with_repairs"] = 0
        applications.append(estimated)
    return {
        "schema_version": "inference-estimate-v1", "application_count": len(applications),
        "applications": applications,
        "portfolio_calls": "output-dependent; excluded from application bounds",
        "expensive_applications": [
            item["application"] for item in sorted(
                applications,
                key=lambda value: int(str(value.get("cold_calls_max_with_repairs", 0))),
                reverse=True,
            )
        ],
    }


@app.command("evaluate-applications")
def evaluate_applications_command(
    workspace: Path = typer.Option(..., exists=True, file_okay=False),
    evaluation_dir: Path = typer.Option(..., help="Separate local evaluation directory."),
    model_dir: Path = typer.Option(..., exists=True, file_okay=False),
    experiment: str = typer.Option("contract-private", help="baseline or contract-private"),
    threads: int = typer.Option(4, min=1, max=4),
    application: str | None = typer.Option(None, help="Optional staged application ID."),
    resume: bool = typer.Option(False, help="Reuse this evaluation's own checkpoints."),
    check_only: bool = typer.Option(
        False, help="Check setup and model integrity without inference or evaluation writes.",
    ),
    max_inference_calls_per_application: int | None = typer.Option(None, min=1),
) -> None:
    """Evaluate extracted applications in isolation; keep detailed artifacts local."""
    from portfolio_analyzer.qwen.application_evaluation import (
        EvaluationSetupError,
        evaluate_applications,
    )

    typer.echo("Evaluating local extraction; detailed artifacts stay in the evaluation directory.")
    try:
        report, code = evaluate_applications(
            workspace=workspace, evaluation_dir=evaluation_dir, model_dir=model_dir,
            experiment=experiment, threads=threads, application=application, resume=resume,
            max_calls=max_inference_calls_per_application, progress=typer.echo,
            check_only=check_only,
        )
    except EvaluationSetupError as exc:
        typer.echo(f"Evaluation setup failed [{exc.code}]: {exc}")
        if exc.incomplete:
            typer.echo("LOCAL ONLY: incomplete application identifiers/names; do not export.")
            typer.echo("Ordinal | Application ID | Application name | Extraction status")
            for item in exc.incomplete:
                # Quote/escape control characters and redact credential patterns in inventory text.
                identifier = json.dumps(
                    redact_sensitive_text(item.application_id), ensure_ascii=True,
                )
                name = json.dumps(redact_sensitive_text(item.application_name), ensure_ascii=True)
                typer.echo(f"{item.ordinal} | {identifier} | {name} | {item.status.value}")
            typer.echo("Retry extraction for a listed ID with extract --workspace <workspace> "
                       "--application <ID>, or evaluate a fully extracted application.")
        raise typer.Exit(code=1) from None
    except KeyboardInterrupt:
        typer.echo("Evaluation interrupted before application processing.")
        raise typer.Exit(code=130) from None
    except Exception:
        typer.echo("Evaluation setup failed; check extraction, separate paths, policy and runtime. "
                   "No source analysis was published; no exception content displayed.")
        raise typer.Exit(code=1) from None
    if check_only:
        typer.echo(f"Preflight passed: {report['applications_selected']} application(s). "
                   "No inference or evaluation files written. Write permissions are untested.")
        return
    from portfolio_analyzer.qwen.benchmark_summary import _summarize_applications

    typer.echo(_summarize_applications(json.dumps(report).encode("utf-8")))
    typer.echo("Use benchmark-summary on the new metrics-NNNN.json file. "
               "LOCAL-REVIEW and state files contain application content and must stay local.")
    if code:
        raise typer.Exit(code=code)


@app.command("benchmark-summary")
def benchmark_summary(
    report_path: Path = typer.Option(..., "--input", exists=True, dir_okay=False, readable=True),
) -> None:
    """Summarize synthetic microbenchmarks or isolated application evaluation metrics."""
    from portfolio_analyzer.qwen.benchmark_summary import summarize_micro_benchmark

    try:
        summary = summarize_micro_benchmark(report_path)
    except (OSError, ValueError):
        typer.echo("Expected valid benchmark or application evaluation metrics; "
                   "no report contents displayed.")
        raise typer.Exit(code=1) from None
    typer.echo(summary)


@app.command("benchmark-inference")
def benchmark_inference(
    model_dir: Path = typer.Option(..., exists=True, file_okay=False, readable=True),
    output: Path = typer.Option(..., help="New metadata-only JSON result file."),
    threads: str = typer.Option("1,2,3,4", help="CPU thread settings, each in a fresh process."),
    repetitions: int = typer.Option(3, min=1, max=10),
    suite: str = typer.Option("micro", help="micro or application"),
    workload: str = typer.Option("mixed", help="mixed, procedures, oversized, or duplicates"),
    experiments: str = typer.Option(
        "baseline", help="Comma-separated benchmark-only candidates: baseline, clear-object, "
        "compact-schema, targeted-repair, json-stop, combined, field-contract, privacy-rule, "
        "contract-private",
    ),
) -> None:
    """Benchmark synthetic evidence offline without touching production analysis state."""
    from portfolio_analyzer.qwen.benchmark import run_matrix

    try:
        counts = tuple(dict.fromkeys(int(value.strip()) for value in threads.split(",")))
        typer.echo("Starting offline CPU benchmark; results include warm-up and timed runs.")
        success = run_matrix(
            model_dir=model_dir, threads=counts, repetitions=repetitions,
            output=output, suite=suite, workload=workload,
            experiments=tuple(dict.fromkeys(value.strip() for value in experiments.split(","))),
        )
    except KeyboardInterrupt:
        typer.echo("Benchmark interrupted; completed thread-setting results retained.")
        raise typer.Exit(code=130) from None
    except (OSError, ValueError):
        typer.echo("Benchmark failed: check options, local dependencies, and output location.")
        raise typer.Exit(code=1) from None
    if not success:
        typer.echo("Benchmark worker failed; verify the local model and semantic dependencies.")
        raise typer.Exit(code=1)
    typer.echo("Benchmark measurements written. Synthetic results require quality review.")


@app.command()
def report(
    workspace: Path = typer.Option(..., file_okay=False),
    allow_partial: bool = typer.Option(
        False,
        "--allow-partial",
        help="Publish a watermarked report containing only completed applications.",
    ),
) -> None:
    """Render one immutable report model to HTML, Excel, PDF, and normalized CSV."""

    workspace = workspace.resolve()
    try:
        preflight_workspace(workspace)
        settings = AnalyzerSettings(workspace=workspace)
        store = V2StateStore(workspace)
        with store.exclusive_lock():
            analyze_run = _load_current_analysis_chain(
                store,
                allow_stale_analysis=allow_partial,
            )
            stage_run = store.load_current_run(RunPhase.STAGE)
            stage_ref = _reference_by_schema(stage_run, "stage-index-v2")
            stage_index = store.load(stage_ref, StageIndex)
            extract_run = store.load_current_run(RunPhase.EXTRACT)
            (
                bundles,
                profiles,
                portfolio_payload,
                omissions,
                excluded_application_ids,
            ) = _report_analysis_inputs(
                store,
                analyze_run,
                stage_index,
                extract_run,
                allow_partial=allow_partial,
            )
            portfolio = (
                portfolio_payload.analysis if portfolio_payload is not None else None
            )
            candidates = (
                portfolio_payload.candidates if portfolio_payload is not None else ()
            )
            current_fingerprint = analysis_fingerprint(bundles, profiles, portfolio)
            overlay = _review_overlay_or_none(store, analyze_run)
            if overlay is not None and overlay.analysis_fingerprint != current_fingerprint:
                if not allow_partial:
                    raise StateIntegrityError(
                        "current review overlay is stale for the selected analysis inputs"
                    )
                overlay = None
                omissions = (
                    *omissions,
                    ReportOmission(
                        stage=ReportOmissionStage.REPORT,
                        reason=(
                            "Review decisions were omitted because the partial report input "
                            "does not match their originating analysis fingerprint"
                        ),
                    ),
                )
            reviews = _report_review_records(
                overlay,
                candidates=candidates,
                findings=portfolio.findings if portfolio is not None else (),
            )
            report_model = build_portfolio_report_model(
                bundles,
                profiles,
                portfolio,
                candidates,
                generated_at=datetime.now(UTC),
                reviews=reviews,
                omissions=omissions,
                inventory_exclusions=stage_index.exclusions,
                allow_partial=allow_partial,
                excluded_application_ids=excluded_application_ids,
            )
            report_ref = store.put(report_model)
            run_id = _new_run_id(RunPhase.REPORT)
            publication = publish_report_run(
                settings.reports_dir,
                run_id,
                report_model,
                publish_latest=False,
            )
            publication_ref = store.put(publication)
            now = datetime.now(UTC)
            manifest = RunManifest(
                run_id=run_id,
                phase=RunPhase.REPORT,
                status=(
                    RunStatus.COMPLETE
                    if report_model.status == ReportStatus.COMPLETE
                    else RunStatus.PARTIAL
                ),
                started_at=now,
                completed_at=now,
                parent_run_id=analyze_run.run_id,
                input_references=(*analyze_run.input_references, stage_ref),
                applications=analyze_run.applications,
                portfolio_analysis=analyze_run.portfolio_analysis,
                report_model=report_ref,
                report_publication=publication_ref,
                warnings=report_model.warnings,
            )
            store.publish_run(manifest)
            if report_model.status == ReportStatus.COMPLETE:
                publication = publish_report_run(
                    settings.reports_dir,
                    run_id,
                    report_model,
                )
    except typer.Exit:
        raise
    except Exception as exc:
        _abort("Report publication failed safely", exc)

    typer.echo(
        f"Published {report_model.status.value} report run {publication.run_id} "
        f"with {len(publication.artifacts)} verified format(s)."
    )
    typer.echo(str(settings.reports_dir / "runs" / publication.run_id))


@app.command("model-download")
def model_download(
    destination: Path = typer.Option(..., file_okay=False),
) -> None:
    """Download the one approved Qwen revision and verify every allowlisted file."""

    try:
        manifest = acquire_approved_model(destination.resolve(), QWEN_MODEL)
    except Exception as exc:
        _abort("Qwen model acquisition failed", exc)
    typer.echo(
        f"Verified {manifest.repo_id}@{manifest.revision} at {destination.resolve()} "
        f"({manifest.manifest_sha256})."
    )


@app.command("model-verify")
def model_verify(
    model_dir: Path = typer.Option(..., exists=True, file_okay=False, readable=True),
) -> None:
    """Verify the complete local Qwen manifest without network access."""

    try:
        verified = verify_model_directory(model_dir.resolve())
    except Exception as exc:
        _abort("Qwen model verification failed", exc)
    typer.echo(
        f"Verified {verified.manifest.repo_id}@{verified.manifest.revision} "
        f"({verified.manifest.manifest_sha256})."
    )


@app.command("import-review")
def import_review(
    workspace: Path = typer.Option(..., file_okay=False),
    workbook: Path = typer.Option(..., exists=True, readable=True, dir_okay=False),
) -> None:
    """Import decisions only for the exact current analysis and stable evidence identities."""

    workspace = workspace.resolve()
    try:
        preflight_workspace(workspace)
        store = V2StateStore(workspace)
        with store.exclusive_lock():
            analyze_run = _load_current_analysis_chain(store)
            bundles, profiles, portfolio_payload = _load_complete_analysis(
                store, analyze_run
            )
            portfolio = portfolio_payload.analysis
            if portfolio is None:
                raise StateIntegrityError("current portfolio interpretation is unavailable")
            fingerprint = analysis_fingerprint(bundles, profiles, portfolio)
            known_proposals = _known_review_proposals(
                portfolio_payload.candidates,
                portfolio.findings,
            )
            overlay = import_review_overlay(
                workbook.resolve(),
                analysis_fingerprint=fingerprint,
                known_proposals=known_proposals,
            )
            overlay_ref = store.put(
                overlay,
                schema_name=REVIEW_OVERLAY_SCHEMA_VERSION,
            )
            input_references = tuple(
                reference
                for reference in analyze_run.input_references
                if reference.schema_name != REVIEW_OVERLAY_SCHEMA_VERSION
            ) + (overlay_ref,)
            now = datetime.now(UTC)
            manifest = RunManifest(
                run_id=_new_run_id(RunPhase.ANALYZE),
                phase=RunPhase.ANALYZE,
                status=analyze_run.status,
                started_at=now,
                completed_at=now,
                parent_run_id=analyze_run.parent_run_id,
                input_references=input_references,
                applications=analyze_run.applications,
                portfolio_analysis=analyze_run.portfolio_analysis,
                warnings=tuple(
                    (
                        *analyze_run.warnings,
                        f"review overlay imported: {overlay.overlay_id}",
                    )
                ),
                errors=analyze_run.errors,
            )
            store.publish_run(manifest)
    except typer.Exit:
        raise
    except Exception as exc:
        _abort("Review import failed safely", exc)

    typer.echo(
        f"Imported {len(overlay.decisions)} review decision(s) for analysis "
        f"{overlay.analysis_fingerprint}."
    )


@app.command("quality-check")
def quality_check(
    workspace: Path = typer.Option(..., file_okay=False),
    gold_set: Path = typer.Option(..., exists=True, readable=True, dir_okay=False),
) -> None:
    """Evaluate the current immutable analysis against the frozen V2 gold-set policy."""

    workspace = workspace.resolve()
    try:
        preflight_workspace(workspace)
        store = V2StateStore(workspace)
        analyze_run = _load_current_analysis_chain(store)
        bundles, profiles, portfolio = _load_complete_analysis(store, analyze_run)
        result = evaluate_quality(
            gold_set.resolve(),
            bundles,
            profiles,
            portfolio.candidates,
        )
    except typer.Exit:
        raise
    except Exception as exc:
        _abort("Quality check failed safely", exc)

    typer.echo(
        f"Quality policy {result.policy_version}: "
        f"{result.reviewed_applications} reviewed application(s)."
    )
    if result.capability_recall is not None:
        typer.echo(f"Capability recall: {result.capability_recall:.3f}.")
    if result.related_pair_f1 is not None:
        typer.echo(f"Semantic-pair F1: {result.related_pair_f1:.3f}.")
    if result.calibrated_semantic_threshold is not None:
        typer.echo(
            "Calibrated semantic-threshold recommendation: "
            f"{result.calibrated_semantic_threshold:.6f}."
        )
    if not result.passed:
        for reason in result.reasons:
            typer.echo(f"FAILED: {reason}", err=True)
        raise typer.Exit(code=1)
    typer.echo("Quality check passed.")


def _model_provenance(provider: LocalQwenProvider) -> ModelProvenance:
    """Return the fixed policy identity used by every inference and cache key."""

    try:
        transformers_version = importlib.metadata.version("transformers")
    except importlib.metadata.PackageNotFoundError as exc:
        raise ValueError(
            "Local Qwen analysis requires the installed transformers runtime"
        ) from exc
    provenance = ModelProvenance(
        model_manifest_sha256=provider.verified.manifest.manifest_sha256,
        prompt_version=TWO_STAGE_PROMPT_VERSION,
        output_schema_version="+".join(
            (
                APPLICATION_EVIDENCE_SCHEMA_VERSION,
                LOGICAL_INTERPRETATION_SCHEMA_VERSION,
                APPLICATION_INTERPRETATION_SCHEMA_VERSION,
                PORTFOLIO_ANALYSIS_SCHEMA_VERSION,
            )
        ),
        inference_library_version=f"transformers-{transformers_version}",
        generation_parameters=(
            KeyValueFact(name="do_sample", value="false"),
            KeyValueFact(name="local_files_only", value="true"),
            KeyValueFact(
                name="logical_unit_context_tokens",
                value=str(LOGICAL_UNIT_OPERATIONAL_CONTEXT_TOKENS),
            ),
            KeyValueFact(name="max_new_tokens", value=str(DEFAULT_MAX_OUTPUT_TOKENS)),
            KeyValueFact(
                name="model_context_ceiling_tokens",
                value=str(REVIEWED_CONTEXT_TOKENS),
            ),
            KeyValueFact(name="num_beams", value="1"),
            KeyValueFact(name="safetensors_only", value="true"),
            KeyValueFact(name="seed", value="0"),
            KeyValueFact(
                name="synthesis_context_tokens",
                value=str(SYNTHESIS_OPERATIONAL_CONTEXT_TOKENS),
            ),
            KeyValueFact(name="trust_remote_code", value="false"),
        ),
    )
    from portfolio_analyzer.qwen.experiments import EXPERIMENTS

    experiment = getattr(provider, "experiment", EXPERIMENTS["baseline"])
    if experiment.name != "baseline":
        # Keep baseline fingerprints stable; every experimental policy has separate caches.
        provenance = provenance.model_copy(update={
            "generation_parameters": tuple(sorted(
                (*provenance.generation_parameters,
                 KeyValueFact(name="inference_experiment", value=experiment.identity)),
                key=lambda item: item.name,
            )),
        })
    return provenance



def _compatible_previous_analysis(
    store: V2StateStore,
    record: ApplicationRunRecord | None,
    bundle: ApplicationEvidenceBundle,
    provenance: ModelProvenance,
    extraction_snapshots: tuple[ContentReference, ...],
) -> tuple[ApplicationRunRecord, ApplicationAnalysisPayload] | None:
    """Reuse only a complete, evidence-identical analysis from this fixed model policy."""

    if (
        record is None
        or record.status != RunStatus.COMPLETE
        or record.evidence_bundle is None
        or record.interpretation is None
        or record.extraction_snapshots != extraction_snapshots
        or set(record.artifact_ids) != {item.artifact_id for item in bundle.artifacts}
    ):
        return None
    previous_bundle = store.load(record.evidence_bundle, ApplicationEvidenceBundle)
    if (
        previous_bundle.application_id != bundle.application_id
        or evidence_bundle_fingerprint(previous_bundle)
        != evidence_bundle_fingerprint(bundle)
    ):
        return None
    payload = store.load(record.interpretation, ApplicationAnalysisPayload)
    if (
        payload.application_id != bundle.application_id
        or payload.status != "complete"
        or payload.interpretation is None
        or payload.source_bundle_sha256 != evidence_bundle_fingerprint(bundle)
        or payload.interpretation.provenance != provenance
    ):
        return None
    return record, payload


def _required_profile(payload: ApplicationAnalysisPayload) -> ApplicationInterpretation:
    if payload.status != "complete" or payload.interpretation is None:
        raise StateIntegrityError("complete application record lacks its Qwen profile")
    return payload.interpretation


def _load_current_analysis_chain(
    store: V2StateStore,
    *,
    allow_stale_analysis: bool = False,
) -> RunManifest:
    """Load analysis, optionally deferring per-application staleness to partial reports."""

    stage_run = store.load_current_run(RunPhase.STAGE)
    stage_ref = _reference_by_schema(stage_run, "stage-index-v2")
    extract_run = store.load_current_run(RunPhase.EXTRACT)
    if stage_ref not in extract_run.input_references:
        raise StateIntegrityError("current extraction is stale for the staged inventory")
    analyze_run = store.load_current_run(RunPhase.ANALYZE)
    if not allow_stale_analysis and analyze_run.parent_run_id != extract_run.run_id:
        raise StateIntegrityError("current analysis is stale for the current extraction")
    return analyze_run


def _load_complete_analysis(
    store: V2StateStore,
    manifest: RunManifest,
) -> tuple[
    tuple[ApplicationEvidenceBundle, ...],
    tuple[ApplicationInterpretation, ...],
    PortfolioAnalysisPayload,
]:
    if manifest.status != RunStatus.COMPLETE:
        raise StateIntegrityError("current analysis is not complete")
    if manifest.portfolio_analysis is None:
        raise StateIntegrityError("current analysis has no portfolio synthesis")
    bundles: list[ApplicationEvidenceBundle] = []
    profiles: list[ApplicationInterpretation] = []
    for record in manifest.applications:
        if (
            record.status != RunStatus.COMPLETE
            or record.evidence_bundle is None
            or record.interpretation is None
        ):
            raise StateIntegrityError(
                f"current analysis is incomplete for application {record.application_id}"
            )
        bundle = store.load(record.evidence_bundle, ApplicationEvidenceBundle)
        payload = store.load(record.interpretation, ApplicationAnalysisPayload)
        profile = _required_profile(payload)
        if not is_current_model_provenance(profile.provenance):
            raise StateIntegrityError(
                f"analysis uses an obsolete model policy for {record.application_id}"
            )
        if profile.source_bundle_sha256 != evidence_bundle_fingerprint(bundle):
            raise StateIntegrityError(
                f"analysis evidence fingerprint mismatch for {record.application_id}"
            )
        bundles.append(bundle)
        profiles.append(profile)
    portfolio = store.load(manifest.portfolio_analysis, PortfolioAnalysisPayload)
    if portfolio.status != "complete" or portfolio.analysis is None:
        raise StateIntegrityError("current portfolio synthesis is incomplete")
    if not is_current_model_provenance(portfolio.analysis.provenance):
        raise StateIntegrityError("current portfolio uses an obsolete model policy")
    expected_hashes = tuple(
        sorted(interpretation_fingerprint(item) for item in profiles)
    )
    if portfolio.application_profile_sha256s != expected_hashes:
        raise StateIntegrityError("portfolio synthesis is stale for application profiles")
    return tuple(bundles), tuple(profiles), portfolio


def _report_analysis_inputs(
    store: V2StateStore,
    manifest: RunManifest,
    stage_index: StageIndex,
    extract_run: RunManifest,
    *,
    allow_partial: bool,
) -> tuple[
    tuple[ApplicationEvidenceBundle, ...],
    tuple[ApplicationInterpretation, ...],
    PortfolioAnalysisPayload | None,
    tuple[ReportOmission, ...],
    tuple[str, ...],
]:
    expected_application_ids = {
        item.application_id for item in stage_index.applications
    }
    manifest_application_ids = {item.application_id for item in manifest.applications}
    extraction_records = {
        item.application_id: item for item in extract_run.applications
    }
    if set(extraction_records) != expected_application_ids:
        raise StateIntegrityError(
            "current extraction scope does not match the staged application scope"
        )
    if not allow_partial:
        if manifest_application_ids != expected_application_ids:
            raise StateIntegrityError(
                "current analysis scope does not match the staged application scope"
            )
        complete_bundles, complete_profiles, complete_portfolio = (
            _load_complete_analysis(store, manifest)
        )
        return complete_bundles, complete_profiles, complete_portfolio, (), ()

    records = {item.application_id: item for item in manifest.applications}
    included_bundles: list[ApplicationEvidenceBundle] = []
    included_profiles: list[ApplicationInterpretation] = []
    omissions: list[ReportOmission] = []
    excluded: list[str] = []
    for application in stage_index.applications:
        application_id = application.application_id
        record = records.get(application_id)
        extraction_record = extraction_records[application_id]
        analysis_is_current = bool(
            record is not None
            and extraction_record.status == RunStatus.COMPLETE
            and record.extraction_snapshots
            == extraction_record.extraction_snapshots
            and set(record.artifact_ids) == set(application.artifact_ids)
            and set(extraction_record.artifact_ids) == set(application.artifact_ids)
        )
        if (
            analysis_is_current
            and record is not None
            and record.status == RunStatus.COMPLETE
            and record.evidence_bundle is not None
            and record.interpretation is not None
        ):
            bundle = store.load(record.evidence_bundle, ApplicationEvidenceBundle)
            if _bundle_matches_stage_application(bundle, application, stage_index):
                payload = store.load(record.interpretation, ApplicationAnalysisPayload)
                profile = _required_profile(payload)
                if not is_current_model_provenance(profile.provenance):
                    analysis_is_current = False
                else:
                    if profile.source_bundle_sha256 != evidence_bundle_fingerprint(bundle):
                        raise StateIntegrityError(
                            f"analysis evidence fingerprint mismatch for {application_id}"
                        )
                    included_bundles.append(bundle)
                    included_profiles.append(profile)
                    continue
            analysis_is_current = False

        excluded.append(application_id)
        reasons: tuple[str, ...]
        if record is not None and not analysis_is_current:
            reasons = ("analysis is stale for the current extraction",)
        else:
            reasons = (
                tuple((*record.errors, *record.warnings))
                if record is not None
                else ("application is absent from the analysis manifest",)
            )
        fallback_reason = (
            f"application analysis status is {record.status.value}"
            if record is not None
            else "application analysis is unavailable"
        )
        omissions.append(
            ReportOmission(
                application_id=application_id,
                stage=ReportOmissionStage.ANALYZE,
                reason="; ".join(reasons) or fallback_reason,
            )
        )
        if record is None or record.interpretation is None:
            continue
        payload = store.load(record.interpretation, ApplicationAnalysisPayload)
        for unit in payload.unit_results:
            if unit.status in {"complete", "abstained"}:
                continue
            omissions.append(
                ReportOmission(
                    application_id=application_id,
                    logical_unit_id=unit.logical_unit_id,
                    stage=ReportOmissionStage.ANALYZE,
                    reason=unit.reason or f"logical-unit status is {unit.status}",
                )
            )

    if not included_bundles:
        raise StateIntegrityError(
            "partial reporting requires at least one completed application"
        )
    partial_portfolio: PortfolioAnalysisPayload | None = None
    if not excluded and manifest.portfolio_analysis is not None:
        candidate = store.load(manifest.portfolio_analysis, PortfolioAnalysisPayload)
        expected_hashes = tuple(
            sorted(interpretation_fingerprint(item) for item in included_profiles)
        )
        if (
            candidate.application_profile_sha256s == expected_hashes
            and (
                candidate.analysis is None
                or is_current_model_provenance(candidate.analysis.provenance)
            )
        ):
            partial_portfolio = candidate
        if candidate.status != "complete" and candidate.reason:
            omissions.append(
                ReportOmission(
                    stage=ReportOmissionStage.ANALYZE,
                    reason=candidate.reason,
                )
            )
    return (
        tuple(included_bundles),
        tuple(included_profiles),
        partial_portfolio,
        tuple(omissions),
        tuple(sorted(excluded)),
    )


def _analysis_manifest_uses_current_model(
    store: V2StateStore, manifest: RunManifest
) -> bool:
    """Gate review carry-forward without authorizing historical model output.

    A failed portfolio synthesis may have no model-authored portfolio payload at
    all.  In that case the current application interpretations are sufficient to
    keep a dormant overlay reachable; proposal/evidence identity matching still
    decides whether any decision can be carried into the next complete run.
    """
    profiles: list[ApplicationInterpretation] = []
    for record in manifest.applications:
        if record.interpretation is None:
            return False
        payload = store.load(record.interpretation, ApplicationAnalysisPayload)
        if payload.interpretation is None:
            return False
        profiles.append(payload.interpretation)
    if not profiles or not all(
        is_current_model_provenance(profile.provenance) for profile in profiles
    ):
        return False
    if manifest.portfolio_analysis is None:
        return True
    portfolio = store.load(manifest.portfolio_analysis, PortfolioAnalysisPayload)
    return portfolio.analysis is None or is_current_model_provenance(
        portfolio.analysis.provenance
    )


def _bundle_matches_stage_application(
    bundle: ApplicationEvidenceBundle,
    application: StagedApplication,
    stage_index: StageIndex,
) -> bool:
    """Confirm a stale-run candidate still represents the current per-app stage facts."""

    if (
        bundle.application_id != application.application_id
        or bundle.application_name != application.application_name
        or bundle.inventory_record_ids != (application.application_id,)
    ):
        return False
    staged_artifacts = {
        item.artifact_id: item
        for item in stage_index.artifacts
        if item.application_id == application.application_id
    }
    bundle_artifacts = {item.artifact_id: item for item in bundle.artifacts}
    if set(staged_artifacts) != set(application.artifact_ids) or set(
        bundle_artifacts
    ) != set(application.artifact_ids):
        return False
    for artifact_id, staged in staged_artifacts.items():
        artifact = bundle_artifacts[artifact_id]
        if (
            artifact.application_id != staged.application_id
            or artifact.source_locator != staged.source_locator
            or artifact.staged_relative_path != staged.staged_relative_path
            or artifact.filename != staged.filename
            or artifact.access_format != staged.access_format
            or artifact.sha256 != staged.sha256
            or artifact.size_bytes != staged.size_bytes
        ):
            return False
    expected_claims = {item.claim_id: item for item in application.owner_claims}
    for description in application.inventory_descriptions:
        claim = OwnerClaim(
            application_id=application.application_id,
            field="description",
            value=description,
            source="inventory workbook",
        )
        expected_claims[claim.claim_id] = claim
    actual_claims = {item.claim_id: item for item in bundle.owner_claims}
    return actual_claims == expected_claims


def _known_review_proposals(
    candidates: tuple[PortfolioCandidate, ...],
    findings: tuple[PortfolioFinding, ...],
) -> dict[str, tuple[str, ...]]:
    proposals = {
        item.candidate_id: item.evidence_ids for item in candidates
    } | {item.finding_id: item.evidence_ids for item in findings}
    if len(proposals) != len(candidates) + len(findings):
        raise StateIntegrityError("proposal identities collide in the current analysis")
    return proposals


def _report_review_records(
    overlay: ReviewOverlay | None,
    *,
    candidates: tuple[PortfolioCandidate, ...],
    findings: tuple[PortfolioFinding, ...],
) -> tuple[ReportReviewRecord, ...]:
    if overlay is None:
        return ()
    subjects = {
        item.candidate_id: (item.application_ids, item.evidence_ids)
        for item in candidates
    } | {
        item.finding_id: (item.application_ids, item.evidence_ids)
        for item in findings
    }
    statuses = {
        ReviewDecisionKind.ACCEPT: ReviewStatus.ACCEPTED,
        ReviewDecisionKind.EDIT: ReviewStatus.EDITED,
        ReviewDecisionKind.REJECT: ReviewStatus.REJECTED,
    }
    output: list[ReportReviewRecord] = []
    for decision in overlay.decisions:
        subject = subjects.get(decision.proposal_id)
        if subject is None:
            raise StateIntegrityError(
                f"review decision references unknown proposal {decision.proposal_id}"
            )
        application_ids, evidence_ids = subject
        if decision.evidence_ids != tuple(sorted(set(evidence_ids))):
            raise StateIntegrityError(
                f"review decision evidence is stale for {decision.proposal_id}"
            )
        for application_id in application_ids:
            output.append(
                ReportReviewRecord(
                    application_id=application_id,
                    subject_id=decision.proposal_id,
                    status=statuses[decision.decision],
                    edited_value=decision.edited_value,
                    reviewer=decision.reviewer,
                    notes=decision.notes,
                    evidence_ids=decision.evidence_ids,
                )
            )
    return tuple(output)


def _review_overlay_or_none(
    store: V2StateStore,
    manifest: RunManifest,
) -> ReviewOverlay | None:
    reference = _review_overlay_reference_or_none(manifest)
    return store.load(reference, ReviewOverlay) if reference is not None else None


def _review_overlay_reference_or_none(
    manifest: RunManifest,
) -> ContentReference | None:
    matches = tuple(
        reference
        for reference in manifest.input_references
        if reference.schema_name == REVIEW_OVERLAY_SCHEMA_VERSION
    )
    if not matches:
        return None
    if len(matches) != 1:
        raise StateIntegrityError("analysis manifest contains multiple review overlays")
    return matches[0]


def _stage_manifest(
    index: StageIndex,
    index_ref: ContentReference,
    *,
    previous: RunManifest | None,
) -> RunManifest:
    failures_by_application: dict[str, list[str]] = defaultdict(list)
    for failure in index.failures:
        failures_by_application[failure.application_id].append(
            f"{failure.filename}: {failure.reason}"
        )
    records = tuple(
        ApplicationRunRecord(
            application_id=application.application_id,
            artifact_ids=application.artifact_ids,
            status=(
                RunStatus.FAILED
                if failures_by_application[application.application_id]
                else RunStatus.COMPLETE
            ),
            errors=tuple(failures_by_application[application.application_id]),
        )
        for application in index.applications
    )
    unrepresented_failure_ids = set(failures_by_application) - {
        item.application_id for item in index.applications
    }
    records += tuple(
        ApplicationRunRecord(
            application_id=application_id,
            status=RunStatus.FAILED,
            errors=tuple(failures_by_application[application_id]),
        )
        for application_id in sorted(unrepresented_failure_ids)
    )
    if index.failures:
        status = RunStatus.PARTIAL if index.artifacts else RunStatus.FAILED
    elif not index.artifacts:
        status = RunStatus.PARTIAL
    else:
        status = RunStatus.COMPLETE
    errors = tuple(
        f"{failure.application_id}/{failure.filename}: {failure.reason}"
        for failure in index.failures
    )
    warnings = tuple(
        [
            f"excluded unsupported primary {item.application_id}/{item.filename}"
            for item in index.exclusions
        ]
        + (["inventory contains no eligible Access application"] if not index.artifacts else [])
    )
    now = datetime.now(UTC)
    return RunManifest(
        run_id=_new_run_id(RunPhase.STAGE),
        phase=RunPhase.STAGE,
        status=status,
        started_at=now,
        completed_at=now,
        parent_run_id=previous.run_id if previous is not None else None,
        input_references=(index_ref,),
        applications=records,
        warnings=warnings,
        errors=errors,
    )


def _compatible_previous_extractions(
    store: V2StateStore,
    previous: RunManifest | None,
    index: StageIndex,
) -> dict[str, ApplicationRunRecord]:
    if previous is None:
        return {}
    current = {item.artifact_id: item for item in index.artifacts}
    settings = AnalyzerSettings(workspace=store.root)
    output: dict[str, ApplicationRunRecord] = {}
    for record in previous.applications:
        if record.status != RunStatus.COMPLETE:
            continue
        snapshots = _snapshot_references_by_artifact(store, record)
        if set(snapshots) != set(record.artifact_ids):
            continue
        compatible = True
        for artifact_id, reference in snapshots.items():
            staged = current.get(artifact_id)
            snapshot = store.load(reference, ExtractedArtifactSnapshot)
            try:
                approved_libraries = (
                    tuple(
                        item.reference
                        for item in resolve_approved_libraries(
                            settings,
                            record.application_id,
                            primary_filename=staged.filename,
                        )
                    )
                    if staged is not None
                    else ()
                )
            except (OSError, ValueError):
                compatible = False
                break
            if (
                staged is None
                or staged.application_id != record.application_id
                or snapshot.application_id != record.application_id
                or snapshot.artifact_id != artifact_id
                or staged.sha256 != snapshot.artifact_sha256
                or snapshot.extractor_version != WindowsAccessExtractor.version
                or snapshot.extraction_policy_version != EXTRACTION_POLICY_VERSION
                or snapshot.approved_libraries != approved_libraries
            ):
                compatible = False
                break
        if compatible:
            output[record.application_id] = record
    return output


def _snapshot_references_by_artifact(
    store: V2StateStore,
    record: ApplicationRunRecord,
) -> dict[str, ContentReference]:
    output: dict[str, ContentReference] = {}
    for reference in record.extraction_snapshots:
        snapshot = store.load(reference, ExtractedArtifactSnapshot)
        if snapshot.artifact_id in output:
            raise StateIntegrityError("duplicate extraction snapshot artifact identity")
        output[snapshot.artifact_id] = reference
    return output


def _extraction_artifact(
    workspace: Path,
    artifact: StagedArtifactRecord,
) -> VerifiedStagedArtifact:
    return VerifiedStagedArtifact(
        artifact_id=artifact.artifact_id,
        tool_inventory_id=artifact.application_id,
        local_staged_path=workspace / artifact.staged_relative_path,
        filename=artifact.filename,
        extension=".accdb" if artifact.access_format == "accdb" else ".mdb",
        size_bytes=artifact.size_bytes,
        sha256=artifact.sha256,
    )


def _aggregate_run_status(records: list[ApplicationRunRecord]) -> RunStatus:
    if not records or all(item.status == RunStatus.FAILED for item in records):
        return RunStatus.FAILED
    if any(item.status != RunStatus.COMPLETE for item in records):
        return RunStatus.PARTIAL
    return RunStatus.COMPLETE


def _reference_by_schema(manifest: RunManifest, schema_name: str) -> ContentReference:
    matches = [item for item in manifest.input_references if item.schema_name == schema_name]
    if len(matches) != 1:
        raise StateIntegrityError(
            f"current {manifest.phase.value} run does not contain one {schema_name} reference"
        )
    return matches[0]


def _current_or_none(store: V2StateStore, phase: RunPhase) -> RunManifest | None:
    try:
        return store.load_current_run(phase)
    except StateIntegrityError as exc:
        if "no valid current run" in str(exc):
            return None
        raise


def _new_run_id(phase: RunPhase) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{phase.value}-{timestamp}-{secrets.token_hex(4)}"


def _abort(prefix: str, error: Exception) -> None:
    typer.echo(f"{prefix}: {redact_sensitive_text(str(error))}", err=True)
    raise typer.Exit(code=1)


def _progress_reporter(application_id: str) -> Callable[[str], None]:
    def report(message: str) -> None:
        typer.echo(f"[{application_id}] {redact_sensitive_text(message)}")

    return report
