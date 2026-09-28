import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from portfolio_analyzer.access.export_safety import EnvironmentExportSafetyGate
from portfolio_analyzer.access.libraries import VerifiedApprovedLibrary
from portfolio_analyzer.access.windows_extractor import (
    AccessExtractionError,
    WindowsAccessExtractor,
    _DaoExtractionCoverage,
)
from portfolio_analyzer.config import AnalyzerSettings
from portfolio_analyzer.library_identity import (
    ApprovedLibraryReference,
    LibraryInjectionStatus,
)
from portfolio_analyzer.models import (
    AccessExtractedObject,
    AccessExtractionResult,
    VerifiedStagedArtifact,
)
from portfolio_analyzer.staging.hashing import sha256_file
from portfolio_analyzer.v2.workflow import snapshot_from_extraction_result


class _DefinitionDatabase:
    def __init__(self, documents: dict[str, list[Any]]) -> None:
        self._documents = documents

    def Containers(self, name: str) -> SimpleNamespace:  # noqa: N802 - mirrors COM
        return SimpleNamespace(Documents=self._documents.get(name, []))


class _WritingAccess:
    def __init__(self, *, failing_name: str | None = None) -> None:
        self.failing_name = failing_name
        self.calls: list[tuple[int, str, Path]] = []

    def SaveAsText(self, constant: int, name: str, target: str) -> None:  # noqa: N802
        path = Path(target)
        self.calls.append((constant, name, path))
        path.write_text("Definition body\nPWD=raw-export-secret", encoding="utf-8")
        if name == self.failing_name:
            raise RuntimeError("Password=raw-exception-secret")


class _TrackingTable:
    def __init__(
        self,
        *,
        name: str,
        connect: str,
        fields: list[Any] | None = None,
        fail_extraction: bool = False,
    ) -> None:
        self.Name = name
        self.Connect = connect
        self.SourceTableName = "dbo.Customer" if connect else ""
        self.Attributes = 0
        self._fields = fields or []
        self.field_reads = 0
        self.fail_extraction = fail_extraction

    @property
    def Fields(self) -> list[Any]:  # noqa: N802 - mirrors COM
        self.field_reads += 1
        if self.Connect:
            raise AssertionError("linked TableDef.Fields must not be enumerated")
        return self._fields


class _UnknownConnectTable:
    Name = "UnknownConnection"
    SourceTableName = "dbo.Unknown"
    Attributes = 0

    def __init__(self) -> None:
        self.field_reads = 0

    @property
    def Connect(self) -> str:  # noqa: N802 - mirrors COM
        raise RuntimeError("Connect unavailable")

    @property
    def Fields(self) -> list[Any]:  # noqa: N802 - mirrors COM
        self.field_reads += 1
        raise AssertionError("Fields are unsafe when link status is unknown")


class _PropertyFailureQuery:
    Name = "qryPropertyFailure"
    SQL = "SELECT * FROM LocalTable"
    Type = 0
    Connect = ""
    ReturnsRecords = True
    MaxRecords = 0
    Attributes = 0
    Parameters: list[Any] = []

    @property
    def ODBCTimeout(self) -> int:  # noqa: N802 - mirrors COM
        raise RuntimeError("property unavailable")


class _FailingObjectExtractor(WindowsAccessExtractor):
    def _table_object(
        self,
        table: Any,
        index: int,
        errors: list[str],
        progress: Any,
    ) -> Any:
        if getattr(table, "fail_extraction", False):
            raise RuntimeError("Password=object-secret")
        return super()._table_object(table, index, errors, progress)

    def _query_object(
        self,
        query: Any,
        index: int,
        errors: list[str],
        progress: Any,
    ) -> Any:
        if getattr(query, "fail_extraction", False):
            raise RuntimeError("PWD=query-secret")
        return super()._query_object(query, index, errors, progress)


class _BrokenComCollection:
    def __init__(self, first: Any) -> None:
        self.first = first

    def __iter__(self) -> Any:
        yield self.first
        raise RuntimeError("Password=enumerator-secret")


class _ExecutionTrapDatabase:
    def __init__(self) -> None:
        self.TableDefs = [_TrackingTable(name="linked", connect="ODBC;DSN=Finance")]
        self.QueryDefs = [_query(Name="qryStatic")]

    def Execute(self, *_args: Any) -> None:  # noqa: N802 - mirrors COM
        raise AssertionError("DAO Execute must never be called")

    def OpenRecordset(self, *_args: Any) -> None:  # noqa: N802 - mirrors COM
        raise AssertionError("DAO OpenRecordset must never be called")

    def RefreshLink(self, *_args: Any) -> None:  # noqa: N802 - mirrors COM
        raise AssertionError("DAO RefreshLink must never be called")


def _extractor(tmp_path: Path) -> WindowsAccessExtractor:
    return WindowsAccessExtractor(AnalyzerSettings(workspace=tmp_path / "workspace"))


def _query(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "Name": "qryNormal",
        "SQL": "SELECT * FROM LocalTable",
        "Type": 0,
        "Connect": "",
        "ReturnsRecords": True,
        "ODBCTimeout": 60,
        "MaxRecords": 0,
        "Attributes": 0,
        "Parameters": [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_querydef_metadata_includes_internal_pass_through_and_redacts_connect(
    tmp_path: Path,
) -> None:
    pass_through = _query(
        Name="~sq_crosstab_detail",
        SQL="SELECT * FROM dbo.Deal",
        Type=112,
        Connect=(
            "ODBC;DRIVER={ODBC Driver 17 for SQL Server};SERVER=SQL01;"
            "DATABASE=CorporateTrust;UID=analyst;PWD=TOP-SECRET"
        ),
        ReturnsRecords=True,
        ODBCTimeout=90,
        MaxRecords=500,
        Parameters=[SimpleNamespace(Name="pDate", Type=8, Direction=1)],
    )
    database = SimpleNamespace(TableDefs=[], QueryDefs=[pass_through])
    errors: list[str] = []

    objects = _extractor(tmp_path)._schema_objects(database, errors, None)

    assert errors == []
    assert len(objects) == 1
    query = objects[0]
    assert query.object_type == "query"
    assert query.name == "~sq_crosstab_detail"
    assert query.definition == "SELECT * FROM dbo.Deal"
    assert query.properties["dao_type_code"] == "112"
    assert query.properties["query_kind"] == "pass_through"
    assert query.properties["returns_records"] == "True"
    assert query.properties["odbc_timeout"] == "90"
    assert query.properties["max_records"] == "500"
    assert query.properties["attributes"] == "0"
    assert query.properties["hidden"] == "true"
    assert query.properties["system"] == "true"
    assert json.loads(query.properties["parameters"]) == [
        {"direction": "1", "name": "pDate", "ordinal": "0", "type": "8"}
    ]
    connect = query.properties["connect"]
    assert "server=SQL01" in connect
    assert "database=CorporateTrust" in connect
    assert "driver=ODBC Driver 17 for SQL Server" in connect
    assert "uid=<redacted>" in connect
    assert "pwd=<redacted>" in connect
    assert "analyst" not in connect
    assert "TOP-SECRET" not in connect


def test_query_property_failure_is_scoped_and_later_query_continues(tmp_path: Path) -> None:
    database = SimpleNamespace(
        TableDefs=[],
        QueryDefs=[_PropertyFailureQuery(), _query(Name="qryAfterFailure")],
    )
    errors: list[str] = []

    objects = _extractor(tmp_path)._schema_objects(database, errors, None)

    assert [item.name for item in objects] == ["qryPropertyFailure", "qryAfterFailure"]
    assert objects[0].properties["odbc_timeout"] == ""
    assert any("QueryDef[0].ODBCTimeout unavailable" in error for error in errors)


def test_mandatory_dao_lane_never_calls_execution_recordset_or_link_refresh(
    tmp_path: Path,
) -> None:
    errors: list[str] = []

    objects = _extractor(tmp_path)._schema_objects(
        _ExecutionTrapDatabase(), errors, None
    )

    assert [item.name for item in objects] == ["linked", "qryStatic"]
    assert errors == []


def test_schema_extraction_isolates_object_failures_and_accounts_for_every_dao_item(
    tmp_path: Path,
) -> None:
    failed_table = _TrackingTable(name="badTable", connect="", fail_extraction=True)
    good_table = _TrackingTable(name="goodTable", connect="")
    failed_query = _query(Name="badQuery", fail_extraction=True)
    good_query = _query(Name="goodQuery")
    database = SimpleNamespace(
        TableDefs=[failed_table, good_table],
        QueryDefs=[failed_query, good_query],
    )
    coverage = _DaoExtractionCoverage()
    errors: list[str] = []

    objects = _FailingObjectExtractor(
        AnalyzerSettings(workspace=tmp_path / "workspace")
    )._schema_objects(database, errors, None, coverage=coverage)

    assert [item.name for item in objects] == ["goodTable", "goodQuery"]
    assert coverage.tabledef_enumerated_count == 2
    assert coverage.tabledef_succeeded_count == 1
    assert coverage.tabledef_failed_count == 1
    assert coverage.querydef_enumerated_count == 2
    assert coverage.querydef_succeeded_count == 1
    assert coverage.querydef_failed_count == 1
    assert any("TableDef[0] extraction failed" in error for error in errors)
    assert any("QueryDef[0] extraction failed" in error for error in errors)
    assert all("object-secret" not in error for error in errors)
    assert all("query-secret" not in error for error in errors)


def test_core_dao_collection_enumeration_failure_remains_fatal(tmp_path: Path) -> None:
    database = SimpleNamespace(
        TableDefs=[],
        QueryDefs=_BrokenComCollection(_query(Name="qryBeforeEnumeratorFailure")),
    )
    coverage = _DaoExtractionCoverage()
    errors: list[str] = []

    with pytest.raises(AccessExtractionError, match="DAO QueryDefs enumeration failed") as caught:
        _extractor(tmp_path)._schema_objects(
            database,
            errors,
            None,
            coverage=coverage,
        )

    assert coverage.querydef_enumerated_count == 1
    assert coverage.querydef_succeeded_count == 1
    assert coverage.querydef_failed_count == 0
    assert "enumerator-secret" not in str(caught.value)


def test_querydef_attributes_and_parameter_property_failures_are_preserved_safely(
    tmp_path: Path,
) -> None:
    class _ParameterWithMissingDirection:
        Name = "pAccount"
        Type = 10

        @property
        def Direction(self) -> int:  # noqa: N802 - mirrors COM
            raise RuntimeError("direction unavailable")

    query = _query(
        Name="qrySystemHidden",
        Attributes=-2147483645,
        Parameters=[
            _ParameterWithMissingDirection(),
            SimpleNamespace(Name="pAsOf", Type=8, Direction=1),
        ],
    )
    errors: list[str] = []

    result = _extractor(tmp_path)._query_object(query, 0, errors, None)

    assert result.properties["attributes"] == "-2147483645"
    assert result.properties["hidden"] == "true"
    assert result.properties["system"] == "true"
    assert json.loads(result.properties["parameters"]) == [
        {"direction": "", "name": "pAccount", "ordinal": "0", "type": "10"},
        {"direction": "1", "name": "pAsOf", "ordinal": "1", "type": "8"},
    ]
    assert any("Parameters[0].Direction unavailable" in error for error in errors)


def test_export_lane_retains_mutated_copy_hash_when_export_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged_database = tmp_path / "staged.accdb"
    staged_database.write_bytes(b"verified-staged-bytes")
    working_bundle = tmp_path / "working"
    working_bundle.mkdir()
    working_database = working_bundle / staged_database.name
    working_database.write_bytes(staged_database.read_bytes())
    artifact = VerifiedStagedArtifact(
        artifact_id="artifact-1",
        tool_inventory_id="app-1",
        local_staged_path=staged_database,
        filename=staged_database.name,
        extension=".accdb",
        size_bytes=staged_database.stat().st_size,
        sha256=sha256_file(staged_database),
    )
    extractor = _extractor(tmp_path)
    library_reference = ApprovedLibraryReference(
        filename="reviewed.accdb",
        sha256="b" * 64,
    )
    approved_library = VerifiedApprovedLibrary(
        path=tmp_path / "reviewed.accdb",
        reference=library_reference,
    )
    monkeypatch.setattr(
        extractor,
        "_prepare_working_bundle",
        lambda *_args, **_kwargs: (working_database, working_bundle, [], []),
    )

    def mutate_copy(_path: Path, _client: Any) -> None:
        working_database.write_bytes(b"mutated-disposable-copy")

    monkeypatch.setattr(extractor, "_enable_and_verify_startup_bypass", mutate_copy)
    monkeypatch.setattr(extractor, "_open_with_startup_bypass", lambda *_args: None)
    monkeypatch.setattr(
        extractor,
        "_export_definitions",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("SaveAsText failed")),
    )
    monkeypatch.setitem(sys.modules, "win32api", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "win32con", SimpleNamespace())
    fake_access = SimpleNamespace(
        AutomationSecurity=None,
        Visible=None,
        CurrentDb=lambda: SimpleNamespace(),
    )
    win32_client = SimpleNamespace(DispatchEx=lambda _name: fake_access)

    outcome = extractor._run_export_lane(
        artifact,
        staged_database,
        tmp_path / "exports",
        win32_client,
        approved_libraries=(approved_library,),
        progress=None,
        cleanup=False,
    )

    assert outcome.objects == []
    assert any(
        "SaveAsText lane failed safely" in warning for warning in outcome.warnings
    )
    assert outcome.derived_copy_sha256 == sha256_file(working_database)
    assert outcome.derived_copy_sha256 != artifact.sha256
    assert outcome.injected_libraries == (library_reference,)
    assert outcome.library_injection_status == LibraryInjectionStatus.INJECTED


def test_extraction_coverage_survives_strict_snapshot_conversion(tmp_path: Path) -> None:
    extracted = AccessExtractionResult(
        tool_inventory_id="app-1",
        artifact_id="artifact-1",
        staged_path=tmp_path / "app.accdb",
        extractor_version="fixture-extractor",
        approved_libraries=[
            ApprovedLibraryReference(filename="reviewed.accdb", sha256="b" * 64)
        ],
        injected_libraries=[
            ApprovedLibraryReference(filename="reviewed.accdb", sha256="b" * 64)
        ],
        library_injection_status=LibraryInjectionStatus.INJECTED,
        objects=[AccessExtractedObject(object_type="table", name="LocalTable")],
        extraction_errors=["one object failed"],
        coverage_status="partial",
        tabledef_enumerated_count=2,
        tabledef_succeeded_count=1,
        tabledef_failed_count=1,
        querydef_enumerated_count=3,
        querydef_succeeded_count=2,
        querydef_failed_count=1,
    )

    snapshot = snapshot_from_extraction_result(
        application_id="app-1",
        artifact_id="artifact-1",
        artifact_sha256="a" * 64,
        staged_path=tmp_path / "app.accdb",
        extracted=extracted,
    )

    assert snapshot.tabledef_enumerated_count == 2
    assert snapshot.tabledef_succeeded_count == 1
    assert snapshot.tabledef_failed_count == 1
    assert snapshot.querydef_enumerated_count == 3
    assert snapshot.querydef_succeeded_count == 2
    assert snapshot.querydef_failed_count == 1
    assert snapshot.approved_libraries == tuple(extracted.approved_libraries)
    assert snapshot.injected_libraries == tuple(extracted.injected_libraries)
    assert snapshot.library_injection_status == LibraryInjectionStatus.INJECTED


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"tool_inventory_id": "other-app"}, "different application"),
        ({"artifact_id": "other-artifact"}, "different artifact"),
        ({"staged_path": Path("other.accdb")}, "different staged path"),
    ],
)
def test_snapshot_conversion_rejects_mismatched_worker_identity(
    tmp_path: Path,
    update: dict[str, object],
    message: str,
) -> None:
    staged_path = tmp_path / "staged.accdb"
    staged_path.write_bytes(b"primary")
    extracted = AccessExtractionResult(
        tool_inventory_id="app-1",
        artifact_id="artifact-1",
        staged_path=staged_path,
        extractor_version="fixture-extractor",
    ).model_copy(update=update)

    with pytest.raises(ValueError, match=message):
        snapshot_from_extraction_result(
            application_id="app-1",
            artifact_id="artifact-1",
            artifact_sha256="a" * 64,
            staged_path=staged_path,
            extracted=extracted,
        )


def test_linked_tables_skip_fields_while_local_tables_capture_them(tmp_path: Path) -> None:
    linked = _TrackingTable(
        name="lnkCustomer",
        connect="ODBC;SERVER=SQL01;DATABASE=Warehouse;UID=user;PWD=secret",
    )
    local = _TrackingTable(
        name="CustomerCache",
        connect="",
        fields=[
            SimpleNamespace(
                Name="CustomerId",
                Type=4,
                Size=4,
                Required=True,
                AllowZeroLength=False,
            )
        ],
    )
    database = SimpleNamespace(TableDefs=[linked, local], QueryDefs=[])
    errors: list[str] = []

    objects = _extractor(tmp_path)._schema_objects(database, errors, None)

    assert errors == []
    assert linked.field_reads == 0
    assert local.field_reads == 1
    assert objects[0].object_type == "linked_table"
    assert "fields" not in objects[0].properties
    assert objects[1].object_type == "table"
    assert json.loads(objects[1].properties["fields"]) == [
        {
            "allow_zero_length": "False",
            "name": "CustomerId",
            "required": "True",
            "size": "4",
            "type": "4",
        }
    ]
    assert "secret" not in objects[0].properties["connect"]


def test_connect_property_failure_does_not_trigger_field_enumeration(tmp_path: Path) -> None:
    table = _UnknownConnectTable()
    errors: list[str] = []

    result = _extractor(tmp_path)._table_object(table, 0, errors, None)

    assert table.field_reads == 0
    assert "fields" not in result.properties
    assert any("TableDef[0].Connect unavailable" in error for error in errors)


def test_save_as_text_uses_generated_paths_and_removes_raw_exports(tmp_path: Path) -> None:
    working_bundle = tmp_path / "working-bundle"
    working_bundle.mkdir()
    successful_name = "../../escape/PWD=object-name-secret"
    failing_name = "failure-form"
    database = _DefinitionDatabase(
        {
            "Forms": [
                SimpleNamespace(Name=successful_name),
                SimpleNamespace(Name=failing_name),
            ]
        }
    )
    access = _WritingAccess(failing_name=failing_name)
    errors: list[str] = []

    results = _extractor(tmp_path)._export_definitions(
        access, database, working_bundle, errors, None
    )

    assert len(results) == 1
    assert "object-name-secret" not in results[0].name
    assert "raw-export-secret" not in (results[0].definition or "")
    assert len(access.calls) == 2
    export_root = (working_bundle / "exports").resolve()
    for constant, original_name, target in access.calls:
        assert constant == 2
        assert original_name in {successful_name, failing_name}
        assert target.parent == export_root
        assert re.fullmatch(r"form-[0-9a-f]{24}\.txt", target.name)
        assert original_name not in target.name
        assert not target.exists()
    assert not export_root.exists()
    assert all("raw-exception-secret" not in error for error in errors)
    assert any("Could not export form" in error for error in errors)


@pytest.mark.parametrize(
    ("network", "macros", "low_privilege", "expected_fragment"),
    [
        (None, "1", True, "network isolation"),
        ("1", None, True, "macro policy"),
        ("1", "1", False, "identity is privileged"),
    ],
)
def test_save_as_text_gate_fails_closed_without_every_worker_control(
    monkeypatch: pytest.MonkeyPatch,
    network: str | None,
    macros: str | None,
    low_privilege: bool,
    expected_fragment: str,
) -> None:
    monkeypatch.setattr(
        "portfolio_analyzer.access.export_safety.platform.system", lambda: "Windows"
    )
    monkeypatch.setattr(
        "portfolio_analyzer.access.export_safety._is_low_privilege_windows_identity",
        lambda: low_privilege,
    )
    for name, value in (
        ("ACCESS_ANALYZER_NETWORK_ISOLATED", network),
        ("ACCESS_ANALYZER_MACROS_DISABLED", macros),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    decision = EnvironmentExportSafetyGate().evaluate()

    assert not decision.permitted
    assert expected_fragment in decision.reason


def test_save_as_text_gate_accepts_attested_unprivileged_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "portfolio_analyzer.access.export_safety.platform.system", lambda: "Windows"
    )
    monkeypatch.setattr(
        "portfolio_analyzer.access.export_safety._is_low_privilege_windows_identity",
        lambda: True,
    )
    monkeypatch.setenv("ACCESS_ANALYZER_NETWORK_ISOLATED", "1")
    monkeypatch.setenv("ACCESS_ANALYZER_MACROS_DISABLED", "1")

    assert EnvironmentExportSafetyGate().evaluate().permitted
