"""Portfolio CLI.  Commands preserve the staging-before-analysis boundary."""

from __future__ import annotations

import json
import multiprocessing
import platform
import subprocess
from multiprocessing.process import BaseProcess
from pathlib import Path
from queue import Empty
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
from portfolio_analyzer.reporting.writers import write_csv, write_executive_pdf, write_workbook
from portfolio_analyzer.staging.copying import ArtifactStager

app = typer.Typer(no_args_is_help=True, help="Static, evidence-driven Access portfolio analysis.")


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


class AccessExtractionTimeoutError(TimeoutError):
    """Raised when an isolated Access worker exceeds its allotted time."""


def _access_extraction_worker(
    artifact: StagedArtifact, destination: str, workspace: str, result_queue: Any
) -> None:
    """Run COM extraction in a disposable process so a hung database cannot block the portfolio."""
    try:
        settings = AnalyzerSettings(workspace=Path(workspace))
        settings.ensure_workspace()
        extracted = WindowsAccessExtractor(settings).extract(artifact, Path(destination))
        result_queue.put({"status": "ok", "extracted": extracted.model_dump(mode="json")})
    except Exception as exc:
        result_queue.put({"status": "error", "error": f"{type(exc).__name__}: {exc}"})


def _extract_with_timeout(
    artifact: StagedArtifact, destination: Path, settings: AnalyzerSettings, timeout_seconds: int
) -> ExtractedApplication:
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(
        target=_access_extraction_worker,
        args=(artifact, str(destination), str(settings.workspace), result_queue),
    )
    process.start()
    process.join(timeout_seconds)
    try:
        if process.is_alive():
            _terminate_worker_tree(process)
            raise AccessExtractionTimeoutError(
                f"Extraction exceeded {timeout_seconds} seconds and was terminated"
            )
        try:
            result = result_queue.get(timeout=3)
        except Empty as exc:
            raise RuntimeError(
                f"Extraction worker exited without a result (exit code {process.exitcode})"
            ) from exc
        if result["status"] != "ok":
            raise RuntimeError(str(result["error"]))
        return ExtractedApplication.model_validate(result["extracted"])
    finally:
        result_queue.close()
        result_queue.join_thread()


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
            artifact = stager.stage_primary(record)
            artifacts.append(artifact)
            save_inventory_and_artifact(session, record, artifact)
            for supporting in stager.stage_supporting(record):
                artifacts.append(supporting)
                save_inventory_and_artifact(session, record, supporting)
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
    typer.echo(f"Staged {success_count}/{len(primary)} primary artifacts. State: {state_path}")


@app.command()
def analyze(
    workspace: Path = typer.Option(...),
    force: bool = typer.Option(False, help="Re-extract unchanged staged artifacts."),
    tool_id: str | None = typer.Option(None, help="Analyze one staged tool inventory ID."),
    timeout_seconds: int = typer.Option(
        300, min=10, max=3600, help="Maximum time per isolated Access extraction."
    ),
) -> None:
    """Extract only verified local Access copies and perform static analysis."""
    settings = _settings(workspace)
    state_path = settings.analysis_dir / "staging_state.json"
    if not state_path.exists():
        raise typer.BadParameter(
            "No staging state found. Run 'stage' first; source paths are not accepted."
        )
    _, artifacts = _read_state(state_path)
    eligible = [
        artifact
        for artifact in artifacts
        if artifact.status.value == "staged"
        and artifact.is_primary
        and artifact.extension in {".accdb", ".mdb"}
        and (tool_id is None or artifact.tool_inventory_id == tool_id)
    ]
    typer.echo(f"{len(eligible)} locally staged artifacts are eligible for Windows extraction.")
    typer.echo("No source paths were opened.")
    if platform.system() != "Windows":
        typer.echo(
            "Access extraction is unavailable here; run this command on Windows with Access."
        )
        return
    output_path = _analysis_state_path(settings)
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
            )
            evidence, datasources, dependencies = analyze_application(extracted)
            results_by_key[key] = {
                "tool_inventory_id": artifact.tool_inventory_id,
                "sha256": artifact.sha256,
                "extractor_version": extractor.version,
                "extracted": extracted.model_dump(mode="json"),
                "evidence": [item.model_dump(mode="json") for item in evidence],
                "datasources": [item.model_dump(mode="json") for item in datasources],
                "dependencies": [item.model_dump(mode="json") for item in dependencies],
            }
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
    typer.echo(f"Analysis state written to {output_path}")


@app.command("analyze-tool")
def analyze_tool(
    tool_id: str = typer.Option(..., "--id", help="Tool Inventory ID to analyze."),
    workspace: Path = typer.Option(...),
    force: bool = typer.Option(False, help="Re-extract even if unchanged."),
    timeout_seconds: int = typer.Option(300, min=10, max=3600),
) -> None:
    """Analyze one successfully staged Access application, never its inventory path."""
    analyze(
        workspace=workspace,
        force=force,
        tool_id=tool_id,
        timeout_seconds=timeout_seconds,
    )


@app.command()
def report(workspace: Path = typer.Option(...)) -> None:
    """Create baseline provenance reports from staging records."""
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
    write_workbook(
        settings.reports_dir / "Portfolio_Analysis.xlsx",
        inventory,
        artifacts,
        evidence,
        datasources,
        dependencies,
        capabilities,
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
    )
    typer.echo(f"Reports written to {settings.reports_dir}")


@app.command()
def run(
    inventory: Path = typer.Option(..., exists=True, readable=True),
    workspace: Path = typer.Option(...),
) -> None:
    """Stage, then analyze staged Access copies on Windows, then produce reports."""
    stage(inventory=inventory, workspace=workspace)
    analyze(workspace=workspace, force=False)
    report(workspace=workspace)
