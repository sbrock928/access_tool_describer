# Build Specification

## Scope and success criteria

This implementation establishes a maintainable Python 3.13 analyzer for Access estates. Its core deliverable is evidence, not execution: metadata, exported object definitions, static source findings, normalized dependencies, similarity signals, taxonomy candidates, and evidence-backed reports.

## Safety boundary

`InventoryRecord.filepath` is a source-only location. `ArtifactStager` is the sole component that reads it, only to copy files. It produces a `StagedArtifact` only after local existence, containment, size, and SHA-256 checks pass. `assert_trusted_staged_artifact` is required by every extractor. Failed staging is terminal for that artifact.

## MVP decisions

- SQLite/SQLAlchemy persist normalized portfolio records and checkpoint hashes.
- Cross-platform deterministic analyzers operate on `ExtractedApplication` fixtures or Windows exports.
- The taxonomy is assembled from observed deterministic signals and their portfolio frequency; no business-domain category is imposed up front.
- The Windows adapter uses Access metadata and `SaveAsText` exports; it does not open forms/reports, execute queries, invoke macros, or invoke VBA.
- Excel workbooks are recorded as dependencies only, not reverse engineered.

## Known extraction limits

Access object definitions and VBA may be unavailable for compiled, encrypted, corrupted, permission-protected, or version-incompatible databases. Dynamic SQL, runtime paths, and late-bound automation remain uncertain. These conditions must be represented as extraction errors or manual-review items, never resolved by executing code.
