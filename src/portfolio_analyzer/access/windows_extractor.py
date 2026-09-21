"""Conservative Windows Access metadata extraction.

No executable object is opened or run.  This adapter must be run in an
isolated analysis environment; see docs/ACCESS_EXTRACTION.md.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from portfolio_analyzer.access.safety import validate_access_extraction_request
from portfolio_analyzer.config import AnalyzerSettings
from portfolio_analyzer.models import ExtractedApplication, ExtractedObject, StagedArtifact
from portfolio_analyzer.staging.hashing import sha256_file


class WindowsAccessExtractor:
    version = "windows-com-metadata-v5"

    def __init__(self, settings: AnalyzerSettings) -> None:
        self.settings = settings

    def extract(
        self,
        artifact: StagedArtifact,
        destination: Path,
        *,
        progress: Callable[[str], None] | None = None,
        cleanup: bool = True,
    ) -> ExtractedApplication:
        staged_database_path = validate_access_extraction_request(
            artifact, destination, self.settings
        )
        destination.mkdir(parents=True, exist_ok=True)
        try:
            import win32api  # type: ignore[import-untyped]
            import win32com.client  # type: ignore[import-untyped]
            import win32con  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - Windows environment concern
            raise RuntimeError("pywin32 is required for Windows Access extraction") from exc

        access: Any = win32com.client.DispatchEx("Access.Application")
        objects: list[ExtractedObject] = []
        errors: list[str] = []
        working_bundle: Path | None = None
        try:
            database_path, working_bundle, workspace_additions, workspace_warnings = (
                self._prepare_working_bundle(
                staged_database_path, destination
                )
            )
            for addition in workspace_additions:
                _progress(progress, addition)
            errors.extend(workspace_warnings)
            _progress(progress, "Starting hidden Access automation instance")
            # msoAutomationSecurityForceDisable. This is requested before opening the staged copy.
            access.AutomationSecurity = 3
            access.Visible = False
            _progress(progress, "Opening verified local staged copy")
            self._open_with_startup_bypass(access, database_path, win32api, win32con, progress)
            database = access.CurrentDb()
            _progress(progress, "Enumerating table and query metadata")
            objects.extend(self._schema_objects(database, errors, progress))
            objects.extend(self._reference_objects(access, errors, progress))
            objects.extend(
                self._export_definitions(access, database, destination, errors, progress)
            )
        except Exception as exc:  # COM errors are recorded without a retry against source.
            errors.append(f"Access metadata extraction failed: {exc}")
        finally:
            if cleanup:
                _progress(progress, "Closing local staged database")
                with suppress(Exception):
                    access.CloseCurrentDatabase()
                _progress(progress, "Closing Access automation instance")
                with suppress(Exception):
                    access.Quit()
            if cleanup and working_bundle is not None:
                _progress(progress, "Removing temporary Access working bundle")
                with suppress(OSError):
                    shutil.rmtree(working_bundle)
        _progress(progress, "Extraction complete")
        return ExtractedApplication(
            tool_inventory_id=artifact.tool_inventory_id,
            staged_path=staged_database_path,
            extractor_version=self.version,
            objects=objects,
            extraction_errors=errors,
        )

    def _prepare_working_bundle(
        self, staged_database_path: Path, destination: Path
    ) -> tuple[Path, Path, list[str], list[str]]:
        """Create a minimal disposable bundle with the primary database and curated libraries.

        The canonical staged bundle is hash-verified before this method is reached and is never
        changed. Access opens only the disposable copy. It deliberately does not mirror a source
        folder or search every staged tool: that would multiply disk use for every extraction.
        """
        working_bundle = destination / "_working_bundle"
        if working_bundle.exists():
            shutil.rmtree(working_bundle)
        working_bundle.mkdir(parents=True)
        working_database_path = working_bundle / staged_database_path.name
        shutil.copy2(staged_database_path, working_database_path)
        additions: list[str] = []
        warnings: list[str] = []

        shared_candidates = _access_files_by_name(self.settings.shared_libraries_dir)

        for filename, versions in sorted(shared_candidates.items()):
            target = working_bundle / filename
            if len(versions) != 1:
                warnings.append(
                    f"Curated shared library '{filename}' was not added: "
                    f"{len(versions)} different versions were found."
                )
                continue
            shutil.copy2(next(iter(versions.values())), target)
            additions.append(f"Added curated shared library '{filename}' to temporary bundle.")
        return working_database_path, working_bundle, additions, warnings

    def _open_with_startup_bypass(
        self,
        access: Any,
        database_path: Path,
        win32api: Any,
        win32con: Any,
        progress: Callable[[str], None] | None,
    ) -> None:
        """Open with the documented Shift bypass so AutoExec/startup code cannot run.

        The bypass depends on the database allowing it. Its setting cannot be queried reliably
        before an Access database is open, so a failed preflight must not prevent all extraction.
        Run this only in the isolated, non-privileged environment required by the documentation.
        """
        _progress(progress, "Opening with Shift startup bypass")
        win32api.keybd_event(win32con.VK_SHIFT, 0, 0, 0)
        try:
            # Keep Shift held through the open call so startup options are bypassed.
            time.sleep(0.25)
            access.OpenCurrentDatabase(str(database_path), False)
        finally:
            win32api.keybd_event(win32con.VK_SHIFT, 0, win32con.KEYEVENTF_KEYUP, 0)

    def _schema_objects(
        self, database: Any, errors: list[str], progress: Callable[[str], None] | None
    ) -> list[ExtractedObject]:
        objects: list[ExtractedObject] = []
        try:
            for table in database.TableDefs:
                if not str(table.Name).startswith("MSys"):
                    _progress(progress, f"Reading table metadata: {table.Name}")
                    objects.append(
                        ExtractedObject(
                            object_type="linked_table" if table.Connect else "table",
                            name=str(table.Name),
                            properties={"connect": str(table.Connect or "")},
                        )
                    )
            for query in database.QueryDefs:
                if not str(query.Name).startswith("~sq"):
                    _progress(progress, f"Reading query definition: {query.Name}")
                    objects.append(
                        ExtractedObject(
                            object_type="query", name=str(query.Name), definition=str(query.SQL)
                        )
                    )
        except Exception as exc:
            errors.append(f"DAO metadata enumeration failed: {exc}")
        return objects

    def _export_definitions(
        self,
        access: Any,
        database: Any,
        destination: Path,
        errors: list[str],
        progress: Callable[[str], None] | None,
    ) -> list[ExtractedObject]:
        # SaveAsText emits definitions to the extraction workspace; it does not open objects.
        object_types = {
            "Forms": (2, "form"),
            "Reports": (3, "report"),
            "Scripts": (4, "macro"),
            "Modules": (5, "module"),
        }
        results: list[ExtractedObject] = []
        for container_name, (constant, kind) in object_types.items():
            try:
                for document in database.Containers(container_name).Documents:
                    name = str(document.Name)
                    target = destination / f"{kind}_{name}.txt"
                    _progress(progress, f"Exporting {kind}: {name}")
                    access.SaveAsText(constant, name, str(target))
                    results.append(
                        ExtractedObject(
                            object_type=kind,
                            name=name,
                            definition=target.read_text(errors="replace"),
                        )
                    )
            except Exception as exc:
                errors.append(f"Could not export {container_name}: {exc}")
        return results

    def _reference_objects(
        self, access: Any, errors: list[str], progress: Callable[[str], None] | None
    ) -> list[ExtractedObject]:
        """Capture reference metadata without evaluating or compiling VBA code."""
        results: list[ExtractedObject] = []
        try:
            for reference in access.References:
                name = _reference_value(reference, "Name") or "<unnamed reference>"
                _progress(progress, f"Reading Access/VBA reference: {name}")
                results.append(
                    ExtractedObject(
                        object_type="reference",
                        name=name,
                        properties={
                            "full_path": _reference_value(reference, "FullPath") or "",
                            "guid": _reference_value(reference, "Guid") or "",
                            "is_broken": _reference_value(reference, "IsBroken") or "False",
                        },
                    )
                )
        except Exception as exc:
            errors.append(f"Access/VBA reference enumeration failed: {exc}")
        return results


def _progress(callback: Callable[[str], None] | None, message: str) -> None:
    if callback is not None:
        callback(message)


def _reference_value(reference: Any, property_name: str) -> str | None:
    try:
        return str(getattr(reference, property_name))
    except Exception:
        return None


def _access_files_by_name(
    directory: Path,
) -> dict[str, dict[str, Path]]:
    """Return unique byte versions of local Access databases keyed by case-insensitive name."""
    files_by_name: dict[str, list[Path]] = {}
    if not directory.exists():
        return {}
    for candidate in directory.rglob("*"):
        if (
            not candidate.is_file()
            or candidate.suffix.casefold() not in {".accdb", ".mdb"}
        ):
            continue
        files_by_name.setdefault(candidate.name.casefold(), []).append(candidate)

    candidates: dict[str, dict[str, Path]] = {}
    for name, files in files_by_name.items():
        if len(files) == 1:
            # No comparison is required for a uniquely named candidate. This avoids hashing every
            # database in a large workspace on each extraction.
            candidates[name] = {"single": files[0]}
            continue
        candidates[name] = {sha256_file(candidate): candidate for candidate in files}
    return candidates
