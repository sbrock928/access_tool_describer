"""Portfolio CLI.  Commands preserve the staging-before-analysis boundary."""

from __future__ import annotations

import json
import multiprocessing
import platform
import subprocess
import time
from collections.abc import Callable
from functools import partial
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any

import typer

from portfolio_analyzer.access.windows_extractor import WindowsAccessExtractor
from portfolio_analyzer.analysis.application import analyze_application
from portfolio_analyzer.capabilities.discovery import discover_capabilities
from portfolio_analyzer.config import AnalyzerSettings
from portfolio_analyzer.inventory.loader import load_inventory
from portfolio_analyzer.models import (
    Datasource,
    Dependency,
    Evidence,
    ExtractedApplication,
    InventoryRecord,
    StagedArtifact,
)
from portfolio_analyzer.persistence.database import create_session_factory
from portfolio_analyzer.persistence.repository import save_inventory_and_artifact
from portfolio_analyzer.portfolio.recommendations import build_recommendations
from portfolio_analyzer.reporting.coverage import build_analysis_coverage
from portfolio_analyzer.reporting.writers import write_csv, write_executive_pdf, write_workbook
from portfolio_analyzer.staging.copying import ArtifactStager

app = typer.Typer(no_args_is_help=True, help="Static, evidence-driven Access portfolio analysis.")
STATIC_ANALYSIS_VERSION = "static-analysis-v2"


def _settings(workspace: Path) -> AnalyzerSettings:
    settings = AnalyzerSettings(workspace=workspace.resolve())
    settings.ensure_workspace()
    return settings


def _read_state(state_path: Path) -> tuple[list[InventoryRecord], list[StagedArtifact]]:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    return (
        [InventoryRecord.model_validate(item) for item in state["inventory"]],
        [StagedArtifact.model_validate(item) for item in state["artifacts"]],
    )


def _analysis_state_path(settings: AnalyzerSettings) -> Path:
    return settings.analysis_dir / "analysis_state.json"


def _extraction_state_path(settings: AnalyzerSettings) -> Path:
    return settings.extracted_dir / "extraction_state.json"


def _load_extraction_state(settings: AnalyzerSettings) -> tuple[dict[str, Any], bool]:
    """Load extraction snapshots, migrating the pre-split state format when available."""
    extraction_path = _extraction_state_path(settings)
    if extraction_path.exists():
        return json.loads(extraction_path.read_text(encoding="utf-8")), False
    legacy_path = _analysis_state_path(settings)
    if not legacy_path.exists():
        raise typer.BadParameter("No extraction state found. Run 'extract' on Windows first.")
    legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
    applications = [
        {
            "tool_inventory_id": item["tool_inventory_id"],
            "sha256": item["sha256"],
            "extractor_version": item["extractor_version"],
            "extracted": item["extracted"],
        }
        for item in legacy.get("applications", [])
        if "extracted" in item
    ]
    if not applications:
        raise typer.BadParameter("No extraction state found. Run 'extract' on Windows first.")
    state = {"applications": applications}
    extraction_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state, True


def _eligible_staged_artifacts(
    settings: AnalyzerSettings, tool_id: str | None
) -> list[StagedArtifact]:
    state_path = settings.analysis_dir / "staging_state.json"
    if not state_path.exists():
        raise typer.BadParameter(
            "No staging state found. Run 'stage' first; source paths are not accepted."
        )
    _, artifacts = _read_state(state_path)
    return [
        artifact
        for artifact in artifacts
        if artifact.status.value == "staged"
        and artifact.is_primary
        and artifact.extension in {".accdb", ".mdb"}
        and (tool_id is None or artifact.tool_inventory_id == tool_id)
    ]


class AccessExtractionTimeoutError(TimeoutError):
    """Raised when an isolated Access worker exceeds its allotted time."""


def _access_extraction_worker(
    artifact: StagedArtifact,
    destination: str,
    workspace: str,
    progress_path: str,
    result_path: str,
) -> None:
    """Run COM extraction in a disposable process so a hung database cannot block the portfolio."""
    try:
        settings = AnalyzerSettings(workspace=Path(workspace))
        settings.ensure_workspace()
        progress_file = Path(progress_path)

        def write_progress(message: str) -> None:
            progress_file.write_text(message, encoding="utf-8")

        extracted = WindowsAccessExtractor(settings).extract(
            artifact,
            Path(destination),
            progress=write_progress,
            # COM shutdown can block despite successful exports. The disposable worker releases
            # its COM references on exit, while the parent enforces a timeout for the whole process.
            cleanup=False,
        )
        _write_worker_result(
            Path(result_path), {"status": "ok", "extracted": extracted.model_dump(mode="json")}
        )
    except Exception as exc:
        _write_worker_result(
            Path(result_path), {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        )


def _write_worker_result(path: Path, result: dict[str, Any]) -> None:
    """Atomically publish a worker result without multiprocessing queue shutdown semantics."""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(result), encoding="utf-8")
    temporary.replace(path)


def _extract_with_timeout(
    artifact: StagedArtifact,
    destination: Path,
    settings: AnalyzerSettings,
    timeout_seconds: int,
    on_progress: Callable[[str], None],
) -> ExtractedApplication:
    context = multiprocessing.get_context("spawn")
    destination.mkdir(parents=True, exist_ok=True)
    progress_path = destination / "_extraction_progress.txt"
    result_path = destination / "_extraction_result.json"
    progress_path.unlink(missing_ok=True)
    result_path.unlink(missing_ok=True)
    process = context.Process(
        target=_access_extraction_worker,
        args=(
            artifact,
            str(destination),
            str(settings.workspace),
            str(progress_path),
            str(result_path),
        ),
    )
    process.start()
    last_progress: str | None = None
    worker_result: dict[str, Any] | None = None
    deadline = time.monotonic() + timeout_seconds
    try:
        while process.is_alive() and time.monotonic() < deadline:
            process.join(0.25)
            if result_path.exists():
                worker_result = json.loads(result_path.read_text(encoding="utf-8"))
                # Do not let a COM-release hang consume the full tool timeout.
                process.join(2)
                if process.is_alive():
                    _terminate_worker_tree(process)
                break
            if progress_path.exists():
                progress = progress_path.read_text(encoding="utf-8")
                if progress and progress != last_progress:
                    last_progress = progress
                    on_progress(progress)
        if process.is_alive():
            _terminate_worker_tree(process)
            operation = last_progress or "before the first Access checkpoint"
            raise AccessExtractionTimeoutError(
                f"Extraction exceeded {timeout_seconds} seconds during: {operation}"
            )
        if worker_result is None:
            if result_path.exists():
                worker_result = json.loads(result_path.read_text(encoding="utf-8"))
            else:
                raise RuntimeError(
                    f"Extraction worker exited without a result (exit code {process.exitcode})"
                )
        result = worker_result
        if result["status"] != "ok":
            raise RuntimeError(str(result["error"]))
        return ExtractedApplication.model_validate(result["extracted"])
    finally:
        if not process.is_alive():
            process.close()
            # The child intentionally avoids COM shutdown because that can hang. Once this parent
            # has joined or terminated that dedicated child, Windows releases the database handle
            # and this exact disposable directory can be safely removed.
            working_bundle = destination / "_working_bundle"
            if working_bundle.exists():
                import shutil

                shutil.rmtree(working_bundle, ignore_errors=True)


def _terminate_worker_tree(process: BaseProcess) -> None:
    """Terminate only the dedicated worker tree, never a user Access session by name."""
    if platform.system() == "Windows" and process.pid is not None:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            capture_output=True,
            timeout=15,
        )
    else:
        process.terminate()
    process.join(15)
    if process.is_alive():
        process.kill()
        process.join(5)


def _echo_progress(tool_inventory_id: str, message: str) -> None:
    typer.echo(f"[{tool_inventory_id}] {message}")


@app.command()
def stage(
    inventory: Path = typer.Option(..., exists=True, readable=True),
    workspace: Path = typer.Option(...),
) -> None:
    """Copy-only staging. Failures are recorded and never analyzed in place."""
    settings = _settings(workspace)
    records = load_inventory(inventory)
    stager = ArtifactStager(settings)
    session_factory = create_session_factory(settings.analysis_dir / "evidence.sqlite")
    artifacts: list[StagedArtifact] = []
    with session_factory.begin() as session:
        for record in records:
            for artifact in stager.stage_application_bundle(record):
                artifacts.append(artifact)
                save_inventory_and_artifact(session, record, artifact)
    state_path = settings.analysis_dir / "staging_state.json"
    state_path.write_text(
        json.dumps(
            {
                "inventory": [record.model_dump(mode="json") for record in records],
                "artifacts": [item.model_dump(mode="json") for item in artifacts],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    primary = [artifact for artifact in artifacts if artifact.is_primary]
    success_count = sum(a.status.value == "staged" for a in primary)
    typer.echo(f"Staged {success_count}/{len(primary)} primary artifacts.")
    typer.echo(f"Copied {len(artifacts)} bundle files.")
    typer.echo(f"State: {state_path}")


@app.command()
def extract(
    workspace: Path = typer.Option(...),
    force: bool = typer.Option(False, help="Re-extract unchanged staged artifacts."),
    tool_id: str | None = typer.Option(None, help="Extract one staged tool inventory ID."),
    timeout_seconds: int = typer.Option(
        300, min=10, max=3600, help="Maximum time per isolated Access extraction."
    ),
) -> None:
    """Extract Access metadata once from verified local copies; no interpretation occurs here."""
    settings = _settings(workspace)
    eligible = _eligible_staged_artifacts(settings, tool_id)
    typer.echo(f"{len(eligible)} locally staged artifacts are eligible for Windows extraction.")
    typer.echo("No source paths were opened.")
    if platform.system() != "Windows":
        typer.echo(
            "Access extraction is unavailable here; run this command on Windows with Access."
        )
        return
    output_path = _extraction_state_path(settings)
    previous = (
        json.loads(output_path.read_text(encoding="utf-8"))
        if output_path.exists() and not force
        else {}
    )
    extractor = WindowsAccessExtractor(settings)
    completed = {
        (result["tool_inventory_id"], result["sha256"])
        for result in previous.get("applications", [])
        if result.get("extractor_version") == extractor.version and "extracted" in result
    }
    results_by_key = (
        {}
        if force
        else {
            (result["tool_inventory_id"], result["sha256"]): result
            for result in previous.get("applications", [])
        }
    )
    for artifact in eligible:
        key = (artifact.tool_inventory_id, artifact.sha256)
        if key in completed:
            typer.echo(f"[{artifact.tool_inventory_id}] unchanged; reusing prior extraction.")
            continue
        try:
            typer.echo(
                f"[{artifact.tool_inventory_id}] extracting {artifact.filename} "
                f"(timeout: {timeout_seconds}s)..."
            )
            extracted = _extract_with_timeout(
                artifact,
                settings.extracted_dir / artifact.tool_inventory_id,
                settings,
                timeout_seconds,
                partial(_echo_progress, artifact.tool_inventory_id),
            )
            results_by_key[key] = {
                "tool_inventory_id": artifact.tool_inventory_id,
                "sha256": artifact.sha256,
                "extractor_version": extractor.version,
                "extracted": extracted.model_dump(mode="json"),
            }
            if extracted.extraction_errors:
                typer.echo(f"[{artifact.tool_inventory_id}] completed with extraction warnings:")
                for error in extracted.extraction_errors:
                    typer.echo(f"[{artifact.tool_inventory_id}]   {error}")
            else:
                typer.echo(f"[{artifact.tool_inventory_id}] completed.")
        except Exception as exc:
            # Continue the portfolio. This path never retries extraction against the source file.
            results_by_key[key] = {
                "tool_inventory_id": artifact.tool_inventory_id,
                "sha256": artifact.sha256,
                "extractor_version": extractor.version,
                "error": str(exc),
            }
            typer.echo(f"[{artifact.tool_inventory_id}] failed safely: {exc}")
    results = list(results_by_key.values())
    output_path.write_text(json.dumps({"applications": results}, indent=2), encoding="utf-8")
    typer.echo(f"Extraction state written to {output_path}")


@app.command("extract-tool")
def extract_tool(
    tool_id: str = typer.Option(..., "--id", help="Tool Inventory ID to analyze."),
    workspace: Path = typer.Option(...),
    force: bool = typer.Option(False, help="Re-extract even if unchanged."),
    timeout_seconds: int = typer.Option(300, min=10, max=3600),
) -> None:
    """Extract one successfully staged Access application, never its inventory path."""
    extract(
        workspace=workspace,
        force=force,
        tool_id=tool_id,
        timeout_seconds=timeout_seconds,
    )


@app.command()
def analyze(
    workspace: Path = typer.Option(...),
    force: bool = typer.Option(
        False, help="Re-run static analysis from saved extraction snapshots."
    ),
    tool_id: str | None = typer.Option(None, help="Analyze one extracted tool inventory ID."),
) -> None:
    """Derive evidence from saved extraction snapshots without opening Access or source files."""
    settings = _settings(workspace)
    extraction_state, migrated = _load_extraction_state(settings)
    if migrated:
        typer.echo(
            "Migrated existing extraction snapshots to workspace/extracted/extraction_state.json."
        )
    output_path = _analysis_state_path(settings)
    previous = (
        json.loads(output_path.read_text(encoding="utf-8"))
        if output_path.exists() and not force
        else {}
    )
    completed = {
        (result["tool_inventory_id"], result["sha256"])
        for result in previous.get("applications", [])
        if result.get("analysis_version") == STATIC_ANALYSIS_VERSION and "evidence" in result
    }
    results_by_key = (
        {}
        if force
        else {
            (result["tool_inventory_id"], result["sha256"]): result
            for result in previous.get("applications", [])
        }
    )
    eligible = [
        result
        for result in extraction_state.get("applications", [])
        if "extracted" in result and (tool_id is None or result["tool_inventory_id"] == tool_id)
    ]
    typer.echo(f"{len(eligible)} saved extraction snapshots are eligible for static analysis.")
    for result in eligible:
        key = (result["tool_inventory_id"], result["sha256"])
        if key in completed:
            typer.echo(f"[{result['tool_inventory_id']}] unchanged; reusing prior analysis.")
            continue
        try:
            typer.echo(f"[{result['tool_inventory_id']}] analyzing saved extraction snapshot...")
            extracted = ExtractedApplication.model_validate(result["extracted"])
            evidence, datasources, dependencies = analyze_application(extracted)
            results_by_key[key] = {
                "tool_inventory_id": result["tool_inventory_id"],
                "sha256": result["sha256"],
                "extractor_version": result["extractor_version"],
                "analysis_version": STATIC_ANALYSIS_VERSION,
                "evidence": [item.model_dump(mode="json") for item in evidence],
                "datasources": [item.model_dump(mode="json") for item in datasources],
                "dependencies": [item.model_dump(mode="json") for item in dependencies],
            }
            typer.echo(f"[{result['tool_inventory_id']}] analysis complete.")
        except Exception as exc:
            results_by_key[key] = {
                "tool_inventory_id": result["tool_inventory_id"],
                "sha256": result["sha256"],
                "extractor_version": result["extractor_version"],
                "analysis_version": STATIC_ANALYSIS_VERSION,
                "error": str(exc),
            }
            typer.echo(f"[{result['tool_inventory_id']}] analysis failed safely: {exc}")
    output_path.write_text(
        json.dumps({"applications": list(results_by_key.values())}, indent=2), encoding="utf-8"
    )
    typer.echo(f"Analysis state written to {output_path}")


@app.command("analyze-tool")
def analyze_tool(
    tool_id: str = typer.Option(..., "--id", help="Tool Inventory ID to analyze."),
    workspace: Path = typer.Option(...),
    force: bool = typer.Option(False, help="Re-run static analysis from its saved snapshot."),
) -> None:
    """Re-analyze one saved extraction snapshot without reopening Access."""
    analyze(workspace=workspace, force=force, tool_id=tool_id)


@app.command()
def report(workspace: Path = typer.Option(...)) -> None:
    """Create evidence, coverage, dependency, and modernization reports."""
    settings = _settings(workspace)
    state_path = settings.analysis_dir / "staging_state.json"
    if not state_path.exists():
        raise typer.BadParameter("No staging state found. Run 'stage' first.")
    inventory, artifacts = _read_state(state_path)
    analysis_path = _analysis_state_path(settings)
    state = json.loads(analysis_path.read_text(encoding="utf-8")) if analysis_path.exists() else {}
    evidence = [
        Evidence.model_validate(value)
        for result in state.get("applications", [])
        for value in result.get("evidence", [])
    ]
    datasources = [
        Datasource.model_validate(value)
        for result in state.get("applications", [])
        for value in result.get("datasources", [])
    ]
    dependencies = [
        Dependency.model_validate(value)
        for result in state.get("applications", [])
        for value in result.get("dependencies", [])
    ]
    capabilities = discover_capabilities(evidence)
    recommendations = build_recommendations(capabilities, datasources)
    extraction_path = _extraction_state_path(settings)
    extraction_state = (
        json.loads(extraction_path.read_text(encoding="utf-8"))
        if extraction_path.exists()
        else {}
    )
    coverage = build_analysis_coverage(
        inventory,
        artifacts,
        extraction_state,
        state,
        capabilities,
    )
    write_workbook(
        settings.reports_dir / "Portfolio_Analysis.xlsx",
        inventory,
        artifacts,
        evidence,
        datasources,
        dependencies,
        capabilities,
        recommendations=recommendations,
        coverage=coverage,
    )
    write_executive_pdf(
        settings.reports_dir / "Portfolio_Analysis.pdf",
        inventory,
        artifacts,
        capabilities,
        recommendations=recommendations,
        evidence=evidence,
        datasources=datasources,
        dependencies=dependencies,
        coverage=coverage,
    )
    write_csv(
        settings.reports_dir / "applications.csv",
        [
            {
                "tool_inventory_id": item.tool_inventory_id,
                "tool_name": item.tool_name,
                "inventory_file_name": item.inventory_filename,
                "stated_description": item.stated_description or "",
                "original_source_path": str(item.filepath),
            }
            for item in inventory
        ],
        headers=[
            "tool_inventory_id",
            "tool_name",
            "inventory_file_name",
            "stated_description",
            "original_source_path",
        ],
    )
    write_csv(
        settings.reports_dir / "artifacts.csv",
        [
            {
                "tool_inventory_id": item.tool_inventory_id,
                "original_source_path": str(item.original_source_path),
                "local_staged_path": str(item.local_staged_path or ""),
                "sha256": item.sha256 or "",
                "status": item.status.value,
                "error": item.error or "",
            }
            for item in artifacts
        ],
        headers=[
            "tool_inventory_id",
            "original_source_path",
            "local_staged_path",
            "sha256",
            "status",
            "error",
        ],
    )
    write_csv(
        settings.reports_dir / "analysis_coverage.csv",
        [
            {
                "tool_inventory_id": item.tool_inventory_id,
                "tool_name": item.tool_name,
                "staging_status": item.staging_status,
                "extraction_status": item.extraction_status,
                "analysis_status": item.analysis_status,
                "extracted_object_count": item.extracted_object_count,
                "extraction_warning_count": item.extraction_warning_count,
                "evidence_count": item.evidence_count,
                "datasource_count": item.datasource_count,
                "dependency_count": item.dependency_count,
                "capability_count": item.capability_count,
                "notes": " | ".join(item.notes),
            }
            for item in coverage
        ],
        headers=[
            "tool_inventory_id",
            "tool_name",
            "staging_status",
            "extraction_status",
            "analysis_status",
            "extracted_object_count",
            "extraction_warning_count",
            "evidence_count",
            "datasource_count",
            "dependency_count",
            "capability_count",
            "notes",
        ],
    )
    write_csv(
        settings.reports_dir / "evidence.csv",
        [
            {
                "tool_inventory_id": item.tool_inventory_id,
                "artifact_path": item.artifact_path,
                "object_type": item.object_type,
                "object_name": item.object_name,
                "location": item.location or "",
                "evidence": item.text,
                "inference": item.inference or "",
                "confidence": item.confidence.value,
            }
            for item in evidence
        ],
        headers=[
            "tool_inventory_id",
            "artifact_path",
            "object_type",
            "object_name",
            "location",
            "evidence",
            "inference",
            "confidence",
        ],
    )
    write_csv(
        settings.reports_dir / "datasources.csv",
        [
            {
                "tool_inventory_id": item.tool_inventory_id,
                "platform": item.platform,
                "server": item.server or "",
                "database": item.database or "",
                "schema": item.schema_name or "",
                "object": item.object_name or "",
                "operation": item.operation,
                "confidence": item.confidence.value,
                "evidence_count": len(item.evidence),
                "connection_summary": item.connection_summary or "",
            }
            for item in datasources
        ],
        headers=[
            "tool_inventory_id",
            "platform",
            "server",
            "database",
            "schema",
            "object",
            "operation",
            "confidence",
            "evidence_count",
            "connection_summary",
        ],
    )
    write_csv(
        settings.reports_dir / "dependencies.csv",
        [
            {
                "tool_inventory_id": item.tool_inventory_id,
                "source": item.source,
                "target": item.target,
                "dependency_type": item.dependency_type,
                "operation": item.operation,
                "confidence": item.confidence.value,
                "evidence_count": len(item.evidence),
            }
            for item in dependencies
        ],
        headers=[
            "tool_inventory_id",
            "source",
            "target",
            "dependency_type",
            "operation",
            "confidence",
            "evidence_count",
        ],
    )
    write_csv(
        settings.reports_dir / "capabilities.csv",
        [
            {
                "tool_inventory_id": item.tool_inventory_id,
                "capability": item.capability,
                "layer": item.layer,
                "confidence": item.confidence.value,
                "evidence_count": len(item.evidence),
            }
            for item in capabilities
        ],
        headers=[
            "tool_inventory_id",
            "capability",
            "layer",
            "confidence",
            "evidence_count",
        ],
    )
    write_csv(
        settings.reports_dir / "recommendations.csv",
        [
            {
                "category": item.category,
                "title": item.title,
                "confidence": item.confidence.value,
                "affected_tool_ids": " | ".join(item.affected_tool_ids),
                "application_count": len(item.affected_tool_ids),
                "evidence_count": len(item.evidence),
                "rationale": item.rationale,
            }
            for item in recommendations
        ],
        headers=[
            "category",
            "title",
            "confidence",
            "affected_tool_ids",
            "application_count",
            "evidence_count",
            "rationale",
        ],
    )
    typer.echo(f"Reports written to {settings.reports_dir}")


@app.command()
def run(
    inventory: Path = typer.Option(..., exists=True, readable=True),
    workspace: Path = typer.Option(...),
) -> None:
    """Stage, extract locally on Windows, analyze saved snapshots, then produce reports."""
    stage(inventory=inventory, workspace=workspace)
    extract(workspace=workspace, force=False, tool_id=None, timeout_seconds=300)
    analyze(workspace=workspace, force=False, tool_id=None)
    report(workspace=workspace)
