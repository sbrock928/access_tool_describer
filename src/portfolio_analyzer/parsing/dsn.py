"""Static Windows ODBC DSN resolution without opening an ODBC connection."""

from __future__ import annotations

import struct
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from types import MappingProxyType
from typing import Protocol

from portfolio_analyzer.parsing.connections import (
    NormalizedConnectionIdentity,
    merge_connection_identities,
    normalize_connection_identity,
    parse_connection_string_details,
)


class DsnResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"
    WRONG_BITNESS = "wrong_bitness"
    PERMISSION_DENIED = "permission_denied"
    UNSUPPORTED_FILE_DSN = "unsupported_file_dsn"
    MALFORMED = "malformed"


class RegistryScope(StrEnum):
    USER = "user"
    SYSTEM = "system"


class RegistryBitness(IntEnum):
    X86 = 32
    X64 = 64


class RegistryLookupOutcome(StrEnum):
    MATCHED = "matched"
    NOT_FOUND = "not_found"
    PERMISSION_DENIED = "permission_denied"


class RegistryPermissionDenied(PermissionError):
    """A deliberately content-free registry permission failure."""

    def __init__(self) -> None:
        super().__init__("ODBC registry access denied")


@dataclass(frozen=True, slots=True)
class RegistryDsnRecord:
    """Raw registry values supplied to the resolver; hidden from representations."""

    values: Mapping[str, object] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


class RegistryReader(Protocol):
    """Injectable static registry boundary used by the cross-platform resolver."""

    def read_dsn(
        self,
        name: str,
        *,
        scope: RegistryScope,
        bitness: RegistryBitness,
    ) -> RegistryDsnRecord | None: ...


@dataclass(frozen=True, slots=True)
class DsnResolutionProvenance:
    source: str
    scope: RegistryScope
    bitness: RegistryBitness
    outcome: RegistryLookupOutcome
    lineage_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RegistryDsnCandidate:
    """One sanitized registry observation kept separate from declared overrides."""

    identity: NormalizedConnectionIdentity
    provenance: DsnResolutionProvenance


@dataclass(frozen=True, slots=True)
class DsnResolution:
    status: DsnResolutionStatus
    dsn: str | None
    identity: NormalizedConnectionIdentity | None
    declared_identity: NormalizedConnectionIdentity | None
    registry_candidates: tuple[RegistryDsnCandidate, ...]
    provenance: tuple[DsnResolutionProvenance, ...]
    reason: str
    candidate_count: int = 0


class WindowsRegistryReader:
    """Read ODBC registry metadata only; this class never imports or invokes ODBC APIs."""

    def read_dsn(
        self,
        name: str,
        *,
        scope: RegistryScope,
        bitness: RegistryBitness,
    ) -> RegistryDsnRecord | None:
        if sys.platform != "win32":
            return None

        import winreg

        root = (
            winreg.HKEY_CURRENT_USER
            if scope is RegistryScope.USER
            else winreg.HKEY_LOCAL_MACHINE
        )
        view_flag = (
            winreg.KEY_WOW64_32KEY if bitness is RegistryBitness.X86 else winreg.KEY_WOW64_64KEY
        )
        access = winreg.KEY_READ | view_flag
        base_path = r"Software\ODBC\ODBC.INI"
        dsn_path = f"{base_path}\\{name}"
        try:
            with winreg.OpenKey(root, dsn_path, 0, access) as key:
                values = _read_registry_values(key, winreg)
        except FileNotFoundError:
            return None
        except PermissionError as error:
            raise RegistryPermissionDenied() from error
        except OSError as error:
            if getattr(error, "winerror", None) == 5:
                raise RegistryPermissionDenied() from error
            return None

        # The DSN key often stores only a driver DLL path. The adjacent static list contains the
        # display driver name, which is more stable and useful for platform normalization.
        try:
            with winreg.OpenKey(root, f"{base_path}\\ODBC Data Sources", 0, access) as key:
                driver_name, _value_type = winreg.QueryValueEx(key, name)
                if isinstance(driver_name, str) and driver_name.strip():
                    values["Driver"] = driver_name
        except (FileNotFoundError, OSError):
            pass
        return RegistryDsnRecord(values)


def resolve_windows_dsn(
    connection_string: str,
    *,
    registry_reader: RegistryReader | None = None,
    target_bitness: RegistryBitness | int | None = None,
) -> DsnResolution:
    """Resolve a Windows DSN from registry metadata without making a database connection."""
    parsed = parse_connection_string_details(connection_string)
    connection_identity = normalize_connection_identity(parsed)
    if parsed.malformed or connection_identity.conflicts:
        return _resolution(
            DsnResolutionStatus.MALFORMED,
            dsn=None,
            identity=None,
            declared_identity=connection_identity,
            reason="malformed_connection_string",
        )

    file_dsns = _unique_normalized(
        (*parsed.values_for("filedsn"), *parsed.values_for("file dsn"))
    )
    if len(file_dsns) > 1:
        return _resolution(
            DsnResolutionStatus.MALFORMED,
            dsn=None,
            identity=None,
            declared_identity=connection_identity,
            reason="conflicting_file_dsn_values",
        )
    if file_dsns:
        return _resolution(
            DsnResolutionStatus.UNSUPPORTED_FILE_DSN,
            dsn=file_dsns[0],
            identity=connection_identity,
            declared_identity=connection_identity,
            reason="file_dsn_not_read",
        )

    dsns = _unique_normalized(parsed.values_for("dsn"))
    if len(dsns) > 1:
        return _resolution(
            DsnResolutionStatus.MALFORMED,
            dsn=None,
            identity=None,
            declared_identity=connection_identity,
            reason="conflicting_dsn_values",
        )
    if not dsns:
        return _resolution(
            DsnResolutionStatus.UNRESOLVED,
            dsn=None,
            identity=connection_identity,
            declared_identity=connection_identity,
            reason="dsn_missing",
        )

    dsn = dsns[0]
    if not _valid_registry_name(dsn):
        return _resolution(
            DsnResolutionStatus.MALFORMED,
            dsn=None,
            identity=None,
            declared_identity=connection_identity,
            reason="invalid_dsn_name",
        )

    bitness = _coerce_bitness(target_bitness)
    other_bitness = RegistryBitness.X86 if bitness is RegistryBitness.X64 else RegistryBitness.X64
    reader = registry_reader or WindowsRegistryReader()
    provenance: list[DsnResolutionProvenance] = []
    candidates: dict[RegistryBitness, list[RegistryDsnCandidate]] = {
        bitness: [],
        other_bitness: [],
    }
    permission_denied: dict[RegistryBitness, set[RegistryScope]] = {
        bitness: set(),
        other_bitness: set(),
    }

    for current_bitness in (bitness, other_bitness):
        for scope in (RegistryScope.USER, RegistryScope.SYSTEM):
            try:
                record = reader.read_dsn(dsn, scope=scope, bitness=current_bitness)
            except (RegistryPermissionDenied, PermissionError):
                provenance.append(
                    DsnResolutionProvenance(
                        source="windows_registry",
                        scope=scope,
                        bitness=current_bitness,
                        outcome=RegistryLookupOutcome.PERMISSION_DENIED,
                    )
                )
                permission_denied[current_bitness].add(scope)
                continue

            if record is None:
                provenance.append(
                    DsnResolutionProvenance(
                        source="windows_registry",
                        scope=scope,
                        bitness=current_bitness,
                        outcome=RegistryLookupOutcome.NOT_FOUND,
                    )
                )
                continue

            string_values = {
                str(key): str(value)
                for key, value in record.values.items()
                if isinstance(value, (str, int))
            }
            registry_identity = normalize_connection_identity(string_values)
            matched_provenance = DsnResolutionProvenance(
                source="windows_registry",
                scope=scope,
                bitness=current_bitness,
                outcome=RegistryLookupOutcome.MATCHED,
                lineage_keys=_lineage_keys(registry_identity),
            )
            candidates[current_bitness].append(
                RegistryDsnCandidate(
                    identity=registry_identity,
                    provenance=matched_provenance,
                )
            )
            provenance.append(matched_provenance)

    all_registry_candidates = tuple(
        candidate
        for current_bitness in (bitness, other_bitness)
        for candidate in candidates[current_bitness]
    )

    target_candidates = candidates[bitness]
    target_user = [
        item for item in target_candidates if item.provenance.scope is RegistryScope.USER
    ]
    target_system = [
        item for item in target_candidates if item.provenance.scope is RegistryScope.SYSTEM
    ]
    if RegistryScope.USER in permission_denied[bitness] or (
        not target_user
        and not target_system
        and RegistryScope.SYSTEM in permission_denied[bitness]
    ):
        return _resolution(
            DsnResolutionStatus.PERMISSION_DENIED,
            dsn=dsn,
            identity=None,
            declared_identity=connection_identity,
            registry_candidates=all_registry_candidates,
            provenance=tuple(provenance),
            reason="target_registry_view_denied",
            candidate_count=len(target_candidates),
        )
    selected_candidates = target_user or target_system
    if not selected_candidates:
        opposite_candidates = candidates[other_bitness]
        if opposite_candidates:
            opposite_user = [
                item
                for item in opposite_candidates
                if item.provenance.scope is RegistryScope.USER
            ]
            opposite_system = [
                item
                for item in opposite_candidates
                if item.provenance.scope is RegistryScope.SYSTEM
            ]
            selected_opposite = opposite_user or opposite_system
            unique_opposite = _unique_identities(
                [candidate.identity for candidate in selected_opposite]
            )
            effective_identity = (
                merge_connection_identities(unique_opposite[0], connection_identity)
                if len(unique_opposite) == 1
                else None
            )
            return _resolution(
                DsnResolutionStatus.WRONG_BITNESS,
                dsn=dsn,
                identity=effective_identity,
                declared_identity=connection_identity,
                registry_candidates=all_registry_candidates,
                provenance=tuple(provenance),
                reason="dsn_found_only_in_other_bitness",
                candidate_count=len(opposite_candidates),
            )
        return _resolution(
            DsnResolutionStatus.UNRESOLVED,
            dsn=dsn,
            identity=connection_identity,
            declared_identity=connection_identity,
            registry_candidates=all_registry_candidates,
            provenance=tuple(provenance),
            reason="dsn_not_found",
        )

    if any(candidate.identity.conflicts for candidate in selected_candidates):
        return _resolution(
            DsnResolutionStatus.AMBIGUOUS,
            dsn=dsn,
            identity=None,
            declared_identity=connection_identity,
            registry_candidates=all_registry_candidates,
            provenance=tuple(provenance),
            reason="registry_identity_conflict",
            candidate_count=len(target_candidates),
        )

    unique_candidates = _unique_identities(
        [candidate.identity for candidate in selected_candidates]
    )
    if len(unique_candidates) > 1:
        return _resolution(
            DsnResolutionStatus.AMBIGUOUS,
            dsn=dsn,
            identity=None,
            declared_identity=connection_identity,
            registry_candidates=all_registry_candidates,
            provenance=tuple(provenance),
            reason="multiple_registry_identities",
            candidate_count=len(target_candidates),
        )
    effective_identity = merge_connection_identities(unique_candidates[0], connection_identity)
    shadowed_conflict = bool(target_user and target_system) and any(
        candidate.identity.key != unique_candidates[0].key for candidate in target_system
    )
    return _resolution(
        DsnResolutionStatus.RESOLVED,
        dsn=dsn,
        identity=effective_identity,
        declared_identity=connection_identity,
        registry_candidates=all_registry_candidates,
        provenance=tuple(provenance),
        reason=(
            "registry_identity_resolved_user_precedence_shadowed_conflict"
            if shadowed_conflict
            else "registry_identity_resolved"
        ),
        candidate_count=len(target_candidates),
    )


def _read_registry_values(key: object, winreg_module: object) -> dict[str, object]:
    values: dict[str, object] = {}
    index = 0
    while True:
        try:
            name, value, _value_type = winreg_module.EnumValue(key, index)  # type: ignore[attr-defined]
        except OSError:
            break
        if isinstance(name, str):
            values[name] = value
        index += 1
    return values


def _coerce_bitness(value: RegistryBitness | int | None) -> RegistryBitness:
    if value is None:
        value = struct.calcsize("P") * 8
    try:
        return RegistryBitness(value)
    except ValueError as error:
        raise ValueError("target_bitness must be 32 or 64") from error


def _valid_registry_name(value: str) -> bool:
    return bool(value and len(value) <= 128 and "\\" not in value and "/" not in value)


def _unique_normalized(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip().casefold() for value in values if value.strip()))


def _unique_identities(
    identities: list[NormalizedConnectionIdentity],
) -> list[NormalizedConnectionIdentity]:
    unique: dict[tuple[str, str, str, str, str, str], NormalizedConnectionIdentity] = {}
    for identity in identities:
        unique.setdefault(identity.key, identity)
    return list(unique.values())


def _lineage_keys(identity: NormalizedConnectionIdentity) -> tuple[str, ...]:
    return tuple(
        key
        for key, value in (
            ("platform", identity.platform if identity.platform != "unknown" else None),
            ("driver", identity.driver),
            ("dsn", identity.dsn),
            ("server", identity.server),
            ("database", identity.database),
            ("file", identity.file),
        )
        if value is not None
    )


def _resolution(
    status: DsnResolutionStatus,
    *,
    dsn: str | None,
    identity: NormalizedConnectionIdentity | None,
    declared_identity: NormalizedConnectionIdentity | None,
    reason: str,
    registry_candidates: tuple[RegistryDsnCandidate, ...] = (),
    provenance: tuple[DsnResolutionProvenance, ...] = (),
    candidate_count: int = 0,
) -> DsnResolution:
    return DsnResolution(
        status=status,
        dsn=dsn,
        identity=identity,
        declared_identity=declared_identity,
        registry_candidates=registry_candidates,
        provenance=provenance,
        reason=reason,
        candidate_count=candidate_count,
    )
