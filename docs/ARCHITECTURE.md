# Architecture

`inventory -> staging -> trusted local artifact -> Windows extraction -> deterministic analysis -> evidence store -> portfolio analysis -> reports`

The `access` package is the only Windows-specific layer. It receives `StagedArtifact`, never `Path` alone, and validates the artifact again before use. `parsing`, `lineage`, `capabilities`, `similarity`, `persistence`, and `reporting` are OS-independent.

Deterministic facts (for example a SQL statement references `dbo.Deal`) are stored as evidence independently from interpretations (for example a report-generation capability). This permits later reclassification without re-extraction.

`INVENTORY_ID` remains the stable internal join and checkpoint key. Workspace application folders
and report labels use `EUCTNAME`. EUC names are validated for case-insensitive uniqueness after
Windows-safe normalization, preventing two applications from sharing a directory. On staging, an
unambiguous legacy inventory-ID directory is renamed to its EUC name.
