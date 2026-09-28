"""Isolated, timeout-bounded Access extraction worker."""

from __future__ import annotations

import ctypes
import json
import multiprocessing
import os
import platform
import shutil
import subprocess
import time
from collections.abc import Callable
from contextlib import suppress
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any

from portfolio_analyzer.access.windows_extractor import WindowsAccessExtractor
from portfolio_analyzer.config import AnalyzerSettings
from portfolio_analyzer.models import AccessExtractionResult, VerifiedStagedArtifact
from portfolio_analyzer.redaction import redact_sensitive_text


class AccessExtractionTimeoutError(TimeoutError):
    """Raised when the isolated Access worker exceeds its deadline."""


def run_extraction_with_timeout(
    artifact: VerifiedStagedArtifact,
    destination: Path,
    settings: AnalyzerSettings,
    *,
    timeout_seconds: int,
    on_progress: Callable[[str], None],
) -> AccessExtractionResult:
    context = multiprocessing.get_context("spawn")
    destination.mkdir(parents=True, exist_ok=True)
    progress_path = destination / f".progress-{os.getpid()}.txt"
    result_path = destination / f".result-{os.getpid()}.json"
    process = context.Process(
        target=_worker,
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
                process.join(2)
                if process.is_alive():
                    _terminate_worker_tree(process)
                break
            if progress_path.exists():
                progress = progress_path.read_text(encoding="utf-8")
                if progress and progress != last_progress:
                    last_progress = progress
                    on_progress(redact_sensitive_text(progress))
        if process.is_alive():
            _terminate_worker_tree(process)
            operation = last_progress or "before the first extraction checkpoint"
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
        if worker_result.get("status") != "ok":
            raise RuntimeError(str(worker_result.get("error", "extraction worker failed")))
        return AccessExtractionResult.model_validate(worker_result["extracted"])
    finally:
        _release_shift_key()
        if process.is_alive():
            _terminate_worker_tree(process)
        process.close()
        progress_path.unlink(missing_ok=True)
        result_path.unlink(missing_ok=True)
        for working_bundle in destination.glob("_working_bundle-*"):
            if working_bundle.is_dir() and not working_bundle.is_symlink():
                shutil.rmtree(working_bundle, ignore_errors=True)


def _worker(
    artifact: VerifiedStagedArtifact,
    destination: str,
    workspace: str,
    progress_path: str,
    result_path: str,
) -> None:
    try:
        settings = AnalyzerSettings(workspace=Path(workspace))

        def progress(message: str) -> None:
            Path(progress_path).write_text(
                redact_sensitive_text(message), encoding="utf-8"
            )

        extracted = WindowsAccessExtractor(settings).extract(
            artifact,
            Path(destination),
            progress=progress,
            cleanup=True,
        )
        result: dict[str, Any] = {
            "status": "ok",
            "extracted": extracted.model_dump(mode="json"),
        }
    except Exception as exc:
        result = {
            "status": "error",
            "error": f"{type(exc).__name__}: {redact_sensitive_text(str(exc))}",
        }
    _write_json_atomic(Path(result_path), result)


def _terminate_worker_tree(process: BaseProcess) -> None:
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
    _release_shift_key()


def _release_shift_key() -> None:
    if platform.system() != "Windows":
        return
    with suppress(Exception):
        ctypes.windll.user32.keybd_event(0x10, 0, 0x0002, 0)  # type: ignore[attr-defined]


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise ValueError("worker result must be a JSON object")
    return value


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as destination:
            json.dump(value, destination, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
