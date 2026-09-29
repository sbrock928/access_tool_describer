"""Bounded parameter IPC, explicit coverage gaps, and staged integrity."""

import json
from pathlib import Path
from typing import Any

import pytest

from portfolio_analyzer.access import query_parameters as subject
from portfolio_analyzer.config import AnalyzerSettings
from portfolio_analyzer.models import (
    AccessExtractedObject,
    AccessExtractionResult,
    VerifiedStagedArtifact,
)
from portfolio_analyzer.staging.hashing import sha256_file
from portfolio_analyzer.v2.workflow import snapshot_from_extraction_result


class Connection:
    def __init__(self, replies: list[Any]) -> None:
        self.replies = list(replies)
        self.sent: list[Any] = []
        self.timeouts: list[float] = []
        self.closed = False

    def poll(self, timeout: float) -> bool:
        self.timeouts.append(timeout)
        if self.replies[0] is None:
            self.replies.pop(0)
            return False
        return True

    def recv_bytes(self) -> bytes:
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return json.dumps(reply).encode()

    def send_bytes(self, value: bytes) -> None:
        self.sent.append(json.loads(value))

    def close(self) -> None:
        self.closed = True


class Process:
    def __init__(self) -> None:
        self.pid: int | None = None
        self.alive = False
        self.closed = False

    def start(self) -> None:
        self.pid = 123
        self.alive = True

    def join(self, timeout: float) -> None:
        pass

    def is_alive(self) -> bool:
        return self.alive

    def close(self) -> None:
        assert not self.alive
        self.closed = True


class Context:
    def __init__(self, scripts: list[list[Any]]) -> None:
        self.scripts = list(scripts)
        self.connections: list[Connection] = []
        self.children: list[Connection] = []
        self.processes: list[Process] = []
        self.killed: list[Process] = []

    def Pipe(self, *, duplex: bool) -> tuple[Connection, Connection]:  # noqa: N802
        assert duplex
        if self.processes:
            assert self.processes[-1].closed
        connection = Connection(self.scripts.pop(0))
        child = Connection([])
        self.connections.append(connection)
        self.children.append(child)
        return connection, child

    def Process(self, *, target: Any, args: Any) -> Process:  # noqa: N802
        assert target is subject._parameter_worker
        process = Process()
        self.processes.append(process)
        return process

    def kill(self, process: Process) -> None:
        self.killed.append(process)
        process.alive = False


def setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scripts: list[list[Any]],
) -> tuple[Context, AccessExtractionResult, VerifiedStagedArtifact, AnalyzerSettings]:
    context = Context(scripts)
    monkeypatch.setattr(subject.multiprocessing, "get_context", lambda method: context)
    monkeypatch.setattr("portfolio_analyzer.access.worker._terminate_worker_tree", context.kill)
    settings = AnalyzerSettings(workspace=tmp_path / "workspace")
    settings.staged_tools_dir.mkdir(parents=True)
    path = settings.staged_tools_dir / "synthetic.accdb"
    path.write_bytes(b"synthetic staged database")
    artifact = VerifiedStagedArtifact(
        artifact_id="artifact", tool_inventory_id="app", local_staged_path=path,
        filename=path.name, extension=".accdb", size_bytes=path.stat().st_size,
        sha256=sha256_file(path),
    )
    extracted = AccessExtractionResult(
        tool_inventory_id="app", artifact_id="artifact", staged_path=path,
        extractor_version="synthetic", querydef_enumerated_count=2, querydef_succeeded_count=2,
        objects=[AccessExtractedObject(
            object_type="query", name=f"q{index}", definition=f"SELECT {index}",
            properties={"parameter_metadata_status": "deferred", "parameters": "[]",
                        "dao_query_index": str(index)},
        ) for index in range(2)],
    )
    return context, extracted, artifact, settings


def response(name: str = "pId") -> dict[str, Any]:
    return {"status": "available", "parameters": [
        {"ordinal": "0", "name": name, "type": "4", "direction": "1"},
    ]}


def test_timeout_retains_sql_restarts_worker_and_records_snapshot_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, extracted, artifact, settings = setup(
        tmp_path, monkeypatch, [["ready", None], ["ready", response()]],
    )
    messages: list[str] = []
    result = subject.enrich_query_parameters(
        extracted, artifact, settings.extracted_dir, settings, progress=messages.append,
    )
    assert [obj.definition for obj in result.objects] == ["SELECT 0", "SELECT 1"]
    assert [obj.properties["parameter_metadata_status"] for obj in result.objects] == [
        "unavailable_timeout", "available",
    ]
    assert result.coverage_status == "partial"
    assert result.querydef_succeeded_count == 2
    assert context.connections[0].timeouts == [30, 10]
    assert context.connections[1].sent == [{"index": 1, "name": "q1"}, None]
    assert len(context.killed) == 2
    assert all(c.closed for c in context.connections + context.children)
    assert all(p.closed for p in context.processes)
    snapshot = snapshot_from_extraction_result(
        application_id="app", artifact_id="artifact", artifact_sha256=artifact.sha256,
        staged_path=artifact.local_staged_path, extracted=result,
    )
    assert snapshot.coverage_status == "partial"
    assert "unavailable_timeout" in snapshot.warnings[0]
    assert snapshot.objects[0].sanitized_properties["parameter_metadata_status"] == (
        "unavailable_timeout"
    )
    assert "SELECT" not in "\n".join(messages)


def test_success_reuses_one_child_and_sanitizes_new_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, extracted, artifact, settings = setup(
        tmp_path, monkeypatch, [["ready", response("PWD=secret-canary"), response()]],
    )
    result = subject.enrich_query_parameters(
        extracted, artifact, settings.extracted_dir, settings, progress=lambda _: None,
    )
    assert len(context.processes) == 1
    assert context.connections[0].sent == [
        {"index": 0, "name": "q0"}, {"index": 1, "name": "q1"}, None,
    ]
    assert result.coverage_status == "complete"
    assert result.extraction_errors == []
    assert "secret-canary" not in result.model_dump_json()
    assert json.loads(result.objects[1].properties["parameters"])[0]["name"] == "pId"


@pytest.mark.parametrize("replies", [
    [None], ["wrong ready"], ["ready", {"status": "worker_unavailable"}],
    ["ready", EOFError("PWD=secret-canary")],
    ["ready", {"status": "available", "parameters": [{"unexpected": "secret-canary"}]}],
])
def test_unavailable_worker_preserves_all_queries_without_restart_storm_or_exception_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replies: list[Any],
) -> None:
    context, extracted, artifact, settings = setup(tmp_path, monkeypatch, [replies])
    result = subject.enrich_query_parameters(
        extracted, artifact, settings.extracted_dir, settings, progress=lambda _: None,
    )
    assert len(context.processes) == 1
    assert result.coverage_status == "partial"
    assert len(result.objects) == len(result.extraction_errors) == 2
    assert all(obj.properties["parameter_metadata_status"] == "unavailable_error"
               for obj in result.objects)
    assert "secret-canary" not in result.model_dump_json()
    assert context.processes[0].closed


def test_staged_tampering_rejected_even_after_parameter_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, extracted, artifact, settings = setup(
        tmp_path, monkeypatch, [["ready", response(), response()]],
    )
    artifact.local_staged_path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="staged artifact changed"):
        subject.enrich_query_parameters(
            extracted, artifact, settings.extracted_dir, settings, progress=lambda _: None,
        )
    assert context.processes[0].closed


@pytest.mark.parametrize("matching_name", [True, False])
def test_child_verifies_stage_opens_read_only_and_checks_query_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, matching_name: bool,
) -> None:
    import sys
    from types import SimpleNamespace

    _, _, artifact, settings = setup(tmp_path, monkeypatch, [])
    monkeypatch.setattr("portfolio_analyzer.access.safety.platform.system", lambda: "Windows")
    opens: list[tuple[Any, ...]] = []
    reads: list[int] = []
    closes: list[bool] = []

    def query(index: int) -> Any:
        reads.append(index)
        return SimpleNamespace(Name="q0", Parameters=[])

    database = SimpleNamespace(QueryDefs=query, Close=lambda: closes.append(True))

    def open_database(*args: Any) -> Any:
        opens.append(args)
        return database

    engine = SimpleNamespace(OpenDatabase=open_database)
    monkeypatch.setitem(sys.modules, "win32com", SimpleNamespace(client=SimpleNamespace()))
    monkeypatch.setitem(sys.modules, "win32com.client", SimpleNamespace())
    monkeypatch.setattr(
        "portfolio_analyzer.access.windows_extractor._create_dao_engine", lambda client: engine,
    )
    connection = Connection([{"index": 0, "name": "q0" if matching_name else "other"}, None])
    subject._parameter_worker(
        connection, artifact, str(settings.extracted_dir), str(settings.workspace),
    )
    assert opens == [(str(artifact.local_staged_path), False, True)]
    assert reads == [0]
    assert closes == [True]
    assert connection.closed
    assert connection.sent == ["ready", {
        "status": "available" if matching_name else "unavailable_error", "parameters": [],
    }]


def test_child_rejects_tampered_stage_before_loading_com(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, artifact, settings = setup(tmp_path, monkeypatch, [])
    monkeypatch.setattr("portfolio_analyzer.access.safety.platform.system", lambda: "Windows")
    artifact.local_staged_path.write_bytes(b"changed")
    connection = Connection([])
    subject._parameter_worker(
        connection, artifact, str(settings.extracted_dir), str(settings.workspace),
    )
    assert connection.sent == [{"status": "worker_unavailable"}]
    assert connection.closed
