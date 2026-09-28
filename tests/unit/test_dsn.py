from __future__ import annotations

from dataclasses import dataclass, field

from portfolio_analyzer.parsing.dsn import (
    DsnResolutionStatus,
    RegistryBitness,
    RegistryDsnRecord,
    RegistryLookupOutcome,
    RegistryPermissionDenied,
    RegistryScope,
    resolve_windows_dsn,
)


@dataclass
class FakeRegistryReader:
    records: dict[tuple[str, RegistryScope, RegistryBitness], RegistryDsnRecord] = field(
        default_factory=dict
    )
    denied: set[tuple[str, RegistryScope, RegistryBitness]] = field(default_factory=set)
    calls: list[tuple[str, RegistryScope, RegistryBitness]] = field(default_factory=list)

    def read_dsn(
        self,
        name: str,
        *,
        scope: RegistryScope,
        bitness: RegistryBitness,
    ) -> RegistryDsnRecord | None:
        key = (name, scope, bitness)
        self.calls.append(key)
        if key in self.denied:
            raise RegistryPermissionDenied()
        return self.records.get(key)


def _record(**values: object) -> RegistryDsnRecord:
    return RegistryDsnRecord(values)


def test_resolves_static_registry_metadata_with_provenance_and_override() -> None:
    reader = FakeRegistryReader(
        records={
            ("finance", RegistryScope.USER, RegistryBitness.X64): _record(
                Driver="ODBC Driver 18 for SQL Server",
                Server="SQL01",
                Database="Warehouse",
                UID="registry-user",
                Password="registry-secret",
                Custom="private-value",
            )
        }
    )

    resolution = resolve_windows_dsn(
        "DSN=Finance;Server=tcp:SQL02,1433;UID=connection-user;PWD=connection-secret",
        registry_reader=reader,
        target_bitness=64,
    )

    assert resolution.status is DsnResolutionStatus.RESOLVED
    assert resolution.dsn == "finance"
    assert resolution.identity is not None
    assert resolution.identity.platform == "sql_server"
    assert resolution.identity.driver == "odbc driver 18 for sql server"
    assert resolution.identity.server == "sql02"
    assert resolution.identity.database == "warehouse"
    assert resolution.declared_identity is not None
    assert resolution.declared_identity.server == "sql02"
    assert len(resolution.registry_candidates) == 1
    assert resolution.registry_candidates[0].identity.server == "sql01"
    assert resolution.provenance[0].outcome is RegistryLookupOutcome.MATCHED
    assert resolution.provenance[0].scope is RegistryScope.USER
    assert resolution.provenance[0].bitness is RegistryBitness.X64
    rendered = repr(resolution)
    assert "registry-secret" not in rendered
    assert "private-value" not in rendered
    assert "connection-secret" not in rendered


def test_equivalent_user_and_system_records_resolve_with_both_provenances() -> None:
    records = {
        ("finance", scope, RegistryBitness.X64): _record(
            Driver="ODBC Driver 18 for SQL Server", Server="SQL01", Database="Warehouse"
        )
        for scope in (RegistryScope.USER, RegistryScope.SYSTEM)
    }

    resolution = resolve_windows_dsn(
        "DSN=Finance",
        registry_reader=FakeRegistryReader(records=records),
        target_bitness=RegistryBitness.X64,
    )

    assert resolution.status is DsnResolutionStatus.RESOLVED
    assert resolution.candidate_count == 2
    assert sum(item.outcome is RegistryLookupOutcome.MATCHED for item in resolution.provenance) == 2


def test_user_registry_record_precedes_conflicting_shadowed_system_record() -> None:
    reader = FakeRegistryReader(
        records={
            ("finance", RegistryScope.USER, RegistryBitness.X64): _record(Server="SQL01"),
            ("finance", RegistryScope.SYSTEM, RegistryBitness.X64): _record(Server="SQL02"),
        }
    )

    resolution = resolve_windows_dsn(
        "DSN=Finance", registry_reader=reader, target_bitness=64
    )

    assert resolution.status is DsnResolutionStatus.RESOLVED
    assert resolution.identity is not None
    assert resolution.identity.server == "sql01"
    assert resolution.reason == "registry_identity_resolved_user_precedence_shadowed_conflict"
    assert resolution.candidate_count == 2


def test_dsn_found_only_in_other_registry_view_reports_wrong_bitness() -> None:
    reader = FakeRegistryReader(
        records={
            ("finance", RegistryScope.SYSTEM, RegistryBitness.X86): _record(
                Driver="SQL Server", Server="SQL01"
            )
        }
    )

    resolution = resolve_windows_dsn(
        "DSN=Finance", registry_reader=reader, target_bitness=64
    )

    assert resolution.status is DsnResolutionStatus.WRONG_BITNESS
    assert resolution.identity is not None
    assert resolution.identity.server == "sql01"


def test_permission_denied_is_a_safe_first_class_status() -> None:
    reader = FakeRegistryReader(
        records={
            ("finance", RegistryScope.SYSTEM, RegistryBitness.X64): _record(Server="SQL01")
        },
        denied={("finance", RegistryScope.USER, RegistryBitness.X64)}
    )

    resolution = resolve_windows_dsn(
        "DSN=Finance", registry_reader=reader, target_bitness=64
    )

    assert resolution.status is DsnResolutionStatus.PERMISSION_DENIED
    assert resolution.reason == "target_registry_view_denied"
    assert resolution.provenance[0].outcome is RegistryLookupOutcome.PERMISSION_DENIED
    assert resolution.candidate_count == 1


def test_missing_dsn_and_absent_registry_entry_are_unresolved() -> None:
    no_dsn = resolve_windows_dsn(
        "Server=SQL01;Database=Warehouse",
        registry_reader=FakeRegistryReader(),
        target_bitness=64,
    )
    not_found = resolve_windows_dsn(
        "DSN=Finance", registry_reader=FakeRegistryReader(), target_bitness=64
    )

    assert no_dsn.status is DsnResolutionStatus.UNRESOLVED
    assert no_dsn.reason == "dsn_missing"
    assert no_dsn.identity is not None
    assert no_dsn.identity.server == "sql01"
    assert not_found.status is DsnResolutionStatus.UNRESOLVED
    assert not_found.reason == "dsn_not_found"
    assert len(not_found.provenance) == 4


def test_file_dsn_is_not_opened_or_interpreted() -> None:
    reader = FakeRegistryReader()
    resolution = resolve_windows_dsn(
        r"FILEDSN=C:\Config\finance.dsn;UID=reader;PWD=secret",
        registry_reader=reader,
        target_bitness=64,
    )

    assert resolution.status is DsnResolutionStatus.UNSUPPORTED_FILE_DSN
    assert resolution.reason == "file_dsn_not_read"
    assert "secret" not in repr(resolution)
    assert reader.calls == []


def test_malformed_and_conflicting_dsn_inputs_fail_without_registry_reads() -> None:
    reader = FakeRegistryReader()

    unclosed = resolve_windows_dsn(
        "DSN={Finance;PWD=do-not-print", registry_reader=reader, target_bitness=64
    )
    conflicting = resolve_windows_dsn(
        "DSN=Finance;DSN=Operations", registry_reader=reader, target_bitness=64
    )

    assert unclosed.status is DsnResolutionStatus.MALFORMED
    assert conflicting.status is DsnResolutionStatus.MALFORMED
    assert "do-not-print" not in repr(unclosed)
    assert reader.calls == []
