"""Fail-closed Windows extraction of static Microsoft Access metadata.

The mandatory lane uses DAO directly in read-only mode. Access.Application is used only for the
optional SaveAsText lane after a hardened-worker gate approves a separately mutated disposable
copy. No query, macro, VBA procedure, link refresh, or recordset is executed.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from portfolio_analyzer.access.export_safety import (
    EnvironmentExportSafetyGate,
    ExportSafetyGate,
)
from portfolio_analyzer.access.libraries import (
    VerifiedApprovedLibrary,
    resolve_approved_libraries,
)
from portfolio_analyzer.access.safety import validate_access_extraction_request
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
from portfolio_analyzer.parsing.connections import redact_connection_string
from portfolio_analyzer.redaction import redact_sensitive_text
from portfolio_analyzer.staging.hashing import sha256_file


class AccessExtractionError(RuntimeError):
    """A failure in mandatory metadata extraction that makes analysis unsafe or incomplete."""


@dataclass(slots=True)
class _DaoExtractionCoverage:
    tabledef_enumerated_count: int = 0
    tabledef_succeeded_count: int = 0
    tabledef_failed_count: int = 0
    querydef_enumerated_count: int = 0
    querydef_succeeded_count: int = 0
    querydef_failed_count: int = 0


@dataclass(slots=True)
class _ExportLaneOutcome:
    objects: list[AccessExtractedObject]
    warnings: list[str]
    derived_copy_sha256: str | None
    injected_libraries: tuple[ApprovedLibraryReference, ...]
    library_injection_status: LibraryInjectionStatus


class WindowsAccessExtractor:
    version = "windows-dao-static-v10"

    def __init__(
        self,
        settings: AnalyzerSettings,
        *,
        export_gate: ExportSafetyGate | None = None,
    ) -> None:
        self.settings = settings
        self.export_gate = export_gate or EnvironmentExportSafetyGate()

    def extract(
        self,
        artifact: VerifiedStagedArtifact,
        destination: Path,
        *,
        progress: Callable[[str], None] | None = None,
        cleanup: bool = True,
    ) -> AccessExtractionResult:
        staged_database_path = validate_access_extraction_request(
            artifact, destination, self.settings
        )
        destination.mkdir(parents=True, exist_ok=True)
        try:
            import win32com.client  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - Windows environment concern
            raise RuntimeError("pywin32 is required for Windows Access extraction") from exc

        objects: list[AccessExtractedObject] = []
        warnings: list[str] = []
        dao_coverage = _DaoExtractionCoverage()
        derived_copy_sha256: str | None = None
        staged_sha256 = sha256_file(staged_database_path)
        if staged_sha256 != artifact.sha256:
            raise AccessExtractionError("verified staged artifact changed before DAO extraction")
        approved_libraries = resolve_approved_libraries(
            self.settings,
            artifact.tool_inventory_id,
            primary_filename=staged_database_path.name,
        )

        _progress(progress, "Opening verified staged copy through read-only DAO")
        dao_engine = _create_dao_engine(win32com.client)
        database: Any | None = None
        try:
            database = dao_engine.OpenDatabase(str(staged_database_path), False, True)
            objects.extend(
                self._schema_objects(
                    database,
                    warnings,
                    progress,
                    coverage=dao_coverage,
                )
            )
        except AccessExtractionError:
            raise
        except Exception as exc:
            raise AccessExtractionError(
                f"DAO database open failed: {_safe_error(exc)}"
            ) from exc
        finally:
            if database is not None:
                with suppress(Exception):
                    database.Close()
            database = None
            dao_engine = None

        if sha256_file(staged_database_path) != staged_sha256:
            raise AccessExtractionError("DAO metadata extraction modified the staged artifact")

        decision = self.export_gate.evaluate()
        if decision.permitted:
            export_outcome = self._run_export_lane(
                artifact,
                staged_database_path,
                destination,
                win32com.client,
                approved_libraries=approved_libraries,
                progress=progress,
                cleanup=cleanup,
            )
            objects.extend(export_outcome.objects)
            warnings.extend(export_outcome.warnings)
            derived_copy_sha256 = export_outcome.derived_copy_sha256
            injected_libraries = export_outcome.injected_libraries
            library_injection_status = export_outcome.library_injection_status
        else:
            warnings.append(f"SaveAsText lane skipped: {decision.reason}")
            injected_libraries = ()
            library_injection_status = (
                LibraryInjectionStatus.SKIPPED_SAFETY_GATE
                if approved_libraries
                else LibraryInjectionStatus.NOT_CONFIGURED
            )

        _progress(progress, "Extraction complete")
        return AccessExtractionResult(
            tool_inventory_id=artifact.tool_inventory_id,
            artifact_id=artifact.artifact_id,
            staged_path=staged_database_path,
            extractor_version=self.version,
            approved_libraries=[item.reference for item in approved_libraries],
            injected_libraries=list(injected_libraries),
            library_injection_status=library_injection_status,
            objects=objects,
            extraction_errors=warnings,
            coverage_status="partial" if warnings else "complete",
            derived_copy_sha256=derived_copy_sha256,
            tabledef_enumerated_count=dao_coverage.tabledef_enumerated_count,
            tabledef_succeeded_count=dao_coverage.tabledef_succeeded_count,
            tabledef_failed_count=dao_coverage.tabledef_failed_count,
            querydef_enumerated_count=dao_coverage.querydef_enumerated_count,
            querydef_succeeded_count=dao_coverage.querydef_succeeded_count,
            querydef_failed_count=dao_coverage.querydef_failed_count,
        )

    def _run_export_lane(
        self,
        artifact: VerifiedStagedArtifact,
        staged_database_path: Path,
        destination: Path,
        win32_client: Any,
        *,
        approved_libraries: tuple[VerifiedApprovedLibrary, ...] = (),
        progress: Callable[[str], None] | None,
        cleanup: bool,
    ) -> _ExportLaneOutcome:
        warnings: list[str] = []
        results: list[AccessExtractedObject] = []
        working_bundle: Path | None = None
        access: Any | None = None
        derived_copy_sha256: str | None = None
        injected_libraries: tuple[ApprovedLibraryReference, ...] = ()
        library_injection_status = (
            LibraryInjectionStatus.FAILED
            if approved_libraries
            else LibraryInjectionStatus.NOT_CONFIGURED
        )
        try:
            database_path, working_bundle, additions, bundle_warnings = (
                self._prepare_working_bundle(
                    staged_database_path,
                    destination,
                    application_id=artifact.tool_inventory_id,
                    approved_libraries=approved_libraries,
                    expected_primary_sha256=artifact.sha256,
                    expected_primary_size_bytes=artifact.size_bytes,
                )
            )
            if approved_libraries:
                injected_libraries = tuple(
                    item.reference for item in approved_libraries
                )
                library_injection_status = LibraryInjectionStatus.INJECTED
            for addition in additions:
                _progress(progress, addition)
            warnings.extend(bundle_warnings)
            _progress(progress, "Hardening disposable copy for startup bypass")
            self._enable_and_verify_startup_bypass(database_path, win32_client)
            derived_copy_sha256 = sha256_file(database_path)

            try:
                import win32api  # type: ignore[import-untyped]
                import win32con  # type: ignore[import-untyped]
            except ImportError as exc:  # pragma: no cover - Windows environment concern
                raise RuntimeError("pywin32 keyboard APIs are required for gated export") from exc

            _progress(progress, "Starting hidden Access export instance")
            access = win32_client.DispatchEx("Access.Application")
            access.AutomationSecurity = 3
            access.Visible = False
            self._open_with_startup_bypass(
                access, database_path, win32api, win32con, progress
            )
            export_database = access.CurrentDb()
            results.extend(
                self._export_definitions(
                    access, export_database, working_bundle, warnings, progress
                )
            )
            return _ExportLaneOutcome(
                objects=results,
                warnings=warnings,
                derived_copy_sha256=derived_copy_sha256,
                injected_libraries=injected_libraries,
                library_injection_status=library_injection_status,
            )
        except Exception as exc:
            warnings.append(f"SaveAsText lane failed safely: {_safe_error(exc)}")
            return _ExportLaneOutcome(
                objects=results,
                warnings=warnings,
                derived_copy_sha256=derived_copy_sha256,
                injected_libraries=injected_libraries,
                library_injection_status=library_injection_status,
            )
        finally:
            if cleanup and access is not None:
                _progress(progress, "Closing disposable Access database")
                with suppress(Exception):
                    access.CloseCurrentDatabase()
                with suppress(Exception):
                    access.Quit()
            access = None
            if cleanup and working_bundle is not None:
                _progress(progress, "Removing disposable Access working bundle")
                shutil.rmtree(working_bundle, ignore_errors=True)

    def _prepare_working_bundle(
        self,
        staged_database_path: Path,
        destination: Path,
        *,
        application_id: str = "",
        approved_libraries: tuple[VerifiedApprovedLibrary, ...] | None = None,
        expected_primary_sha256: str,
        expected_primary_size_bytes: int,
    ) -> tuple[Path, Path, list[str], list[str]]:
        """Create a unique disposable copy plus only explicitly approved libraries."""
        destination.mkdir(parents=True, exist_ok=True)
        working_bundle = Path(
            tempfile.mkdtemp(prefix="_working_bundle-", dir=destination)
        ).resolve()
        try:
            working_database_path = working_bundle / staged_database_path.name
            shutil.copy2(staged_database_path, working_database_path)
            if (
                working_database_path.stat().st_size != expected_primary_size_bytes
                or sha256_file(working_database_path) != expected_primary_sha256
            ):
                raise AccessExtractionError(
                    "disposable Access copy failed pinned size/hash verification"
                )

            additions: list[str] = []
            warnings: list[str] = []
            selected = approved_libraries
            if selected is None:
                selected = resolve_approved_libraries(
                    self.settings,
                    application_id,
                    primary_filename=staged_database_path.name,
                )
            for library in selected:
                target = working_bundle / library.reference.filename
                shutil.copy2(library.path, target)
                if sha256_file(target) != library.reference.sha256:
                    raise AccessExtractionError(
                        "approved library copy failed hash verification: "
                        f"{library.reference.filename}"
                    )
                additions.append(
                    "Added reviewed hash-pinned library "
                    f"'{library.reference.filename}' to disposable bundle."
                )
            return working_database_path, working_bundle, additions, warnings
        except Exception:
            shutil.rmtree(working_bundle, ignore_errors=True)
            raise

    def _enable_and_verify_startup_bypass(
        self, database_path: Path, win32_client: Any
    ) -> None:
        engine = _create_dao_engine(win32_client)
        database: Any | None = None
        try:
            database = engine.OpenDatabase(str(database_path), False, False)
            try:
                database.Properties["AllowBypassKey"].Value = True
            except Exception:
                property_value = database.CreateProperty("AllowBypassKey", 1, True)
                database.Properties.Append(property_value)
        except Exception as exc:
            raise AccessExtractionError(
                f"could not set AllowBypassKey on disposable copy: {_safe_error(exc)}"
            ) from exc
        finally:
            if database is not None:
                with suppress(Exception):
                    database.Close()
            database = None

        enabled = False
        try:
            database = engine.OpenDatabase(str(database_path), False, True)
            enabled = bool(database.Properties["AllowBypassKey"].Value)
        except Exception as exc:
            raise AccessExtractionError(
                f"could not verify AllowBypassKey on disposable copy: {_safe_error(exc)}"
            ) from exc
        finally:
            if database is not None:
                with suppress(Exception):
                    database.Close()
            engine = None
        if not enabled:
            raise AccessExtractionError("AllowBypassKey verification returned false")

    def _open_with_startup_bypass(
        self,
        access: Any,
        database_path: Path,
        win32api: Any,
        win32con: Any,
        progress: Callable[[str], None] | None,
    ) -> None:
        _progress(progress, "Opening disposable copy with Shift startup bypass")
        win32api.keybd_event(win32con.VK_SHIFT, 0, 0, 0)
        try:
            time.sleep(0.25)
            access.OpenCurrentDatabase(str(database_path), False)
        finally:
            win32api.keybd_event(win32con.VK_SHIFT, 0, win32con.KEYEVENTF_KEYUP, 0)

    def _schema_objects(
        self,
        database: Any,
        errors: list[str],
        progress: Callable[[str], None] | None,
        *,
        coverage: _DaoExtractionCoverage | None = None,
    ) -> list[AccessExtractedObject]:
        coverage = coverage or _DaoExtractionCoverage()
        objects: list[AccessExtractedObject] = []
        try:
            for index, table in enumerate(database.TableDefs):
                coverage.tabledef_enumerated_count += 1
                try:
                    objects.append(self._table_object(table, index, errors, progress))
                except Exception as exc:
                    coverage.tabledef_failed_count += 1
                    errors.append(
                        f"TableDef[{index}] extraction failed: {_safe_error(exc)}"
                    )
                else:
                    coverage.tabledef_succeeded_count += 1
        except Exception as exc:
            raise AccessExtractionError(
                f"DAO TableDefs enumeration failed: {_safe_error(exc)}"
            ) from exc
        try:
            for index, query in enumerate(database.QueryDefs):
                coverage.querydef_enumerated_count += 1
                try:
                    objects.append(self._query_object(query, index, errors, progress))
                except Exception as exc:
                    coverage.querydef_failed_count += 1
                    errors.append(
                        f"QueryDef[{index}] extraction failed: {_safe_error(exc)}"
                    )
                else:
                    coverage.querydef_succeeded_count += 1
        except Exception as exc:
            raise AccessExtractionError(
                f"DAO QueryDefs enumeration failed: {_safe_error(exc)}"
            ) from exc
        return objects

    def _table_object(
        self,
        table: Any,
        index: int,
        errors: list[str],
        progress: Callable[[str], None] | None,
    ) -> AccessExtractedObject:
        context = f"TableDef[{index}]"
        name = _property(table, "Name", errors, context, default=f"<table-{index}>")
        error_count = len(errors)
        connect = _property(table, "Connect", errors, context)
        connect_available = len(errors) == error_count
        source_table = _property(table, "SourceTableName", errors, context)
        attributes = _property(table, "Attributes", errors, context, default="0")
        hidden, system = _dao_object_flags(attributes, name)
        _progress(progress, f"Reading table metadata: {name}")
        properties = {
            "connect": _redact_connection(connect, errors, context),
            "source_table_name": source_table,
            "attributes": attributes,
            "hidden": str(hidden).lower(),
            "system": str(system).lower(),
            "connect_metadata_status": "available" if connect_available else "unavailable",
        }
        if connect_available and not connect and not name.startswith("MSys"):
            properties["fields"] = json.dumps(
                _local_field_metadata(table, errors, context),
                sort_keys=True,
                separators=(",", ":"),
            )
        if not connect_available:
            object_type = "table_link_status_unknown"
        elif connect:
            object_type = "linked_table"
        else:
            object_type = "table"
        return AccessExtractedObject(
            object_type=object_type,
            name=name,
            properties=properties,
        )

    def _query_object(
        self,
        query: Any,
        index: int,
        errors: list[str],
        progress: Callable[[str], None] | None,
    ) -> AccessExtractedObject:
        context = f"QueryDef[{index}]"
        name = _property(query, "Name", errors, context, default=f"<query-{index}>")
        sql = _property(query, "SQL", errors, context)
        type_code = _property(query, "Type", errors, context, default="-1")
        error_count = len(errors)
        connect = _property(query, "Connect", errors, context)
        connect_available = len(errors) == error_count
        returns_records = _property(query, "ReturnsRecords", errors, context)
        odbc_timeout = _property(query, "ODBCTimeout", errors, context)
        max_records = _property(query, "MaxRecords", errors, context)
        attributes = _property(query, "Attributes", errors, context, default="0")
        hidden, system = _dao_object_flags(
            attributes,
            name,
            internal_query=name.casefold().startswith("~sq_"),
        )
        _progress(progress, f"Reading query definition: {name}")
        try:
            normalized_type = _query_kind(int(type_code))
        except ValueError:
            normalized_type = "unknown"
        properties = {
            "dao_type_code": type_code,
            "query_kind": normalized_type,
            "connect": _redact_connection(connect, errors, context),
            "connect_metadata_status": "available" if connect_available else "unavailable",
            "returns_records": returns_records,
            "odbc_timeout": odbc_timeout,
            "max_records": max_records,
            "attributes": attributes,
            "parameters": json.dumps(
                _parameter_metadata(query, errors, context),
                sort_keys=True,
                separators=(",", ":"),
            ),
            "hidden": str(hidden).lower(),
            "system": str(system).lower(),
        }
        return AccessExtractedObject(
            object_type="query",
            name=name,
            definition=redact_sensitive_text(sql),
            properties=properties,
        )

    def _export_definitions(
        self,
        access: Any,
        database: Any,
        working_bundle: Path,
        errors: list[str],
        progress: Callable[[str], None] | None,
    ) -> list[AccessExtractedObject]:
        object_types = {
            "Forms": (2, "form"),
            "Reports": (3, "report"),
            "Scripts": (4, "macro"),
            "Modules": (5, "module"),
        }
        export_root = (working_bundle / "exports").resolve()
        export_root.mkdir()
        results: list[AccessExtractedObject] = []
        for container_name, (constant, kind) in object_types.items():
            try:
                documents = database.Containers(container_name).Documents
                for index, document in enumerate(documents):
                    name = _property(
                        document,
                        "Name",
                        errors,
                        f"{container_name}[{index}]",
                        default=f"<{kind}-{index}>",
                    )
                    safe_id = hashlib.sha256(f"{kind}\x1f{name}".encode()).hexdigest()[:24]
                    target = (export_root / f"{kind}-{safe_id}.txt").resolve()
                    if not target.is_relative_to(export_root):
                        raise AccessExtractionError("unsafe SaveAsText export path")
                    _progress(progress, f"Exporting {kind}: {name}")
                    try:
                        access.SaveAsText(constant, name, str(target))
                        definition = redact_sensitive_text(_decode_export(target.read_bytes()))
                        results.append(
                            AccessExtractedObject(
                                object_type=kind,
                                name=name,
                                definition=definition,
                            )
                        )
                    except Exception as exc:
                        errors.append(
                            f"Could not export {kind} '{_safe_label(name)}': {_safe_error(exc)}"
                        )
                    finally:
                        target.unlink(missing_ok=True)
            except Exception as exc:
                errors.append(
                    f"Could not enumerate {container_name}: {_safe_error(exc)}"
                )
        shutil.rmtree(export_root, ignore_errors=True)
        return results

def _create_dao_engine(win32_client: Any) -> Any:
    try:
        return win32_client.Dispatch("DAO.DBEngine.120")
    except Exception as exc:
        raise AccessExtractionError(
            f"ACE DAO 12.0+ is unavailable: {_safe_error(exc)}"
        ) from exc


def _property(
    instance: Any,
    property_name: str,
    errors: list[str],
    context: str,
    *,
    default: str = "",
) -> str:
    try:
        value = getattr(instance, property_name)
        return default if value is None else str(value)
    except Exception as exc:
        errors.append(f"{context}.{property_name} unavailable: {_safe_error(exc)}")
        return default


def _local_field_metadata(instance: Any, errors: list[str], context: str) -> list[dict[str, str]]:
    fields: list[dict[str, str]] = []
    try:
        for index, field in enumerate(instance.Fields):
            field_context = f"{context}.Fields[{index}]"
            fields.append(
                {
                    "name": _property(field, "Name", errors, field_context),
                    "type": _property(field, "Type", errors, field_context),
                    "size": _property(field, "Size", errors, field_context),
                    "required": _property(field, "Required", errors, field_context),
                    "allow_zero_length": _property(
                        field, "AllowZeroLength", errors, field_context
                    ),
                }
            )
    except Exception as exc:
        errors.append(f"{context}.Fields enumeration failed: {_safe_error(exc)}")
    return fields


def _parameter_metadata(instance: Any, errors: list[str], context: str) -> list[dict[str, str]]:
    parameters: list[dict[str, str]] = []
    try:
        for index, parameter in enumerate(instance.Parameters):
            parameter_context = f"{context}.Parameters[{index}]"
            parameters.append(
                {
                    "ordinal": str(index),
                    "name": _property(parameter, "Name", errors, parameter_context),
                    "type": _property(parameter, "Type", errors, parameter_context),
                    "direction": _property(
                        parameter, "Direction", errors, parameter_context
                    ),
                }
            )
    except Exception as exc:
        errors.append(f"{context}.Parameters enumeration failed: {_safe_error(exc)}")
    return parameters


def _dao_object_flags(
    attributes: str,
    name: str,
    *,
    internal_query: bool = False,
) -> tuple[bool, bool]:
    """Decode DAO hidden/system flags while retaining name-based internal-object signals."""

    try:
        unsigned = int(attributes) & 0xFFFFFFFF
    except ValueError:
        unsigned = 0
    hidden = bool(unsigned & 0x1) or name.startswith("~")
    system_mask = 0x80000002
    system = (
        (unsigned & system_mask) == system_mask
        or name.casefold().startswith("msys")
        or internal_query
    )
    return hidden, system


def _query_kind(type_code: int) -> str:
    return {
        0: "select",
        16: "crosstab",
        32: "delete",
        48: "update",
        64: "append",
        80: "make_table",
        96: "data_definition",
        112: "pass_through",
        128: "union",
        144: "pass_through_bulk",
        160: "compound",
        224: "procedure",
        240: "action",
    }.get(type_code, "unknown")


def _redact_connection(value: str, errors: list[str], context: str) -> str:
    if not value:
        return ""
    try:
        return redact_connection_string(value)
    except Exception as exc:
        errors.append(f"{context}.Connect was malformed: {_safe_error(exc)}")
        return "<malformed-connection-redacted>"


def _decode_export(value: bytes) -> str:
    if value.startswith((b"\xff\xfe", b"\xfe\xff")):
        return value.decode("utf-16")
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return value.decode("cp1252")


def _safe_error(error: BaseException) -> str:
    details = [type(error).__name__]
    for attribute in ("hresult", "winerror", "errno"):
        value = getattr(error, attribute, None)
        if isinstance(value, int):
            details.append(f"{attribute}={value}")
    return "[" + ";".join(details) + "]"


def _safe_label(value: str) -> str:
    return redact_sensitive_text(value).replace("\r", " ").replace("\n", " ")[:120]


def _progress(callback: Callable[[str], None] | None, message: str) -> None:
    if callback is not None:
        callback(redact_sensitive_text(message))
