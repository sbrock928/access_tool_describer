# Safety: Local-Copy Rule

The network or other inventory-provided path is source material only. The analyzer copies it with `shutil.copy2`, verifies the local artifact and records its SHA-256, then performs every inspection against that staged local file. It never intentionally opens the source with Access, DAO, ADO, ODBC, COM, or any analysis component.

`ArtifactStager.stage` returns a failed result instead of raising a portfolio-fatal error when copying fails. The pipeline records the error and skips extraction. There is no fallback path from an extraction error to the source file.

The extractor calls `assert_trusted_staged_artifact` to reject unstaged paths, paths outside `workspace/staged_tools`, missing files, and hash mismatches. This guard is tested.
