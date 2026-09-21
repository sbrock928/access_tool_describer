"""Conservative Windows Access metadata extraction.

No executable object is opened or run.  This adapter must be run in an
isolated analysis environment; see docs/ACCESS_EXTRACTION.md.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from portfolio_analyzer.access.safety import validate_access_extraction_request
from portfolio_analyzer.config import AnalyzerSettings
from portfolio_analyzer.models import ExtractedApplication, ExtractedObject, StagedArtifact


class WindowsAccessExtractor:
    version = "windows-com-metadata-v2"

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
        database_path = validate_access_extraction_request(artifact, destination, self.settings)
        destination.mkdir(parents=True, exist_ok=True)
        try:
            import win32com.client  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - Windows environment concern
            raise RuntimeError("pywin32 is required for Windows Access extraction") from exc

        access: Any = win32com.client.DispatchEx("Access.Application")
        objects: list[ExtractedObject] = []
        errors: list[str] = []
        try:
            _progress(progress, "Starting hidden Access automation instance")
            # msoAutomationSecurityForceDisable. This is requested before opening the staged copy.
            access.AutomationSecurity = 3
            access.Visible = False
            _progress(progress, "Opening verified local staged copy")
            access.OpenCurrentDatabase(str(database_path), False)
            database = access.CurrentDb()
            _progress(progress, "Enumerating table and query metadata")
            objects.extend(self._schema_objects(database, errors, progress))
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
        _progress(progress, "Extraction complete")
        return ExtractedApplication(
            tool_inventory_id=artifact.tool_inventory_id,
            staged_path=database_path,
            extractor_version=self.version,
            objects=objects,
            extraction_errors=errors,
        )

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


def _progress(callback: Callable[[str], None] | None, message: str) -> None:
    if callback is not None:
        callback(message)
