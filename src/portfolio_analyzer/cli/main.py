"""Portfolio CLI.  Commands preserve the staging-before-analysis boundary."""

from __future__ import annotations

import json
import multiprocessing
import os
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
from portfolio_analyzer.naming import euc_directory_name
from portfolio_analyzer.persistence.database import create_session_factory
from portfolio_analyzer.persistence.repository import save_inventory_and_artifact
from portfolio_analyzer.portfolio.recommendations import build_recommendations
from portfolio_analyzer.reporting.coverage import build_analysis_coverage
from portfolio_analyzer.reporting.intelligence import (
    write_architecture_mermaid,
    write_intelligence_html,
    write_report_manifest,
    write_semantic_datasets,
)
from portfolio_analyzer.reporting.writers import write_csv, write_executive_pdf, write_workbook
from portfolio_analyzer.semantic.config import (
    load_semantic_settings,
    write_semantic_settings_template,
)
from portfolio_analyzer.semantic.context import (
    initialize_context_file,
    initialize_gold_set,
    load_claims,
    read_gold_set,
)
from portfolio_analyzer.semantic.model_store import APPROVED_MODEL, acquire_approved_model
from portfolio_analyzer.semantic.pipeline import (
    evaluate_gold_set,
    preflight_semantic_provider,
    run_semantic_pipeline,
    semantic_state_is_current,
)
from portfolio_analyzer.semantic.provider import LocalTransformersProvider, SemanticProviderError
from portfolio_analyzer.semantic.review import apply_review_decisions, import_review_workbook
from portfolio_analyzer.semantic.state import (
    read_review_decisions,
    read_semantic_state,
    write_review_decisions,
    write_semantic_state,
)
from portfolio_analyzer.staging.copying import (
    ArtifactStager,
    migrate_legacy_application_directories,
)
from portfolio_analyzer.versions import STATIC_ANALYSIS_VERSION

app = typer.Typer(no_args_is_help=True, help="Static, evidence-driven Access portfolio analysis.")


def _settings(workspace: Path) -> AnalyzerSettings:
    settings = AnalyzerSettings(workspace=workspace.resolve())
    settings.ensure_workspace()
    return settings


def _read_state(state_path: Path) -> tuple[list[InventoryRecord], list[StagedArtifact]]:
    state = _read_json(state_path)
    return (
        [InventoryRecord.model_validate(item) for item in state["inventory"]],
        [StagedArtifact.model_validate(item) for item in state["artifacts"]],
    )


def _analysis_state_path(settings: AnalyzerSettings) -> Path:
    return settings.analysis_dir / "analysis_state.json"


def _extraction_state_path(settings: AnalyzerSettings) -> Path:
    return settings.extracted_dir / "extraction_state.json"


def _semantic_config_path(settings: AnalyzerSettings) -> Path:
    return settings.semantic_dir / "semantic.toml"


def _semantic_state_path(settings: AnalyzerSettings) -> Path:
    return settings.semantic_dir / "semantic_state.json"


def _semantic_context_path(settings: AnalyzerSettings) -> Path:
    return settings.semantic_dir / "business_context.csv"


def _semantic_gold_path(settings: AnalyzerSettings) -> Path:
    return settings.semantic_dir / "gold_set.csv"


def _review_decisions_path(settings: AnalyzerSettings) -> Path:
    return settings.semantic_dir / "review_decisions.json"


def _read_json(path: Path) -> Any:
    """Read JSON without first making a second, file-sized string in memory."""
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def _write_json_atomic(path: Path, value: Any) -> None:
    """Stream JSON to a temporary file and publish it only after a complete write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as destination:
            json.dump(value, destination, indent=2)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _extraction_snapshot_path(settings: AnalyzerSettings, euc_name: str, sha256: str) -> Path:
    return settings.extracted_dir / euc_directory_name(euc_name) / f"snapshot-{sha256}.json"


def _has_extraction(result: dict[str, Any]) -> bool:
    return "extracted" in result or "snapshot_path" in result


def _load_extracted_application(
    settings: AnalyzerSettings, result: dict[str, Any]
) -> ExtractedApplication:
    """Load either a legacy embedded extraction or a current per-application snapshot."""
    if "extracted" in result:
        return ExtractedApplication.model_validate(result["extracted"])
    relative_path = result.get("snapshot_path")
    if not isinstance(relative_path, str):
        raise ValueError("Extraction result has no snapshot payload")
    extraction_root = settings.extracted_dir.resolve()
    snapshot_path = (extraction_root / relative_path).resolve()
    if not snapshot_path.is_relative_to(extraction_root):
        raise ValueError(f"Extraction snapshot is outside the workspace: {relative_path}")
    return ExtractedApplication.model_validate(_read_json(snapshot_path))


def _snapshot_result(
    settings: AnalyzerSettings,
    artifact: StagedArtifact,
    euc_name: str,
    extractor_version: str,
    extracted: ExtractedApplication,
) -> dict[str, Any]:
    """Persist a large payload separately and return its lightweight index entry."""
    if artifact.sha256 is None:
        raise ValueError("A staged artifact must have a SHA-256 before extraction")
    snapshot_path = _extraction_snapshot_path(settings, euc_name, artifact.sha256)
    _write_json_atomic(snapshot_path, extracted.model_dump(mode="json"))
    return {
        "tool_inventory_id": artifact.tool_inventory_id,
        "sha256": artifact.sha256,
        "extractor_version": extractor_version,
        "snapshot_path": snapshot_path.relative_to(settings.extracted_dir).as_posix(),
        "object_count": len(extracted.objects),
        "extraction_errors": list(extracted.extraction_errors),
    }


def _externalize_embedded_extractions(settings: AnalyzerSettings, state: dict[str, Any]) -> bool:
    """Upgrade legacy portfolio-sized payloads to small entries plus snapshot files."""
    applications = state.get("applications", [])
    if not isinstance(applications, list):
        return False
    names = _inventory_names(settings)
    changed = False
    for index, result in enumerate(applications):
        if not isinstance(result, dict) or "extracted" not in result:
            continue
        sha256 = result.get("sha256")
        tool_inventory_id = result.get("tool_inventory_id")
        extracted = result.get("extracted")
        if (
            not isinstance(sha256, str)
            or not isinstance(tool_inventory_id, str)
            or not isinstance(extracted, dict)
        ):
            continue
        euc_name = names.get(tool_inventory_id, tool_inventory_id)
        snapshot_path = _extraction_snapshot_path(settings, euc_name, sha256)
        _write_json_atomic(snapshot_path, extracted)
        objects = extracted.get("objects", [])
        errors = extracted.get("extraction_errors", [])
        lightweight = {key: value for key, value in result.items() if key != "extracted"}
        lightweight.update(
            {
                "snapshot_path": snapshot_path.relative_to(settings.extracted_dir).as_posix(),
                "object_count": len(objects) if isinstance(objects, list) else 0,
                "extraction_errors": list(errors) if isinstance(errors, list) else [],
            }
        )
        applications[index] = lightweight
        changed = True
    return changed


def _read_extraction_state_for_resume(output_path: Path) -> dict[str, Any]:
    """Preserve malformed state for diagnosis and start a safe rebuild."""
    if not output_path.exists():
        return {}
    try:
        state = _read_json(output_path)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        backup = output_path.with_name(
            f"{output_path.stem}.corrupt-{time.time_ns()}{output_path.suffix}"
        )
        output_path.replace(backup)
        typer.echo(
            f"The prior extraction state is incomplete ({exc}). "
            f"It was preserved as {backup.name}; rebuilding the extraction index."
        )
        return {}
    if not isinstance(state, dict):
        raise typer.BadParameter(f"Invalid extraction state in {output_path}: expected an object.")
    return state


def _load_extraction_state(settings: AnalyzerSettings) -> tuple[dict[str, Any], bool]:
    """Load extraction snapshots, migrating the pre-split state format when available."""
    extraction_path = _extraction_state_path(settings)
    if extraction_path.exists():
        state = _read_json(extraction_path)
        migrated = _externalize_embedded_extractions(settings, state)
        if migrated:
            _write_json_atomic(extraction_path, state)
        return state, migrated
    legacy_path = _analysis_state_path(settings)
    if not legacy_path.exists():
        raise typer.BadParameter("No extraction state found. Run 'extract' on Windows first.")
    legacy = _read_json(legacy_path)
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
    _externalize_embedded_extractions(settings, state)
    _write_json_atomic(extraction_path, state)
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


def _inventory_names(settings: AnalyzerSettings) -> dict[str, str]:
    state_path = settings.analysis_dir / "staging_state.json"
    if not state_path.exists():
        return {}
    inventory, _ = _read_state(state_path)
    return {item.tool_inventory_id: item.tool_name for item in inventory}


def _current_analysis_results(
    settings: AnalyzerSettings, artifacts: list[StagedArtifact]
) -> list[dict[str, Any]]:
    analysis_path = _analysis_state_path(settings)
    state = _read_json(analysis_path) if analysis_path.exists() else {}
    current_artifacts = {
        (item.tool_inventory_id, item.sha256) for item in artifacts if item.is_primary
    }
    return [
        result
        for result in state.get("applications", [])
        if result.get("analysis_version") == STATIC_ANALYSIS_VERSION
        and (result.get("tool_inventory_id"), result.get("sha256")) in current_artifacts
    ]


def _normalized_analysis_results(
    analysis_results: list[dict[str, Any]],
) -> tuple[list[Evidence], list[Datasource], list[Dependency]]:
    evidence = [
        Evidence.model_validate(value)
        for result in analysis_results
        for value in result.get("evidence", [])
    ]
    datasources = [
        Datasource.model_validate(value)
        for result in analysis_results
        for value in result.get("datasources", [])
    ]
    dependencies = [
        Dependency.model_validate(value)
        for result in analysis_results
        for value in result.get("dependencies", [])
    ]
    return evidence, datasources, dependencies


def _current_extractions(
    settings: AnalyzerSettings,
    artifacts: list[StagedArtifact],
    *,
    tool_id: str | None = None,
) -> list[tuple[str, ExtractedApplication]]:
    extraction_state, _ = _load_extraction_state(settings)
    current = {
        (item.tool_inventory_id, item.sha256)
        for item in artifacts
        if item.is_primary and item.sha256
    }
    output: list[tuple[str, ExtractedApplication]] = []
    for result in extraction_state.get("applications", []):
        key = (result.get("tool_inventory_id"), result.get("sha256"))
        if key not in current or not _has_extraction(result):
            continue
        if tool_id is not None and result.get("tool_inventory_id") != tool_id:
            continue
        output.append((str(result["sha256"]), _load_extracted_application(settings, result)))
    return output


def _euc_name(tool_inventory_id: str, names: dict[str, str]) -> str:
    return names.get(tool_inventory_id, "Unknown EUC")


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
    _write_json_atomic(path, result)


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
                worker_result = _read_json(result_path)
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
                worker_result = _read_json(result_path)
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
    for message in migrate_legacy_application_directories(settings, records):
        typer.echo(message)
    stager = ArtifactStager(settings)
    session_factory = create_session_factory(settings.analysis_dir / "evidence.sqlite")
    artifacts_by_source: dict[tuple[str, str], tuple[InventoryRecord, StagedArtifact]] = {}
    for record in records:
        for artifact in stager.stage_application_bundle(record):
            key = (
                artifact.tool_inventory_id,
                str(artifact.original_source_path).casefold(),
            )
            existing = artifacts_by_source.get(key)
            if existing is None:
                artifacts_by_source[key] = (record, artifact)
                continue
            existing_record, existing_artifact = existing
            if artifact.status.value == "staged" and existing_artifact.status.value != "staged":
                artifacts_by_source[key] = (record, artifact)
            elif artifact.is_primary and not existing_artifact.is_primary:
                existing_artifact.is_primary = True
                artifacts_by_source[key] = (record, existing_artifact)
            else:
                artifacts_by_source[key] = (existing_record, existing_artifact)
    artifacts = [artifact for _, artifact in artifacts_by_source.values()]
    with session_factory.begin() as session:
        for record, artifact in artifacts_by_source.values():
            save_inventory_and_artifact(session, record, artifact)
    state_path = settings.analysis_dir / "staging_state.json"
    _write_json_atomic(
        state_path,
        {
            "inventory": [record.model_dump(mode="json") for record in records],
            "artifacts": [item.model_dump(mode="json") for item in artifacts],
        },
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
    previous = _read_extraction_state_for_resume(output_path) if not force else {}
    extractor = WindowsAccessExtractor(settings)
    names = _inventory_names(settings)
    if _externalize_embedded_extractions(settings, previous):
        _write_json_atomic(output_path, previous)
        typer.echo("Migrated prior embedded extractions to per-application snapshots.")
    completed = {
        (result["tool_inventory_id"], result["sha256"])
        for result in previous.get("applications", [])
        if result.get("extractor_version") == extractor.version and _has_extraction(result)
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
        euc_name = names.get(artifact.tool_inventory_id, artifact.tool_inventory_id)
        if key in completed:
            typer.echo(f"[{euc_name}] unchanged; reusing prior extraction.")
            continue
        try:
            typer.echo(
                f"[{euc_name}] extracting {artifact.filename} (timeout: {timeout_seconds}s)..."
            )
            extracted = _extract_with_timeout(
                artifact,
                settings.extracted_dir / euc_directory_name(euc_name),
                settings,
                timeout_seconds,
                partial(_echo_progress, euc_name),
            )
            results_by_key[key] = _snapshot_result(
                settings, artifact, euc_name, extractor.version, extracted
            )
            if extracted.extraction_errors:
                typer.echo(f"[{euc_name}] completed with extraction warnings:")
                for error in extracted.extraction_errors:
                    typer.echo(f"[{euc_name}]   {error}")
            else:
                typer.echo(f"[{euc_name}] completed.")
        except Exception as exc:
            # Continue the portfolio. This path never retries extraction against the source file.
            results_by_key[key] = {
                "tool_inventory_id": artifact.tool_inventory_id,
                "sha256": artifact.sha256,
                "extractor_version": extractor.version,
                "error": str(exc),
            }
            typer.echo(f"[{euc_name}] failed safely: {exc}")
        # Checkpoint each application. A crash cannot discard previously completed work.
        _write_json_atomic(output_path, {"applications": list(results_by_key.values())})
    if not output_path.exists() or not eligible:
        _write_json_atomic(output_path, {"applications": list(results_by_key.values())})
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
    previous = _read_json(output_path) if output_path.exists() and not force else {}
    completed = {
        (
            result["tool_inventory_id"],
            result["sha256"],
            result.get("extractor_version"),
        )
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
        if _has_extraction(result) and (tool_id is None or result["tool_inventory_id"] == tool_id)
    ]
    names = _inventory_names(settings)
    typer.echo(f"{len(eligible)} saved extraction snapshots are eligible for static analysis.")
    for result in eligible:
        key = (result["tool_inventory_id"], result["sha256"])
        analysis_key = (*key, result.get("extractor_version"))
        euc_name = names.get(result["tool_inventory_id"], result["tool_inventory_id"])
        if analysis_key in completed:
            typer.echo(f"[{euc_name}] unchanged; reusing prior analysis.")
            continue
        try:
            typer.echo(f"[{euc_name}] analyzing saved extraction snapshot...")
            extracted = _load_extracted_application(settings, result)
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
            typer.echo(f"[{euc_name}] analysis complete.")
        except Exception as exc:
            results_by_key[key] = {
                "tool_inventory_id": result["tool_inventory_id"],
                "sha256": result["sha256"],
                "extractor_version": result["extractor_version"],
                "analysis_version": STATIC_ANALYSIS_VERSION,
                "error": str(exc),
            }
            typer.echo(f"[{euc_name}] analysis failed safely: {exc}")
    _write_json_atomic(output_path, {"applications": list(results_by_key.values())})
    typer.echo(f"Analysis state written to {output_path}")


@app.command("analyze-tool")
def analyze_tool(
    tool_id: str = typer.Option(..., "--id", help="Tool Inventory ID to analyze."),
    workspace: Path = typer.Option(...),
    force: bool = typer.Option(False, help="Re-run static analysis from its saved snapshot."),
) -> None:
    """Re-analyze one saved extraction snapshot without reopening Access."""
    analyze(workspace=workspace, force=force, tool_id=tool_id)


@app.command("semantic-init")
def semantic_init(workspace: Path = typer.Option(...)) -> None:
    """Create local semantic configuration, owner-context, and gold-set templates."""
    settings = _settings(workspace)
    state_path = settings.analysis_dir / "staging_state.json"
    inventory: list[InventoryRecord] = []
    artifacts: list[StagedArtifact] = []
    if state_path.exists():
        inventory, artifacts = _read_state(state_path)
    analysis_results = _current_analysis_results(settings, artifacts)
    evidence, _, _ = _normalized_analysis_results(analysis_results)
    capabilities = discover_capabilities(evidence)
    extraction_path = _extraction_state_path(settings)
    extraction_state = _read_json(extraction_path) if extraction_path.exists() else {}
    coverage = build_analysis_coverage(
        inventory,
        artifacts,
        extraction_state,
        {"applications": analysis_results},
        capabilities,
    )
    created = [
        (
            _semantic_config_path(settings),
            write_semantic_settings_template(_semantic_config_path(settings)),
        ),
        (
            _semantic_context_path(settings),
            initialize_context_file(_semantic_context_path(settings), inventory),
        ),
        (
            _semantic_gold_path(settings),
            initialize_gold_set(_semantic_gold_path(settings), inventory, coverage, size=20),
        ),
    ]
    for path, was_created in created:
        typer.echo(f"{'Created' if was_created else 'Preserved'}: {path}")
    typer.echo(
        "The approved model is not downloaded automatically. Install semantic dependencies, "
        "then run 'portfolio-analyzer semantic-model-download --workspace ...'."
    )
    typer.echo(
        "Only that explicit acquisition command can contact Hugging Face; semantic analysis "
        "loads the verified local files directly in offline mode."
    )


@app.command("semantic-model-download")
def semantic_model_download(workspace: Path = typer.Option(...)) -> None:
    """Acquire the allowlisted model at its immutable Hugging Face revision."""
    settings = _settings(workspace)
    config_path = _semantic_config_path(settings)
    if not config_path.exists():
        raise typer.BadParameter("No semantic configuration found. Run 'semantic-init' first.")
    semantic_settings = load_semantic_settings(config_path)
    typer.echo(
        f"Downloading approved model {APPROVED_MODEL.repo_id}@{APPROVED_MODEL.revision} "
        f"to {semantic_settings.model.local_path}"
    )
    try:
        manifest = acquire_approved_model(semantic_settings.model.local_path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise typer.BadParameter(f"Approved model acquisition failed: {exc}") from exc
    typer.echo(
        json.dumps(
            {
                "repo_id": manifest.repo_id,
                "revision": manifest.revision,
                "license": manifest.license,
                "architecture": manifest.architecture,
                "files": len(manifest.files),
                "manifest_sha256": manifest.manifest_sha256,
            },
            indent=2,
        )
    )


@app.command("semantic-check")
def semantic_check(workspace: Path = typer.Option(...)) -> None:
    """Verify the local model, structured output, citations, and gold-set quality offline."""
    settings = _settings(workspace)
    config_path = _semantic_config_path(settings)
    if not config_path.exists():
        raise typer.BadParameter("No semantic configuration found. Run 'semantic-init' first.")
    semantic_settings = load_semantic_settings(config_path)
    try:
        provider = LocalTransformersProvider(semantic_settings)
        result = preflight_semantic_provider(semantic_settings, provider)
    except (OSError, SemanticProviderError, ValueError) as exc:
        raise typer.BadParameter(f"Offline semantic preflight failed: {exc}") from exc
    state_path = settings.analysis_dir / "staging_state.json"
    inventory, _ = _read_state(state_path) if state_path.exists() else ([], [])
    try:
        state = read_semantic_state(_semantic_state_path(settings))
    except ValueError as exc:
        raise typer.BadParameter(f"Semantic state is incompatible; rerun semantic: {exc}") from exc
    gold_result = evaluate_gold_set(read_gold_set(_semantic_gold_path(settings)), inventory, state)
    typer.echo(json.dumps({"provider": result, "gold_set": gold_result}, indent=2))
    if not gold_result.get("passed"):
        raise typer.Exit(code=1)


@app.command("semantic")
def semantic_analysis(
    workspace: Path = typer.Option(...),
    tool_id: str | None = typer.Option(None, help="Refresh one tool inventory ID."),
    force: bool = typer.Option(False, help="Refresh compatible cached semantic results."),
) -> None:
    """Run resumable semantic analysis with the approved in-process model, fully offline."""
    settings = _settings(workspace)
    config_path = _semantic_config_path(settings)
    if not config_path.exists():
        raise typer.BadParameter("No semantic configuration found. Run 'semantic-init' first.")
    semantic_settings = load_semantic_settings(config_path)
    try:
        provider = LocalTransformersProvider(semantic_settings)
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(f"Approved local model verification failed: {exc}") from exc
    staging_path = settings.analysis_dir / "staging_state.json"
    if not staging_path.exists():
        raise typer.BadParameter("No staging state found. Run 'stage' first.")
    inventory, artifacts = _read_state(staging_path)
    if tool_id is not None and tool_id not in {item.tool_inventory_id for item in inventory}:
        raise typer.BadParameter(f"Unknown tool inventory ID: {tool_id}")
    analysis_results = _current_analysis_results(settings, artifacts)
    evidence, datasources, _ = _normalized_analysis_results(analysis_results)
    capabilities = discover_capabilities(evidence)
    extraction_state, _ = _load_extraction_state(settings)
    coverage = build_analysis_coverage(
        inventory,
        artifacts,
        extraction_state,
        {"applications": analysis_results},
        capabilities,
    )
    claims = load_claims(
        _semantic_context_path(settings),
        inventory,
        include_inventory_description=semantic_settings.policy.include_inventory_description,
    )
    extracted = _current_extractions(settings, artifacts)
    if not extracted:
        raise typer.BadParameter("No current extraction snapshots are available.")
    try:
        prior_state = read_semantic_state(_semantic_state_path(settings))
    except ValueError as exc:
        typer.echo(f"Prior semantic state is incompatible and will be replaced: {exc}")
        prior_state = None
    try:
        state = run_semantic_pipeline(
            semantic_settings,
            provider,
            inventory,
            artifacts,
            extracted,
            evidence,
            datasources,
            coverage,
            claims,
            prior_state=prior_state,
            tool_id=tool_id,
            force=force,
            progress=typer.echo,
        )
    except (SemanticProviderError, ValueError) as exc:
        raise typer.BadParameter(f"Semantic analysis stopped safely: {exc}") from exc
    decisions = read_review_decisions(_review_decisions_path(settings))
    if decisions:
        state = apply_review_decisions(state, decisions)
    write_semantic_state(_semantic_state_path(settings), state)
    typer.echo(
        f"Semantic state written to {_semantic_state_path(settings)} "
        f"({len(state.applications)} profiles, {len(state.errors)} isolated errors)."
    )


@app.command("import-review")
def import_review(
    workspace: Path = typer.Option(...),
    workbook: Path = typer.Option(..., exists=True, readable=True),
) -> None:
    """Import Accept, Edit, or Reject decisions from the workbook Review Queue."""
    settings = _settings(workspace)
    state = read_semantic_state(_semantic_state_path(settings))
    if state is None:
        raise typer.BadParameter("No semantic state found. Run 'semantic' first.")
    imported = import_review_workbook(workbook)
    decisions = read_review_decisions(_review_decisions_path(settings))
    decisions.update(imported)
    updated = apply_review_decisions(state, decisions)
    write_review_decisions(_review_decisions_path(settings), decisions)
    write_semantic_state(_semantic_state_path(settings), updated)
    typer.echo(f"Imported {len(imported)} review decisions; {len(decisions)} retained in total.")


@app.command()
def report(
    workspace: Path = typer.Option(...),
    semantic_mode: str = typer.Option(
        "auto",
        "--semantic-mode",
        help="Semantic reporting policy: auto, require, or off.",
    ),
) -> None:
    """Create deterministic reports and compatible semantic intelligence when available."""
    semantic_mode = semantic_mode.casefold().strip()
    if semantic_mode not in {"auto", "require", "off"}:
        raise typer.BadParameter("--semantic-mode must be auto, require, or off")
    settings = _settings(workspace)
    state_path = settings.analysis_dir / "staging_state.json"
    if not state_path.exists():
        raise typer.BadParameter("No staging state found. Run 'stage' first.")
    inventory, artifacts = _read_state(state_path)
    application_names = {item.tool_inventory_id: item.tool_name for item in inventory}
    analysis_results = _current_analysis_results(settings, artifacts)
    evidence, datasources, dependencies = _normalized_analysis_results(analysis_results)
    capabilities = discover_capabilities(evidence)
    recommendations = build_recommendations(capabilities, datasources)
    extraction_path = _extraction_state_path(settings)
    extraction_state = _read_json(extraction_path) if extraction_path.exists() else {}
    coverage = build_analysis_coverage(
        inventory,
        artifacts,
        extraction_state,
        {"applications": analysis_results},
        capabilities,
    )
    semantic_state = None
    semantic_partial = False
    semantic_status = "disabled by --semantic-mode off"
    decisions = read_review_decisions(_review_decisions_path(settings))
    if semantic_mode != "off":
        config_path = _semantic_config_path(settings)
        state_error: str | None = None
        try:
            state = read_semantic_state(_semantic_state_path(settings))
        except ValueError as exc:
            state = None
            state_error = str(exc)
        if not config_path.exists():
            semantic_status = "not configured; run semantic-init"
        elif state_error is not None:
            semantic_status = f"incompatible semantic state; rerun semantic analysis: {state_error}"
        elif state is None:
            semantic_status = "not run; deterministic report only"
        else:
            try:
                semantic_settings = load_semantic_settings(config_path)
                claims = load_claims(
                    _semantic_context_path(settings),
                    inventory,
                    include_inventory_description=(
                        semantic_settings.policy.include_inventory_description
                    ),
                )
                if semantic_state_is_current(
                    state,
                    semantic_settings,
                    inventory,
                    artifacts,
                    claims,
                ):
                    semantic_state = apply_review_decisions(state, decisions)
                    failed_profiles = sum(
                        item.status == "failed" for item in semantic_state.applications
                    )
                    partial_profiles = sum(
                        item.status == "partial" for item in semantic_state.applications
                    )
                    profile_total = len(semantic_state.applications)
                    application_total = len(set(application_names))
                    complete_ids = {
                        item.tool_inventory_id
                        for item in semantic_state.applications
                        if item.status == "complete"
                    }
                    semantic_partial = (
                        len(complete_ids) != application_total
                        or profile_total != application_total
                        or failed_profiles > 0
                        or partial_profiles > 0
                        or bool(semantic_state.errors)
                    )
                    semantic_status = (
                        f"current: {len(complete_ids)}/{application_total} complete profiles; "
                        f"{partial_profiles} partial; {failed_profiles} failed; "
                        f"{len(semantic_state.errors)} recorded errors"
                    )
                else:
                    semantic_status = (
                        "stale or incompatible with current artifacts, context, versions, "
                        "parameters, or model checksums"
                    )
            except (OSError, ValueError) as exc:
                semantic_status = f"unavailable: {exc}"
        if semantic_mode == "require" and (semantic_state is None or semantic_partial):
            raise typer.BadParameter(
                f"Complete current semantic results are required but unavailable: {semantic_status}"
            )
    produced: list[Path] = []
    workbook_path = settings.reports_dir / "Portfolio_Analysis.xlsx"
    write_workbook(
        workbook_path,
        inventory,
        artifacts,
        evidence,
        datasources,
        dependencies,
        capabilities,
        recommendations=recommendations,
        coverage=coverage,
        semantic=semantic_state,
        semantic_status=semantic_status,
        review_decisions=decisions,
    )
    produced.append(workbook_path)
    pdf_path = settings.reports_dir / "Portfolio_Analysis.pdf"
    write_executive_pdf(
        pdf_path,
        inventory,
        artifacts,
        capabilities,
        recommendations=recommendations,
        evidence=evidence,
        datasources=datasources,
        dependencies=dependencies,
        coverage=coverage,
        semantic=semantic_state,
        semantic_status=semantic_status,
    )
    produced.append(pdf_path)
    html_path = settings.reports_dir / "Portfolio_Intelligence.html"
    write_intelligence_html(
        html_path,
        inventory,
        coverage,
        semantic_state,
        semantic_status=semantic_status,
        dependencies=dependencies,
    )
    produced.append(html_path)
    write_csv(
        settings.reports_dir / "applications.csv",
        [
            {
                "euc_name": item.tool_name,
                "inventory_file_name": item.inventory_filename,
                "stated_description": item.stated_description or "",
                "original_source_path": str(item.filepath),
            }
            for item in inventory
        ],
        headers=[
            "euc_name",
            "inventory_file_name",
            "stated_description",
            "original_source_path",
        ],
    )
    write_csv(
        settings.reports_dir / "artifacts.csv",
        [
            {
                "euc_name": _euc_name(item.tool_inventory_id, application_names),
                "original_source_path": str(item.original_source_path),
                "local_staged_path": str(item.local_staged_path or ""),
                "sha256": item.sha256 or "",
                "status": item.status.value,
                "error": item.error or "",
            }
            for item in artifacts
        ],
        headers=[
            "euc_name",
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
                "euc_name": item.tool_name,
                "primary_file": item.inventory_filename,
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
            "euc_name",
            "primary_file",
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
                "euc_name": _euc_name(item.tool_inventory_id, application_names),
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
            "euc_name",
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
                "euc_name": _euc_name(item.tool_inventory_id, application_names),
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
            "euc_name",
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
                "euc_name": _euc_name(item.tool_inventory_id, application_names),
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
            "euc_name",
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
                "euc_name": _euc_name(item.tool_inventory_id, application_names),
                "capability": item.capability,
                "layer": item.layer,
                "confidence": item.confidence.value,
                "evidence_count": len(item.evidence),
            }
            for item in capabilities
        ],
        headers=[
            "euc_name",
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
                "affected_euc_names": " | ".join(
                    _euc_name(tool_id, application_names) for tool_id in item.affected_tool_ids
                ),
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
            "affected_euc_names",
            "application_count",
            "evidence_count",
            "rationale",
        ],
    )
    produced.extend(
        settings.reports_dir / name
        for name in (
            "applications.csv",
            "artifacts.csv",
            "analysis_coverage.csv",
            "evidence.csv",
            "datasources.csv",
            "dependencies.csv",
            "capabilities.csv",
            "recommendations.csv",
        )
    )
    if semantic_state is not None:
        produced.extend(
            write_semantic_datasets(
                settings.reports_dir,
                semantic_state,
                application_names,
            )
        )
        mermaid_path = settings.reports_dir / "Target_Architecture.md"
        write_architecture_mermaid(mermaid_path, semantic_state)
        produced.append(mermaid_path)
    manifest_path = settings.reports_dir / "report_manifest.json"
    write_report_manifest(
        manifest_path,
        produced,
        semantic_state,
        semantic_status=semantic_status,
    )
    typer.echo(f"Reports written to {settings.reports_dir}")
    typer.echo(f"Semantic coverage: {semantic_status}")


@app.command()
def run(
    inventory: Path = typer.Option(..., exists=True, readable=True),
    workspace: Path = typer.Option(...),
    with_semantic: bool = typer.Option(
        False,
        "--semantic",
        help="Run local semantic analysis before reporting.",
    ),
) -> None:
    """Stage, extract locally on Windows, analyze saved snapshots, then produce reports."""
    stage(inventory=inventory, workspace=workspace)
    extract(workspace=workspace, force=False, tool_id=None, timeout_seconds=300)
    analyze(workspace=workspace, force=False, tool_id=None)
    if with_semantic:
        semantic_analysis(workspace=workspace, tool_id=None, force=False)
    report(workspace=workspace, semantic_mode="require" if with_semantic else "auto")
