"""Bound DAO parameter metadata reads in a reusable, disposable child process."""

from __future__ import annotations

import json
import multiprocessing
from collections.abc import Callable
from contextlib import suppress
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

from portfolio_analyzer.access.safety import validate_access_extraction_request
from portfolio_analyzer.config import AnalyzerSettings
from portfolio_analyzer.models import (
    AccessExtractedObject,
    AccessExtractionResult,
    VerifiedStagedArtifact,
)
from portfolio_analyzer.redaction import redact_sensitive_text
from portfolio_analyzer.staging.hashing import sha256_file

PARAMETER_TIMEOUT_SECONDS = 10
STARTUP_TIMEOUT_SECONDS = 30


def _parameter_worker(
    connection: Connection, artifact: VerifiedStagedArtifact, destination: str, workspace: str,
) -> None:
    from portfolio_analyzer.access.windows_extractor import _create_dao_engine, _parameter_metadata

    database: Any = None
    try:
        path = validate_access_extraction_request(
            artifact, Path(destination), AnalyzerSettings(workspace=Path(workspace)),
        )
        if sha256_file(path) != artifact.sha256:
            raise ValueError("staged artifact integrity failure")
        import win32com.client  # type: ignore[import-untyped]

        engine = _create_dao_engine(win32com.client)
        database = engine.OpenDatabase(str(path), False, True)
        connection.send_bytes(b'"ready"')
        while True:
            request = json.loads(connection.recv_bytes())
            if request is None:
                break
            if (
                not isinstance(request, dict) or set(request) != {"index", "name"}
                or type(request["index"]) is not int or request["index"] < 0
                or not isinstance(request["name"], str)
            ):
                raise ValueError("invalid parameter request")
            errors: list[str] = []
            try:
                query = database.QueryDefs(request["index"])
                if redact_sensitive_text(str(query.Name)) != request["name"]:
                    raise ValueError("query identity mismatch")
                parameters = _parameter_metadata(query, errors, "QueryDef")
                status = "unavailable_error" if errors else "available"
            except Exception:
                parameters, status = [], "unavailable_error"
            connection.send_bytes(json.dumps({"status": status, "parameters": parameters},
                                             ensure_ascii=True).encode("utf-8"))
    except (EOFError, BrokenPipeError):
        pass
    except Exception:
        # Never transport raw COM exception text, paths or connection values.
        with suppress(Exception):
            connection.send_bytes(b'{"status":"worker_unavailable"}')
    finally:
        connection.close()
        if database is not None:
            with suppress(Exception):
                database.Close()


def enrich_query_parameters(
    extracted: AccessExtractionResult, artifact: VerifiedStagedArtifact, destination: Path,
    settings: AnalyzerSettings, *, progress: Callable[[str], None],
) -> AccessExtractionResult:
    """Keep every source definition; explicitly mark unavailable parameter evidence.

    A timed-out child is killed before the next query is attempted in a fresh child.
    No COM objects cross processes. The enclosing extraction worker retains its total deadline.
    """
    from portfolio_analyzer.access.worker import _terminate_worker_tree

    context = multiprocessing.get_context("spawn")
    process: Any = None
    connection: Any = None
    warnings = list(extracted.extraction_errors)
    objects = []
    unavailable_worker = False

    def stop() -> None:
        nonlocal process, connection
        if connection is not None:
            with suppress(Exception):
                connection.send_bytes(b"null")
            connection.close()
            connection = None
        if process is not None:
            if process.pid is not None:
                process.join(0.5)
                if process.is_alive():
                    _terminate_worker_tree(process)
            process.close()
            process = None

    try:
        for ordinal, obj in enumerate(extracted.objects, 1):
            if obj.object_type != "query" or obj.properties.get("parameter_metadata_status") != (
                "deferred"
            ):
                objects.append(obj)
                continue
            parameters: list[dict[str, str]] = []
            status = "unavailable_error"
            if not unavailable_worker:
                try:
                    if process is None:
                        progress("Starting isolated DAO parameter metadata worker")
                        connection, child = context.Pipe(duplex=True)
                        process = context.Process(target=_parameter_worker, args=(
                            child, artifact, str(destination), str(settings.workspace),
                        ))
                        try:
                            process.start()
                        finally:
                            child.close()
                        if not connection.poll(STARTUP_TIMEOUT_SECONDS) or json.loads(
                            connection.recv_bytes(),
                        ) != "ready":
                            unavailable_worker = True
                            stop()
                    if not unavailable_worker:
                        progress(f"Reading isolated DAO parameters for object {ordinal}; "
                                 f"deadline={PARAMETER_TIMEOUT_SECONDS}s")
                        connection.send_bytes(json.dumps({
                            "index": int(obj.properties["dao_query_index"]), "name": obj.name,
                        }).encode("utf-8"))
                        if not connection.poll(PARAMETER_TIMEOUT_SECONDS):
                            status = "unavailable_timeout"
                            stop()
                        else:
                            value = json.loads(connection.recv_bytes())
                            if value.get("status") in {"available", "unavailable_error"}:
                                candidate = value["parameters"]
                                if not isinstance(candidate, list) or any(
                                    not isinstance(item, dict) or set(item) != {
                                        "ordinal", "name", "type", "direction",
                                    } or any(not isinstance(v, str) for v in item.values())
                                    for item in candidate
                                ):
                                    raise ValueError("invalid parameter response")
                                status, parameters = value["status"], candidate
                            else:
                                unavailable_worker = True
                                stop()
                except Exception:
                    # A broken process must not be reused for subsequent requests.
                    unavailable_worker = True
                    stop()
            properties = dict(obj.properties)
            properties.update(
                parameters=json.dumps(parameters, sort_keys=True, separators=(",", ":")),
                parameter_metadata_status=status,
            )
            objects.append(AccessExtractedObject.model_validate({
                **obj.model_dump(), "properties": properties,
            }))
            if status != "available":
                warnings.append(f"Query parameter metadata {status}: {obj.name}; "
                                "SQL and other metadata retained; parameter coverage is incomplete")
                progress(f"Parameter metadata {status} for object {ordinal}; "
                         "retaining source with explicit incomplete coverage")
    finally:
        stop()
    if sha256_file(artifact.local_staged_path) != artifact.sha256:
        raise ValueError("staged artifact changed during parameter metadata extraction")
    return AccessExtractionResult.model_validate({
        **extracted.model_dump(),
        "objects": objects, "extraction_errors": warnings,
        "coverage_status": "partial" if warnings else extracted.coverage_status,
    })
