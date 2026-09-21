# Safety: Local-Copy Rule

The network or other inventory-provided path is source material only. The analyzer copies it with `shutil.copy2`, verifies the local artifact and records its SHA-256, then performs every inspection against that staged local file. It never intentionally opens the source with Access, DAO, ADO, ODBC, COM, or any analysis component.

`ArtifactStager.stage` returns a failed result instead of raising a portfolio-fatal error when copying fails. The pipeline records the error and skips extraction. There is no fallback path from an extraction error to the source file.

For an inventory-listed application, staging mirrors every regular file below the source file's parent
folder into the local bundle, preserving relative paths. This makes sibling databases, DLLs, templates,
and configuration files available to local Access inspection. Access and Office lock files are excluded.
Only the inventory-listed primary `.accdb` is eligible for extraction; copied sibling `.accdb` files are
supporting artifacts and are never opened as that tool's primary database.

The extractor calls `assert_trusted_staged_artifact` to reject unstaged paths, paths outside `workspace/staged_tools`, missing files, and hash mismatches. This guard is tested.
